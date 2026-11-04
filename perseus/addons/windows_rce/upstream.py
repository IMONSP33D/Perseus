"""The WindowsRCE sandbox API, as the relay calls it. Used by the relay process only, never by Perseus.

    POST /execute   {"language", "code", "files", "command", "stdin", "timeout", "network", "revert", "collect"}
    GET  /status    VM state, ssh_reachable, busy, last_revert, last_error, runs
    POST /revert    revert the VM to its clean snapshot now

Every request carries the X-API-Key header. Failures of the API itself (not of the user's
program) become SandboxError codes, so the relay reports them the same way the code
runner's broker does.
"""

from __future__ import annotations

import json
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from perseus.sandbox.backends import SandboxError, fingerprint, tooling_file

SECTION = "windows_rce"
SECRET_KEY = "api_key"
PLACEHOLDER = "..."
MAX_RESPONSE = 192 * 1024 * 1024   # a run's JSON, collected files as base64 included (max_download_mb <= 128)


def read_secret(config_path: str | Path) -> str:
    """[windows_rce] api_key from Config/tooling.toml. Called by the relay only; opens tooling.toml alone."""
    if not config_path:
        return ""
    path = tooling_file(config_path)
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SandboxError("config", f"can't read {path} for [{SECTION}] {SECRET_KEY} ({type(e).__name__})")
    key = str((data.get(SECTION) or {}).get(SECRET_KEY) or "").strip()
    return "" if key.endswith(PLACEHOLDER) else key


def check_url(url: str) -> str:
    """The API's base URL: http(s), a host, an optional port, nothing else. Returns it without a trailing slash."""
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SandboxError("config", f"[{SECTION}] url must look like http://127.0.0.1:8765 (got {url!r})")
    if u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
        raise SandboxError("config", f"[{SECTION}] url must be the API's address only, with no path or credentials")
    return f"{u.scheme}://{u.netloc}"


class Upstream:
    def __init__(self, url: str, key: str, opener=None):
        if not key:
            raise SandboxError("config", f"no [{SECTION}] api_key in Config/tooling.toml")
        self.base = check_url(url)
        self.key = key
        # the API is a fixed address from tooling.toml; never route it through a proxy from the environment
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def describe(self) -> dict:
        return {"provider": "windows_rce", "api_url": self.base, "key": fingerprint(self.key)}

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-API-Key": self.key, "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read(MAX_RESPONSE) or b"{}")
        except urllib.error.HTTPError as e:
            raise _translate_http(e)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise SandboxError("unavailable", f"WindowsRCE API at {self.base} unreachable ({type(reason).__name__}); "
                                              "is start-api.ps1 running and the tunnel up?")
        except ValueError:
            raise SandboxError("backend", "WindowsRCE API answered with something that isn't JSON")

    def status(self) -> dict:
        return self._call("GET", "/status", timeout=15)

    def revert(self) -> dict:
        return self._call("POST", "/revert", {}, timeout=300)

    def execute(self, body: dict, wait_busy_s: float = 120.0) -> dict:
        """Run one job. The API takes one job at a time; while it says busy (a revert or another run),
        wait and retry for up to `wait_busy_s` before reporting quota."""
        deadline = time.monotonic() + wait_busy_s
        delay = 2.0
        while True:
            try:
                return self._call("POST", "/execute", body, timeout=int(body.get("timeout", 60)) + 600)
            except SandboxError as e:
                if e.code != "quota" or time.monotonic() + delay > deadline:
                    raise
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)


def _translate_http(e: urllib.error.HTTPError) -> SandboxError:
    try:
        detail = json.loads(e.read(64 * 1024) or b"{}").get("detail")
    except (ValueError, AttributeError, OSError):
        detail = None
    detail = str(detail)[:300] if detail else f"HTTP {e.code}"
    if e.code in (401, 403):
        return SandboxError("auth", "the WindowsRCE API rejected the key in [windows_rce] api_key")
    if e.code == 503:
        return SandboxError("quota", f"the Windows VM is busy ({detail})")
    if e.code in (400, 404, 413, 422):
        return SandboxError("config", f"the WindowsRCE API refused the request: {detail}")
    return SandboxError("backend", f"the WindowsRCE API failed: {detail}")
