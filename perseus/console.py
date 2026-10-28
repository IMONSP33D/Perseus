"""Terminal front end for the engine: the test harness until the dashboard is wired (Phase 1).

Tool calls print as feed lines under each answer. Ctrl-C during an answer is
the Stop button (the partial answer is kept); Ctrl-C at the prompt exits.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .engine import Engine
from .feed import ToolEvent

DIM, RESET, RED, YELLOW, GREEN = "\033[2m", "\033[0m", "\033[31m", "\033[33m", "\033[32m"
MARK = {"ok": f"{GREEN}✓{RESET}", "cached": f"{GREEN}↺{RESET}", "blocked": f"{RED}⊘{RESET}",
        "refused": f"{RED}✗{RESET}", "error": f"{YELLOW}!{RESET}", "stopped": f"{YELLOW}■{RESET}"}

HELP = """Commands:
  /depth quick|standard|deep   research depth for the next answers
  /web off|on                  kill switch for web access
  /feed                        every tool call this session
  /export                      save the conversation + tool feed to logs/
  /prompt                      the raw prompt for the next request, after the chat template
  /reset                       start a new conversation
  /exit                        stop Perseus"""


def feed_line(ev: ToolEvent) -> str:
    bits = [f"  {MARK.get(ev.status, '·')} {ev.tool}", ev.summary[:90]]
    if ev.lookup:
        bits.append(f"{DIM}[{ev.lookup}]{RESET}")
    if ev.status in ("blocked", "refused", "error"):
        bits.append(f"{DIM}— {ev.status}: {ev.reason}{RESET}")
    elif ev.http_status:
        bits.append(f"{DIM}{ev.http_status} · {(ev.bytes or 0) / 1024:.0f} KB · {(ev.duration_ms or 0) / 1000:.1f}s{RESET}")
    if ev.warning:
        bits.append(f"{YELLOW}⚑ {ev.warning}{RESET}")
    return " ".join(bits)


class Console:
    def __init__(self, engine: Engine, depths: dict[str, int], depth: str, export_dir: Path, gateway=None,
                 refresh_tools=None, recorder=None, model_dir: Path | None = None):
        self.model_dir = model_dir
        self.engine, self.depths, self.depth = engine, depths, depth
        self.export_dir, self.gateway, self.refresh_tools, self.recorder = export_dir, gateway, refresh_tools, recorder
        self.history: list[dict] = []
        engine.settings.lookup_limit = depths[depth]
        engine.feed.subscribe(self._on_event)
        self._thinking = False

    def _on_event(self, ev: ToolEvent) -> None:
        if ev.status != "running":
            self._end_thought()
            print(("\n" if self._mid_line else "") + feed_line(ev), flush=True)
            self._mid_line = False

    _mid_line = False

    def _on_text(self, text: str, thought: bool) -> None:
        if thought and not self._thinking:
            self._thinking = True
            print(DIM, end="")
        elif not thought:
            self._end_thought()
        print(text, end="", flush=True)
        self._mid_line = not text.endswith("\n")

    def _end_thought(self):
        if self._thinking:
            self._thinking = False
            print(RESET + "\n", end="", flush=True)

    def ask(self, question: str) -> str:
        """One answer, with Ctrl-C as Stop. Returns the answer text."""
        if self.refresh_tools:
            for change in self.refresh_tools():
                print(f"  {DIM}tools: {change}{RESET}")
        box: dict = {}
        t = threading.Thread(target=lambda: box.update(r=self.engine.run_turn(self.history, question, self._on_text)),
                             daemon=True)
        t0 = time.monotonic()
        t.start()
        while t.is_alive():
            try:
                t.join(0.2)
            except KeyboardInterrupt:
                print(f"\n  {YELLOW}stopping after the current step…{RESET}", flush=True)
                self.engine.stop()
        self._end_thought()
        r = box["r"]
        self.history += r.messages
        if self.recorder:
            self.recorder.turn(question, r)
        tail = [f"{time.monotonic() - t0:.1f}s"]
        if r.lookups_used:
            tail.append(f"{r.lookups_used} of {self.engine.settings.lookup_limit} lookups")
        if r.stopped:
            tail.append("stopped")
        print(f"\n{DIM}  ({', '.join(tail)}){RESET}")
        if r.error:
            print(f"  {RED}✗ {r.error}{RESET}")
        return r.answer

    def export(self) -> Path:
        from .session import Recorder
        rec = self.recorder or Recorder()
        path = self.export_dir / f"session-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return rec.export(path, self.history, self.engine.feed.export())

    def loop(self) -> None:
        print(f"\nTerminal console · research depth {self.depth} ({self.depths[self.depth]} lookups) · /help\n")
        while True:
            try:
                q = input("you › ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not q:
                continue
            if q.startswith("/"):
                if not self.command(q):
                    return
                continue
            print("model › ", end="", flush=True)
            self.ask(q)

    def command(self, q: str) -> bool:
        cmd, _, arg = q.partition(" ")
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            print(HELP)
        elif cmd == "/depth" and arg in self.depths:
            self.depth = arg
            self.engine.settings.lookup_limit = self.depths[arg]
            print(f"  research depth {arg}: up to {self.depths[arg]} lookups per answer")
        elif cmd == "/web" and arg in ("on", "off") and self.gateway:
            (self.gateway.enable if arg == "on" else self.gateway.disable)()
            print(f"  web access {arg}")
        elif cmd == "/feed":
            for ev in self.engine.feed.events:
                print(feed_line(ev))
        elif cmd == "/export":
            print(f"  saved {self.export()}")
        elif cmd == "/prompt" and self.model_dir:
            from .devtools import render_prompt
            try:
                msgs = [self.engine.system_message(), *self.history]
                text, n = render_prompt(self.model_dir, msgs, self.engine.registry.specs())
                print(f"{DIM}{text}{RESET}\n  ({n:,} tokens of {self.engine.settings.max_context:,})")
            except Exception as e:
                print(f"  couldn't render the prompt: {type(e).__name__}: {e}")
        elif cmd == "/reset":
            self.history.clear()
            print("  new conversation")
        else:
            print(HELP)
        return True
