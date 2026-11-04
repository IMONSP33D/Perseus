"""The tool feed: a live, chronological record of every tool call.

The dashboard's right-hand panel renders this; the conversation export
includes it. Listeners get every new or updated event (the server will
forward them over SSE in Phase 1).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import time
from dataclasses import asdict, dataclass, field, replace
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
    file: dict | None = None        # a file the tool handed the user (see clean_file); shown in the chat, never sent to the model

    def to_dict(self) -> dict:
        """JSON-ready. A binary file's bytes stay out (the download serves them); everything else is kept."""
        if self.file is None or "data" not in self.file:
            return asdict(self)
        d = asdict(replace(self, file=None))
        d["file"] = {k: v for k, v in self.file.items() if k != "data"}
        return d


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


FILE_MAX_CHARS = 500_000
FILE_MAX_BYTES = 64 * 1024 * 1024


def clean_file(f: Any) -> dict | None:
    """A file a tool handed the user (output_file), sizes capped. Either text or exact bytes:

        name         flat file name, e.g. "notes.md" or "app.exe"
        content      the complete text of a text file
        data         the exact bytes of a binary file (never JSON-encoded; served by the download)
        mime         its MIME type, used for the download
        language     a label for the preview, e.g. "python" (text files)
        description  an optional one-line note for the file card
        source       where the bytes came from, e.g. "windows:publish/app.exe"
        executable   true for files that run code when opened (.exe, .dll, .ps1, ...): the card warns
    Every file gets bytes and sha256; text files get lines, binary files binary: true.
    """
    if not isinstance(f, dict) or not isinstance(f.get("name"), str):
        return None
    name = f["name"].replace("/", "_").replace("\\", "_").strip()[:120]
    if not name:
        return None
    if isinstance(f.get("data"), (bytes, bytearray)):
        data = bytes(f["data"])
        if len(data) > FILE_MAX_BYTES:
            return None
        out = {"name": name, "data": data, "binary": True, "bytes": len(data),
               "sha256": hashlib.sha256(data).hexdigest()}
    elif isinstance(f.get("content"), str):
        content = f["content"][:FILE_MAX_CHARS]
        raw = content.encode("utf-8")
        out = {"name": name, "content": content, "bytes": len(raw), "lines": _lines(content),
               "sha256": hashlib.sha256(raw).hexdigest()}
    else:
        return None
    for key, cap in (("mime", 80), ("language", 40), ("description", 200), ("source", 620)):
        if isinstance(f.get(key), str) and f[key].strip():
            out[key] = f[key].strip()[:cap]
    if f.get("executable") is True:
        out["executable"] = True
    out.setdefault("mime", "application/octet-stream" if out.get("binary") else "text/plain")
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
    for key in ("url", "query", "expression", "filename"):
        if key in args:
            return str(args[key])[:limit]
    return json.dumps(args, ensure_ascii=False)[:limit]
