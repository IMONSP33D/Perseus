"""Boot-time records: one JSON line per stage in logs/boot-times.jsonl.

The number tracked on every phase is minutes from instance start to first
token, broken into driver check, env install, download and model load.
preflight.sh writes the same line format from bash.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import LOG_DIR, STATE_DIR

TIMINGS = LOG_DIR / "boot-times.jsonl"


def run_id() -> str:
    path = STATE_DIR / "run_id"
    try:
        return path.read_text().strip()
    except OSError:
        rid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rid)
        return rid


def record(stage: str, seconds: float, **extra) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = {"run": run_id(), "stage": stage, "seconds": round(seconds, 1),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra}
    with TIMINGS.open("a") as f:
        f.write(json.dumps(line) + "\n")


@contextmanager
def stage(name: str, **extra):
    t0 = time.monotonic()
    try:
        yield
    finally:
        record(name, time.monotonic() - t0, **extra)


def instance_age_seconds() -> float | None:
    """Seconds since this instance (VM or container) started.

    /proc/uptime is host uptime inside a container, so measure from PID 1's
    start time instead, which is the container's init or the VM's.
    """
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        fields = Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])  # field 22 overall; 20th after the ")"
        return uptime - start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def report(path: Path = TIMINGS) -> str:
    try:
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except OSError:
        return "no boot timings recorded yet"
    if not rows:
        return "no boot timings recorded yet"
    last = rows[-1]["run"]
    rows = [r for r in rows if r["run"] == last]
    width = max(len(r["stage"]) for r in rows)
    lines = [f"Run {last}"]
    for r in rows:
        lines.append(f"  {r['stage']:<{width}}  {_fmt(r['seconds'])}")
    total = next((r for r in reversed(rows) if r["stage"] == "boot_to_first_token"), None)
    if total:
        lines.append(f"  instance start → first token: {_fmt(total['seconds'])}")
    return "\n".join(lines)


def _fmt(s: float) -> str:
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m{sec:02d}s" if m else f"{s:.1f}s"
