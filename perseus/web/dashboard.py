"""Dashboard state: chats, settings and turns, independent of HTTP.

One user, in memory, gone at teardown (export from the UI to keep anything).
Each chat has its own Engine and tool feed; the model, registry, gateway and
search client are shared.
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from perseus import __version__
from perseus.engine.loop import Engine, EngineSettings, TurnOptions
from perseus.engine.feed import Feed, ToolEvent
from perseus.engine.session import Recorder

PERSONAS = {
    "engineer": "You are a senior software engineer. Give precise, working answers, show code when it helps, "
                "and state assumptions and edge cases.",
    "planner": "You are a careful planner. Break goals into concrete, ordered steps with owners, dependencies "
               "and risks, and keep plans short and actionable.",
    "researcher": "You are a meticulous researcher. Check claims against sources, cite the URLs you used, keep "
                  "facts apart from inference, and say what you couldn't verify.",
}
CREATIVITY = {"precise": 0.2, "balanced": 0.7, "creative": 1.0}
LENGTH = {"short": 1024, "medium": 4096, "long": 16384}
THINKING = ("off", "low", "medium", "high")
# Thinking budget per answer, on top of Length. Past it Perseus wraps up (see Engine.run_turn), so
# the levels mean something even for models whose template has no effort setting (Qwen3).
THINK_BUDGET = {"off": 0, "low": 2048, "medium": 8192, "high": 32768}
CONTEXT_STEPS = (("8k", 8192), ("32k", 32768), ("128k", 131072))
MAX_PROMPT_CHARS = 8000
MAX_MESSAGE_CHARS = 400_000


class BadRequest(Exception):
    pass


class Busy(Exception):
    pass


@dataclass
class ChatRecorder(Recorder):
    """Recorder that also keeps, per answer, the last request sent and every raw reply (for the raw view)."""
    raw: dict = field(default_factory=dict)   # turn -> {"messages", "tools", "outputs": [...]}

    def llm(self, turn, step, messages, tools, reply):
        super().llm(turn, step, messages, tools, reply)
        r = self.raw.setdefault(turn, {"outputs": []})
        r["messages"], r["tools"] = messages, tools or []
        r["outputs"].append({"step": step, "content": reply.content, "reasoning": reply.reasoning,
                             "tool_calls": reply.tool_calls, "finish_reason": reply.finish_reason,
                             "usage": reply.usage})


@dataclass
class Chat:
    id: str
    title: str
    engine: Engine
    recorder: ChatRecorder
    created: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)    # what the model sees next turn
    messages: list[dict] = field(default_factory=list)   # what the dashboard shows
    busy: bool = False
    turn_of: dict = field(default_factory=dict)          # assistant message id -> engine turn number

    @property
    def feed(self) -> Feed:
        return self.engine.feed

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "created": self.created, "busy": self.busy,
                "messages": len(self.messages)}

    def full(self) -> dict:
        return {**self.summary(), "messages": self.messages, "feed": [event_dict(e) for e in self.feed.events]}


def event_dict(ev: ToolEvent) -> dict:
    d = ev.to_dict()
    if isinstance(d.get("result"), str) and len(d["result"]) > 20_000:
        d["result"] = d["result"][:20_000] + " …"
    return d


@dataclass
class Settings:
    internet: bool = True
    thinking: str = "medium"
    persona: str = "researcher"
    custom_prompt: str = ""
    depth: str = "standard"
    engines: dict = field(default_factory=dict)
    creativity: str = "balanced"
    length: str = "medium"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Dashboard:
    def __init__(self, *, info: dict, llm: Any, registry: Any, ctx: Any, depths: dict[str, int],
                 engine_settings: dict, engine_list: list[str], status: Callable[[], dict],
                 restart_model: Callable[[int], None] | None = None, raw_prompt: Callable | None = None,
                 refresh_tools: Callable[[], list[str]] | None = None, gateway: Any = None):
        self.info = info                      # model, hardware, context, capacity, thinks, ...
        self.llm, self.registry, self.ctx = llm, registry, ctx
        self.depths = depths
        self.engine_settings = engine_settings
        self.status = status
        self.restart_model = restart_model
        self.raw_prompt = raw_prompt
        self.refresh_tools = refresh_tools
        self.gateway = gateway
        self.settings = Settings(depth="standard" if "standard" in depths else next(iter(depths)),
                                 engines={e: True for e in engine_list})
        self.chats: dict[str, Chat] = {}
        self.order: list[str] = []            # newest first
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self.restarting = False

    # ------------------------------------------------------------------ chats
    def new_chat(self) -> Chat:
        with self._lock:
            cid = f"c{next(self._ids)}"
            rec = ChatRecorder(meta={"model": self.info.get("model")})
            eng = Engine(self.llm, self.registry, self.ctx, Feed(), EngineSettings(**self.engine_settings),
                         recorder=rec)
            chat = Chat(cid, "New chat", eng, rec)
            self.chats[cid] = chat
            self.order.insert(0, cid)
            return chat

    def get(self, cid: str) -> Chat:
        chat = self.chats.get(cid)
        if chat is None:
            raise KeyError(cid)
        return chat

    def list_chats(self) -> list[dict]:
        with self._lock:
            return [self.chats[c].summary() for c in self.order]

    def delete(self, cid: str) -> None:
        with self._lock:
            chat = self.get(cid)
            chat.engine.stop()
            del self.chats[cid]
            self.order.remove(cid)

    def delete_all(self) -> None:
        with self._lock:
            for c in list(self.chats.values()):
                c.engine.stop()
            self.chats.clear()
            self.order.clear()

    # ------------------------------------------------------------------ settings
    def update_settings(self, patch: dict) -> dict:
        s = self.settings
        for key, value in patch.items():
            if key == "internet":
                s.internet = _bool(value, key)
            elif key == "thinking":
                s.thinking = _choice(value, THINKING, key)
            elif key == "persona":
                s.persona = _choice(value, (*PERSONAS, "custom"), key)
            elif key == "custom_prompt":
                if not isinstance(value, str) or len(value) > MAX_PROMPT_CHARS:
                    raise BadRequest(f"custom_prompt must be text up to {MAX_PROMPT_CHARS} characters")
                s.custom_prompt = value
            elif key == "depth":
                s.depth = _choice(value, tuple(self.depths), key)
            elif key == "engines":
                if not isinstance(value, dict) or any(k not in s.engines or not isinstance(v, bool)
                                                      for k, v in value.items()):
                    raise BadRequest(f"engines must map configured engines ({', '.join(s.engines)}) to true/false")
                s.engines.update(value)
            elif key == "creativity":
                s.creativity = _choice(value, tuple(CREATIVITY), key)
            elif key == "length":
                s.length = _choice(value, tuple(LENGTH), key)
            else:
                raise BadRequest(f"unknown setting {key!r}")
        if self.gateway is not None:
            (self.gateway.enable if s.internet else self.gateway.disable)()
        return s.to_dict()

    def context_options(self) -> list[dict]:
        cap = int(self.info["capacity"])
        if cap >= 2048:
            cap -= cap % 1024          # the same rounding preflight applies to its own choice
        cur = int(self.info["context"])
        opts = [{"id": i, "label": i.upper(), "tokens": t, "available": t <= cap} for i, t in CONTEXT_STEPS]
        opts.append({"id": "max", "label": "Max", "tokens": cap, "available": True})
        for o in opts:
            o["current"] = o["tokens"] == cur
        if not any(o["current"] for o in opts):
            # preflight's pick (or a hand-set max_context) isn't one of the steps: show it as its own option
            label = f"{cur // 1024}K" if cur >= 1024 else str(cur)
            opts.insert(len(opts) - 1, {"id": "current", "label": label, "tokens": cur, "available": True,
                                         "current": True})
        return opts

    def apply_context(self, tokens: int) -> None:
        cap = int(self.info["capacity"])
        if not isinstance(tokens, int) or not 1024 <= tokens <= cap:
            raise BadRequest(f"context must be between 1024 and {cap:,} tokens on this box")
        if self.restart_model is None:
            raise BadRequest("model restart isn't available")
        if any(c.busy for c in self.chats.values()):
            raise Busy("an answer is still running; stop it or wait, then restart")
        self.restarting = True
        try:
            self.info.pop("last_restart_error", None)
            self.restart_model(tokens)
            self.info["context"] = tokens
            self.engine_settings["max_context"] = tokens
            for c in self.chats.values():
                c.engine.settings.max_context = tokens
        finally:
            self.restarting = False

    def state(self) -> dict:
        tool_names = self.registry.names()
        return {"perseus": __version__, "info": self.info, "settings": self.settings.to_dict(),
                "depths": self.depths, "context_options": self.context_options(),
                "creativity": list(CREATIVITY), "length": list(LENGTH), "thinking": list(THINKING),
                "personas": [*PERSONAS, "custom"], "tools": tool_names, "status": self.status(),
                "restarting": self.restarting, "chats": self.list_chats()}

    # ------------------------------------------------------------------ turns
    def turn_options(self) -> TurnOptions:
        s = self.settings
        persona = s.custom_prompt.strip() if s.persona == "custom" else PERSONAS.get(s.persona)
        extra: dict = {}
        if self.info.get("thinks"):
            # VERIFY on the pinned vLLM: Qwen3-style templates read enable_thinking; gpt-oss reads reasoning_effort
            if s.thinking == "off":
                extra["chat_template_kwargs"] = {"enable_thinking": False}
            else:
                extra["chat_template_kwargs"] = {"enable_thinking": True}
                if self.info.get("reasoning_effort"):
                    extra["reasoning_effort"] = s.thinking
        cap = max(512, int(self.info["context"]) // 2)
        answer = min(LENGTH[s.length], cap)
        think = THINK_BUDGET[s.thinking] if self.info.get("thinks") else 0
        return TurnOptions(allow_web=s.internet, lookup_limit=self.depths[s.depth], persona=persona or None,
                           temperature=CREATIVITY[s.creativity], max_tokens=min(answer + think, cap),
                           think_tokens=think or None, answer_tokens=answer,
                           chat_extra=extra, engines=[e for e, on in s.engines.items() if on] or None)

    def start_turn(self, cid: str, text: str) -> "queue.Queue[dict]":
        """Run one answer in a worker thread. Returns a queue of stream events, ending with done or error."""
        if not isinstance(text, str) or not text.strip():
            raise BadRequest("message is empty")
        if len(text) > MAX_MESSAGE_CHARS:
            raise BadRequest(f"message is longer than {MAX_MESSAGE_CHARS:,} characters")
        if self.restarting:
            raise Busy("the model is restarting")
        chat = self.get(cid)
        with self._lock:
            if chat.busy:
                raise Busy("this chat is already answering")
            chat.busy = True
        if self.refresh_tools:
            self.refresh_tools()
        opts = self.turn_options()
        q: queue.Queue = queue.Queue()
        now = time.time()
        user_msg = {"id": f"m{now:.6f}u", "role": "user", "text": text, "at": now}
        msg = {"id": f"m{now:.6f}", "role": "assistant", "content": "", "reasoning": "", "streaming": True,
               "started": now, "think_secs": None, "tool_ids": [], "lookups": 0,
               "limit": opts.lookup_limit, "usage": {}, "stopped": False, "error": None,
               "phase": "thinking", "closed": None, "truncated": False}
        if not chat.messages:
            chat.title = " ".join(text.split())[:48] or "New chat"
        chat.messages += [user_msg, msg]
        q.put({"type": "start", "chat": chat.summary(), "user": user_msg, "message": msg})

        def on_phase(phase: str):
            if msg["phase"] != "closing" or phase == "closing":   # once wrapping up, it stays that way
                msg["phase"] = phase
                q.put({"type": "phase", "phase": msg["phase"]})

        def on_text(piece: str, thought: bool):
            if msg["phase"] != "closing":
                msg["phase"] = "thinking" if thought else "answering"
            if thought:
                msg["reasoning"] += piece
            else:
                if not msg["content"] and msg["reasoning"] and msg["think_secs"] is None:
                    msg["think_secs"] = round(time.time() - msg["started"], 1)
                msg["content"] += piece
            q.put({"type": "text", "text": piece, "thought": thought})

        def on_event(ev: ToolEvent):
            if ev.status != "running" and ev.id not in msg["tool_ids"]:
                msg["tool_ids"].append(ev.id)
            q.put({"type": "tool", "event": event_dict(ev)})

        unsubscribe = chat.feed.subscribe(on_event)

        def work():
            try:
                res = chat.engine.run_turn(chat.history, text, on_text, opts, on_phase=on_phase)
                chat.history += res.messages
                chat.turn_of[msg["id"]] = chat.engine._turn
                chat.recorder.turn(text, res)
                msg.update(content=res.answer if not msg["content"] else msg["content"],
                           streaming=False, lookups=res.lookups_used, usage=res.usage, stopped=res.stopped,
                           error=res.error, ttft=res.first_token_s, phase=None, closed=res.closed,
                           truncated=res.truncated,
                           duration=round(time.time() - msg["started"], 1))
                if msg["reasoning"] and msg["think_secs"] is None:
                    msg["think_secs"] = msg["duration"]
                q.put({"type": "done", "message": msg, "context_used": context_used(res.usage)})
            except Exception as e:  # never leave the client hanging
                msg.update(streaming=False, error=f"{type(e).__name__}: {e}")
                q.put({"type": "done", "message": msg})
            finally:
                unsubscribe()
                chat.busy = False
                q.put(None)

        threading.Thread(target=work, daemon=True, name=f"turn-{cid}").start()
        return q

    def stop(self, cid: str) -> None:
        self.get(cid).engine.stop()

    def close_out(self, cid: str, seconds: float = 10.0) -> float:
        chat = self.get(cid)
        if not chat.busy:
            raise BadRequest("nothing to close out: this chat isn't answering")
        chat.engine.close_out(seconds)
        return seconds

    # ------------------------------------------------------------------ raw view, export
    def raw(self, cid: str, mid: str) -> dict:
        chat = self.get(cid)
        turn = chat.turn_of.get(mid)
        rec = chat.recorder.raw.get(turn) if turn else None
        if not rec:
            raise KeyError(mid)
        messages, tools = rec.get("messages", []), rec.get("tools", [])
        prompt, tokens, note = None, None, None
        if self.raw_prompt:
            try:
                prompt, tokens = self.raw_prompt(messages, tools)
            except Exception as e:
                note = f"couldn't apply the chat template here ({type(e).__name__}); showing the request as JSON"
        if prompt is None:
            prompt = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, indent=1)
        usage = rec["outputs"][-1].get("usage") if rec["outputs"] else {}
        return {"prompt": prompt, "prompt_tokens": tokens or (usage or {}).get("prompt_tokens"),
                "completion_tokens": sum((o.get("usage") or {}).get("completion_tokens", 0) for o in rec["outputs"]),
                "outputs": rec["outputs"], "steps": len(rec["outputs"]), "note": note}

    def export_all(self) -> dict:
        with self._lock:
            return {"perseus": __version__,
                    "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "model": self.info.get("model"), "hardware": self.info.get("hardware"),
                    "settings": self.settings.to_dict(),
                    "chats": [{"id": c.id, "title": c.title, "created": c.created, "messages": c.messages,
                               "model_messages": c.history, "tool_feed": c.feed.export(),
                               "turns": c.recorder.turns, "model_exchanges": c.recorder.exchanges}
                              for c in (self.chats[i] for i in self.order)]}


def context_used(usage: dict) -> int | None:
    if not usage:
        return None
    return int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))


def _bool(v, key):
    if not isinstance(v, bool):
        raise BadRequest(f"{key} must be true or false")
    return v


def _choice(v, options, key):
    if v not in options:
        raise BadRequest(f"{key} must be one of {', '.join(map(str, options))}")
    return v
