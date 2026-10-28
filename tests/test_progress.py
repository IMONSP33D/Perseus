"""The live download panel and per-file progress."""

import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from perseus.progress import LivePanel, bar, fit, render, spark, summary_line, visible_len
from setup import download as dl

GB = 1000 ** 3


def snapshot(**over):
    files = [{"name": f"model-0000{i}-of-00007.safetensors", "size": 4 * GB, "have": h, "state": s}
             for i, (h, s) in enumerate([(4 * GB, "done"), (3 * GB, "active"), (GB, "active"), (0, "waiting")], 1)]
    st = {"repo": "orcarouter/Qwen3.8-27B-Uncensored-FP8", "sha": "f3a9c1e2b4d5" + "0" * 28,
          "dest": "/mnt/nvme/perseus/weights/orcarouter/Qwen3.8-27B-Uncensored-FP8", "state": "downloading",
          "bytes": 8 * GB, "total": 16 * GB, "rate": 1.4 * GB, "rate_avg": 1.2 * GB, "eta": 6.0, "elapsed": 7.0,
          "files": files, "history": [0.5 * GB, GB, 1.4 * GB], "disk_free": 400 * 1024 ** 3, "disk_mount": "/mnt/nvme"}
    st.update(over)
    return st


class Render(unittest.TestCase):
    def test_bar_and_spark(self):
        self.assertEqual("".join(bar(0.5, 10)), "█████░░░░░")
        self.assertEqual(len("".join(bar(0.33, 17))), 17)
        self.assertEqual("".join(bar(1.0, 4)), "████")
        self.assertEqual(spark([0, 5, 10], 3), "▁▄█")

    def test_lines_fit_every_width(self):
        for width in (56, 64, 80, 100, 140):
            for line in render(snapshot(), width, color=True):
                self.assertLessEqual(visible_len(line), max(56, min(width, 110)), (width, line))

    def test_content(self):
        text = "\n".join(render(snapshot(), 100, color=False))
        self.assertIn("50.0%", text)
        self.assertIn("8.0 / 16.0 GB", text)
        self.assertIn("1.40 GB/s now", text)
        self.assertIn("files 1 of 4 done", text)
        self.assertIn("model-00002-of-00007.safetensors", text)
        self.assertIn("· 1 waiting", text)
        self.assertIn("Ctrl-C pauses", text)
        self.assertIn("Ctrl-C stops waiting", "\n".join(render(snapshot(), 100, False, viewer=True)))
        narrow = "\n".join(render(snapshot(), 56, color=False))
        self.assertIn("00002", narrow)                 # shard numbers survive shortening

    def test_states(self):
        self.assertIn("verifying", "\n".join(render(snapshot(state="verifying"), 80, False)))
        self.assertIn("✓ downloaded and verified", "\n".join(render(snapshot(state="done"), 80, False)))
        self.assertIn("✗ download failed: verify", "\n".join(render(snapshot(state="failed", error="verify"), 80, False)))
        self.assertIn("! slow", "\n".join(render(snapshot(warning="slow"), 80, False)))

    def test_fit_keeps_colour_codes_balanced(self):
        line = "\033[1m" + "x" * 50 + "\033[0m tail"
        out = fit(line, 20)
        self.assertEqual(visible_len(out), 20)
        self.assertTrue(out.endswith("\033[0m"))

    def test_summary_line(self):
        self.assertEqual(summary_line(snapshot()), "  8.0 / 16.0 GB (50%) · 1.40 GB/s · ETA 0:06 · 1/4 files")

    def test_live_redraw_moves_up_and_plain_mode_throttles(self):
        term = io.StringIO()
        p = LivePanel(term)
        p.width = lambda: 100
        p.update(snapshot())
        h = p._height
        p.update(snapshot(bytes=9 * GB))
        self.assertIn(f"\033[{h}F", term.getvalue())       # second frame redraws over the first
        p.close()
        self.assertTrue(term.getvalue().endswith("\033[?25h"))   # cursor restored
        out = io.StringIO()
        plain = LivePanel(None, plain=out, plain_every=3600)
        plain.update(snapshot())
        plain.update(snapshot())
        self.assertEqual(out.getvalue().count("\n"), 1)


