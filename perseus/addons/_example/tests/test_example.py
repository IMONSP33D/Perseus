"""The checks every addon should have: the manifest matches its template, the tools load and are gated."""

import unittest
from pathlib import Path

from perseus.addons import check
from perseus.addons._example.addon import ADDON, CONFIG, plan
from perseus.engine.tool_loader import import_problem
from perseus.keys import Key

HERE = Path(__file__).resolve().parents[1]


class Manifest(unittest.TestCase):
    def test_manifest_is_complete(self):
        self.assertEqual(check(ADDON, HERE, expect_name=ADDON.name), [])   # as it will be once the folder is renamed

    def test_template_matches_the_schema(self):
        import tomllib
        tpl = tomllib.loads((HERE / "tooling.toml").read_text())[ADDON.name]
        self.assertEqual(set(tpl), set(CONFIG))
        for k, v in tpl.items():
            self.assertIsInstance(CONFIG[k], Key)
            self.assertIsNone(CONFIG[k].validate(v), k)

    def test_plan_is_off_until_the_key_is_set(self):
        self.assertFalse(plan({}, "/c", {}).start)
        self.assertFalse(plan({"enabled": False, "api_key_set": True}, "/c", {}).start)
        self.assertIn("api_key", plan({"api_key_set": False}, "/c", {}).reason)
        self.assertTrue(plan({"api_key_set": True, "url": "http://127.0.0.1:9000"}, "/c", {}).start)

    def test_tool_files_pass_the_import_check(self):
        for p in (HERE / "tools").glob("*.py"):
            self.assertIsNone(import_problem(p), p.name)
