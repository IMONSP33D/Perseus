"""Config/update.py (run by Config/configuration.sh): create, migrate and update the split config, keeping values."""

import importlib.util
import io
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from perseus import CONFIG_TEMPLATES, ROOT
from perseus.config import CONFIG_FILES, load

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "hf_" + "z" * 34
E2B = "e2b_" + "q" * 30

spec = importlib.util.spec_from_file_location("perseus_config_update", ROOT / "Config" / "update.py")
update = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = update      # dataclasses look their module up by name
spec.loader.exec_module(update)


def presplit_toml() -> str:
    """A filled-in perseus.toml from before the split."""
    return (FIXTURES.joinpath("perseus_presplit.toml").read_text()
            .replace('"org/model-name"', '"Qwen/Qwen3-32B"').replace('"hf_..."', f'"{TOKEN}"')
            .replace('"e2b_..."', f'"{E2B}"').replace("port = 8080", "port = 9090"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / "Config"
        self.templates = self.tmp / "templates"
        shutil.copytree(CONFIG_TEMPLATES, self.templates)
        self._real = update.TEMPLATES
        update.TEMPLATES = self.templates

    def tearDown(self):
        update.TEMPLATES = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_update(self, *args) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = update.main(["--dir", str(self.dir), *args])
        return code, out.getvalue()

    def edit_template(self, name: str, old: str, new: str) -> None:
        path = self.templates / name
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def edit_mine(self, name: str, old: str, new: str) -> None:
        path = self.dir / name
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def value(self, name: str, section: str, key: str):
        return tomllib.loads((self.dir / name).read_text())[section].get(key)

    def backups(self) -> list[Path]:
        b = self.dir / ".backup"
        return sorted(b.iterdir()) if b.is_dir() else []


class FirstRun(Base):
    def test_copies_the_templates_private_and_says_what_to_fill_in(self):
        code, out = self.run_update()
        self.assertEqual(code, 0, out)
        for name in CONFIG_FILES:
            self.assertEqual((self.dir / name).read_text(), (self.templates / name).read_text())
            self.assertEqual(stat.S_IMODE((self.dir / name).stat().st_mode), 0o600)
            self.assertTrue((self.dir / ".base" / name).is_file())
        self.assertIn("still to fill in", out)
        self.assertIn("[model] id", out)
        self.assertIn("[code_runner] api_key", out)
        self.assertEqual(self.backups(), [])                           # nothing of yours to back up yet

    def test_running_again_changes_nothing(self):
        self.run_update()
        before = {n: (self.dir / n).read_text() for n in CONFIG_FILES}
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertEqual(before, {n: (self.dir / n).read_text() for n in CONFIG_FILES})
        self.assertEqual(self.backups(), [])
        self.assertIn("unchanged", out)


class Migration(Base):
    def test_old_perseus_toml_is_split_with_every_value_kept(self):
        legacy = self.tmp / "perseus.toml"
        legacy.write_text(presplit_toml())
        (self.tmp / ".perseus-data").write_text("/data/perseus\n")
        self.dir.mkdir()
        code, out = self.run_update("--from", str(legacy))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.value("provider.toml", "model", "id"), "Qwen/Qwen3-32B")
        self.assertEqual(self.value("provider.toml", "secrets", "hf_token"), TOKEN)
        self.assertEqual(self.value("settings.toml", "server", "port"), 9090)
        self.assertEqual(self.value("tooling.toml", "code_runner", "api_key"), E2B)   # moved beside its tool
        self.assertNotIn("code_runner_api_key", (self.dir / "provider.toml").read_text())
        self.assertIn("renamed; your value kept", out)
        # the old file is gone from where people would edit it, but kept in the backup
        self.assertFalse(legacy.exists())
        self.assertTrue((self.backups()[0] / "perseus.toml").is_file())
        self.assertEqual((self.dir / ".perseus-data").read_text(), "/data/perseus\n")
        # and Perseus reads the result
        cfg = load(self.dir)
        self.assertEqual((cfg["model"]["id"], cfg["server"]["port"], cfg["code_runner"]["api_key"]),
                         ("Qwen/Qwen3-32B", 9090, E2B))
        for secret in (TOKEN, E2B):
            self.assertNotIn(secret, out)                              # secrets are never printed

    def test_renamed_value_is_transformed(self):
        legacy = self.tmp / "perseus.toml"
        legacy.write_text(presplit_toml().replace('network = "packages"', "internet = true"))
        code, out = self.run_update("--from", str(legacy))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.value("tooling.toml", "code_runner", "network"), "full")


