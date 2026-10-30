"""output_file: the tool, the engine keeping file content away from the model, and the dashboard download."""

import json
import unittest

from perseus.engine.feed import clean_file
from perseus.engine.loop import Engine
from perseus.engine.prompts import system_prompt
from perseus.model.client import Reply
from perseus.tools import Registry, ToolContext, ToolFailure, load_builtin
from perseus.tools.output_file import clean_name, file_type, output_file
from perseus.web.dashboard import Dashboard
from perseus.web.server import Server
from tests.test_server import Client

DOC = "# Notes\n\nSome **bold** text.\n\n```py\nprint('hi')\n```\n"


def registry_with_output_file() -> Registry:
    reg = Registry()
    reg.add(load_builtin().loaded("output_file"))
    return reg


class FileLLM:
    """Calls output_file once, then answers in one sentence."""

    def __init__(self, filename="notes.md", content=DOC):
        self.filename, self.content = filename, content
        self.requests = []

    def chat(self, messages, tools=None, tool_choice="auto", max_tokens=None, on_text=None, should_stop=None, extra=None):
        self.requests.append(messages)
        if messages[-1]["role"] != "tool":
            args = json.dumps({"filename": self.filename, "content": self.content, "description": "your notes"})
            return Reply(tool_calls=[{"id": "f1", "type": "function",
                                      "function": {"name": "output_file", "arguments": args}}],
                         finish_reason="tool_calls")
        text = "Here are your notes as a file."
        if on_text:
            on_text(text, False)
        return Reply(content=text, finish_reason="stop")


class ToolTests(unittest.TestCase):
    def test_returns_the_file_for_the_user_and_a_short_receipt(self):
        r = output_file(ToolContext(), "notes.md", DOC, "  your notes ")
        self.assertEqual(r["delivered"], "notes.md")
        self.assertEqual(r["file"]["content"], DOC)
        self.assertEqual((r["file"]["mime"], r["file"]["language"]), ("text/markdown", "markdown"))
        self.assertEqual(r["file"]["description"], "your notes")
        self.assertEqual(r["lines"], 7)

    def test_names_are_flat_and_safe(self):
        self.assertEqual(clean_name("src/app/main.py"), "main.py")
        self.assertEqual(clean_name("C:\\work\\report.md"), "report.md")
        for bad in ("", "..", ".env", "a/", "bad<name>.txt", "x" * 200):
            with self.assertRaises(ToolFailure, msg=bad):
                clean_name(bad)

    def test_types_from_extension(self):
        self.assertEqual(file_type("a.py"), ("text/x-python", "python"))
        self.assertEqual(file_type("Dockerfile"), ("text/plain", "dockerfile"))
        self.assertEqual(file_type("weird.zzz"), ("text/plain", "text"))

    def test_clean_file_rejects_junk_and_caps_fields(self):
        self.assertIsNone(clean_file("nope"))
        self.assertIsNone(clean_file({"name": "a.md"}))
        f = clean_file({"name": "../a.md", "content": "x", "description": "d" * 500})
        self.assertEqual(f["name"], ".._a.md")
        self.assertEqual(len(f["description"]), 200)
        self.assertEqual(f["mime"], "text/plain")

    def test_prompt_mentions_it_only_when_offered(self):
        self.assertIn("output_file", system_prompt(["output_file"], 5))
        self.assertNotIn("output_file", system_prompt(["calculator"], 5))


class EngineTests(unittest.TestCase):
    def test_model_gets_a_receipt_and_the_event_carries_the_file(self):
        llm = FileLLM()
        e = Engine(llm=llm, registry=registry_with_output_file(), ctx=ToolContext())
        res = e.run_turn([], "write my notes as a markdown file")
        self.assertEqual(res.answer, "Here are your notes as a file.")
        told = next(m for m in res.messages if m["role"] == "tool")["content"]
        self.assertNotIn("**bold**", told)                 # the content isn't echoed back to the model
        self.assertIn('"delivered": "notes.md"', told)
        ev = e.feed.events[0]
        self.assertEqual(ev.status, "ok")
        self.assertEqual(ev.file["content"], DOC)
        self.assertEqual(ev.summary, "notes.md")
        self.assertEqual(ev.feed["pill"], "File ready")

    def test_bad_name_is_a_tool_error_not_a_file(self):
        e = Engine(llm=FileLLM(filename="../../etc/.passwd"), registry=registry_with_output_file(), ctx=ToolContext())
        e.run_turn([], "write it")
        self.assertEqual(e.feed.events[0].status, "error")
        self.assertIsNone(e.feed.events[0].file)


