"""The engine's handle on the sandbox broker; tools see it as `ctx.sandbox`.

Like perseus.search, this talks to one fixed loopback address that nothing
from the model can change, with the per-boot token. The socket guard allows it
by port (socketguard.allow_loopback_port), and the gateway refuses loopback, so
fetch_url can never reach the broker.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from perseus.tools import ToolFailure

# what the model is told when the sandbox itself fails (not the program)
MESSAGES = {
    "unavailable": "the code sandbox is unreachable right now; try again shortly or answer without running code",
    "quota": "the code sandbox is busy or out of quota; answer without running code",
    "auth": "the code sandbox rejected Perseus's credentials; answer without running code",
    "config": "the code sandbox refused the request",
    "backend": "the code sandbox failed; answer without running code",
}


class SandboxClient:
    def __init__(self, port: int, token: str, request_timeout_s: float = 300.0):
        self.base = f"http://127.0.0.1:{int(port)}"   # fixed; nothing from the model reaches this string
        self.token = token
        self.request_timeout_s = request_timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
        self.languages: list[str] = []
        self.internet = False

    @property
    def reaches_web(self) -> bool:
        return self.internet

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read(8 * 1024 * 1024) or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read(1024 * 1024) or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"error": {"code": "unavailable", "message": type(e).__name__}}

    def healthy(self) -> bool:
        try:
            with self._opener.open(self.base + "/healthz", timeout=3) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def info(self) -> dict:
        status, data = self._call("GET", "/info")
        if status != 200:
            err = data.get("error") or {}
            return {"error": {"code": err.get("code", "unavailable"), "message": err.get("message", f"HTTP {status}")}}
        self.languages = list(data.get("languages") or [])
        self.internet = bool(data.get("internet"))
        limits = data.get("limits") or {}
        if limits:
            self.request_timeout_s = limits.get("build_timeout_s", 120) + limits.get("run_timeout_s", 30) + 90
        return data

    def run(self, language: str, code: str, stdin: str = "") -> dict:
        status, data = self._call("POST", "/run", {"language": language, "code": code, "stdin": stdin},
                                  timeout=self.request_timeout_s)
        if status == 200:
            return data
        err = data.get("error") or {}
        code_ = err.get("code", "backend")
        if status == 400:   # the request itself was wrong (e.g. a language that isn't installed): say exactly why
            raise ToolFailure(err.get("message") or MESSAGES["config"])
        raise ToolFailure(MESSAGES.get(code_, MESSAGES["backend"]))
