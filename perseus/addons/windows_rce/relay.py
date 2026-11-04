"""The RCE relay: a small supervised process between Perseus and the WindowsRCE API.

It holds the API key, checks every request against the [windows_rce] settings, and
turns the API's answer into the fixed outcome vocabulary run_code uses, so the model
reads Windows results the same way it reads Linux ones.

    python -m perseus.addons.windows_rce.relay --port N
    env PERSEUS_RCE_TOKEN      per-boot bearer token (required)
        PERSEUS_RCE_SETTINGS   JSON of the [windows_rce] config section, plus the Config folder's path;
                               the relay reads [windows_rce] api_key from Config/tooling.toml itself

Endpoints (all JSON, all but /healthz need the token):
    GET  /healthz   200 once start-up has finished (success or not), 503 before
    GET  /info      languages, limits, policy, whether the API is reachable, or the start-up error
    POST /run       {"language", "code", "files", "command", "stdin", "timeout_s", "network",
                     "keep_vm_state", "collect"} -> a run result
    POST /revert    put the VM back to its clean snapshot

Policy, enforced here whatever the model asks for:
  * network       only when allow_network = true; otherwise a request for it is refused
  * keep state    only when allow_keep_state = true; a VM left dirty is reverted after
                  idle_revert_s without use, and when the relay stops
  * time          timeout_s is capped at run_timeout_s
  * output        stdout and stderr capped at max_output_kb (head and tail kept);
                  collected text shown to the model capped at max_collect_kb in total
  * downloads     every collected file's exact bytes (text or binary, such as a built .exe) passed on
                  as base64 for output_file, up to max_download_mb in total per run

The log (logs/windows_rce.log) records which API and key (as a fingerprint) the relay uses,
then each run's job id, language, outcome and timing. Never the code or its output.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from perseus.addons.windows_rce.upstream import SECTION, Upstream, read_secret
from perseus.sandbox.backends import SandboxError
from perseus.sandbox.runner import cap

MAX_BODY = 2 * 1024 * 1024
HTTP_FOR = {"config": 400, "auth": 502, "quota": 429, "unavailable": 503, "backend": 502}

# language -> (label, main file the API writes `code` to). The API's own names; aliases are not offered.
LANGUAGES = {
    "python": ("Python 3", "main.py"),
    "node": ("Node.js", "main.js"),
    "powershell": ("Windows PowerShell 5.1", "main.ps1"),
    "cmd": ("cmd batch", "main.bat"),
    "csharp": ("C# (.NET)", "Program.cs"),
}
MAX_FILES = 20
MAX_COLLECT = 10
# relative paths inside the job folder: no drive letters, no absolute paths, no "..", no odd characters
PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\- ]{0,99}(/[A-Za-z0-9_][A-Za-z0-9_.\- ]{0,99}){0,5}")
CS_ERROR = re.compile(r"error CS\d{4}")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} windows_rce: {msg}", file=sys.stderr, flush=True)


@dataclass
class RelaySettings:
    url: str = "http://127.0.0.1:8765"
    allow_network: bool = False
    allow_keep_state: bool = True
    run_timeout_s: int = 120
    max_output_kb: int = 64
    max_collect_kb: int = 512
    max_download_mb: int = 64
    idle_revert_s: int = 600

    @classmethod
    def from_config(cls, cfg: dict) -> "RelaySettings":
        d = cls()
        return cls(url=str(cfg.get("url") or d.url),
                   allow_network=bool(cfg.get("allow_network", d.allow_network)),
                   allow_keep_state=bool(cfg.get("allow_keep_state", d.allow_keep_state)),
                   run_timeout_s=int(cfg.get("run_timeout_s", d.run_timeout_s)),
                   max_output_kb=int(cfg.get("max_output_kb", d.max_output_kb)),
                   max_collect_kb=int(cfg.get("max_collect_kb", d.max_collect_kb)),
                   max_download_mb=int(cfg.get("max_download_mb", d.max_download_mb)),
                   idle_revert_s=int(cfg.get("idle_revert_s", d.idle_revert_s)))


def clean_path(p: object) -> str:
    """A relative path inside the job folder, with forward slashes; raises SandboxError("config") otherwise."""
    if not isinstance(p, str):
        raise SandboxError("config", "file paths must be strings")
    norm = p.replace("\\", "/").strip()
    if not PATH_RE.fullmatch(norm) or any(part.strip(".") == "" for part in norm.split("/")):
        raise SandboxError("config", f"{p!r} isn't a plain relative path like 'data/input.csv' "
                                     "(no drive letters, no leading slash, no '..')")
    return norm


class Relay:
    def __init__(self, settings: dict, token: str, upstream: Upstream | None = None):
        self.cfg = dict(settings)
        self.s = RelaySettings.from_config(settings)
        self.token = token
        self.upstream = upstream
        self.state: dict = {"starting": True, "error": None, "reachable": None, "vm": None}
        self._run_lock = threading.Lock()
        self.dirty = False              # the VM holds state from a keep_vm_state run
        self.last_used = time.monotonic()
        self._stop = threading.Event()

    # ---------------------------------------------------------------- start-up
    def start(self) -> None:
        try:
            if self.upstream is None:
                key = read_secret(self.cfg.get("config_path", ""))
                self.upstream = Upstream(self.s.url, key)
            ident = self.upstream.describe()
            self.state["identity"] = ident
            log("identity: " + " · ".join(f"{k}={v}" for k, v in ident.items()))
            self.check()
        except SandboxError as e:
            self.state["error"] = {"code": e.code, "message": str(e)}
            log(f"not available ({e.code}): {e}")
        except Exception as e:  # never crash-loop on a bug; report it instead
            self.state["error"] = {"code": "backend", "message": type(e).__name__}
            log(f"start-up failed ({type(e).__name__}: {e})")
        finally:
            self.state["starting"] = False

    def check(self) -> dict:
        """Ask the API how the VM is. Unreachable is a state, not a start-up failure: the SSH tunnel
        or start-api.ps1 may come up after Perseus does. A rejected key is a failure."""
        try:
            st = self.upstream.status()
        except SandboxError as e:
            if e.code == "auth":
                raise
            self.state.update(reachable=False, vm=None, unreachable=str(e))
            log(f"API not reachable yet: {e}")
            return self.state
        vm = {k: st.get(k) for k in ("vm_state", "state", "ssh_reachable", "busy", "runs", "last_error")
              if k in st}
        self.state.update(reachable=True, vm=vm, unreachable=None)
        log("API reachable: " + ", ".join(f"{k}={v}" for k, v in vm.items()))
        return self.state

    def info(self) -> dict:
        s = self.s
        return {"backend": "windows_rce", "languages": list(LANGUAGES),
                "limits": {"run_timeout_s": s.run_timeout_s, "max_output_kb": s.max_output_kb,
                           "max_collect_kb": s.max_collect_kb, "max_download_mb": s.max_download_mb,
                           "idle_revert_s": s.idle_revert_s},
                "allow_network": s.allow_network, "allow_keep_state": s.allow_keep_state,
                "dirty": self.dirty,
                **{k: v for k, v in self.state.items() if k != "starting"}}

    # ---------------------------------------------------------------- requests
    def parse(self, body: dict) -> dict:
        """The API request for a /run body, after every policy check. Raises SandboxError("config")."""
        s = self.s
        language = body.get("language")
        if language not in LANGUAGES:
            raise SandboxError("config", f"language must be one of {', '.join(LANGUAGES)}")
        code, command, stdin = body.get("code") or "", body.get("command") or "", body.get("stdin") or ""
        if not all(isinstance(v, str) for v in (code, command, stdin)):
            raise SandboxError("config", "code, command and stdin must be strings")
        if not code.strip() and not command.strip():
            raise SandboxError("config", "send the program as code (or a command line to run)")
        if len(command) > 4000:
            raise SandboxError("config", "command is too long (4000 characters at most)")
        files = body.get("files") or {}
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise SandboxError("config", f"files must be an object of at most {MAX_FILES} path: text pairs")
        clean_files = {}
        main = LANGUAGES[language][1].lower()
        for path, text in files.items():
            p = clean_path(path)
            if not isinstance(text, str):
                raise SandboxError("config", f"files[{path!r}] must be text")
            if p.lower() == main and code.strip():
                raise SandboxError("config", f"{p} is where `code` goes; give the extra file another name")
            clean_files[p] = text
        collect = body.get("collect") or []
        if not isinstance(collect, list) or len(collect) > MAX_COLLECT:
            raise SandboxError("config", f"collect must be a list of at most {MAX_COLLECT} paths")
        timeout = body.get("timeout_s", min(60, s.run_timeout_s))
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= s.run_timeout_s:
            raise SandboxError("config", f"timeout_s must be a whole number of seconds from 1 to {s.run_timeout_s}")
        network = body.get("network", False) is True
        if network and not s.allow_network:
            raise SandboxError("config", "network access is switched off for the Windows VM "
                                         f"([{SECTION}] allow_network = false); run it without network")
        keep = body.get("keep_vm_state", False) is True
        if keep and not s.allow_keep_state:
            raise SandboxError("config", f"keeping VM state is switched off ([{SECTION}] allow_keep_state = false); "
                                         "every run starts clean, so send all files and installs in one run")
        req = {"language": language, "code": code, "files": clean_files, "stdin": stdin, "timeout": timeout,
               "network": network, "revert": not keep, "collect": [clean_path(c) for c in collect]}
        if command.strip():
            req["command"] = command
        return req

    def run(self, body: dict) -> tuple[int, dict]:
        if self.state["error"] or self.upstream is None:
            return 503, {"error": self.state["error"] or {"code": "unavailable", "message": "starting up"}}
        try:
            req = self.parse(body)
        except SandboxError as e:
            return 400, {"error": {"code": e.code, "message": str(e)}}
        if not self._run_lock.acquire(timeout=self.s.run_timeout_s + 300):
            return 429, {"error": {"code": "quota", "message": "the Windows VM is busy with another run"}}
        t0 = time.monotonic()
        try:
            raw = self.upstream.execute(req)
            self.dirty = not req["revert"]
            self.state["reachable"] = True
            result = self.shape(req, raw)
            result["seconds"] = round(time.monotonic() - t0, 2)
            log(f"run {req['language']} job {result.get('job_id', '?')} (network {'on' if req['network'] else 'off'}, "
                f"vm {result['vm_state']}): {result['status']} in {result['seconds']}s")
            return 200, result
        except SandboxError as e:
            if e.code == "unavailable":
                self.state["reachable"] = False
            log(f"run {req['language']}: API error ({e.code}): {e}")
            return HTTP_FOR[e.code], {"error": {"code": e.code, "message": str(e)}}
        except Exception as e:
            log(f"run {req['language']}: crashed ({type(e).__name__}: {e})")
            return 502, {"error": {"code": "backend", "message": f"relay failed ({type(e).__name__})"}}
        finally:
            self.last_used = time.monotonic()
            self._run_lock.release()

    def shape(self, req: dict, raw: dict) -> dict:
        """The API's answer in run_code's vocabulary: ok, compile_error, runtime_error, timeout."""
        s = self.s
        exit_code, timed_out = raw.get("exit_code"), bool(raw.get("timed_out"))
        out_raw, err_raw = str(raw.get("stdout") or ""), str(raw.get("stderr") or "")
        if exit_code == 255 and not out_raw and not err_raw and not timed_out:
            self.dirty = True     # whatever state the VM is in, it isn't known to be clean
            raise SandboxError("unavailable", "the API couldn't reach the VM over SSH (exit 255, no output)")
        per_stream = max(1024, s.max_output_kb * 1024 // 2)
        stdout, cut_o = cap(out_raw, per_stream)
        stderr, cut_e = cap(err_raw, per_stream)
        if timed_out or exit_code is None:
            status, note = "timeout", f"the program ran past {req['timeout']}s and was stopped"
        elif exit_code == 0:
            status, note = "ok", None
        elif req["language"] == "csharp" and CS_ERROR.search(out_raw + err_raw):
            status, note = "compile_error", None
        else:
            status, note = "runtime_error", None
        out = {"language": req["language"], "status": status, "exit_code": exit_code, "stdout": stdout,
               "stderr": stderr, "network": "on" if req["network"] else "off",
               "vm_state": "reverted" if req["revert"] else "kept", "backend": "windows_rce",
               "job_id": str(raw.get("job_id") or ""), "run_seconds": raw.get("duration_s")}
        if note:
            out["note"] = note
        if cut_o or cut_e or raw.get("truncated"):
            out["output_cut"] = True
        files, missing = self.collected(raw.get("collected_b64") or {})
        if files:
            out["files"] = files
        missing += [str(m) for m in raw.get("missing") or []]
        if missing:
            out["missing"] = missing
        return out

    def collected(self, blobs: dict) -> tuple[list[dict], list[str]]:
        """Collected files: text (capped) or just name and size for the model, plus each file's
        exact bytes as base64 ("b64") for output_file while the max_download_mb budget lasts."""
        budget = self.s.max_collect_kb * 1024
        download = self.s.max_download_mb * 1024 * 1024
        files, missing = [], []
        for path, b64 in list(blobs.items())[:MAX_COLLECT]:
            try:
                data = base64.b64decode(b64, validate=True)
            except (ValueError, TypeError):
                missing.append(str(path))
                continue
            entry: dict = {"path": str(path).replace("\\", "/"), "bytes": len(data)}
            text = None
            if b"\x00" not in data[:8192]:
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = None
            if text is None:
                entry["binary"] = True
            elif budget <= 0:
                entry["omitted"] = True
            else:
                shown, cut = cap(text, budget)
                entry["text"] = shown
                if cut:
                    entry["cut"] = True
                budget -= len(shown.encode())
            if len(data) <= download:
                entry["b64"] = b64 if isinstance(b64, str) else base64.b64encode(data).decode()
                download -= len(data)
            else:
                entry["too_large"] = True       # over this run's download budget: the model is told why
            files.append(entry)
        return files, missing

    def revert(self, reason: str = "requested") -> tuple[int, dict]:
        if self.state["error"] or self.upstream is None:
            return 503, {"error": self.state["error"] or {"code": "unavailable", "message": "starting up"}}
        if not self._run_lock.acquire(timeout=self.s.run_timeout_s + 300):
            return 429, {"error": {"code": "quota", "message": "the Windows VM is busy with another run"}}
        try:
            self.upstream.revert()
            self.dirty = False
            log(f"VM reverted to its clean snapshot ({reason})")
            return 200, {"reverted": True}
        except SandboxError as e:
            log(f"revert ({reason}) failed ({e.code}): {e}")
            return HTTP_FOR[e.code], {"error": {"code": e.code, "message": str(e)}}
        finally:
            self.last_used = time.monotonic()
            self._run_lock.release()

    # ---------------------------------------------------------------- housekeeping
    def sweep(self) -> bool:
        """Revert a VM that was left dirty and hasn't been used for idle_revert_s. True if it reverted."""
        if not self.dirty or time.monotonic() - self.last_used < self.s.idle_revert_s:
            return False
        if not self._run_lock.acquire(blocking=False):
            return False
        self._run_lock.release()
        return self.revert(f"idle {self.s.idle_revert_s}s with kept state")[0] == 200

    def housekeeping(self, every_s: float = 15.0) -> None:
        while not self._stop.wait(every_s):
            try:
                self.sweep()
            except Exception as e:
                log(f"housekeeping failed ({type(e).__name__}: {e})")

    def shutdown(self) -> None:
        self._stop.set()
        if self.dirty:
            self.revert("relay stopping")