class Updates(Base):
    """A git pull brings new templates; your files get the changes without losing your values."""

    def setUp(self):
        super().setUp()
        self.run_update()
        self.edit_mine("provider.toml", '"org/model-name"', '"Qwen/Qwen3-32B"')
        self.edit_mine("provider.toml", '"hf_..."', f'"{TOKEN}"')
        self.edit_mine("tooling.toml", '"e2b_..."', f'"{E2B}"')
        self.edit_mine("settings.toml", "port = 8080", "port = 9090")

    def test_new_keys_and_changed_defaults_arrive_your_values_stay(self):
        self.edit_template("settings.toml", "warn_download_minutes = 20",
                           "warn_download_minutes = 20\nnew_limit = 7           # a brand-new key")
        self.edit_template("settings.toml", "fetch_timeout_s = 15", "fetch_timeout_s = 25")   # you never set it
        self.edit_template("settings.toml", "port = 8080", "port = 8000")                     # you set 9090
        self.edit_template("provider.toml", '"org/model-name"', '"org/another-name"')
        code, out = self.run_update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.value("settings.toml", "limits", "new_limit"), 7)
        self.assertEqual(self.value("settings.toml", "limits", "fetch_timeout_s"), 25)
        self.assertEqual(self.value("settings.toml", "server", "port"), 9090)
        self.assertEqual(self.value("provider.toml", "model", "id"), "Qwen/Qwen3-32B")
        self.assertEqual(self.value("provider.toml", "secrets", "hf_token"), TOKEN)
        self.assertEqual(self.value("tooling.toml", "code_runner", "api_key"), E2B)
        self.assertIn("# a brand-new key", (self.dir / "settings.toml").read_text())   # docs arrive too
        self.assertIn("+ [limits] new_limit = 7", out)
        self.assertIn("~ [limits] fetch_timeout_s: 15 -> 25", out)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn("port = 9090", (self.backups()[0] / "settings.toml").read_text())

    def test_a_key_moving_to_another_file_takes_your_value_along(self):
        # [search] moves from settings.toml to tooling.toml upstream
        settings = (self.templates / "settings.toml").read_text()
        block = settings[settings.index("[search]"):settings.index("[backend]")]
        (self.templates / "settings.toml").write_text(settings.replace(block, ""))
        (self.templates / "tooling.toml").write_text((self.templates / "tooling.toml").read_text() + "\n" + block)
        self.edit_mine("settings.toml", "port = 8888", "port = 8899")
        code, out = self.run_update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.value("tooling.toml", "search", "port"), 8899)
        self.assertNotIn("[search]", (self.dir / "settings.toml").read_text())
        self.assertIn("moved from settings.toml to tooling.toml", out)
        load(self.dir)                                                   # still a valid config

    def test_a_removed_key_is_listed_and_saved_not_silently_dropped(self):
        self.edit_mine("settings.toml", "max_restarts = 3", "max_restarts = 5")
        self.edit_template("settings.toml", "max_restarts = 3             # crash restarts per process "
                           "(vLLM, SearXNG), with backoff\n", "")
        code, out = self.run_update()
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.value("settings.toml", "backend", "max_restarts"))
        self.assertIn("- [backend] max_restarts = 5", out)
        saved = tomllib.loads((self.backups()[0] / "unused-keys.toml").read_text())
        self.assertEqual(saved["backend"]["max_restarts"], 5)

    def test_check_shows_the_plan_and_writes_nothing(self):
        self.edit_template("settings.toml", "fetch_timeout_s = 15", "fetch_timeout_s = 25")
        before = (self.dir / "settings.toml").read_text()
        code, out = self.run_update("--check")
        self.assertEqual(code, 1)
        self.assertIn("dry run", out)
        self.assertIn("fetch_timeout_s", out)
        self.assertEqual((self.dir / "settings.toml").read_text(), before)
        self.assertEqual(self.backups(), [])

    def test_the_same_key_in_two_files_stops_without_touching_anything(self):
        (self.dir / "settings.toml").write_text((self.dir / "settings.toml").read_text()
                                                + '\n[secrets]\nhf_token = "hf_' + "y" * 34 + '"\n')
        before = {n: (self.dir / n).read_text() for n in CONFIG_FILES}
        code, out = self.run_update()
        self.assertEqual(code, 2)
        self.assertIn("set in both", out)
        self.assertNotIn("y" * 34, out)
        self.assertEqual(before, {n: (self.dir / n).read_text() for n in CONFIG_FILES})

    def test_a_key_you_put_in_the_wrong_file_is_moved_home(self):
        self.edit_mine("provider.toml", f'hf_token = "{TOKEN}"', "")
        (self.dir / "settings.toml").write_text((self.dir / "settings.toml").read_text()
                                                + f'\n[secrets]\nhf_token = "{TOKEN}"\n')
        code, out = self.run_update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.value("provider.toml", "secrets", "hf_token"), TOKEN)
        self.assertNotIn("[secrets]", (self.dir / "settings.toml").read_text())

    def test_a_broken_file_of_yours_is_never_overwritten(self):
        (self.dir / "settings.toml").write_text("[server\n")
        code, out = self.run_update()
        self.assertEqual(code, 2)
        self.assertIn("nothing was changed", out)
        self.assertEqual((self.dir / "settings.toml").read_text(), "[server\n")


