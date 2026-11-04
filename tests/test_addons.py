"""The addon contract: a folder under perseus/addons/ is found, checked, configured, loaded and offered
without any core file naming it; a broken one is skipped with a reason; the shipped addons comply."""

import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

from perseus import addons
from perseus.addons import Addon, Plan, check
from perseus.config import ADDON_SECTIONS, FORMER_SECTIONS, SCHEMA, home_file
from perseus.engine.tool_loader import ToolLoader
from perseus.keys import Key
from perseus.tools import REGISTRY, ToolContext, load_builtin
from tests.configs import update

ROOT = Path(__file__).resolve().parents[1]
CORE = [p for p in (ROOT / "perseus").rglob("*.py") if "addons" not in p.parts] \
    + list((ROOT / "setup").glob("*.py")) + [ROOT / "perseus.py", ROOT / "preflight.sh",
                                              ROOT / "Config" / "templates" / "tooling.toml"]

MANIFEST = '''
from perseus.addons import Addon, Plan
from perseus.keys import Key

def plan(cfg, config_path, environ):
    return Plan(bool(cfg.get("api_key_set")), "ok" if cfg.get("api_key_set") else "off (no key)")

class Client:
    def __init__(self, port, token): self.port, self.token = port, token
    def healthy(self): return True

def connect(client, registry):
    registry.provide("{name}", note="Limit 9.")
    return True, "fine"

ADDON = Addon(name="{name}", label="{label}", summary="A test addon.",
              agent_use="{agent_use}",
              config={{"api_key": Key((str,), "", secret=True), "enabled": Key((bool,), True)}},
              plan=plan, client=Client, connect=connect, former_names={former})
'''
TOOL = '''
from perseus.tools import tool

@tool(requires="{name}")
def {name}_ping(ctx) -> str:
    """Ping the {name} service."""
    return "pong" if ctx.{name} is not None else "off"
'''
TEMPLATE = '''{formerly}[{name}]                      # a test addon
api_key = "..."              # its key
enabled = true               # on by default
'''


def make_addon(root: Path, name: str, *, agent_use="Use {name}_ping whenever the user says ping; it returns pong.",
               former=(), manifest=MANIFEST, tool=TOOL, template=TEMPLATE, label="Test") -> Path:
    d = root / name
    (d / "tools").mkdir(parents=True)
    (d / "__init__.py").write_text("")
    formerly = f"# formerly: {', '.join(former)}\n" if former else ""
    (d / "tooling.toml").write_text(template.format(name=name, formerly=formerly))
    (d / "addon.py").write_text(manifest.format(name=name, label=label, agent_use=agent_use.format(name=name),
                                                former=repr(tuple(former))))
    (d / "tools" / f"{name}_ping.py").write_text(tool.format(name=name))
    return d


