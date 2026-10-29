"""The tool feed: a live, chronological record of every tool call.

The dashboard's right-hand panel renders this; the conversation export
includes it. Listeners get every new or updated event (the server will
forward them over SSE in Phase 1).
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

STATUSES = ("running", "ok", "error", "blocked", "refused", "cached", "stopped")


@dataclass
class ToolEvent:
    id: int
    turn: int
    tool: str
    summary: str                    # one line: the arguments that matter
    args: Any                       # exactly what the model sent (parsed, or the raw string if it didn't parse)
    status: str = "running"
    started: float = field(default_factory=time.time)
    duration_ms: int | None = None
    reason: str | None = None       # why blocked / refused / failed, in words
    code: str | None = None         # machine code, e.g. "internal_address"
    url: str | None = None
    http_status: int | None = None
    bytes: int | None = None
    result: str | None = None       # the text returned to the model
    warning: str | None = None      # e.g. instruction-like phrases found in a page
    lookup: str | None = None       # "7 of 20"
    outcome: str | None = None      # a tool's own one-word result, e.g. run_code's "program failed · exit 1"

    def to_dict(self) -> dict:
        return asdict(self)


class Feed:
    def __init__(self):
        self.events: list[ToolEvent] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[ToolEvent], None]] = []

    def subscribe(self, fn: Callable[[ToolEvent], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def start(self, turn: int, tool: str, args: Any, summary: str, **fields) -> ToolEvent:
        with self._lock:
            ev = ToolEvent(next(self._ids), turn, tool, summary, args, **fields)
            self.events.append(ev)
        self._emit(ev)
        return ev

    def finish(self, ev: ToolEvent, status: str, **fields) -> ToolEvent:
        assert status in STATUSES, status
        ev.status = status
        ev.duration_ms = int((time.time() - ev.started) * 1000)
        for k, v in fields.items():
            setattr(ev, k, v)
        self._emit(ev)
        return ev

    def _emit(self, ev: ToolEvent) -> None:
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                pass

    def export(self) -> list[dict]:
        with self._lock:
            return [e.to_dict() for e in self.events]

    def clear(self) -> None:
        with self._lock:
            self.events.clear()


def summarize_args(tool: str, args: Any, limit: int = 120) -> str:
    if not isinstance(args, dict):
        return str(args)[:limit]
    for key in ("url", "query", "expression"):
        if key in args:
            return str(args[key])[:limit]
    return json.dumps(args, ensure_ascii=False)[:limit]
