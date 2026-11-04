"""The backend contract: everything Perseus needs from a code sandbox, and nothing more.

A backend opens a fresh, isolated Linux machine (a session), writes source
files into its job folder, runs commands there with a time limit, and destroys
it. That is the whole interface, on purpose: every provider can do these three
things, so any provider can sit behind it. Anything fancier (snapshots, port
previews, persistence) belongs in `Capabilities`, never in the contract.

Backends run only inside the sandbox broker process (perseus.addons.e2b_code_environment.broker),
never inside Perseus itself, so a provider SDK and its API key never reach the
process that runs tools. Every backend must pass tests/sandbox_conformance.py.

Adding a provider: one module in this folder with a class that implements
`Backend`, plus an entry in BACKENDS.
"""

from __future__ import annotations

import importlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from perseus.addons.common import AddonError, fingerprint, read_section_secret, tooling_file  # noqa: F401 (re-exported)

SECTION = "e2b_code_environment"       # the addon's [section] in Config/tooling.toml
FORMER_SECTIONS = ("code_runner", "secrets:code_runner_api_key")   # old homes of the key, read until configuration.sh moves it
SECRET_KEY = "api_key"                 # [e2b_code_environment] api_key, beside the tool's settings
PLACEHOLDER = "e2b_..."

# ---------------------------------------------------------------- errors
SandboxError = AddonError   # the addon's own name for a failure of the sandbox itself (not of the user's program)


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
    network: str = "none"       # "none", "packages" or "full" (perseus.addons.e2b_code_environment.network)
    lifetime_s: int = 300       # hard upper bound; the session is destroyed after this even if never closed


@dataclass
class Capabilities:
    os: str = "linux"
    controls_network: bool = True       # can actually switch the sandbox's internet off
    network_levels: tuple = ("none", "packages", "full")   # the levels this backend can enforce
    max_lifetime_s: int = 3600
    extras: dict = field(default_factory=dict)


# ---------------------------------------------------------------- protocol
class Session(Protocol):
    id: str                     # the provider's own id for this sandbox (E2B: sandbox_id), for logs and lookups

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

    def describe(self) -> dict:
        """Who and where: provider, API host, SDK version, key fingerprint and source. Never the key."""

    def open(self, spec: SessionSpec) -> Session:
        """A fresh sandbox with an empty job folder and no credentials or host environment."""


def config_secret(config_path: str) -> str:
    """[e2b_code_environment] api_key from Config/tooling.toml. Called by the broker process only, never by Perseus.

    Opens tooling.toml alone, so the broker never reads the Hugging Face token in provider.toml.
    A key still under the old [e2b_code_environment] name works until configuration.sh moves it."""
    return read_section_secret(config_path, SECTION, SECRET_KEY, (PLACEHOLDER,), FORMER_SECTIONS)


# ---------------------------------------------------------------- discovery
# name -> "module:Class". Only names listed in CONFIGURABLE may be chosen in tooling.toml;
# the others exist for tests and development.
BACKENDS = {
    "e2b": "perseus.addons.e2b_code_environment.backends.e2b:E2BBackend",
    "local_unsafe": "perseus.addons.e2b_code_environment.backends.local_unsafe:LocalUnsafeBackend",
    "fake": "perseus.addons.e2b_code_environment.backends.fake:FakeBackend",
}
CONFIGURABLE = ("e2b",)


def make(name: str, settings: dict) -> Backend:
    if name not in BACKENDS:
        raise SandboxError("config", f"unknown sandbox backend {name!r} (known: {', '.join(BACKENDS)})")
    module, _, cls = BACKENDS[name].partition(":")
    return getattr(importlib.import_module(module), cls)(settings)
