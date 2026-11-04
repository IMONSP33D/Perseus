"""The addon's own process: owns the API key, enforces the [example] policy, answers on loopback.

    python -m perseus.addons.example.service --port N
    env PERSEUS_EXAMPLE_TOKEN      per-boot bearer token (required; every request but /healthz carries it)
        PERSEUS_EXAMPLE_SETTINGS   JSON of the [example] section plus the Config folder's path; the service
                                   reads [example] api_key from Config/tooling.toml itself, so the key is
                                   never in Perseus's environment or memory

Why a separate process when the service already has an API: the engine's socket guard lets it reach
only its own loopback ports, and no API key ever sits in the process that runs tools. This is also
where limits live (timeouts, sizes), so the model can only ask for what tooling.toml allows.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from perseus.addons.common import AddonError, fingerprint, read_section_secret

SECTION = "example"


def log(msg: str) -> None:
    print(f"{SECTION}: {msg}", file=sys.stderr, flush=True)   # the launcher writes stderr to logs/example.log


class Service:
    def __init__(self, settings: dict):
        self.cfg = settings
        self.error: dict | None = None
        self.key = ""
        try:
            self.key = read_section_secret(settings.get("config_path", ""), SECTION)
            if not self.key:
                raise AddonError("config", f"no [{SECTION}] api_key in Config/tooling.toml")
            log(f"using {settings.get('url')} with key {fingerprint(self.key)}")
        except AddonError as e:
            self.error = {"code": e.code, "message": str(e)}
            log(f"start-up failed: {e}")

    def info(self) -> dict:
        if self.error:
            return {"error": self.error}
        return {"identity": {"api_url": self.cfg.get("url"), "key": fingerprint(self.key)},
                "limits": {"timeout_s": int(self.cfg.get("timeout_s", 30))}}

    def echo(self, req: dict) -> dict:
        # enforce the policy here, whatever the model asked for
        timeout = min(float(req.get("timeout_s") or 30), float(self.cfg.get("timeout_s", 30)))
        text = str(req.get("text", ""))[:10_000]
        # ... call the real service at self.cfg["url"] with self.key here ...
        return {"text": text, "timeout_s": timeout}


def serve(port: int, token: str, settings: dict) -> None:
    svc = Service(settings)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # quiet; the service logs what matters itself
            pass

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            given = self.headers.get("Authorization", "")
            return given.startswith("Bearer ") and hmac.compare_digest(given[7:], token)

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            if not self._authed():
                return self._send(401, {"error": {"code": "auth", "message": "bad token"}})
            if self.path == "/info":
                return self._send(200, svc.info())
            self._send(404, {"error": {"code": "config", "message": "no such endpoint"}})

        def do_POST(self):
            if not self._authed():
                return self._send(401, {"error": {"code": "auth", "message": "bad token"}})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(min(n, 1024 * 1024)) or b"{}")
            except ValueError:
                return self._send(400, {"error": {"code": "config", "message": "bad JSON"}})
            if self.path == "/echo":
                if svc.error:
                    return self._send(503, {"error": svc.error})
                try:
                    return self._send(200, svc.echo(req))
                except AddonError as e:
                    return self._send(502, {"error": {"code": e.code, "message": str(e)}})
            self._send(404, {"error": {"code": "config", "message": "no such endpoint"}})

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args(argv)
    token = os.environ.get("PERSEUS_EXAMPLE_TOKEN", "")
    if not token:
        log("PERSEUS_EXAMPLE_TOKEN is not set")
        return 2
    settings = json.loads(os.environ.get("PERSEUS_EXAMPLE_SETTINGS") or "{}")
    serve(a.port, token, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
