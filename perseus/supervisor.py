"""Process supervision for vLLM and SearXNG, the two processes Perseus doesn't own directly.

  * a crashed process restarts with backoff (5 s, 15 s, 45 s), up to max_restarts per session
  * while restarting, its state is "restarting" (the dashboard shows "Model restarting")
  * an out-of-memory crash is reported as such, with a suggested fix
  * when restarts run out the process is "failed" and the last 40 log lines are kept
  * a spec's `remedy` can change the command before the next attempt, when the crash log says
    what would work (vLLM: "lower max_num_seqs to at most 637")
  * the same error twice in a row from the same command fails at once: retrying can't help
Each process runs in its own process group, so stopping it stops its workers too.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

BACKOFF = (5, 15, 45)
OOM_MARKERS = ("CUDA out of memory", "OutOfMemoryError", "No available memory for the cache blocks",
               "insufficient memory", "larger than the available KV cache", "out of memory")


@dataclass
class ProcessSpec:
    name: str
    cmd: list[str]
    env: dict[str, str]
    log_path: Path
    healthy: Callable[[], bool]
    cwd: Path | None = None
    ready_timeout_s: float = 3600
    max_restarts: int = 3
    explain: Callable[[str], list[str]] | None = None    # log text -> hints
    remedy: Callable[[str, list[str]], tuple[list[str], str] | None] | None = None  # (log, cmd) -> (new cmd, note)


@dataclass
class Managed:
    spec: ProcessSpec
    proc: subprocess.Popen | None = None
    state: str = "stopped"          # starting | up | restarting | failed | stopped
    restarts: int = 0
    last_exit: int | None = None
    reason: str = ""
    hints: list[str] = field(default_factory=list)
    tail: str = ""
    started_at: float = 0.0
    intentional: bool = False       # a restart someone asked for: not a crash
    last_cause: str = ""            # the previous crash's root cause and command, to spot a repeat
    last_cmd: list[str] = field(default_factory=list)
    remedies: list[str] = field(default_factory=list)   # what was changed automatically, in order


class Supervisor:
    def __init__(self, on_state: Callable[[str, Managed], None] | None = None, backoff=BACKOFF, poll_s: float = 1.0):
        self.procs: dict[str, Managed] = {}
        self.on_state = on_state
        self.backoff = backoff
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._spawning = threading.Lock()       # one terminate/spawn at a time, so no process is orphaned
        self._watchers: dict[str, threading.Thread] = {}

    # ---------------------------------------------------------------- lifecycle
    def add(self, spec: ProcessSpec) -> Managed:
        m = Managed(spec)
        self.procs[spec.name] = m
        return m

    def start(self, name: str) -> Managed:
        m = self.procs[name]
        self._spawn(m, "starting")
        t = threading.Thread(target=self._watch, args=(m,), daemon=True, name=f"watch-{name}")
        self._watchers[name] = t
        t.start()
        return m

    def wait_ready(self, name: str, progress: Callable[[float], None] | None = None) -> Managed:
        """Block until healthy. Returns the Managed; its state says up or failed."""
        m = self.procs[name]
        t0 = time.monotonic()
        while True:
            if m.state == "up" or m.state == "failed":
                return m
            if m.state == "starting" and m.proc and m.proc.poll() is None and self._healthy(m):
                self._set(m, "up")
                return m
            if time.monotonic() - t0 > m.spec.ready_timeout_s:
                self._fail(m, f"not healthy after {m.spec.ready_timeout_s:.0f}s")
                return m
            if progress:
                progress(time.monotonic() - t0)
            time.sleep(self.poll_s)

    def restart(self, name: str, cmd: list[str] | None = None, timeout_s: float = 30) -> Managed:
        """Deliberate restart (new settings, or the dashboard's Restart button). Doesn't count as a crash."""
        m = self.procs[name]
        with self._lock:
            m.intentional = True
        try:
            with self._spawning:
                self._set(m, "restarting")
                self._terminate(m, timeout_s)
                if cmd is not None:
                    m.spec.cmd = cmd
                m.reason, m.hints, m.tail = "", [], ""
                m.last_cause, m.last_cmd = None, None
                self._spawn(m, "restarting")
        finally:
            with self._lock:
                m.intentional = False
        if name not in self._watchers or not self._watchers[name].is_alive():
            t = threading.Thread(target=self._watch, args=(m,), daemon=True, name=f"watch-{name}")
            self._watchers[name] = t
            t.start()
        return m

    def stop_all(self, timeout_s: float = 30) -> None:
        self._stop.set()
        for m in self.procs.values():
            self._terminate(m, timeout_s)
            if m.state != "failed":
                self._set(m, "stopped")

    def status(self) -> dict:
        """For /healthz: each process up, restarting or failed."""
        return {n: {"state": m.state, "restarts": m.restarts, **({"reason": m.reason} if m.reason else {})}
                for n, m in self.procs.items()}

    # ---------------------------------------------------------------- internals
    def _spawn(self, m: Managed, state: str) -> None:
        s = m.spec
        s.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = s.log_path.open("a")
        log.write(f"\n$ {' '.join(s.cmd)}\n")
        log.flush()
        m.proc = subprocess.Popen(s.cmd, stdout=log, stderr=subprocess.STDOUT, env=s.env, cwd=s.cwd,
                                  start_new_session=True)
        log.close()  # the child holds its own descriptor
        m.started_at = time.monotonic()
        self._set(m, state)

    def _watch(self, m: Managed) -> None:
        while not self._stop.is_set():
            time.sleep(self.poll_s)
            if m.state in ("failed", "stopped") or m.proc is None:
                return
            proc = m.proc
            code = proc.poll()
            if code is None:
                if m.state == "restarting" and not m.intentional and self._healthy(m):
                    self._set(m, "up")
                    m.last_cause, m.last_cmd = None, None
                continue
            if self._stop.is_set():
                return
            if m.intentional or m.proc is not proc:
                continue  # a restart someone asked for replaced this process
            m.last_exit = code
            attempt = last_run(m.spec.log_path)          # this attempt's whole log, not just the end
            cause = root_cause(attempt)
            m.tail = cause_excerpt(attempt, cause) if cause else tail(m.spec.log_path)
            m.hints = explain_crash(attempt, m.spec)
            m.reason = m.hints[0] if m.hints else (f"exited with code {code}: {cause}" if cause
                                                   else f"exited with code {code}")
            fix = self._remedy(m, attempt)
            repeated = bool(cause) and cause == m.last_cause and m.spec.cmd == m.last_cmd
            m.last_cause, m.last_cmd = cause, list(m.spec.cmd)
            if fix:
                m.spec.cmd, note = fix
                m.remedies.append(note)
                m.reason = f"{m.reason}; retrying with {note}"
            elif repeated:
                self._fail(m, f"{m.reason}; the same error twice in a row, so not retrying")
                return
            if m.restarts >= m.spec.max_restarts:
                self._fail(m, f"{m.reason}; gave up after {m.restarts} restart(s)")
                return
            delay = self.backoff[min(m.restarts, len(self.backoff) - 1)]
            m.restarts += 1
            self._set(m, "restarting")
            if self._stop.wait(delay):
                return
            with self._spawning:
                if m.intentional or m.proc is not proc:
                    continue
                self._spawn(m, "restarting")

    def _remedy(self, m: Managed, log_text: str) -> tuple[list[str], str] | None:
        if not m.spec.remedy:
            return None
        try:
            fix = m.spec.remedy(log_text, list(m.spec.cmd))
        except Exception:
            return None
        return fix if fix and fix[0] != m.spec.cmd else None

    def _healthy(self, m: Managed) -> bool:
        try:
            return bool(m.spec.healthy())
        except Exception:
            return False

    def _fail(self, m: Managed, reason: str) -> None:
        m.reason = reason
        m.tail = m.tail or tail(m.spec.log_path)
        self._terminate(m, 10)
        self._set(m, "failed")

    def _terminate(self, m: Managed, timeout_s: float) -> None:
        p = m.proc
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        except ProcessLookupError:
            pass

    def _set(self, m: Managed, state: str) -> None:
        with self._lock:
            m.state = state
        if self.on_state:
            try:
                self.on_state(m.spec.name, m)
            except Exception:
                pass


def tail(path: Path, n: int = 40) -> str:
    try:
        with path.open(errors="replace") as f:
            return "".join(deque(f, n))
    except OSError:
        return "(no log)"


def last_run(path: Path, max_lines: int = 3000) -> str:
    """The log of the most recent attempt: everything after the last `$ <command>` line _spawn writes."""
    try:
        with path.open(errors="replace") as f:
            lines = list(deque(f, max_lines))
    except OSError:
        return ""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("$ "):
            return "".join(lines[i + 1:])
    return "".join(lines)


# The last line of a Python traceback, behind vLLM's optional prefixes:
#   (EngineCore pid=4127) ERROR 10-28 14:55:07 [core.py:1510] ValueError: …
_EXC = re.compile(r"^(?:\([^)]*\)\s*)?"
                  r"(?:(?:ERROR|WARNING|INFO|CRITICAL)\s+\d\d-\d\d\s+[\d:.]+\s+\[[^\]]*\]\s*)?"
                  r"((?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Exit|Interrupt)\b(?::.*)?)$")
_WRAPPERS = ("Engine core initialization failed", "EngineCore failed to start", "Worker failed",
             "WorkerProc initialization failed")


def root_cause(log_text: str) -> str:
    """The first real exception in a crash log, skipping vLLM's 'see root cause above' wrappers."""
    found = []
    for line in log_text.splitlines():
        m = _EXC.match(line.strip())
        if m:
            found.append(m.group(1).strip())
    for exc in found:
        if not any(w in exc for w in _WRAPPERS):
            return exc[:400]
    return found[0][:400] if found else ""


def cause_excerpt(log_text: str, cause: str, before: int = 14) -> str:
    """The traceback lines leading up to the root cause, for the terminal."""
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if cause[:120] in line:
            return "\n".join(lines[max(0, i - before): i + 1]) + "\n"
    return "\n".join(lines[-before:]) + "\n"


def explain_crash(log_text: str, spec: ProcessSpec) -> list[str]:
    if spec.explain:
        hints = spec.explain(log_text)
        if hints:
            return hints
    if any(mk in log_text for mk in OOM_MARKERS):
        return [f"{spec.name} ran out of GPU memory",
                "lower [model] max_context or use smaller weights, then run bash preflight.sh --check"]
    return []
