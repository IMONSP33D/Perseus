"""The data directory: chosen and recorded by preflight, found by perseus.py, never in the checkout."""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from perseus import EXAMPLE_PATH, ROOT
from perseus.config import ConfigError, Paths, find_paths, load_resolved, pointer_path, record_data_dir
from setup import check_config

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def config_in(folder: Path, data_dir: str = "auto") -> Path:
    text = (EXAMPLE_PATH.read_text().replace('"org/model-name"', '"Qwen/Qwen2.5-32B-Instruct-AWQ"')
            .replace('data_dir = "auto"', f'data_dir = "{data_dir}"'))
    cfg = folder / "perseus.toml"
    cfg.write_text(text)
    return cfg


def run_check(cfg: Path, *extra) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            check_config.main(["--config", str(cfg), "check", "--meta-file",
                               str(FIXTURES / "meta_qwen25_32b_awq.json"), *extra])
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue() + err.getvalue()


class DataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_hw = os.environ.get("PERSEUS_FAKE_HW")
        os.environ["PERSEUS_FAKE_HW"] = str(FIXTURES / "hw_1x4090.json")

    def tearDown(self):
        if self.old_hw is None:
            os.environ.pop("PERSEUS_FAKE_HW", None)
        else:
            os.environ["PERSEUS_FAKE_HW"] = self.old_hw

    def test_layout_and_secret_files(self):
        p = Paths(self.tmp / "data").ensure()
        for d in (p.weights, p.logs, p.searxng, p.run):
            self.assertTrue(d.is_dir())
        self.assertEqual(stat.S_IMODE(p.run.stat().st_mode), 0o700)
        self.assertEqual(p.model_dir("Qwen/Qwen3-32B"), p.data / "weights" / "Qwen" / "Qwen3-32B")
        key = p.write_secret("vllm.key", "k3y")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(key.read_text().strip(), "k3y")

    def test_pointer_round_trip_and_helpful_errors(self):
        cfg = config_in(self.tmp)
        with self.assertRaises(ConfigError) as e:
            find_paths(cfg)
        self.assertIn("run bash preflight.sh", e.exception.problems[0])
        data = self.tmp / "data"
        data.mkdir()
        self.assertEqual(record_data_dir(cfg, data), self.tmp / ".perseus-data")
        self.assertEqual(find_paths(cfg).data, data.resolve())
        self.assertEqual(find_paths(cfg, self.tmp).data, self.tmp.resolve())   # --data wins
        data.rmdir()
        with self.assertRaises(ConfigError) as e:
            find_paths(cfg)
        self.assertIn("gone", e.exception.problems[0])

    def test_check_writes_everything_into_the_data_dir(self):
        data = self.tmp / "nvme" / "perseus"
        cfg = config_in(self.tmp, str(data))
        code, out = run_check(cfg)
        self.assertEqual(code, 0, out)
        paths = find_paths(cfg)
        self.assertEqual(paths.data, data.resolve())
        r = load_resolved(paths)
        self.assertEqual(r["data"]["dir"], str(data.resolve()))
        self.assertEqual(r["model"]["path"], str(data / "weights" / "Qwen" / "Qwen2.5-32B-Instruct-AWQ"))
        self.assertNotIn("hf_", paths.resolved.read_text())        # no secrets in the resolved file
        plan = json.loads((paths.run / "download-plan.json").read_text())
        self.assertEqual(plan["dest"], r["model"]["path"])
        self.assertFalse((ROOT / "perseus.resolved.toml").exists())  # nothing written into the checkout
        # a second run keeps the same data directory
        self.assertEqual(run_check(cfg)[0], 0)
        self.assertEqual(find_paths(cfg).data, data.resolve())

    def test_data_dir_inside_the_checkout_is_refused(self):
        cfg = config_in(self.tmp, str(ROOT / "weights-here"))
        code, out = run_check(cfg)
        self.assertEqual(code, check_config.EXIT["disk"])
        self.assertIn("inside the checkout", out)
        self.assertFalse((ROOT / "weights-here").exists())

    def test_auto_picks_the_fastest_disk_outside_the_checkout(self):
        nvme = self.tmp / "nvme"
        nvme.mkdir()
        hw = json.loads((FIXTURES / "hw_1x4090.json").read_text())
        hw["disks"] = [{"mount": str(nvme), "free_gib": 400, "nvme": True}, {"mount": "/", "free_gib": 30, "nvme": None}]
        fake = self.tmp / "hw.json"
        fake.write_text(json.dumps(hw))
        os.environ["PERSEUS_FAKE_HW"] = str(fake)
        cfg = config_in(self.tmp)
        code, out = run_check(cfg)
        self.assertEqual(find_paths(cfg).data, (nvme / "perseus").resolve())
        self.assertEqual(code, 0, out)
        data = find_paths(cfg).data
        self.assertNotIn(ROOT, data.parents)
        self.assertNotEqual(data, ROOT)
        self.assertTrue(pointer_path(cfg).is_file())


if __name__ == "__main__":
    unittest.main()