class TwoFileLLM:
    """Delivers two files in one step (parallel tool calls), then a third in the next step."""

    def chat(self, messages, tools=None, tool_choice="auto", max_tokens=None, on_text=None, should_stop=None, extra=None):
        done = sum(1 for m in messages if m["role"] == "tool")
        call = lambda i, name: {"id": f"f{i}", "type": "function", "function": {
            "name": "output_file", "arguments": json.dumps({"filename": name, "content": f"# {name}\n"})}}
        if done == 0:
            return Reply(tool_calls=[call(1, "a.md"), call(2, "b.py")], finish_reason="tool_calls")
        if done == 2:
            return Reply(tool_calls=[call(3, "c.csv")], finish_reason="tool_calls")
        on_text and on_text("Three files.", False)
        return Reply(content="Three files.", finish_reason="stop")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.llm = FileLLM(filename="page.html", content="<script>alert(1)</script>\n")
        dash = Dashboard(info={"model": "m", "context": 32768, "capacity": 32768}, llm=self.llm,
                         registry=registry_with_output_file(), ctx=ToolContext(), depths={"standard": 8},
                         engine_settings={"max_context": 32768}, engine_list=[], status=lambda: {})
        self.srv = Server(dash, "127.0.0.1", 0, token="tok")
        self.srv.start()
        self.c = Client(self.srv.port, self.srv.session)

    def tearDown(self):
        self.srv.stop()

    def test_file_card_streams_and_downloads(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        _, events = self.c.stream(chat["id"], "make a page")
        cards = [e["file"] for e in events if e["type"] == "file"]
        self.assertEqual([c["name"] for c in cards], ["page.html"])
        self.assertNotIn("content", cards[0])                       # content is fetched on demand
        tool_events = [e["event"] for e in events if e["type"] == "tool" and e["event"]["status"] == "ok"]
        self.assertNotIn("content", tool_events[0]["file"])
        done = next(e for e in events if e["type"] == "done")["message"]
        self.assertEqual([f["id"] for f in done["files"]], [cards[0]["id"]])

        _, full = self.c.req("GET", f"/api/chats/{chat['id']}")
        self.assertEqual(full["messages"][1]["files"][0]["name"], "page.html")

        r, body = self.c.req("GET", f"/api/chats/{chat['id']}/files/{cards[0]['id']}", raw=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(body.decode(), "<script>alert(1)</script>\n")
        self.assertEqual(r.getheader("Content-Disposition"), 'attachment; filename="page.html"')
        self.assertTrue(r.getheader("Content-Type").startswith("text/html"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_several_files_in_one_answer(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        self.srv.dash.get(chat["id"]).engine.llm = TwoFileLLM()
        _, events = self.c.stream(chat["id"], "make three files")
        done = next(e for e in events if e["type"] == "done")["message"]
        self.assertEqual([f["name"] for f in done["files"]], ["a.md", "b.py", "c.csv"])
        for f in done["files"]:
            r, body = self.c.req("GET", f"/api/chats/{chat['id']}/files/{f['id']}", raw=True)
            self.assertEqual(body.decode(), f"# {f['name']}\n")

    def test_download_needs_auth_and_a_real_file(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        self.c.stream(chat["id"], "make a page")
        anon = Client(self.srv.port)
        self.assertEqual(anon.req("GET", f"/api/chats/{chat['id']}/files/1")[0], 401)
        self.assertEqual(self.c.req("GET", f"/api/chats/{chat['id']}/files/999")[0], 404)
        self.assertEqual(self.c.req("GET", f"/api/chats/{chat['id']}/files/abc")[0], 404)
        self.assertEqual(self.c.req("GET", "/api/chats/nope/files/1")[0], 404)


if __name__ == "__main__":
    unittest.main()
