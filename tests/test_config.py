import tempfile
import tomllib
import unittest
from pathlib import Path

from perseus import CONFIG_TEMPLATES
from perseus.config import dumps
from perseus.config import ConfigError, load, parse_override_value, split_overrides
from tests.configs import TOKEN, config_dir, edited, template_texts, write_dir

GOOD = template_texts()


def good(*pairs) -> Path:
    """The templates with a model and token filled in, plus any (old, new) edits."""
    return write_dir(edited(GOOD, *pairs))


def write(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class Load(unittest.TestCase):
    def test_templates_with_model_filled_in_are_valid(self):
        cfg = load(good())
        self.assertEqual(cfg["model"]["id"], "Qwen/Qwen3-32B")
        self.assertEqual(cfg["model"]["tensor_parallel"], "auto")      # [model] spans provider and settings
        self.assertEqual(cfg["limits"]["warn_download_minutes"], 20)
        self.assertEqual(cfg["code_runner"]["network"], "packages")
        self.assertEqual(len(cfg.files), 3)
        self.assertFalse(any("belongs in" in w for w in cfg.warnings))

    def test_untouched_templates_name_the_missing_model(self):
        folder = write_dir({n: (CONFIG_TEMPLATES / n).read_text() for n in GOOD})
        with self.assertRaises(ConfigError) as e:
            load(folder)
        self.assertTrue(any("provider.toml" in p and "[model] id" in p and "repo id" in p
                            for p in e.exception.problems))

    def test_unknown_key_reports_file_line_and_suggestion(self):
        folder = good(("revision = ", "revison = "))
        with self.assertRaises(ConfigError) as e:
            load(folder)
        text = (folder / "settings.toml").read_text()
        line = next(n for n, l in enumerate(text.splitlines(), 1) if l.startswith("revison"))
        msg = e.exception.problems[0]
        self.assertIn(f"Config/settings.toml:{line} [model] revison: unknown key", msg)
        self.assertIn("did you mean 'revision'", msg)

    def test_every_problem_listed_at_once(self):
        folder = good(('tensor_parallel = "auto"', "tensor_parallel = 3"),
                      ('research_depth = "standard"', 'research_depth = "huge"'), ("port = 8080", "port = true"))
        with self.assertRaises(ConfigError) as e:
            load(folder)
        joined = "\n".join(e.exception.problems)
        self.assertIn("tensor_parallel", joined)
        self.assertIn("research_depth", joined)
        self.assertIn("got a boolean", joined)
        self.assertEqual(len(e.exception.problems), 3)

    def test_syntax_error_names_the_file_and_line(self):
        texts = dict(GOOD)
        texts["settings.toml"] += "\n[limits\n"
        with self.assertRaises(ConfigError) as e:
            load(write_dir(texts))
        self.assertIn("settings.toml", e.exception.problems[0])
        self.assertIn("line", e.exception.problems[0])

    def test_token_value_never_echoed(self):
        with self.assertRaises(ConfigError) as e:
            load(good((TOKEN, "not-a-token-secret123")))
        self.assertNotIn("secret123", str(e.exception))

    def test_remote_code_needs_pinned_commit(self):
        with self.assertRaises(ConfigError) as e:
            load(good(("trust_remote_code = false", "trust_remote_code = true")))
        self.assertIn("40-character commit", e.exception.problems[0])
        ok = good(("trust_remote_code = false", "trust_remote_code = true"),
                  ('revision = "main"', f'revision = "{"0" * 40}"'))
        self.assertTrue(load(ok)["model"]["trust_remote_code"])

    def test_public_rebinds_to_all_interfaces(self):
        self.assertEqual(load(good(("public = false", "public = true")))["server"]["bind"], "0.0.0.0")

    def test_missing_token_is_a_warning_not_an_error(self):
        cfg = load(good((TOKEN, "hf_...")))
        self.assertEqual(cfg["secrets"]["hf_token"], "")
        self.assertTrue(cfg.warnings)

    def test_public_view_drops_every_secret(self):
        cfg = load(good(('api_key = "e2b_..."', 'api_key = "e2b_' + "k" * 30 + '"')))
        public = cfg.public()
        self.assertNotIn("secrets", public)
        self.assertNotIn("api_key", public["code_runner"])               # tool keys stay out too
        self.assertNotIn("e2b_kkk", repr(public))
        self.assertEqual(cfg["code_runner"]["api_key"], "e2b_" + "k" * 30)


class SplitFolder(unittest.TestCase):
    def test_a_key_set_in_two_files_is_an_error(self):
        texts = dict(GOOD)
        texts["settings.toml"] = texts["settings.toml"].replace("[model]", '[model]\nid = "other/model"', 1)
        with self.assertRaises(ConfigError) as e:
            load(write_dir(texts))
        self.assertIn("[model] id: also set in Config/provider.toml", e.exception.problems[0])

    def test_a_key_in_the_wrong_file_works_with_a_warning(self):
        texts = edited(GOOD, (f'hf_token = "{TOKEN}"', ""))
        texts["settings.toml"] += f'\n[secrets]\nhf_token = "{TOKEN}"\n'
        cfg = load(write_dir(texts))
        self.assertEqual(cfg["secrets"]["hf_token"], TOKEN)
        self.assertTrue(any("settings.toml [secrets] hf_token belongs in Config/provider.toml" in w
                            for w in cfg.warnings))

    def test_missing_files_say_how_to_make_them(self):
        folder = good()
        (folder / "tooling.toml").unlink()
        with self.assertRaises(ConfigError) as e:
            load(folder)
        self.assertIn("tooling.toml not found", e.exception.problems[0])
        self.assertIn("configuration.sh", e.exception.problems[0])

    def test_an_old_perseus_toml_beside_it_gets_a_migration_hint(self):
        parent = Path(tempfile.mkdtemp())
        (parent / "Config").mkdir()
        (parent / "perseus.toml").write_text("[model]\nid = \"a/b\"\n")
        with self.assertRaises(ConfigError) as e:
            load(parent / "Config")
        self.assertIn("old perseus.toml", e.exception.problems[0])

    def test_old_key_location_says_where_it_moved(self):
        texts = dict(GOOD)
        texts["provider.toml"] += 'code_runner_api_key = "e2b_x"\n'      # under [secrets], the pre-split spot
        with self.assertRaises(ConfigError) as e:
            load(write_dir(texts))
        self.assertIn("moved to [code_runner] api_key in tooling.toml", e.exception.problems[0])

    def test_a_single_file_still_loads(self):
        flat = load(good()).data
        single = write(dumps({s: {k: v for k, v in keys.items() if v != ""} for s, keys in flat.items()}))
        cfg = load(single)
        self.assertEqual(cfg["model"]["id"], "Qwen/Qwen3-32B")
        self.assertEqual(cfg.files, (single,))


class Overrides(unittest.TestCase):
    def test_split_both_forms(self):
        rest, ov = split_overrides(["check", "--model.id", "a/b", "--model.max_context=32768", "--meta-file", "x"])
        self.assertEqual(rest, ["check", "--meta-file", "x"])
        self.assertEqual(ov, {"model.id": "a/b", "model.max_context": 32768})

    def test_override_wins_and_is_validated(self):
        self.assertEqual(load(good(), {"model.tensor_parallel": 4})["model"]["tensor_parallel"], 4)
        with self.assertRaises(ConfigError):
            load(good(), {"model.tensor_parallel": 5})
        with self.assertRaises(ConfigError):
            load(good(), {"model.nope": 1})

    def test_value_parsing(self):
        self.assertEqual(parse_override_value("true"), True)
        self.assertEqual(parse_override_value("4"), 4)
        self.assertEqual(parse_override_value("Qwen/Qwen3-32B"), "Qwen/Qwen3-32B")


class TomlWriter(unittest.TestCase):
    def test_round_trip(self):
        data = {"a": {"s": 'quo"te', "i": 3, "f": 0.9, "b": True, "l": ["x", "y"], "skip": None}}
        back = tomllib.loads(dumps(data, "header\nline two"))
        self.assertEqual(back, {"a": {"s": 'quo"te', "i": 3, "f": 0.9, "b": True, "l": ["x", "y"]}})


if __name__ == "__main__":
    unittest.main()
