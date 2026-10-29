"""E2B: each session is a fresh Firecracker microVM in E2B's cloud.

Settings ([code_runner] in perseus.toml):
  key_file      file holding the E2B API key (read here, in the broker, never by Perseus);
                if empty, the broker's E2B_API_KEY environment variable
  e2b_template  template ID or alias with the toolchains installed (setup/sandbox/);
                empty = E2B's default template (Python, Node.js, Bash only)

Written against the e2b Python SDK 2.x (pinned in pyproject.toml).
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

from perseus.sandbox.backends import Capabilities, ExecResult, SandboxError, SessionSpec

WORKDIR = "/home/user/job"
KILL_GRACE_S = 2


def read_key(settings: dict, environ=os.environ) -> str:
    path = (settings.get("key_file") or "").strip()
    if path:
        p = Path(path).expanduser()
        try:
            key = p.read_text().strip()
        except OSError as e:
            raise SandboxError("config", f"can't read the E2B key file {p} ({type(e).__name__})")
        if not key:
            raise SandboxError("config", f"the E2B key file {p} is empty")
        return key
    key = (environ.get("E2B_API_KEY") or "").strip()
    if not key:
        raise SandboxError("config", "no E2B API key: set [code_runner] key_file, or E2B_API_KEY")
    return key


def _translate(e: Exception) -> SandboxError:
    """E2B exceptions -> the shared SandboxError codes."""
    name = type(e).__name__
    if name == "AuthenticationException":
        return SandboxError("auth", "E2B rejected the API key")
    if name in ("RateLimitException", "NotEnoughSpaceException"):
        return SandboxError("quota", "E2B rate limit, concurrency limit or credits reached")
    if name in ("TemplateException", "NotFoundException", "InvalidArgumentException"):
        return SandboxError("config", f"E2B refused the sandbox settings ({name}): {e}"[:300])
    if name in ("SandboxUnreachableException", "ServiceBusyException", "SandboxNotRunningException",
                "SandboxNotFoundException", "TimeoutException", "ConnectError", "ReadTimeout", "ConnectTimeout"):
        return SandboxError("unavailable", f"E2B unreachable ({name})")
    return SandboxError("backend", f"E2B error ({name})")


class E2BSession:
    def __init__(self, sbx):
        self.sbx = sbx
        self._closed = False

    def write_file(self, path: str, content: str) -> None:
        try:
            self.sbx.files.write(f"{WORKDIR}/{path}", content)
        except Exception as e:
            raise _translate(e)

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        from e2b import CommandExitException

        t = max(1, int(round(timeout_s)))
        # coreutils `timeout` kills the program inside the VM; the connection timeout is only a backstop
        wrapped = f"timeout -k {KILL_GRACE_S} {t} bash -c {shlex.quote(cmd)}"
        out: list[str] = []
        err: list[str] = []
        t0 = time.monotonic()
        try:
            r = self.sbx.commands.run(wrapped, cwd=WORKDIR, timeout=t + KILL_GRACE_S + 10,
                                      on_stdout=out.append, on_stderr=err.append)
            code, stdout, stderr = r.exit_code, r.stdout, r.stderr
        except CommandExitException as e:
            code, stdout, stderr = e.exit_code, e.stdout, e.stderr
        except Exception as e:
            if type(e).__name__ == "TimeoutException":   # backstop fired: treat as a killed program
                return ExecResult(None, "".join(out), "".join(err), True, time.monotonic() - t0)
            raise _translate(e)
        elapsed = time.monotonic() - t0
        if code in (124, 137) and elapsed >= t * 0.95:
            return ExecResult(None, stdout or "".join(out), stderr or "".join(err), True, elapsed)
        return ExecResult(code, stdout, stderr, False, elapsed)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sbx.kill()
        except Exception:
            pass   # the sandbox's own lifetime timeout destroys it anyway


class E2BBackend:
    name = "e2b"

    def __init__(self, settings: dict, environ=os.environ):
        try:
            import e2b  # noqa: F401
        except ImportError:
            raise SandboxError("config", "the e2b package isn't installed (bash preflight.sh installs it)")
        self.key = read_key(settings, environ)
        self.template = (settings.get("e2b_template") or "").strip() or None

    def capabilities(self) -> Capabilities:
        return Capabilities(os="linux", controls_network=True, max_lifetime_s=3600,
                            extras={"provider": "e2b", "template": self.template or "default"})

    def open(self, spec: SessionSpec) -> E2BSession:
        from e2b import Sandbox

        try:
            sbx = Sandbox.create(template=self.template, timeout=min(int(spec.lifetime_s), 3600),
                                 allow_internet_access=bool(spec.internet), api_key=self.key,
                                 metadata={"app": "perseus"})
        except Exception as e:
            raise _translate(e)
        session = E2BSession(sbx)
        try:
            sbx.commands.run(f"mkdir -p {WORKDIR}", timeout=30)
        except Exception as e:
            session.close()
            raise _translate(e)
        return session
