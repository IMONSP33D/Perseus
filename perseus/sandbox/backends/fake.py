"""fake: a scripted backend for unit tests. Runs nothing; never selectable in perseus.toml.

    backend = FakeBackend({})
    backend.on("./main", ExecResult(0, "hi\n"))      # exact command -> result
    backend.fail_open = SandboxError("quota", "...")  # make open() fail

Unscripted commands succeed with empty output, so language checks pass by default.
`sessions` records every session for assertions (files written, commands run, closed).
"""

from __future__ import annotations

from typing import Callable

from perseus.sandbox.backends import Capabilities, ExecResult, SandboxError, SessionSpec


class FakeSession:
    def __init__(self, backend: "FakeBackend", spec: SessionSpec):
        self.backend, self.spec = backend, spec
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, float]] = []
        self.closed = False

    def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        self.commands.append((cmd, timeout_s))
        for match, result in self.backend.script:
            if (match(cmd) if callable(match) else match == cmd):
                r = result(self, cmd) if callable(result) else result
                if isinstance(r, SandboxError):
                    raise r
                return r
        return ExecResult(0, "", "")

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    name = "fake"

    def __init__(self, settings: dict | None = None, environ=None):
        self.settings = settings or {}
        self.script: list[tuple[str | Callable[[str], bool], object]] = []
        self.sessions: list[FakeSession] = []
        self.fail_open: SandboxError | None = None

    def on(self, match, result) -> "FakeBackend":
        self.script.append((match, result))
        return self

    def capabilities(self) -> Capabilities:
        return Capabilities(os="linux", controls_network=True, max_lifetime_s=3600)

    def open(self, spec: SessionSpec) -> FakeSession:
        if self.fail_open:
            raise self.fail_open
        s = FakeSession(self, spec)
        self.sessions.append(s)
        return s
