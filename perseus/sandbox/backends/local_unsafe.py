"""local_unsafe: runs programs as ordinary local processes. FOR TESTS AND DEVELOPMENT ONLY.

This is not a sandbox: the code shares this machine's kernel, files and users.
It exists so the conformance suite can exercise the real broker -> runner ->
backend path on a laptop or CI runner without a provider account.

It refuses to start unless PERSEUS_UNSAFE_LOCAL_SANDBOX=1 is set, it can never
be selected in tooling.toml (see backends.CONFIGURABLE), and it only runs when
it can cut the network with a user+network namespace (`unshare -rn`).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from perseus.sandbox.backends import Capabilities, ExecResult, SandboxError, SessionSpec

FLAG = "PERSEUS_UNSAFE_LOCAL_SANDBOX"


def namespaces_work() -> bool:
    if not shutil.which("unshare"):
        return False
    try:
        return subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class LocalSession:
    def __init__(self, network: str):
        self.dir = Path(tempfile.mkdtemp(prefix="perseus-job-"))
        self.id = f"local:{self.dir.name}"
        self.internet = network == "full"     # no host allowlist here: "packages" is refused up front
        # a clean environment: nothing from the host process (keys, tokens, proxies) leaks in
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.dir), "LANG": "C.UTF-8",
                    "TMPDIR": str(self.dir)}
        # rustup finds its toolchains through HOME; point it at the host's (paths only, no credentials)
        host_home = Path(os.path.expanduser("~"))
        for var, default in (("RUSTUP_HOME", host_home / ".rustup"), ("CARGO_HOME", host_home / ".cargo")):
            path = os.environ.get(var) or (str(default) if default.is_dir() else "")
            if path:
                self.env[var] = path

    def write_file(self, path: str, content: str) -> None:
        target = (self.dir / path).resolve()
        if self.dir.resolve() not in target.parents:
            raise SandboxError("backend", "path escapes the job folder")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        argv = ["bash", "-c", cmd] if self.internet else ["unshare", "-rn", "bash", "-c", cmd]
        t0 = time.monotonic()
        proc = subprocess.Popen(argv, cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout_s)
            return ExecResult(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"),
                              False, time.monotonic() - t0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            out, err = proc.communicate()
            return ExecResult(None, out.decode(errors="replace"), err.decode(errors="replace"), True,
                              time.monotonic() - t0)

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class LocalUnsafeBackend:
    name = "local_unsafe"

    def __init__(self, settings: dict, environ=os.environ):
        if environ.get(FLAG) != "1":
            raise SandboxError("config", f"local_unsafe is for tests only; set {FLAG}=1 to use it")
        if not namespaces_work():
            raise SandboxError("config", "local_unsafe needs `unshare -rn` (user and network namespaces)")

    def capabilities(self) -> Capabilities:
        return Capabilities(os="linux", controls_network=True, max_lifetime_s=3600, network_levels=("none", "full"),
                            extras={"unsafe": True})

    def describe(self) -> dict:
        return {"provider": "local_unsafe", "note": "TEST BACKEND: programs run as local processes on this machine"}

    def open(self, spec: SessionSpec) -> LocalSession:
        if spec.network not in ("none", "full"):
            raise SandboxError("config", f"local_unsafe can't enforce network = {spec.network!r} (none or full only)")
        return LocalSession(spec.network)
