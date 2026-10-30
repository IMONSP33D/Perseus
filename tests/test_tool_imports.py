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
                    "import asyncio", "exec('x')"):
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
