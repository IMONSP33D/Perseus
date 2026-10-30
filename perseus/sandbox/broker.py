"""The sandbox broker: a small supervised process that owns the code-sandbox backend.

Why a separate process: Perseus's socket guard refuses every outbound
connection except the web gateway's, and tools never hold credentials. The
broker is where the provider SDK, its network access and its API key live;
Perseus talks to it only on loopback, with a per-boot token, through
perseus.sandbox.client. Swapping providers changes this process, never Perseus.

    python -m perseus.sandbox.broker --port N
    env PERSEUS_SANDBOX_TOKEN     per-boot bearer token (required)
        PERSEUS_SANDBOX_SETTINGS  JSON of the [code_runner] config section, plus the Config folder's path;
                                  the broker reads [code_runner] api_key from Config/tooling.toml itself
        E2B_API_KEY               fallback when tooling.toml has no key

Endpoints (all JSON):
    GET  /healthz   200 once the start-up probe has finished (success or not), 503 before
    GET  /info      backend, languages found, network check, or the start-up error
    POST /run       {"language", "code", "stdin"} -> a runner.run_program result

The log (logs/sandbox.log) records, at start-up, which provider, API host and key this broker uses
(the key only as a fingerprint such as e2b_…3f9a), and then every sandbox's lifecycle: the
provider's sandbox id when it is created, each step's command, exit code and timing, and
whether it was destroyed. It never records the code or its output.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from perseus.sandbox import backends
from perseus.sandbox.backends import SandboxError
from perseus.sandbox.runner import RunSettings, probe, run_program

MAX_BODY = 1024 * 1024
MAX_CONCURRENT_RUNS = 4
HTTP_FOR = {"config": 409, "auth": 502, "quota": 429, "unavailable": 503, "backend": 502}


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} sandbox: {msg}", file=sys.stderr, flush=True)


def describe_event(e: dict) -> str:
    """One readable log line per sandbox lifecycle event."""
    sid = e.get("sandbox", "?")
    if e["event"] == "open":
        return f"sandbox {sid}: created in {e['ms']} ms ({e['purpose']}, network {e['network']})"
    if e["event"] == "open_failed":
        return f"sandbox: create FAILED after {e['ms']} ms ({e['purpose']}): {e['code']}: {e['error']}"
    if e["event"] == "exec":
        end = "timed out" if e["timed_out"] else f"exit {e['exit']}"
        return f"sandbox {sid}: `{e['cmd']}` -> {end} in {e['ms']} ms"
    if e["event"] == "close":
        how = {True: "destroyed", False: "destroy FAILED (it expires on its own)", None: "closed"}[e.get("killed")]
        return f"sandbox {sid}: {how}, lived {e['lived_ms'] / 1000:.1f} s"
    return f"sandbox event {e}"


class Broker:
    def __init__(self, backend_name: str, settings: dict, token: str, environ=os.environ, backend=None):
        self.backend_name, self.cfg, self.token = backend_name, settings, token
        self.run_settings = RunSettings.from_config(settings)
        self.environ = environ
        self.backend = backend
        self.state: dict = {"probing": True, "error": None, "languages": [], "missing": [],
                            "network_blocked": None}
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)

    # ---------------------------------------------------------------- start-up
    def start(self) -> None:
        try:
            if self.backend is None:
                settings = dict(self.cfg)
                settings["api_key"] = backends.config_secret(settings.pop("config_path", ""))
                self.backend = backends.make(self.backend_name, settings)
            ident = self.identity()
            self.state["identity"] = ident
            log("identity: " + " · ".join(f"{k}={v}" for k, v in ident.items()))
            if ident.get("env_overrides"):
                log(f"WARNING: these environment variables redirect the provider's SDK: {ident['env_overrides']}")
            self.state.update(probe(self.backend, self.run_settings, on_event=self.event))
            caps = self.backend.capabilities()
            self.state["capabilities"] = {"os": caps.os, "controls_network": caps.controls_network, **caps.extras}
            log(f"{self.backend_name}: {len(self.state['languages'])} language(s) available "
                f"({', '.join(self.state['languages']) or 'none'}); missing: {', '.join(self.state['missing']) or 'none'}; "
                f"network {self.state['network']}: arbitrary sites blocked {self.state['network_blocked']}, "
                f"registries reachable {self.state['packages_ok']}; "
                f"probe {self.state['seconds']}s")
        except SandboxError as e:
            self.state["error"] = {"code": e.code, "message": str(e)}
            log(f"{self.backend_name}: not available ({e.code}): {e}")
        except Exception as e:  # never crash-loop on a provider bug; report it instead
            self.state["error"] = {"code": "backend", "message": f"{type(e).__name__}"}
            log(f"{self.backend_name}: start-up failed ({type(e).__name__}: {e})")
        finally:
            self.state["probing"] = False

    def identity(self) -> dict:
        try:
            return dict(self.backend.describe()) if hasattr(self.backend, "describe") else {"provider": self.backend_name}
        except Exception as e:
            return {"provider": self.backend_name, "describe_error": type(e).__name__}

    def event(self, e: dict) -> None:
        log(describe_event(e))

    def info(self) -> dict:
        return {"backend": self.backend_name, "network": self.run_settings.network,
                "limits": {"build_timeout_s": self.run_settings.build_timeout_s,
                           "run_timeout_s": self.run_settings.run_timeout_s,
                           "max_output_kb": self.run_settings.max_output_kb},
                **{k: v for k, v in self.state.items() if k != "probing"}}

    # ---------------------------------------------------------------- runs
    def run(self, body: dict) -> tuple[int, dict]:
        if self.state["error"] or self.backend is None:
            return 503, {"error": self.state["error"] or {"code": "unavailable", "message": "starting up"}}
        language, code, stdin = body.get("language"), body.get("code"), body.get("stdin") or ""
        if not isinstance(language, str) or not isinstance(code, str) or not isinstance(stdin, str):
            return 400, {"error": {"code": "config", "message": "language and code must be strings"}}
        if language not in self.state["languages"]:
            return 400, {"error": {"code": "config", "message": f"{language!r} isn't installed in this sandbox "
                                                                f"(available: {', '.join(self.state['languages'])})"}}
        if not self._slots.acquire(blocking=False):
            return 429, {"error": {"code": "quota", "message": "too many programs running at once"}}
        try:
            result = run_program(self.backend, language, code, stdin, self.run_settings, on_event=self.event)
            log(f"run {language} in sandbox {result['sandbox_id']} ({result['backend']}, network {result['network']}): "
                f"{result['status']} in {result['seconds']}s")
            return 200, result
        except SandboxError as e:
            log(f"run {language}: sandbox error ({e.code}): {e}")
            return HTTP_FOR[e.code], {"error": {"code": e.code, "message": str(e)}}
        except Exception as e:
            log(f"run {language}: crashed ({type(e).__name__}: {e})")
            return 502, {"error": {"code": "backend", "message": f"sandbox backend failed ({type(e).__name__})"}}
        finally:
            self._slots.release()


def make_handler(broker: Broker):
    class Handler(BaseHTTPRequestHandler):
        server_version = "perseus-sandbox"

        def log_message(self, *a):  # quiet: the broker logs runs itself
            pass

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {broker.token}".encode())

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(503 if broker.state["probing"] else 200, {"probing": broker.state["probing"]})
            if not self._authorized():
                return self._send(401, {"error": {"code": "auth", "message": "bad token"}})
            if self.path == "/info":
                return self._send(200, broker.info())
            self._send(404, {"error": {"code": "config", "message": "not found"}})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": {"code": "auth", "message": "bad token"}})
            if self.path != "/run":
                return self._send(404, {"error": {"code": "config", "message": "not found"}})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"error": {"code": "config", "message": "request too large"}})
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"error": {"code": "config", "message": "not JSON"}})
            if not isinstance(body, dict):
                return self._send(400, {"error": {"code": "config", "message": "expected a JSON object"}})
            self._send(*broker.run(body))

    return Handler


def serve(broker: Broker, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(broker))
    httpd.daemon_threads = True
    threading.Thread(target=broker.start, name="sandbox-probe", daemon=True).start()
    return httpd


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m perseus.sandbox.broker")
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args(argv)
    token = os.environ.get("PERSEUS_SANDBOX_TOKEN", "")
    if len(token) < 16:
        sys.exit("PERSEUS_SANDBOX_TOKEN missing")
    settings = json.loads(os.environ.get("PERSEUS_SANDBOX_SETTINGS") or "{}")
    broker = Broker(settings.get("backend", "e2b"), settings, token)
    httpd = serve(broker, a.port)
    log(f"listening on 127.0.0.1:{a.port} (backend {broker.backend_name})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
