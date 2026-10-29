import unittest

from perseus.model.thinksplit import ANSWER, THOUGHT, ThinkSplitter


def run(chunks):
    """Feed chunks; return (answer, thinking) as a caller following the RETRACT rule would see them."""
    sp = ThinkSplitter()
    answer, thinking = "", ""
    for kind, text in [p for c in chunks for p in sp.feed(c)] + sp.flush():
        if kind == ANSWER:
            answer += text
        elif kind == THOUGHT:
            thinking += text
        else:
            assert answer.endswith(text), (answer, text)
            answer = answer[:len(answer) - len(text)]
            thinking += text
    return answer, thinking


def every_split(s):
    """The string cut into one-char chunks, so every tag lands across chunk boundaries."""
    return list(s)


class ThinkSplitTest(unittest.TestCase):
    def test_plain_answer_untouched(self):
        self.assertEqual(run(["Hello ", "world. 3 < 4 and <b>bold</b>."]), ("Hello world. 3 < 4 and <b>bold</b>.", ""))

    def test_explicit_block(self):
        self.assertEqual(run(["<think>plan it</think>\n\nThe answer."]), ("The answer.", "plan it"))

    def test_second_block_mid_answer(self):
        a, t = run(["Part one. ", "<think>hmm, check again</think>", "Part two."])
        self.assertEqual((a, t), ("Part one. Part two.", "hmm, check again"))

    def test_stray_close_moves_answer_to_thinking(self):
        a, t = run(["The user wants X. Let me ", "work it out.", "</think>\n\n", "Here is X."])
        self.assertEqual((a, t), ("Here is X.", "The user wants X. Let me work it out."))

    def test_tags_split_across_chunks(self):
        s = "<think>a</think>B<thinking>c</thinking>D"
        self.assertEqual(run(every_split(s)), ("BD", "ac"))
        self.assertEqual(run(every_split("x y</think>z")), ("z", "x y"))

    def test_other_tag_styles(self):
        self.assertEqual(run(["◁think▷k◁/think▷ok"]), ("ok", "k"))
        self.assertEqual(run(["<reasoning>r</reasoning>ok"]), ("ok", "r"))

    def test_tags_inside_code_fence_are_kept(self):
        s = "Use it like:\n```xml\n<think>example</think>\n```\nDone."
        self.assertEqual(run([s]), (s, ""))
        self.assertEqual(run(every_split(s)), (s, ""))

    def test_unclosed_block_at_end_is_thinking(self):
        self.assertEqual(run(["Answer. <think>still going"]), ("Answer. ", "still going"))

    def test_partial_tag_prefix_flushed_at_end(self):
        self.assertEqual(run(["a <thi"]), ("a <thi", ""))


if __name__ == "__main__":
    unittest.main()
