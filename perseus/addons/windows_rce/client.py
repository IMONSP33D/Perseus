"""The engine's handle on the RCE relay; tools see it as `ctx.windows`.

Like perseus.sandbox.client: one fixed loopback address that nothing from the
model can change, a per-boot token, never a proxy. The socket guard allows the
relay's port; the gateway refuses loopback, so fetch_url can never reach it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from perseus.tools import ToolFailure

# what the model is told when the VM or its API fails (not the program)
MESSAGES = {
    "unavailable": "the Windows VM is unreachable right now (its API or tunnel is down); "
                   "answer without running Windows code, or tell the user to start it",
    "quota": "the Windows VM is busy with another run; try again shortly",
    "auth": "the Windows VM's API rejected Perseus's key; answer without running Windows code",
    "config": "the Windows VM refused the request",
    "backend": "the Windows VM failed; answer without running Windows code",
}


class WindowsClient:
    def __init__(self, port: int, token: str, request_timeout_s: float = 900.0):
        self.base = f"http://127.0.0.1:{int(port)}"   # fixed; nothing from the model reaches this string
        self.token = token
        self.request_timeout_s = request_timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.limits: dict = {}

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> tuple[int, dict]:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read(16 * 1024 * 1024) or b"{}")
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
        status, data = self._call("GET", "/info", timeout=30)
        if status != 200:
            err = data.get("error") or {}
            return {"error": {"code": err.get("code", "unavailable"), "message": err.get("message", f"HTTP {status}")}}
        self.limits = data.get("limits") or {}
        if self.limits:
            # a run, plus waiting out the run or revert ahead of it, plus the API's own overhead
            self.request_timeout_s = int(self.limits.get("run_timeout_s", 120)) * 2 + 420
        return data

    def _result(self, status: int, data: dict) -> dict:
        if status == 200:
            return data
        err = data.get("error") or {}
        if status == 413:
            raise ToolFailure("the program and its files are too large for the Windows VM; send less")
        if status == 400:   # the request itself was wrong or not allowed: say exactly why
            raise ToolFailure(err.get("message") or MESSAGES["config"])
        raise ToolFailure(MESSAGES.get(err.get("code", "backend"), MESSAGES["backend"]))

    def run(self, request: dict) -> dict:
        return self._result(*self._call("POST", "/run", request, timeout=self.request_timeout_s))

    def revert(self) -> dict:
        return self._result(*self._call("POST", "/revert", {}, timeout=self.request_timeout_s))