def make_handler(relay: Relay):
    class Handler(BaseHTTPRequestHandler):
        server_version = "perseus-rce-relay"

        def log_message(self, *a):  # quiet: the relay logs runs itself
            pass

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {relay.token}".encode())

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(503 if relay.state["starting"] else 200, {"starting": relay.state["starting"]})
            if not self._authorized():
                return self._send(401, {"error": {"code": "auth", "message": "bad token"}})
            if self.path == "/info":
                if relay.upstream is not None and not relay.state["error"] and not relay.state["reachable"]:
                    try:
                        relay.check()        # the tunnel may have come up since start-up
                    except SandboxError:
                        pass
                return self._send(200, relay.info())
            self._send(404, {"error": {"code": "config", "message": "not found"}})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": {"code": "auth", "message": "bad token"}})
            if self.path not in ("/run", "/revert"):
                return self._send(404, {"error": {"code": "config", "message": "not found"}})
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": {"code": "config", "message": "request too large"}})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": {"code": "config", "message": "not JSON"}})
            if not isinstance(body, dict):
                return self._send(400, {"error": {"code": "config", "message": "expected a JSON object"}})
            self._send(*(relay.run(body) if self.path == "/run" else relay.revert()))

    return Handler


def serve(relay: Relay, port: int, host: str = "127.0.0.1", housekeeping: bool = True) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(relay))
    httpd.daemon_threads = True
    threading.Thread(target=relay.start, name="rce-start", daemon=True).start()
    if housekeeping:
        threading.Thread(target=relay.housekeeping, name="rce-housekeeping", daemon=True).start()
    return httpd


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m perseus.addons.windows_rce.relay")
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args(argv)
    token = os.environ.get("PERSEUS_RCE_TOKEN", "")
    if len(token) < 16:
        sys.exit("PERSEUS_RCE_TOKEN missing")
    relay = Relay(json.loads(os.environ.get("PERSEUS_RCE_SETTINGS") or "{}"), token)
    httpd = serve(relay, a.port)
    log(f"listening on 127.0.0.1:{a.port} (API {relay.s.url})")

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        relay.shutdown()


if __name__ == "__main__":
    main()
