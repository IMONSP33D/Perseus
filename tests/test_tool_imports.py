"""perseus/tools/ holds only tools, and no tool file imports network, process or FFI libraries."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from perseus.engine.tool_loader import ToolLoader, import_problem
from perseus.tools import TOOLS_DIR, Registry, load_builtin


class ToolFolder(unittest.TestCase):
    def test_every_file_in_tools_passes_the_import_ban(self):
        files = sorted(TOOLS_DIR.glob("*.py"))
        self.assertIn("__init__.py", [p.name for p in files])   # the registry is scanned too
        for p in files:
            self.assertIsNone(import_problem(p), p.name)

    def test_one_file_per_tool(self):
        reg = load_builtin()
        for name in reg.all_names():
            self.assertEqual(reg.loaded(name).source, f"perseus.tools.{name}", name)
        self.assertEqual(reg.all_names(), ["calculator", "current_time", "fetch_url", "output_file", "run_code",
                                          "web_search"])
        # run_code needs the code sandbox, so it's hidden until the launcher provides it
        self.assertEqual(reg.names(), ["calculator", "current_time", "fetch_url", "output_file", "web_search"])
        self.assertTrue(reg.get("fetch_url").lookup and reg.get("web_search").lookup)
        self.assertFalse(reg.get("calculator").lookup)

    def test_banned_imports_detected(self):
        d = Path(tempfile.mkdtemp())
        for src in ("import socket", "from urllib.request import urlopen", "import requests as r",
                    "import subprocess", "__import__('http')", "from http import client", "import ctypes",
                    "import _socket", "import os\nos.system('curl x')", "from os import popen", "import importlib",
                    "import asyncio", "exec('x')", "import os as o\no.system('x')", "import os\ngetattr(os, 'system')",
                    "import builtins"):
            (d / "t.py").write_text(src + "\n")
            self.assertIsNotNone(import_problem(d / "t.py"), src)
        (d / "t.py").write_text("import math, json, os\nfrom datetime import date\nx = os.path.join('a', 'b')\n")
        self.assertIsNone(import_problem(d / "t.py"))


class HotReload(unittest.TestCase):
    def test_load_reload_refuse_remove(self):
        d = Path(tempfile.mkdtemp())
        reg = Registry()
        good = ("from perseus.tools import tool\n"
                "@tool(description='d', parameters={'type': 'object', 'properties': {}})\n"
                "def hello(ctx):\n    return 'hi'\n")
        import perseus.tools as pt
        orig = pt.REGISTRY
        pt.REGISTRY = reg  # tool files register into the default registry
        try:
            loader = ToolLoader(d, reg, package="test_tools")
            (d / "mine.py").write_text(good)
            (d / "_draft.py").write_text("import socket\n")         # underscore files are skipped
            self.assertEqual(loader.refresh(), ["loaded mine.py"])
            self.assertIn("hello", reg.names())
            self.assertEqual(loader.refresh(), [])
            (d / "evil.py").write_text("import socket\n")
            self.assertTrue(loader.refresh()[0].startswith("refused evil.py"))
            (d / "mine.py").write_text(good.replace("hello", "hello2"))
            os.utime(d / "mine.py", (time.time() + 5, time.time() + 5))
            self.assertEqual(loader.refresh(), ["reloaded mine.py"])
            self.assertIn("hello2", reg.names())
            self.assertNotIn("hello", reg.names())
            (d / "mine.py").unlink()
            loader.refresh()
            self.assertNotIn("hello2", reg.names())
        finally:
            pt.REGISTRY = orig


if __name__ == "__main__":
    unittest.main()


class SignatureSchemas(unittest.TestCase):
    def test_bare_decorator_infers_description_and_schema(self):
        from typing import Annotated, Literal
        from perseus.tools import ToolContext, tool
        reg = Registry()

        @tool(registry=reg)
        def shout(text: Annotated[str, "the text"], times: int = 1, mode: Literal["up", "down"] = "up",
                  tags: list[str] | None = None) -> str:
            """Upper-case some text."""
            return text.upper() * times

        t = reg.get("shout")
        self.assertEqual(t.description, "Upper-case some text.")
        props = t.parameters["properties"]
        self.assertEqual(t.parameters["required"], ["text"])
        self.assertEqual(props["text"], {"type": "string", "description": "the text"})
        self.assertEqual(props["mode"]["enum"], ["up", "down"])
        self.assertEqual(props["tags"]["type"], ["array", "null"])
        self.assertEqual(t.fn(ToolContext(), text="a", times=2), "AA")
        self.assertEqual(reg.validate("shout", {"text": "x", "tags": None})[2], [])
        self.assertTrue(reg.validate("shout", {"text": "x", "mode": "side"})[2])

    def test_explicit_schema_keeps_ctx_under_any_name(self):
        from perseus.tools import ToolContext, tool
        reg = Registry()

        @tool(registry=reg, description="d", parameters={"type": "object", "properties": {"x": {"type": "integer"}}})
        def old_style(c, x):
            return (c, x)

        @tool(registry=reg, description="d", parameters={"type": "object", "properties": {"x": {"type": "integer"}}})
        def no_ctx(x):
            return x
        ctx = ToolContext()
        self.assertEqual(reg.get("old_style").fn(ctx, x=1), (ctx, 1))
        self.assertEqual(reg.get("no_ctx").fn(ctx, x=2), 2)

    def test_missing_description_and_name_clash_rejected(self):
        from perseus.tools import Tool, tool
        reg = Registry()
        with self.assertRaises(ValueError):
            @tool(registry=reg)
            def nodoc(x: int):
                return x
        reg.add(Tool("dup", "d", {"type": "object", "properties": {}}, lambda ctx: 1, source="a"))
        with self.assertRaises(ValueError):
            reg.add(Tool("dup", "d", {"type": "object", "properties": {}}, lambda ctx: 1, source="b"))

    def test_example_file_loads(self):
        d = Path(tempfile.mkdtemp())
        (d / "example.py").write_text((TOOLS_DIR / "_example.py").read_text())
        reg = Registry()
        import perseus.tools as pt
        orig = pt.REGISTRY
        pt.REGISTRY = reg
        try:
            self.assertEqual(ToolLoader(d, reg, package="example_tools").refresh(), ["loaded example.py"])
        finally:
            pt.REGISTRY = orig
        self.assertEqual(reg.all_names(), ["page_size", "repeat", "word_count"])
        self.assertTrue(reg.loaded("page_size").lookup)