class Templates(unittest.TestCase):
    def test_templates_are_mergeable_and_load_once_filled_in(self):
        templates = update.read_templates(CONFIG_TEMPLATES)               # one-line values, no key in two files
        self.assertEqual(set(templates), set(CONFIG_FILES))
        folder = Path(tempfile.mkdtemp()) / "Config"
        folder.mkdir()
        for name in CONFIG_FILES:
            (folder / name).write_text((CONFIG_TEMPLATES / name).read_text().replace('"org/model-name"', '"a/b"'))
        load(folder)

    def test_every_schema_key_has_a_template_line_in_its_home_file(self):
        from perseus.config import SCHEMA, home_file
        templates = update.read_templates(CONFIG_TEMPLATES)
        for section, keys in SCHEMA.items():
            for key in keys:
                with self.subTest(key=f"{section}.{key}"):
                    self.assertIn((section, key), templates[home_file(section, key)].values)

    def test_value_split_ignores_hashes_inside_strings(self):
        self.assertEqual(update.split_value('"a # b"   # note'), ('"a # b"', "   # note"))
        self.assertEqual(update.split_value('["x", "y"]'), ('["x", "y"]', ""))


class Script(unittest.TestCase):
    def test_configuration_sh_runs_the_updater(self):
        tmp = Path(tempfile.mkdtemp())
        r = subprocess.run(["bash", str(ROOT / "Config" / "configuration.sh"), "--dir", str(tmp / "cfg"), "--check"],
                           capture_output=True, text=True, env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                                                                "PERSEUS_PYTHON": sys.executable})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)          # would create three files
        self.assertIn("would be created", r.stdout)
        self.assertFalse((tmp / "cfg" / "provider.toml").exists())


if __name__ == "__main__":
    unittest.main()
