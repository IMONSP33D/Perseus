import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from perseus import download as dl


def fake_hub(write_files: dict[str, int], fail: bool = False):
    """A huggingface_hub stand-in whose snapshot_download writes files of given sizes."""
    mod = types.ModuleType("huggingface_hub")
    calls = {}

    def snapshot_download(repo_id, revision, allow_patterns, local_dir, token, max_workers):
        calls.update(repo_id=repo_id, revision=revision, allow=allow_patterns, token=token)
        if fail:
            raise ConnectionError("network down")
        for name, size in write_files.items():
            p = Path(local_dir) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\0" * size)
        (Path(local_dir) / ".cache").mkdir(exist_ok=True)
        return local_dir

    mod.snapshot_download = snapshot_download
    return mod, calls


class Download(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.files = {"config.json": 10, "model-00001-of-00001.safetensors": 2048}
        self.status = self.tmp / "status.json"

    def run_dl(self, written, fail=False):
        mod, calls = fake_hub(written, fail)
        sys.modules["huggingface_hub"] = mod
        try:
            return dl.download("a/b", "s" * 40, self.files, self.tmp / "m", "hf_tok", self.status, 20,
                               log=lambda *_: None), calls
        finally:
            del sys.modules["huggingface_hub"]

    def test_success_writes_marker_and_requests_exact_files(self):
        _, calls = self.run_dl(self.files)
        self.assertEqual(calls["allow"], sorted(self.files))
        self.assertEqual(calls["revision"], "s" * 40)  # pinned commit, not the branch
        self.assertTrue(dl.is_complete(self.tmp / "m", "s" * 40, self.files))
        self.assertFalse(dl.is_complete(self.tmp / "m", "x" * 40, self.files))
        self.assertFalse((self.tmp / "m" / ".cache").exists())
        self.assertEqual(json.loads(self.status.read_text())["state"], "done")

    def test_size_mismatch_fails_verification(self):
        with self.assertRaises(dl.DownloadError) as e:
            self.run_dl({**self.files, "model-00001-of-00001.safetensors": 100})
        self.assertIn("expected 2048", str(e.exception))
        self.assertFalse((self.tmp / "m" / dl.MARKER).exists())
        self.assertEqual(json.loads(self.status.read_text())["state"], "failed")

    def test_network_error_reported(self):
        with self.assertRaises(dl.DownloadError):
            self.run_dl({}, fail=True)

    def test_scrub_only_removes_matching_token(self):
        home = self.tmp / "hf"
        home.mkdir()
        mine, other = "hf_" + "m" * 34, "hf_" + "o" * 34
        (home / "token").write_text(mine + "\n")
        (home / "stored_tokens").write_text(f"[perseus]\nhf_token = {mine}\n\n[work]\nhf_token = {other}\n")
        removed = dl.scrub_token(mine, home)
        self.assertFalse((home / "token").exists())
        self.assertEqual(len(removed), 2)
        left = (home / "stored_tokens").read_text()
        self.assertNotIn(mine, left)
        self.assertIn(other, left)  # the user's other saved token survives
        self.assertEqual(dl.scrub_token("", home), [])


if __name__ == "__main__":
    unittest.main()
