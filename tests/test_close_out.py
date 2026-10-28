"""Close out and the thinking budget: the model always gets to give an answer."""

import threading
import time
import unittest

from perseus.engine.loop import TurnOptions
from perseus.model.client import Reply
from tests.test_engine import call, engine


class ThinkingLLM:
    """Streams thoughts until should_stop; the wrap-up request (no tools) answers."""

    def __init__(self, first=None, wrap_text="Final: 42.", wrap_slow=False, first_tools=None):
        self.first, self.wrap_text, self.wrap_slow, self.first_tools = first, wrap_text, wrap_slow, first_tools
        self.seen = []

    def chat(self, messages, tools=None, tool_choice="auto", max_tokens=None, on_text=None, should_stop=None, extra=None):
        self.seen.append({"messages": [dict(m) for m in messages], "tools": tools, "extra": extra,
                          "max_tokens": max_tokens})
        if tools is None and "Give your final answer" in (messages[-1].get("content") or ""):
            out = ""
            for word in self.wrap_text.split(" "):
                if should_stop and should_stop():
                    return Reply(content=out, finish_reason="stopped")
                on_text and on_text(word + " ", False)
                out += word + " "
                if self.wrap_slow:
                    time.sleep(0.1)
            return Reply(content=out, finish_reason="stop")
        if self.first_tools and len(self.seen) == 1:
            return Reply(tool_calls=self.first_tools, finish_reason="tool_calls")
        if self.first:
            return self.first
        thought = ""
        for _ in range(2000):
            if should_stop and should_stop():
                return Reply(reasoning=thought, finish_reason="stopped")
            on_text and on_text("hmm, let me think about this. ", True)
            thought += "hmm, let me think about this. "
            time.sleep(0.002)
        return Reply(reasoning=thought, finish_reason="length")


def opts(**kw):
    return TurnOptions(chat_extra={"chat_template_kwargs": {"enable_thinking": True}}, answer_tokens=4096, **kw)


class CloseOut(unittest.TestCase):
    def test_thinking_budget_wraps_up_with_thinking_off(self):
        llm = ThinkingLLM()
        eng, _ = engine(llm)
        phases = []
        r = eng.run_turn([], "what is 6*7?", opts=opts(think_tokens=200), on_phase=phases.append)
        self.assertEqual(r.closed, "budget")
        self.assertEqual(r.answer.strip(), "Final: 42.")
        wrap = llm.seen[-1]
        self.assertIsNone(wrap["tools"])
        self.assertEqual(wrap["extra"]["chat_template_kwargs"]["enable_thinking"], False)
        self.assertIn("hmm, let me think", wrap["messages"][-1]["content"])      # the notes go along
        self.assertEqual(wrap["max_tokens"], 4096)
        self.assertEqual(phases[-1], "closing")
        # the wrap-up prompt is never kept in the history
        self.assertEqual([m["role"] for m in r.messages], ["user", "assistant"])
        self.assertEqual(r.messages[-1]["content"].strip(), "Final: 42.")

    def test_close_out_button_gives_ten_seconds(self):
        llm = ThinkingLLM(wrap_text=" ".join(["word"] * 200), wrap_slow=True)
        eng, _ = engine(llm)
        eng_close = threading.Timer(0.2, lambda: eng.close_out(seconds=0.5))
        eng_close.start()
        t0 = time.monotonic()
        r = eng.run_turn([], "hard question", opts=opts())
        self.assertEqual(r.closed, "user")
        self.assertTrue(r.truncated)                  # the deadline ended it
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(r.answer.startswith("word"))

    def test_out_of_room_while_thinking(self):
        llm = ThinkingLLM(first=Reply(reasoning="long thoughts", finish_reason="length"))
        eng, _ = engine(llm)
        r = eng.run_turn([], "q", opts=opts())
        self.assertEqual(r.closed, "length")
        self.assertEqual(r.answer.strip(), "Final: 42.")

    def test_cut_off_answer_is_flagged(self):
        llm = ThinkingLLM(first=Reply(content="The answer is", finish_reason="length"))
        eng, _ = engine(llm)
        r = eng.run_turn([], "q", opts=opts())
        self.assertIsNone(r.closed)
        self.assertTrue(r.truncated)

    def test_close_during_tools_skips_the_rest(self):
        llm = ThinkingLLM(first_tools=[call("spy", {"n": 1}), call("spy", {"n": 2})])
        eng, ran = engine(llm)
        orig = eng._handle

        def handle(*a, **k):
            out = orig(*a, **k)
            eng.close_out(seconds=5)
            return out
        eng._handle = handle
        r = eng.run_turn([], "q", opts=opts())
        self.assertEqual(ran, [1])
        self.assertEqual(r.closed, "user")
        roles = [m["role"] for m in r.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "tool", "assistant"])   # history stays valid
        self.assertIn("Skipped", r.messages[3]["content"])

    def test_stop_still_wins(self):
        llm = ThinkingLLM()
        eng, _ = engine(llm)
        threading.Timer(0.1, eng.stop).start()
        r = eng.run_turn([], "q", opts=opts(think_tokens=10_000_000))
        self.assertTrue(r.stopped)
        self.assertIsNone(r.closed)
        self.assertEqual(len(llm.seen), 1)


if __name__ == "__main__":
    unittest.main()
