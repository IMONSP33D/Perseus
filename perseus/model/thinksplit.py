"""Catch thinking that vLLM's reasoning parser hands over as answer text.

The parser splits <think>...</think> once, at the start of a reply. Thinking still
reaches `content` when:
  - the model opens a second <think> block later in the same reply;
  - the chat template already opened the block, so only a stray </think> arrives;
  - a request turns thinking off (the Close out wrap-up sends enable_thinking: False),
    which makes the Qwen3 / DeepSeek parsers stop splitting, and the template ignores it;
  - no reasoning parser was picked for the model at all.

ThinkSplitter runs over the streamed `content` deltas and sorts them into answer and
thought. Tags inside a ``` code fence are left alone, so an answer can still show them.
"""

from __future__ import annotations

ANSWER, THOUGHT, RETRACT = "answer", "thought", "retract"

OPEN_TAGS = ("<think>", "<thinking>", "<reasoning>", "◁think▷")
CLOSE_TAGS = ("</think>", "</thinking>", "</reasoning>", "◁/think▷")
_ALL = OPEN_TAGS + CLOSE_TAGS
_FENCE = "```"


def _partial_tail(s: str) -> int:
    """Length of the longest suffix of s that could be the start of a tag (held back until more arrives)."""
    best = 0
    for tag in _ALL:
        for n in range(min(len(tag) - 1, len(s)), best, -1):
            if s.endswith(tag[:n]):
                best = n
                break
    return best


class ThinkSplitter:
    """Feed content deltas in order; get back (kind, text) pieces.

    kind is ANSWER, THOUGHT or RETRACT. RETRACT carries text that was already given out
    as ANSWER in this reply and turned out to be thinking (a </think> with no opening tag):
    the caller removes it from the end of the answer and adds it to the thinking.
    """

    def __init__(self) -> None:
        self.inside = False      # within an explicit <think> block
        self.answer = ""         # answer text given out so far in this reply
        self._buf = ""           # held-back text that may be the start of a tag
        self._fences = 0         # ``` seen in the answer so far; odd = inside a code block
        self._trim = False       # drop leading whitespace: a thinking block was just taken out

    def feed(self, text: str) -> list[tuple[str, str]]:
        return self._run(self._buf + text, final=False)

    def flush(self) -> list[tuple[str, str]]:
        return self._run(self._buf, final=True)

    # ----------------------------------------------------------------
    def _emit(self, out: list, kind: str, text: str) -> None:
        if not text:
            return
        if kind == ANSWER:
            tail = self.answer[-2:]          # a fence can straddle two pieces
            self._fences += (tail + text).count(_FENCE) - tail.count(_FENCE)
            self.answer += text
        if out and out[-1][0] == kind and kind != RETRACT:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))

    def _run(self, s: str, final: bool) -> list[tuple[str, str]]:
        self._buf = ""
        out: list[tuple[str, str]] = []
        while s:
            if self._trim:
                s = s.lstrip(" \n")
                if not s:
                    break
                self._trim = False
            in_code = not self.inside and self._fences % 2 == 1
            if in_code:
                # inside a code block in the answer: only look for the closing fence
                i = s.find(_FENCE)
                if i < 0:
                    keep = 0 if final else next((n for n in (2, 1) if s.endswith(_FENCE[:n])), 0)
                    self._emit(out, ANSWER, s[:len(s) - keep])
                    self._buf = s[len(s) - keep:]
                    return out
                self._emit(out, ANSWER, s[:i + 3])
                s = s[i + 3:]
                continue

            tags = CLOSE_TAGS if self.inside else OPEN_TAGS + CLOSE_TAGS
            hits = [(s.find(t), t) for t in tags if t in s]
            fence = -1 if self.inside else s.find(_FENCE)
            if hits:
                i, tag = min(hits)
                if 0 <= fence < i:          # a code block opens before the tag: the tag is code
                    self._emit(out, ANSWER, s[:fence + 3])
                    s = s[fence + 3:]
                    continue
                before, s = s[:i], s[i + len(tag):]
                if self.inside:
                    self._emit(out, THOUGHT, before)
                    self.inside = False
                elif tag in OPEN_TAGS:
                    self._emit(out, ANSWER, before)
                    self.inside = True
                else:
                    # a closing tag with no opening one: this reply's answer so far was thinking
                    moved = self.answer
                    if moved:
                        out.append((RETRACT, moved))
                        self.answer, self._fences = "", 0
                    self._emit(out, THOUGHT, before)
                if not self.inside:
                    # no blank lines or doubled spaces where the block was, even if they come in the next chunk
                    self._trim = not self.answer or self.answer[-1].isspace()
                continue

            keep = 0 if final else _partial_tail(s)
            body, self._buf = s[:len(s) - keep], s[len(s) - keep:]
            self._emit(out, THOUGHT if self.inside else ANSWER, body)
            return out
        return out
