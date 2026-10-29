import tempfile
import unittest
from pathlib import Path

from setup.hardware import GIB, Disk, HardwareError, driver_ok, parse_smi_query, parse_topology, pick_data_dir
from perseus.model.launch import build_vllm_cmd, explain_crash, vllm_env

SMI = """0, NVIDIA H100 80GB HBM3, 81559, 580.65.06, 9.0
1, NVIDIA H100 80GB HBM3, 81559, 580.65.06, 9.0
"""
TOPO_NV = """\tGPU0\tGPU1\tGPU2\tCPU Affinity
GPU0\t X \tNV18\tNV18\t0-47
GPU1\tNV18\t X \tNV18\t0-47
GPU2\tNV18\tNV18\t X \t0-47
"""
TOPO_PCIE = TOPO_NV.replace("NV18", "PHB")

RESOLVED = {
    "model": {"id": "Qwen/Qwen3-32B", "path": "/data/perseus-models/Qwen--Qwen3-32B", "revision_sha": "f" * 40},
    "vllm": {"tensor_parallel_size": 2, "max_model_len": 40960, "gpu_memory_utilization": 0.9,
             "dtype": "bfloat16", "quantization": "", "kv_cache_dtype": "auto", "cpu_offload_gb": 0.0,
             "tool_call_parser": "hermes", "reasoning_parser": "qwen3", "trust_remote_code": False},
    "backend": {"fast_start": False},
}


class Hardware(unittest.TestCase):
    def test_smi_query(self):
        gpus, driver = parse_smi_query(SMI)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[1].compute_cap, (9, 0))
        self.assertAlmostEqual(gpus[0].vram_gib, 79.6, places=1)
        self.assertEqual(driver, "580.65.06")

    def test_topology(self):
        self.assertEqual(parse_topology(TOPO_NV, 3), "nvlink")
        self.assertEqual(parse_topology(TOPO_PCIE, 3), "pcie")
        self.assertEqual(parse_topology(TOPO_NV.replace("NV18\t X \tNV18", "PHB\t X \tNV18", 1), 3), "partial-nvlink")

    def test_driver_compare(self):
        self.assertTrue(driver_ok("580.65.06", "580.0"))
        self.assertTrue(driver_ok("590.1", "580.0"))
        self.assertFalse(driver_ok("570.172.08", "580.0"))

    def test_data_dir_prefers_nvme_and_never_lands_in_the_checkout(self):
        root = Path(tempfile.mkdtemp()) / "perseus"
        root.mkdir()
        disks = [Disk("/", 900 * GIB, None), Disk("/mnt/nvme", 500 * GIB, True), Disk("/mnt/hdd", 4000 * GIB, False)]
        path, d = pick_data_dir(disks, 400 * GIB, root)
        self.assertEqual((str(path), d.mount), ("/mnt/nvme/perseus", "/mnt/nvme"))
        path, d = pick_data_dir(disks, 700 * GIB, root)  # NVMe too small: most free space next
        self.assertEqual(d.mount, "/mnt/hdd")
        path, _ = pick_data_dir([Disk("/", 900 * GIB, None)], 100 * GIB, root)
        self.assertEqual(path, root.parent / "perseus-data")    # beside the checkout, not in it
        with self.assertRaises(HardwareError):
            pick_data_dir(disks, 5000 * GIB, root)


