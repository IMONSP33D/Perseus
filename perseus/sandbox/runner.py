"""Build-and-run logic shared by every backend: one fresh sandbox per call.

    write the source -> build (if the language has a build step) -> run -> destroy

Outcomes are reported in a fixed vocabulary, because small models fix code far
better when they can tell "didn't compile" from "crashed" from "too slow":

    ok             ran and exited 0
    compile_error  the build step failed (its output is in `build`)
    build_timeout  the build step ran past build_timeout_s
    runtime_error  ran and exited non-zero (or was killed by a signal)
    timeout        ran past run_timeout_s and was killed

Output is capped per stream, keeping the head and the tail (where errors usually are).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import json

from perseus.sandbox import network
from perseus.sandbox.backends import Backend, SandboxError, SessionSpec
from perseus.sandbox.languages import LANGUAGES

NET_PROBE_SH = "timeout 6 bash -c 'echo > /dev/tcp/1.1.1.1/443' 2>/dev/null && echo CONNECTED || echo BLOCKED"
SIGNALS = {4: "SIGILL", 6: "SIGABRT (abort or failed assertion)", 8: "SIGFPE (arithmetic error)",
           9: "SIGKILL (often out of memory)", 11: "SIGSEGV (segmentation fault)", 15: "SIGTERM"}


@dataclass
class RunSettings:
    network: str = network.DEFAULT          # "none", "packages" or "full"
    build_timeout_s: int = 120
    run_timeout_s: int = 30
    max_output_kb: int = 64
    languages: list[str] = field(default_factory=list)   # empty = every language in LANGUAGES

    @classmethod
    def from_config(cls, cfg: dict) -> "RunSettings":
        level = cfg.get("network") or ("full" if cfg.get("internet") else network.DEFAULT)  # older resolved files
        return cls(network=level if level in network.LEVELS else network.DEFAULT,
                   build_timeout_s=int(cfg.get("build_timeout_s", 120)),
                   run_timeout_s=int(cfg.get("run_timeout_s", 30)),
                   max_output_kb=int(cfg.get("max_output_kb", 64)),
                   languages=list(cfg.get("languages") or []))

    def wanted(self) -> list[str]:
        return [n for n in LANGUAGES if not self.languages or n in self.languages]


def cap(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the first two thirds and the last third of the budget; mark what was cut."""
    raw = text.encode()
    if len(raw) <= max_bytes:
        return text, False
    head, tail = raw[: max_bytes * 2 // 3], raw[-(max_bytes // 3):]
    cut = len(raw) - len(head) - len(tail)
    return (head.decode(errors="ignore") + f"\n… [{cut:,} bytes cut] …\n" + tail.decode(errors="ignore")), True


def _exit_note(code: int | None) -> str | None:
    if code is None:
        return None
    sig = -code if code < 0 else (code - 128 if code > 128 else 0)
    if sig:
        return f"killed by signal {sig}: {SIGNALS.get(sig, 'signal')}"
    return None


def run_program(backend: Backend, language: str, code: str, stdin: str = "",
                settings: RunSettings | None = None) -> dict:
    s = settings or RunSettings()
    level = s.network
    lang = LANGUAGES.get(language)
    if lang is None:
        raise SandboxError("config", f"unknown language {language!r}")
    per_stream = max(1024, s.max_output_kb * 1024 // 2)
    session = backend.open(SessionSpec(network=level, lifetime_s=s.build_timeout_s + s.run_timeout_s + 120))
    out: dict = {"language": language, "status": "ok", "exit_code": None, "build": None, "network": level}
    t0 = time.monotonic()
    try:
        session.write_file(lang.file, code)
        redirect = "< /dev/null"
        if stdin:
            session.write_file("stdin.txt", stdin)
            redirect = "< stdin.txt"

        if lang.build:
            b = session.exec(lang.build, s.build_timeout_s)
            text, cut = cap((b.stdout + ("\n" if b.stdout and b.stderr else "") + b.stderr).strip(), per_stream)
            out["build"] = {"exit_code": b.exit_code, "output": text, "seconds": round(b.duration_s, 2)}
            if cut:
                out["build"]["cut"] = True
            if b.timed_out:
                out.update(status="build_timeout", note=f"the build ran past {s.build_timeout_s}s and was stopped")
                return out
            if b.exit_code != 0:
                out.update(status="compile_error", exit_code=b.exit_code)
                return out

        r = session.exec(f"{lang.run} {redirect}", s.run_timeout_s)
        stdout, cut_o = cap(r.stdout, per_stream)
        stderr, cut_e = cap(r.stderr, per_stream)
        out.update(stdout=stdout, stderr=stderr, exit_code=r.exit_code, run_seconds=round(r.duration_s, 2))
        if cut_o or cut_e:
            out["output_cut"] = True
        if r.timed_out:
            out.update(status="timeout", note=f"the program ran past {s.run_timeout_s}s and was stopped")
        elif r.exit_code != 0:
            out["status"] = "runtime_error"
            note = _exit_note(r.exit_code)
            if note:
                out["note"] = note
        return out
    finally:
        out["seconds"] = round(time.monotonic() - t0, 2)
        session.close()


def _reachable(session, have_python: bool, urls: list[str]) -> dict[str, bool]:
    """Which URLs a program in this session can reach (real HTTPS requests from inside it)."""
    if have_python:
        session.write_file("netprobe.py", network.PROBE_PY)
        r = session.exec(f"python3 netprobe.py '{json.dumps(urls)}'", 60)
        for line in r.stdout.splitlines():
            if line.startswith("PROBE "):
                try:
                    return {u: bool(v) for u, v in json.loads(line[6:]).items()}
                except ValueError:
                    break
        return {u: True for u in urls}       # couldn't tell: assume reachable, so checks fail closed
    r = session.exec(NET_PROBE_SH, 15)       # no Python: one raw check stands in for every URL
    return {u: "CONNECTED" in r.stdout for u in urls}


def probe(backend: Backend, settings: RunSettings | None = None) -> dict:
    """Which toolchains this backend's sandbox has, and whether its network matches the setting.

    network_blocked  arbitrary sites are unreachable at the configured level (None for "full")
    packages_ok      the registries are reachable ("packages" only)
    """
    s = settings or RunSettings()
    t0 = time.monotonic()
    levels = backend.capabilities().network_levels
    if s.network not in levels:
        raise SandboxError("config", f"this sandbox backend can't enforce network = {s.network!r} "
                                     f"(it supports {', '.join(levels)})")
    out: dict = {"network": s.network, "network_blocked": None, "packages_ok": None}
    session = backend.open(SessionSpec(network=s.network, lifetime_s=300))
    available, missing = [], []
    try:
        for name in s.wanted():
            r = session.exec(LANGUAGES[name].check, 30)
            (available if r.exit_code == 0 else missing).append(name)
        py = "python" in available
        urls = list(network.PROBE_ARBITRARY) + ([network.PROBE_PACKAGES] if s.network == "packages" else [])
        if s.network != "full":
            seen = _reachable(session, py, urls)
            out["network_blocked"] = not any(seen[u] for u in network.PROBE_ARBITRARY)
            if s.network == "packages":
                out["packages_ok"] = seen[network.PROBE_PACKAGES]
    finally:
        session.close()
    out.update(languages=available, missing=missing, seconds=round(time.monotonic() - t0, 2))
    return out
