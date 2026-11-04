"""What tools get as ctx.example: a fixed loopback address and a per-boot token, never a proxy, never a key.

The launcher builds one of these with the port and token it chose, so nothing the model sends can
change where a tool connects. The socket guard allows this port; the gateway refuses loopback, so
fetch_url can never reach it. If your addon runs no process of its own, this class can talk to the
service directly; keep the key in the process that needs it and out of the engine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from perseus.tools import ToolFailure

# what the model is told when the service (not the user's request) fails, by AddonError code
MESSAGES = {
    "unavailable": "the example service is unreachable right now; answer without it",
    "quota": "the example service is busy; try again shortly",
    "auth": "the example service rejected Perseus's key; answer without it",
    "config": "the example service refused the request",
    "backend": "the example service failed; answer without it",
}


class ExampleClient:
    def __init__(self, port: int, token: str):
        self.base = f"http://127.0.0.1:{int(port)}"   # fixed; nothing from the model reaches this string
        self.token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read(1024 * 1024) or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"error": {"code": "unavailable", "message": type(e).__name__}}

    def healthy(self) -> bool:
        """The launcher polls this until the process is up (and restarts the process when it stops being)."""
        try:
            with self._opener.open(self.base + "/healthz", timeout=3) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def info(self) -> dict:
        """What connect() reads once at boot: the service's identity and limits, or {"error": {...}}."""
        _, body = self._call("GET", "/info")
        return body

    def echo(self, text: str, timeout_s: float) -> dict:
        """One tool call. Raise ToolFailure with a message the model can act on; never leak the key."""
        status, body = self._call("POST", "/echo", {"text": text, "timeout_s": timeout_s}, timeout=timeout_s + 5)
        err = body.get("error") if isinstance(body, dict) else None
        if status != 200 or err:
            code = (err or {}).get("code", "backend")
            raise ToolFailure(MESSAGES.get(code, MESSAGES["backend"]))
        return body