class FileProgress(unittest.TestCase):
    def test_states_from_disk(self):
        d = Path(tempfile.mkdtemp())
        files = {"a.safetensors": 100, "sub/b.safetensors": 200, "c.safetensors": 300}
        (d / "a.safetensors").write_bytes(b"x" * 100)
        inc = d / ".cache/huggingface/download/sub/b.safetensors.e7a1.incomplete"
        inc.parent.mkdir(parents=True)
        inc.write_bytes(b"x" * 50)
        fp = {f["name"]: (f["state"], f["have"]) for f in dl.file_progress(d, files)}
        self.assertEqual(fp, {"a.safetensors": ("done", 100), "sub/b.safetensors": ("active", 50),
                              "c.safetensors": ("waiting", 0)})


    def test_hub_2_3_temp_names(self):
        # huggingface_hub 2.3: <file>.<etag>.<random8>.incomplete; the index must not claim the shard's bytes
        d = Path(tempfile.mkdtemp())
        files = {"model.safetensors": 300, "model.safetensors.index.json": 40}
        root = d / ".cache/huggingface/download"
        root.mkdir(parents=True)
        (root / "model.safetensors.e7a1.1a2b3c4d.incomplete").write_bytes(b"x" * 120)
        (root / "model.safetensors.index.json.9f00.5e6f7a8b.incomplete").write_bytes(b"x" * 7)
        fp = {f["name"]: (f["state"], f["have"]) for f in dl.file_progress(d, files)}
        self.assertEqual(fp, {"model.safetensors": ("active", 120), "model.safetensors.index.json": ("active", 7)})


class HubProgress(unittest.TestCase):
    """Xet keeps chunks in memory and writes each file at the end: progress must come from the hub's counters."""

    def test_progress_while_nothing_is_on_disk(self):
        seen = {}
        release = threading.Event()

        def snapshot_download(repo_id, revision, allow_patterns, local_dir, token, max_workers, tqdm_class):
            # the same bars, in the same way, as huggingface_hub 2.3's snapshot_download
            transfer = tqdm_class(desc="Downloading bytes", total=0, initial=0, unit="B")
            written = tqdm_class(desc="Reconstructing (incomplete total...)", total=0, initial=0, unit="B")
            with tqdm_class(total=1, desc="Fetching 1 files") as files_bar:
                for _ in range(8):
                    transfer.update(GB // 8)
                    written.update(GB // 10)
                    written.set_postfix_str("x", refresh=False)
                    time.sleep(0.1)
                time.sleep(1.2)   # status.json is rewritten once a second
                seen["mid"] = __import__("json").loads(status.read_text())
                release.wait(5)
                p = Path(local_dir) / "m.safetensors"
                with open(p, "wb") as f:
                    f.truncate(GB)
                files_bar.update(1)
            return local_dir

        mod = types.ModuleType("huggingface_hub")
        mod.snapshot_download = snapshot_download
        sys.modules["huggingface_hub"] = mod
        d = Path(tempfile.mkdtemp())
        status = d / "status.json"
        threading.Timer(3.0, release.set).start()
        try:
            dl.download("a/b", "s" * 40, {"m.safetensors": GB}, d / "m", None, status, 20, panel=None, tick=0.05)
        finally:
            del sys.modules["huggingface_hub"]
        mid = seen["mid"]
        self.assertEqual(mid["state"], "downloading")
        self.assertGreater(mid["bytes"], 0.5 * GB)          # from the hub, though no file is on disk yet
        self.assertGreater(mid["rate"], 0)
        self.assertIsNotNone(mid["eta"])
        self.assertGreater(mid["received"], 0.5 * GB)
        self.assertLess(mid["bytes"], GB)


class Pause(unittest.TestCase):
    def test_ctrl_c_pauses_and_records_it(self):
        mod = types.ModuleType("huggingface_hub")
        mod.snapshot_download = lambda **kw: time.sleep(30)
        sys.modules["huggingface_hub"] = mod
        d = Path(tempfile.mkdtemp())
        status = d / "status.json"
        t = threading.Timer(0.6, lambda: __import__("_thread").interrupt_main())
        t.start()
        try:
            with self.assertRaises(dl.Paused):
                dl.download("a/b", "s" * 40, {"x.safetensors": 10}, d / "m", None, status, 20, panel=None)
        finally:
            del sys.modules["huggingface_hub"]
        import json
        self.assertEqual(json.loads(status.read_text())["state"], "paused")


if __name__ == "__main__":
    unittest.main()
