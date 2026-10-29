"""The backend contract: everything Perseus needs from a code sandbox, and nothing more.

A backend opens a fresh, isolated Linux machine (a session), writes source
files into its job folder, runs commands there with a time limit, and destroys
it. That is the whole interface, on purpose: every provider can do these three
things, so any provider can sit behind it. Anything fancier (snapshots, port
previews, persistence) belongs in `Capabilities`, never in the contract.

Backends run only inside the sandbox broker process (perseus.sandbox.broker),
never inside Perseus itself, so a provider SDK and its API key never reach the
process that runs tools. Every backend must pass tests/sandbox_conformance.py.

Adding a provider: one module in this folder with a class that implements
`Backend`, plus an entry in BACKENDS.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Protocol


# ---------------------------------------------------------------- errors
class SandboxError(Exception):
    """A failure of the sandbox itself (not of the user's program). Backends translate
    their provider's exceptions into one of these codes, so every backend fails alike."""

    CODES = ("unavailable", "auth", "quota", "config", "backend")

    def __init__(self, code: str, message: str):
        assert code in self.CODES, code
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- values
@dataclass
class ExecResult:
    exit_code: int | None       # None when the command was killed (timeout)
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0


@dataclass
class SessionSpec:
    internet: bool = False      # outbound network from inside the sandbox
    lifetime_s: int = 300       # hard upper bound; the session is destroyed after this even if never closed


@dataclass
class Capabilities:
    os: str = "linux"
    controls_network: bool = True       # can actually switch the sandbox's internet off
    max_lifetime_s: int = 3600
    extras: dict = field(default_factory=dict)


# ---------------------------------------------------------------- protocol
class Session(Protocol):
    def write_file(self, path: str, content: str) -> None:
        """Write `content` to `path`, relative to the job folder."""

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        """Run a shell command in the job folder. A command that runs past timeout_s is
        killed and reported with timed_out=True (never raised). Non-zero exits are results,
        not errors. Raise SandboxError only when the sandbox itself fails."""

    def close(self) -> None:
        """Destroy the sandbox. Idempotent; must not raise."""


class Backend(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    def open(self, spec: SessionSpec) -> Session:
        """A fresh sandbox with an empty job folder and no credentials or host environment."""


# ---------------------------------------------------------------- discovery
# name -> "module:Class". Only names listed in CONFIGURABLE may be chosen in perseus.toml;
# the others exist for tests and development.
BACKENDS = {
    "e2b": "perseus.sandbox.backends.e2b:E2BBackend",
    "local_unsafe": "perseus.sandbox.backends.local_unsafe:LocalUnsafeBackend",
    "fake": "perseus.sandbox.backends.fake:FakeBackend",
}
CONFIGURABLE = ("e2b",)


def make(name: str, settings: dict) -> Backend:
    if name not in BACKENDS:
        raise SandboxError("config", f"unknown sandbox backend {name!r} (known: {', '.join(BACKENDS)})")
    module, _, cls = BACKENDS[name].partition(":")
    return getattr(importlib.import_module(module), cls)(settings)
