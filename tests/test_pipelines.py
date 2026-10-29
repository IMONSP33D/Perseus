import tempfile
import unittest
from pathlib import Path

from perseus.pipelines import (
    PipelineError,
    describe,
    handoff_packet,
    load_all,
    parse,
    parse_handoff,
    stage_prompt,
)


def doc(*stages, **head):
    return {"pipeline": {"name": "P", "summary": "s", **head}, "stage": list(stages)}


def work(sid, inputs=("request",), outputs=("out",), **k):
    return {"id": sid, "role": sid.title(), "goal": "g", "prompt": "p", "inputs": list(inputs),
            "outputs": list(outputs), **k}


def review(sid, target, inputs, **k):
    return {"id": sid, "role": "Reviewer", "goal": "g", "prompt": "p", "kind": "review", "inputs": list(inputs),
            "on_revise": target, "done_when": ["it works"], **k}


class Format(unittest.TestCase):
    def test_shipped_pipelines_load(self):
        pipelines, errors = load_all()
        self.assertEqual(errors, {})
        self.assertGreaterEqual(len(pipelines), 2)
        for pl in pipelines:
            self.assertTrue(any(s.kind == "review" for s in pl.stages), pl.id)

    def test_valid_chain(self):
        pl = parse(doc(work("plan", outputs=["plan"]), work("build", ["request", "plan"], ["code"]),
                       review("check", "build", ["plan", "code"])), "x")
        self.assertEqual([s.id for s in pl.stages], ["plan", "build", "check"])
        self.assertEqual(pl.stage("check").produces, ["verdict", "feedback"])
        self.assertEqual(pl.producer("code").id, "build")

    def test_prompt_is_reflowed(self):
        pl = parse(doc(work("a", prompt="One line\nwrapped here.\n\nNext para:\n- item one\n- item two")), "x")
        self.assertEqual(pl.stages[0].prompt, "One line wrapped here.\n\nNext para:\n- item one\n- item two")

    def test_problems_are_named(self):
        cases = [
            (doc(work("a", inputs=["plan"])), "isn't produced by an earlier stage"),
            (doc(work("a"), work("a", outputs=["o2"])), "id used twice"),
            (doc(work("a", outputs=[])), "needs at least one output"),
            (doc(work("a"), review("r", "zzz", ["out"])), "on_revise must name an earlier work stage"),
            (doc(work("a"), review("r", "a", ["out"], max_rounds=9)), "max_rounds"),
            (doc(work("a"), review("r", "a", ["out"], done_when=[])), "done_when"),
            (doc(work("a", thinking="huge")), "thinking must be"),
            (doc(work("a", colour="red")), "unknown key 'colour'"),
            (doc(work("a", outputs=["feedback"])), "reserved"),
            (doc(work("Bad Id")), "id must be"),
            (doc(), "at least one"),
        ]
        for d, msg in cases:
            with self.subTest(msg=msg), self.assertRaises(PipelineError) as e:
                parse(d, "x")
            self.assertIn(msg, str(e.exception))

    def test_broken_file_doesnt_hide_others(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, "good.toml").write_text('[pipeline]\nname="G"\nsummary="s"\n[[stage]]\nid="a"\nrole="A"\n'
                                            'goal="g"\nprompt="p"\noutputs=["o"]\n')
            Path(t, "bad.toml").write_text("[pipeline\n")
            Path(t, "_skip.toml").write_text("nonsense")
            pipelines, errors = load_all(Path(t))
        self.assertEqual([p.id for p in pipelines], ["good"])
        self.assertIn("not valid TOML", errors["bad.toml"])


class Handoffs(unittest.TestCase):
    def setUp(self):
        self.pl = parse(doc(work("plan", outputs=["plan"]), work("build", ["request", "plan"], ["code", "notes"]),
                            review("check", "build", ["plan", "code"])), "x")

    def test_packet_carries_only_declared_inputs(self):
        st = self.pl.stage("check")
        pkt = handoff_packet(self.pl, st, "the ask", {"plan": "P", "code": "C", "notes": "secret notes"})
        self.assertIn('<artifact name="code" from="build">\nC\n</artifact>', pkt)
        self.assertNotIn("the ask", pkt)            # the reviewer didn't list the request
        self.assertNotIn("secret notes", pkt)

    def test_revision_packet(self):
        pkt = handoff_packet(self.pl, self.pl.stage("build"), "ask", {"plan": "P", "code": "old"}, (1, "fix it"))
        self.assertIn("Revision 1 of 2", pkt)
        self.assertIn('<previous name="code">\nold\n</previous>', pkt)
        self.assertIn("<feedback>\nfix it\n</feedback>", pkt)

    def test_prompt_offers_only_installed_tools(self):
        pl = parse(doc(work("a", tools=["web_search", "nope"])), "x")
        text = stage_prompt(pl, pl.stages[0], ["web_search"])
        self.assertIn("Tools you may call: web_search.", text)
        self.assertNotIn("nope", text)
        self.assertIn('<artifact name="out">', text)
        self.assertEqual(describe(pl, ["web_search"])["stages"][0]["missing_tools"], ["nope"])

    def test_parse_work_handoff(self):
        st = self.pl.stage("build")
        h = parse_handoff(st, 'working...\n<handoff>\n<artifact name="code">x = 1</artifact>\n'
                              '<artifact name="notes">run it</artifact>\n<artifact name="extra">no</artifact>\n</handoff>')
        self.assertTrue(h.ok)
        self.assertEqual(h.artifacts, {"code": "x = 1", "notes": "run it"})
        h = parse_handoff(st, '<handoff><artifact name="code">x</artifact></handoff>')
        self.assertEqual(h.problems, ["artifact 'notes' is missing or empty"])
        self.assertIn("no <handoff>", parse_handoff(st, "just text").problems[0])

    def test_parse_review_handoff_uses_last_block(self):
        st = self.pl.stage("check")
        text = ("Example: <handoff><verdict>pass</verdict></handoff>\nMy review...\n"
                "<handoff>\n<verdict> Revise </verdict>\n<feedback>null check missing</feedback>\n</handoff>")
        h = parse_handoff(st, text)
        self.assertEqual((h.verdict, h.feedback, h.ok), ("revise", "null check missing", True))
        self.assertEqual(parse_handoff(st, "<handoff><verdict>revise</verdict></handoff>").problems,
                         ["a revise verdict needs feedback saying what must change"])
        self.assertEqual(parse_handoff(st, "<handoff><verdict>maybe</verdict></handoff>").problems,
                         ["verdict must be pass or revise"])


if __name__ == "__main__":
    unittest.main()
