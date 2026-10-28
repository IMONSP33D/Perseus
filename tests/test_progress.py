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