class Launcher(unittest.TestCase):
    def test_vllm_command(self):
        cmd = build_vllm_cmd(RESOLVED, 41234)
        s = " ".join(cmd)
        self.assertEqual(cmd[1:3], ["serve", "/data/perseus-models/Qwen--Qwen3-32B"])
        for part in ("--host 127.0.0.1", "--port 41234", "--tensor-parallel-size 2", "--max-model-len 40960",
                     "--tool-call-parser hermes", "--reasoning-parser qwen3", "--enable-auto-tool-choice",
                     "--served-model-name Qwen/Qwen3-32B"):
            self.assertIn(part, s)
        for absent in ("--quantization", "--kv-cache-dtype", "--cpu-offload-gb", "--trust-remote-code",
                       "--enforce-eager", "--api-key"):
            self.assertNotIn(absent, s)

    def test_vllm_command_options(self):
        r = {**RESOLVED, "vllm": {**RESOLVED["vllm"], "quantization": "fp8", "kv_cache_dtype": "fp8",
                                  "cpu_offload_gb": 40.0, "reasoning_parser": ""}}
        s = " ".join(build_vllm_cmd(r, 1, fast_start=True))
        for part in ("--quantization fp8", "--kv-cache-dtype fp8", "--cpu-offload-gb 40", "--enforce-eager"):
            self.assertIn(part, s)
        self.assertNotIn("--reasoning-parser", s)

    def test_env_has_key_offline_and_no_token(self):
        import os
        os.environ["HF_TOKEN"] = "hf_shouldnotleak"
        try:
            env = vllm_env("k3y")
        finally:
            del os.environ["HF_TOKEN"]
        self.assertNotIn("HF_TOKEN", env)
        self.assertEqual((env["VLLM_API_KEY"], env["HF_HUB_OFFLINE"]), ("k3y", "1"))
        self.assertNotIn("hf_shouldnotleak", " ".join(env.values()))

    def test_oom_explained(self):
        hint = explain_crash("torch.OutOfMemoryError: CUDA out of memory", RESOLVED)
        self.assertIn("max_context", " ".join(hint))


if __name__ == "__main__":
    unittest.main()


class MaxNumSeqs(unittest.TestCase):
    LOG = ("(EngineCore pid=11668) ERROR 10-29 15:11:44 [core.py:1510] ValueError: max_num_seqs (1024) exceeds "
           "available Mamba cache blocks (637). Each decode sequence requires one Mamba cache block, so CUDA graph "
           "capture cannot proceed. Please lower max_num_seqs to at most 637 or increase gpu_memory_utilization.")

    def test_limit_read_from_vllms_error(self):
        from perseus.model.launch import seqs_limit
        self.assertEqual(seqs_limit(self.LOG), 637)
        self.assertEqual(seqs_limit("ValueError: max_num_seqs (64) exceeds available Mamba cache blocks (40)."), 40)
        self.assertIsNone(seqs_limit("torch.OutOfMemoryError: CUDA out of memory"))

    def test_remedy_only_when_it_changes_something(self):
        from perseus.model.launch import remedy
        r = {"vllm": {}}
        self.assertEqual(remedy(self.LOG, ["vllm", "--max-num-seqs", "1024"], r)[0], ["vllm", "--max-num-seqs", "637"])
        self.assertEqual(remedy(self.LOG, ["vllm"], {"vllm": {}})[0], ["vllm", "--max-num-seqs", "637"])
        self.assertIsNone(remedy(self.LOG, ["vllm", "--max-num-seqs", "16"], {"vllm": {}}))
        self.assertIsNone(remedy("ValueError: something else", ["vllm"], {"vllm": {}}))
        self.assertIsNone(remedy("lower max_num_seqs to at most 0", ["vllm"], {"vllm": {}}))

    def test_explained_in_plain_words(self):
        from perseus.model.launch import explain_crash
        hints = explain_crash(self.LOG, {"vllm": {"max_model_len": 262144}})
        self.assertIn("637 parallel requests", hints[0])
        hints = explain_crash("lower max_num_seqs to at most 0", {"vllm": {"max_model_len": 262144}})
        self.assertIn("max_context lower", hints[1])


class DefaultSeqs(unittest.TestCase):
    def test_one_user_needs_few_parallel_requests(self):
        cmd = build_vllm_cmd(RESOLVED, 41234)
        self.assertEqual(cmd[cmd.index("--max-num-seqs") + 1], "16")
        r = {**RESOLVED, "vllm": {**RESOLVED["vllm"], "max_num_seqs": 637}}   # a remedy recorded earlier
        cmd = build_vllm_cmd(r, 41234)
        self.assertEqual(cmd[cmd.index("--max-num-seqs") + 1], "637")
