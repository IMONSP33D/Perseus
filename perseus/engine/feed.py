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
    feed: dict | None = None        # a tool's own card view (see clean_feed); shown in the feed, never sent to the model

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


FEED_TONES = ("", "warn", "bad", "run")
FEED_SECTION_CHARS = 20_000


def clean_feed(view: Any) -> dict | None:
    """A tool's display-only card view, made safe to store and show (all plain text, sizes capped).

        pill      badge text, e.g. "Program failed" (replaces "OK" when the call itself succeeded)
        tone      "", "warn", "bad" or "run": the badge colour
        summary   one line under the tool name, e.g. "Python · 42 lines"
        headline  the one line that matters, e.g. the error a program died with
        detail    small text after the badge, e.g. "exit 1 · 0.7 s"
        sections  [{"title", "text"}]: readable blocks in Details (code, stdin, stdout, stderr)
    """
    if not isinstance(view, dict):
        return None
    out: dict = {}
    for key, cap in (("pill", 30), ("summary", 160), ("headline", 240), ("detail", 120)):
        if isinstance(view.get(key), str) and view[key].strip():
            out[key] = view[key].strip()[:cap]
    tone = view.get("tone", "")
    out["tone"] = tone if tone in FEED_TONES else ""
    sections = []
    for sec in view.get("sections") or []:
        if isinstance(sec, dict) and isinstance(sec.get("text"), str) and isinstance(sec.get("title"), str):
            text = sec["text"]
            if len(text) > FEED_SECTION_CHARS:
                text = text[:FEED_SECTION_CHARS] + f"\n… ({len(sec['text']) - FEED_SECTION_CHARS:,} more characters)"
            sections.append({"title": sec["title"][:80], "text": text})
    if sections:
        out["sections"] = sections[:8]
    return out


def _lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0


def summarize_args(tool: str, args: Any, limit: int = 120) -> str:
    if not isinstance(args, dict):
        return str(args)[:limit]
    if isinstance(args.get("code"), str):               # run_code and anything like it: key facts, not the source
        lang = str(args.get("language") or "code")
        n = _lines(args["code"])
        bits = [lang, f"{n} line{'' if n == 1 else 's'}"]
        if args.get("stdin"):
            bits.append(f"stdin {_lines(args['stdin'])} line{'' if _lines(args['stdin']) == 1 else 's'}")
        return " · ".join(bits)[:limit]
    for key in ("url", "query", "expression"):
        if key in args:
            return str(args[key])[:limit]
    return json.dumps(args, ensure_ascii=False)[:limit]
