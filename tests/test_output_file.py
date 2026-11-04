"""output_file: the tool, the engine keeping file content away from the model, and the dashboard download.

Text the model writes, small binaries it encodes, and files a tool brought back (an .exe a
program built in the Windows VM) all end up as a download card; binary bytes never reach
the model or a JSON string.
"""

import base64
import hashlib
import json
import unittest

from perseus.engine.feed import ToolEvent, clean_file
from perseus.engine.outputs import Outputs, make_ref
from perseus.engine.loop import Engine
from perseus.engine.prompts import system_prompt
from perseus.model.client import Reply
from perseus.tools import Registry, ToolContext, ToolFailure, load_builtin
from perseus.tools.output_file import clean_name, file_type, output_file
from perseus.web.dashboard import Dashboard
from perseus.web.server import Server
from tests.test_server import Client

DOC = "# Notes\n\nSome **bold** text.\n\n```py\nprint('hi')\n```\n"
# enough of a PE file to look like one: MZ header, NUL bytes, bytes that aren't valid UTF-8
EXE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + bytes(range(256)) * 64 + b"PE\x00\x00\xc3\x28"


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


def store_with(*files) -> ToolContext:
    out = Outputs()
    for ref, data in files:
        out.add(ref, data, "test")
    return ToolContext(outputs=out)


class BinaryToolTests(unittest.TestCase):
    def test_exactly_one_source_of_content(self):
        for kwargs in ({}, {"content": "a", "source": "windows:a.exe"},
                       {"content": "a", "content_base64": "YQ=="}):
            with self.assertRaises(ToolFailure, msg=kwargs) as c:
                output_file(ToolContext(), "a.txt", **kwargs)
            self.assertIn("exactly one", str(c.exception))

    def test_binary_pasted_into_content_is_refused_not_corrupted(self):
        with self.assertRaises(ToolFailure) as c:
            output_file(ToolContext(), "app.exe", EXE.decode("latin-1"))
        self.assertIn("collect", str(c.exception))

    def test_base64_binary_is_delivered_as_exact_bytes(self):
        r = output_file(ToolContext(), "app.exe", content_base64=base64.b64encode(EXE).decode())
        self.assertEqual(r["kind"], "binary")
        self.assertEqual(r["file"]["data"], EXE)
        self.assertEqual(r["file"]["mime"], "application/vnd.microsoft.portable-executable")
        self.assertTrue(r["file"]["executable"])
        self.assertEqual(r["sha256"], hashlib.sha256(EXE).hexdigest())
        self.assertNotIn("data", json.dumps({k: v for k, v in r.items() if k not in ("file",)}))

    def test_bad_base64_is_a_clear_error(self):
        with self.assertRaises(ToolFailure):
            output_file(ToolContext(), "a.bin", content_base64="not base64!!")

    def test_source_delivers_the_collected_bytes(self):
        ctx = store_with(("windows:bin/Release/publish/app.exe", EXE))
        r = output_file(ctx, "app.exe", source="windows:bin/Release/publish/app.exe", description="the build")
        self.assertEqual(r["file"]["data"], EXE)
        self.assertEqual(r["file"]["source"], "windows:bin/Release/publish/app.exe")
        # a bare path or file name works when it's unambiguous
        self.assertEqual(output_file(ctx, "app.exe", source="app.exe")["file"]["data"], EXE)

    def test_unknown_source_names_what_is_there(self):
        ctx = store_with(("windows:dist/tool.exe", EXE))
        with self.assertRaises(ToolFailure) as c:
            output_file(ctx, "app.exe", source="windows:dist/app.exe")
        self.assertIn("windows:dist/tool.exe", str(c.exception))
        with self.assertRaises(ToolFailure) as c:
            output_file(ToolContext(), "app.exe", source="windows:dist/app.exe")
        self.assertIn("collect", str(c.exception))

    def test_collected_text_stays_text_byte_for_byte(self):
        raw = "\ufeffname,count\r\nwidgets,3\r\n".encode("utf-8")      # a BOM and CRLF line endings
        r = output_file(store_with(("windows:out/report.csv", raw)), "report.csv", source="windows:out/report.csv")
        self.assertEqual(r["kind"], "text")
        self.assertEqual(r["file"]["content"].encode("utf-8"), raw)
        self.assertEqual(r["file"]["language"], "csv")

    def test_image_bytes_stay_binary_even_when_they_decode(self):
        r = output_file(ToolContext(), "dot.png", content_base64=base64.b64encode(b"just ascii").decode())
        self.assertEqual((r["kind"], r["file"]["mime"]), ("binary", "image/png"))
        self.assertNotIn("executable", clean_file(r["file"]) or {})

    def test_clean_file_keeps_bytes_and_hash(self):
        f = clean_file({"name": "app.exe", "data": EXE, "mime": "application/vnd.microsoft.portable-executable",
                        "executable": True, "source": "windows:app.exe"})
        self.assertEqual(f["data"], EXE)
        self.assertEqual((f["bytes"], f["binary"], f["sha256"]), (len(EXE), True, hashlib.sha256(EXE).hexdigest()))
        self.assertIsNone(clean_file({"name": "big.bin", "data": b"x" * (64 * 1024 * 1024 + 1)}))

    def test_event_export_leaves_out_the_bytes(self):
        ev = ToolEvent(1, 1, "output_file", "app.exe", {}, file=clean_file({"name": "app.exe", "data": EXE}))
        d = json.loads(json.dumps(ev.to_dict()))
        self.assertNotIn("data", d["file"])
        self.assertEqual(d["file"]["bytes"], len(EXE))
        self.assertEqual(ev.file["data"], EXE)          # the event itself still holds them for the download


