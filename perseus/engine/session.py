"""Session recording, export and replay (the developer harness).

Recorder keeps every model exchange and tool result of a session in memory;
export() writes one JSON file the dashboard offers as "Export conversation".
replay() re-runs a recorded session through the current engine with no GPU
and no network: the model's replies come from the recording, and web tools
return what they returned then. Use it to check that a change to validation,
tools or limits behaves as expected against real past sessions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from perseus import __version__
from perseus.model.client import Reply


@dataclass
class Recorder:
    meta: dict = field(default_factory=dict)
    exchanges: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)

    def llm(self, turn: int, step: int, messages: list[dict], tools, reply: Reply) -> None:
        self.exchanges.append({"turn": turn, "step": step, "n_messages": len(messages),
                               "tools_offered": [t["function"]["name"] for t in tools or []],
                               "reply": {"content": reply.content, "reasoning": reply.reasoning,
                                         "tool_calls": reply.tool_calls, "finish_reason": reply.finish_reason,
                                         "first_token_s": reply.first_token_s, "usage": reply.usage}})

    def turn(self, user: str, result) -> None:
        self.turns.append({"user": user, "answer": result.answer, "stopped": result.stopped,
                           "error": result.error, "lookups": result.lookups_used, "steps": result.steps})

    def export(self, path: Path, history: list[dict], feed: list[dict]) -> Path:
        doc = {"perseus": __version__, "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "meta": self.meta, "turns": self.turns, "messages": history, "tool_feed": feed,
               "model_exchanges": self.exchanges}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        return path


class ReplayLLM:
    """Answers with the recorded replies, in order."""

    def __init__(self, exchanges: list[dict]):
        self._replies = [e["reply"] for e in exchanges]
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice="auto", max_tokens=None, on_text=None, should_stop=None,
             extra=None) -> Reply:
        if self.calls >= len(self._replies):
            return Reply(content="(replay: no more recorded replies)", finish_reason="stop")
        r = self._replies[self.calls]
        self.calls += 1
        if on_text and r.get("content"):
            on_text(r["content"], False)
        return Reply(content=r.get("content") or "", reasoning=r.get("reasoning") or "",
                     tool_calls=json.loads(json.dumps(r.get("tool_calls") or [])),
                     finish_reason=r.get("finish_reason"))


def recorded_tool_results(feed: list[dict]) -> dict[str, tuple[str, str]]:
    """(tool, args) -> (the text the model was told, the status), for replaying web tools offline."""
    out = {}
    for ev in feed:
        if ev.get("status") in ("ok", "cached", "blocked", "error") and ev.get("result") is not None:
            out[ev["tool"] + json.dumps(ev.get("args"), sort_keys=True, ensure_ascii=False)] = (ev["result"], ev["status"])
    return out


def replay(export_path: Path, make_engine) -> list[dict]:
    """Re-run every recorded turn. `make_engine(llm, tool_override)` builds an Engine.

    Returns one row per turn comparing the recorded and replayed outcome.
    """
    doc = json.loads(export_path.read_text())
    recorded = recorded_tool_results(doc.get("tool_feed", []))

    def override(name: str, args: dict):
        key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
        if key in recorded:
            text, status = recorded[key]
            rec = _Recorded(text)
            rec.status = "ok" if status == "cached" else status
            return rec
        return None  # not in the recording: the engine runs the real tool (offline tools only, see below)

    llm = ReplayLLM(doc.get("model_exchanges", []))
    engine = make_engine(llm, override)
    history: list[dict] = []
    rows = []
    for t in doc.get("turns", []):
        res = engine.run_turn(history, t["user"])
        history += res.messages
        rows.append({"user": t["user"][:80], "recorded_answer": t["answer"][:120], "replayed_answer": res.answer[:120],
                     "same": res.answer == t["answer"], "refused": sum(e.status == "refused" for e in res.events),
                     "blocked": sum(e.status == "blocked" for e in res.events)})
    return rows


class _Recorded(str):
    """Marks a recorded result so the engine passes it through verbatim."""
    verbatim = True
