import tempfile
import tomllib
import unittest
from pathlib import Path

from perseus import EXAMPLE_PATH, tomlw
from perseus.config import ConfigError, load, parse_override_value, split_overrides

GOOD = EXAMPLE_PATH.read_text().replace('"org/model-name"', '"Qwen/Qwen3-32B"').replace(
    '"hf_..."', '"hf_' + "a" * 34 + '"')


def write(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class Load(unittest.TestCase):
    def test_example_with_model_filled_in_is_valid(self):
        cfg = load(write(GOOD))
        self.assertEqual(cfg["model"]["id"], "Qwen/Qwen3-32B")
        self.assertEqual(cfg["model"]["tensor_parallel"], "auto")
        self.assertEqual(cfg["limits"]["warn_download_minutes"], 20)

    def test_untouched_example_names_the_missing_model(self):
        with self.assertRaises(ConfigError) as e:
            load(EXAMPLE_PATH)
        self.assertTrue(any("[model] id" in p and "repo id" in p for p in e.exception.problems))

    def test_unknown_key_reports_line_and_suggestion(self):
        text = GOOD.replace("revision = ", "revison = ")
        with self.assertRaises(ConfigError) as e:
            load(write(text))
        line = next(n for n, l in enumerate(text.splitlines(), 1) if l.startswith("revison"))
        msg = e.exception.problems[0]
        self.assertIn(f":{line} [model] revison: unknown key", msg)
        self.assertIn("did you mean 'revision'", msg)

    def test_every_problem_listed_at_once(self):
        text = GOOD.replace('tensor_parallel = "auto"', "tensor_parallel = 3").replace(
            'research_depth = "standard"', 'research_depth = "huge"').replace("port = 8080", "port = true")
        with self.assertRaises(ConfigError) as e:
            load(write(text))
        joined = "\n".join(e.exception.problems)
        self.assertIn("tensor_parallel", joined)
        self.assertIn("research_depth", joined)
        self.assertIn("got a boolean", joined)
        self.assertEqual(len(e.exception.problems), 3)

    def test_syntax_error_has_line(self):
        with self.assertRaises(ConfigError) as e:
            load(write(GOOD + "\n[limits\n"))
        self.assertIn("line", e.exception.problems[0])

    def test_token_value_never_echoed(self):
        with self.assertRaises(ConfigError) as e:
            load(write(GOOD.replace('"hf_' + "a" * 34 + '"', '"not-a-token-secret123"')))
        self.assertNotIn("secret123", str(e.exception))

    def test_remote_code_needs_pinned_commit(self):
        with self.assertRaises(ConfigError) as e:
            load(write(GOOD.replace("trust_remote_code = false", "trust_remote_code = true")))
        self.assertIn("40-character commit", e.exception.problems[0])
        ok = GOOD.replace("trust_remote_code = false", "trust_remote_code = true").replace(
            'revision = "main"', f'revision = "{"0" * 40}"')
        self.assertTrue(load(write(ok))["model"]["trust_remote_code"])

    def test_public_rebinds_to_all_interfaces(self):
        self.assertEqual(load(write(GOOD.replace("public = false", "public = true")))["server"]["bind"], "0.0.0.0")

    def test_missing_token_is_a_warning_not_an_error(self):
        cfg = load(write(GOOD.replace('"hf_' + "a" * 34 + '"', '"hf_..."')))
        self.assertEqual(cfg["secrets"]["hf_token"], "")
        self.assertTrue(cfg.warnings)

    def test_public_view_drops_secrets(self):
        self.assertNotIn("secrets", load(write(GOOD)).public())


class Overrides(unittest.TestCase):
    def test_split_both_forms(self):
        rest, ov = split_overrides(["check", "--model.id", "a/b", "--model.max_context=32768", "--meta-file", "x"])
        self.assertEqual(rest, ["check", "--meta-file", "x"])
        self.assertEqual(ov, {"model.id": "a/b", "model.max_context": 32768})

    def test_override_wins_and_is_validated(self):
        self.assertEqual(load(write(GOOD), {"model.tensor_parallel": 4})["model"]["tensor_parallel"], 4)
        with self.assertRaises(ConfigError):
            load(write(GOOD), {"model.tensor_parallel": 5})
        with self.assertRaises(ConfigError):
            load(write(GOOD), {"model.nope": 1})

    def test_value_parsing(self):
        self.assertEqual(parse_override_value("true"), True)
        self.assertEqual(parse_override_value("4"), 4)
        self.assertEqual(parse_override_value("Qwen/Qwen3-32B"), "Qwen/Qwen3-32B")


class TomlWriter(unittest.TestCase):
    def test_round_trip(self):
        data = {"a": {"s": 'quo"te', "i": 3, "f": 0.9, "b": True, "l": ["x", "y"], "skip": None}}
        back = tomllib.loads(tomlw.dumps(data, "header\nline two"))
        self.assertEqual(back, {"a": {"s": 'quo"te', "i": 3, "f": 0.9, "b": True, "l": ["x", "y"]}})


if __name__ == "__main__":
    unittest.main()