class OutputsStoreTests(unittest.TestCase):
    def test_refs_are_safe(self):
        self.assertEqual(make_ref("windows", "bin\\Release\\app.exe"), "windows:bin/Release/app.exe")
        for bad in ("../secret", "a/../../b", ""):
            self.assertIsNone(make_ref("windows", bad), bad)
        self.assertIsNone(Outputs().add("no-colon", b"x"))

    def test_newest_copy_wins_and_the_oldest_go_first(self):
        store = Outputs(max_total=10)
        store.add("w:a.bin", b"11111")
        store.add("w:a.bin", b"22222")
        self.assertEqual(store.get("w:a.bin").data, b"22222")
        store.add("w:b.bin", b"333333")
        self.assertIsNone(store.get("w:a.bin"))          # over 10 bytes: the oldest file was dropped
        self.assertEqual(store.refs(), ["w:b.bin"])

    def test_ambiguous_short_names_dont_guess(self):
        store = Outputs()
        store.add("w:x86/app.exe", b"1")
        store.add("w:x64/app.exe", b"2")
        self.assertIsNone(store.get("app.exe"))
        self.assertEqual(store.get("x64/app.exe").data, b"2")


class BuildTool:
    """Stands in for run_windows_code: returns a collected .exe under the reserved "outputs" key."""

    def __init__(self):
        from perseus.tools import Tool
        self.tool = Tool("build_exe", "Build an exe.", {"type": "object", "properties": {}},
                         lambda ctx: {"text": 'built app.exe; give it with output_file source="windows:dist/app.exe"',
                                      "outputs": [{"ref": "windows:dist/app.exe", "data": EXE}]})


class ExeLLM:
    """Builds the exe, hands it over by ref, then answers."""

    def chat(self, messages, tools=None, tool_choice="auto", max_tokens=None, on_text=None, should_stop=None, extra=None):
        done = [m for m in messages if m["role"] == "tool"]
        call = lambda i, name, args: Reply(tool_calls=[{"id": f"c{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}], finish_reason="tool_calls")
        if not done:
            return call(1, "build_exe", {})
        if len(done) == 1:
            return call(2, "output_file", {"filename": "app.exe", "source": "windows:dist/app.exe"})
        on_text and on_text("Here is app.exe.", False)
        return Reply(content="Here is app.exe.", finish_reason="stop")


def exe_registry() -> Registry:
    reg = registry_with_output_file()
    reg.add(BuildTool().tool)
    return reg


class BinaryEngineTests(unittest.TestCase):
    def test_bytes_go_to_the_user_never_to_the_model(self):
        e = Engine(llm=ExeLLM(), registry=exe_registry(), ctx=ToolContext())
        res = e.run_turn([], "build me an exe")
        self.assertEqual(res.answer, "Here is app.exe.")
        told = json.dumps([m for m in res.messages if m["role"] == "tool"])
        self.assertNotIn(base64.b64encode(EXE[:64]).decode()[:40], told)
        self.assertIn(hashlib.sha256(EXE).hexdigest(), told)       # the model can tell the user the hash
        ev = next(x for x in e.feed.events if x.tool == "output_file")
        self.assertEqual((ev.status, ev.file["data"]), ("ok", EXE))
        json.dumps(e.feed.export())                                # exports still work
        self.assertIsNone(Engine(llm=None, registry=exe_registry(), ctx=ToolContext()).outputs.get("app.exe"))


class BinaryDownloadTests(unittest.TestCase):
    def setUp(self):
        dash = Dashboard(info={"model": "m", "context": 32768, "capacity": 32768}, llm=ExeLLM(),
                         registry=exe_registry(), ctx=ToolContext(), depths={"standard": 8},
                         engine_settings={"max_context": 32768}, engine_list=[], status=lambda: {})
        self.srv = Server(dash, "127.0.0.1", 0, token="tok")
        self.srv.start()
        self.c = Client(self.srv.port, self.srv.session)

    def tearDown(self):
        self.srv.stop()

    def test_exe_card_and_exact_download(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        _, events = self.c.stream(chat["id"], "build me an exe")
        card = next(e["file"] for e in events if e["type"] == "file")
        self.assertEqual((card["name"], card["binary"], card["executable"], card["bytes"]),
                         ("app.exe", True, True, len(EXE)))
        self.assertNotIn("data", card)
        self.assertEqual(card["sha256"], hashlib.sha256(EXE).hexdigest())

        r, body = self.c.req("GET", f"/api/chats/{chat['id']}/files/{card['id']}", raw=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(body, EXE)                                # byte for byte
        self.assertEqual(r.getheader("Content-Type"), "application/vnd.microsoft.portable-executable")
        self.assertEqual(r.getheader("Content-Disposition"), 'attachment; filename="app.exe"')
        self.assertEqual(r.getheader("Content-Length"), str(len(EXE)))
        self.assertEqual(r.getheader("X-Content-SHA256"), card["sha256"])
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

        _, full = self.c.req("GET", f"/api/chats/{chat['id']}")
        self.assertNotIn("data", json.dumps(full["feed"]))
        r, body = self.c.req("GET", "/api/export", raw=True)
        self.assertEqual(r.status, 200)
        json.loads(body)


if __name__ == "__main__":
    unittest.main()