class Discovery(unittest.TestCase):
    """A throwaway addons tree, importable as its own package."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pkg = self.tmp / "fakeaddons"
        self.pkg.mkdir()
        (self.pkg / "__init__.py").write_text("")
        sys.path.insert(0, str(self.tmp))
        self.addCleanup(lambda: sys.path.remove(str(self.tmp)))
        self.addCleanup(lambda: [sys.modules.pop(m) for m in list(sys.modules) if m.startswith("fakeaddons")])

    def found(self):
        return addons.discover(self.pkg, "fakeaddons", refresh=True)

    def test_folders_become_addons_and_underscored_ones_are_templates(self):
        make_addon(self.pkg, "alpha")
        make_addon(self.pkg, "beta")
        make_addon(self.pkg, "_skeleton")
        (self.pkg / "common_helpers").mkdir()                   # a plain package with no addon.py
        (self.pkg / "common_helpers" / "__init__.py").write_text("")
        self.assertEqual([a.name for a in self.found()], ["alpha", "beta"])
        self.assertEqual(addons.errors(self.pkg), {})
        a = addons.by_name("alpha", self.pkg)
        self.assertEqual((a.tools_package, a.secrets, a.tools_dir.name), ("fakeaddons.alpha.tools", ("api_key",), "tools"))
        self.assertIn("[alpha]", a.template())

    def test_a_broken_addon_is_skipped_with_its_reason_and_the_rest_load(self):
        make_addon(self.pkg, "good")
        bad = make_addon(self.pkg, "bad")
        (bad / "addon.py").write_text("import nonexistent_module_xyz\n")
        make_addon(self.pkg, "silent", agent_use="")             # the manifest refuses an empty agent_use
        make_addon(self.pkg, "terse", agent_use="Pings.")        # or a trivial one
        self.assertEqual([a.name for a in self.found()], ["good"])
        errs = addons.errors(self.pkg)
        self.assertIn("nonexistent_module_xyz", errs["bad"])
        self.assertIn("agent_use", errs["silent"])
        self.assertIn("40+ characters", errs["terse"])

    def test_the_manifest_checks(self):
        d = make_addon(self.pkg, "gamma", former=("old_gamma",))
        a = self.found()[0]
        self.assertEqual(check(a, d), [])
        self.assertEqual(a.former_names, ("old_gamma",))
        # the template must agree with former_names, carry the section and exist
        (d / "tooling.toml").write_text("[gamma]\napi_key = '...'\n")
        self.assertTrue(any("formerly" in p for p in check(a, d)))
        (d / "tooling.toml").write_text("# formerly: old_gamma\n[other]\napi_key = '...'\n")
        self.assertTrue(any("[gamma]" in p for p in check(a, d)))
        (d / "tooling.toml").unlink()
        self.assertTrue(any("missing" in p for p in check(a, d)))
        for f in (d / "tools").glob("*.py"):
            f.unlink()
        self.assertTrue(any("tools/" in p for p in check(a, d)))
        self.assertTrue(any("folder name" in p for p in check(a, d.with_name("delta"))))

    def test_tools_load_from_the_addon_and_stay_hidden_until_connected(self):
        make_addon(self.pkg, "ping")
        a = self.found()[0]
        reg = REGISTRY                                           # @tool registers there, like every tool file
        ToolLoader(a.tools_dir, reg, a.tools_package).refresh()
        self.addCleanup(reg.remove_source, f"{a.tools_package}.ping_ping")
        self.addCleanup(reg.withdraw, "ping")
        self.assertIn("ping_ping", reg.all_names())
        self.assertNotIn("ping_ping", reg.names())              # requires="ping" and nothing provides it yet
        ok, detail = a.connect(a.client(1, "t"), reg)
        self.assertTrue(ok)
        reg.describe(a.name, a.agent_use)
        self.assertIn("ping_ping", reg.names())
        desc = reg.specs({"ping_ping"})[0]["function"]["description"]
        self.assertIn("Ping the ping service.", desc)
        self.assertIn("whenever the user says ping", desc)      # agent_use reaches the model
        self.assertIn("Limit 9.", desc)                         # and connect()'s note after it
        reg.provide("ping")                                     # a later provide() keeps agent_use
        self.assertIn("whenever the user says ping", reg.specs({"ping_ping"})[0]["function"]["description"])
        self.assertEqual(reg.get("ping_ping").fn(ToolContext(ping=a.client(1, "t"))), "pong")
        self.assertEqual(reg.get("ping_ping").fn(ToolContext()), "off")   # an addon that is off reads as None

    def test_update_script_merges_each_addons_template_and_renames_old_sections(self):
        make_addon(self.pkg, "zeta", former=("old_zeta",))
        make_addon(self.pkg, "_skeleton")
        real = update.ADDONS
        update.ADDONS = self.pkg
        try:
            templates = update.read_templates(ROOT / "Config" / "templates")
            text = templates["tooling.toml"].text
            self.assertIn("[zeta]", text)
            self.assertNotIn("[_skeleton]", text)
            self.assertNotIn("formerly", text)
            self.assertEqual(update.MOVES[("old_zeta", "api_key")], ("zeta", "api_key", None))
            folder = self.tmp / "Config"
            folder.mkdir()
            (folder / "tooling.toml").write_text('[old_zeta]\napi_key = "zk_12345678901234"\n')
            for n in ("provider.toml", "settings.toml"):
                (folder / n).write_text((ROOT / "Config" / "templates" / n).read_text())
            yours = update.read_yours(folder, None)
            p = update.plan(folder, templates, yours, None)
            self.assertEqual(tomllib.loads(p.files["tooling.toml"])["zeta"]["api_key"], "zk_12345678901234")
            self.assertTrue(any(old == ("old_zeta", "api_key") for _, old, _ in p.moved))
        finally:
            update.ADDONS = real
            update.MOVES.pop(("old_zeta", "api_key"), None)
            update.MOVES.pop(("old_zeta", "enabled"), None)


class ToolContextByName(unittest.TestCase):
    def test_addons_are_attributes_fixed_fields_stay(self):
        c = ToolContext(gateway="g", limits={"a": 1}, foo="F")
        self.assertEqual((c.gateway, c.limits, c.foo, c.bar, c.search), ("g", {"a": 1}, "F", None, None))
        d = c.with_outputs("O")
        self.assertEqual((d.outputs, d.foo, d.gateway), ("O", "F", "g"))
        self.assertIsNone(c.outputs)
        with self.assertRaises(AttributeError):
            c._private


class ShippedAddons(unittest.TestCase):
    """The addons in the repo comply with the contract, and the core never names them."""

    def test_every_shipped_addon_is_valid(self):
        found = addons.discover()
        self.assertEqual(addons.errors(), {})
        self.assertGreaterEqual(len(found), 2)
        for a in found:
            with self.subTest(addon=a.name):
                self.assertEqual(check(a, a.folder), [])
                self.assertIsInstance(a, Addon)
                self.assertIn(a.name, SCHEMA)
                self.assertEqual(SCHEMA[a.name], a.config)
                self.assertEqual(home_file(a.name, "api_key"), "tooling.toml")
                self.assertEqual(ADDON_SECTIONS[a.name], a.label)
                for old in a.former_names:
                    self.assertEqual(FORMER_SECTIONS[old], a.name)
                # its template agrees with its schema, key for key, and every default validates
                tpl = tomllib.loads(a.template())[a.name]
                self.assertEqual(set(tpl), set(a.config), a.name)
                for k, v in tpl.items():
                    self.assertIsInstance(a.config[k], Key)
                    self.assertIsNone(a.config[k].validate(v), f"{a.name}.{k}")
                p = a.plan({}, "", {})
                self.assertIsInstance(p, Plan)
                self.assertFalse(p.start)                       # no section at all: off, with a reason
                self.assertTrue(p.reason.startswith("off"))

    def test_shipped_tools_say_which_addon_they_need(self):
        reg = load_builtin()
        for a in addons.discover():
            for f in a.tools_dir.glob("[!_]*.py"):
                t = reg.loaded(f.stem)
                self.assertIsNotNone(t, f.name)
                self.assertEqual(t.requires, a.name, f.name)
                self.assertEqual(t.source, f"{a.tools_package}.{f.stem}")

    def test_core_never_names_a_shipped_addon(self):
        """Adding an addon must not touch core: nothing outside perseus/addons/ (and the config migration
        tables, which record history) may mention an addon by its name, capability or ctx attribute."""
        names = [a.name for a in addons.discover()]
        allowed = {ROOT / "Config" / "update.py"}        # MOVES: the record of renamed keys
        offenders = []
        for path in CORE:
            if path in allowed or not path.is_file():
                continue
            text = path.read_text()
            for name in names:
                for ln, line in enumerate(text.splitlines(), 1):
                    if name in line and not line.lstrip().startswith(("#", "*", '"', "(", "-", "//")) \
                            and "RENAMED" not in line and "e.g." not in line:
                        offenders.append(f"{path.relative_to(ROOT)}:{ln}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class Template(unittest.TestCase):
    def test_the_example_template_is_a_valid_addon_once_renamed(self):
        from perseus.addons._example.addon import ADDON
        folder = ROOT / "perseus" / "addons" / "_example"
        self.assertEqual(check(ADDON, folder, expect_name="example"), [])
        self.assertIn("agent_use", (folder / "addon.py").read_text())
        self.assertNotIn("example", [a.name for a in addons.discover()])   # underscored: never loaded
        self.assertNotIn("[example]", update.read_templates(ROOT / "Config" / "templates")["tooling.toml"].text)

    def test_the_guide_exists_and_names_every_required_file(self):
        guide = (ROOT / "docs" / "addons.md").read_text()
        for needle in ("addon.py", "tooling.toml", "tools/", "agent_use", "plan(", "connect(", "requires="):
            self.assertIn(needle, guide)


if __name__ == "__main__":
    unittest.main()
