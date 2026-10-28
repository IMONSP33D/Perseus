"""vLLM crash logs: show the real error, not the 'see root cause above' wrapper."""

import tempfile
import unittest
from pathlib import Path

from perseus.supervisor import cause_excerpt, last_run, root_cause

LOG = """$ vllm serve old-attempt
ValueError: from an earlier attempt
$ vllm serve /data/weights/m
INFO 10-28 10:01:02 [core.py:90] Initializing a V1 LLM engine
(EngineCore_DP0 pid=4211) ERROR 10-28 10:01:40 [core.py:700] EngineCore failed to start.
(EngineCore_DP0 pid=4211) Traceback (most recent call last):
(EngineCore_DP0 pid=4211)   File "/x/vllm/v1/engine/core.py", line 691, in run_engine_core
(EngineCore_DP0 pid=4211)     raise ValueError(msg)
(EngineCore_DP0 pid=4211) ValueError: To serve at least one request with the models's max seq len (131072), (8.0 GiB KV cache is needed, which is larger than the available KV cache memory (0.4 GiB).
Traceback (most recent call last):
  File "/x/vllm/v1/engine/utils.py", line 1431, in wait_for_engine_startup
    raise RuntimeError(
RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}
"""


class RootCause(unittest.TestCase):
    def test_finds_the_engine_core_error_in_this_attempt(self):
        p = Path(tempfile.mkdtemp()) / "vllm.log"
        p.write_text(LOG)
        run = last_run(p)
        self.assertNotIn("earlier attempt", run)
        cause = root_cause(run)
        self.assertTrue(cause.startswith("ValueError: To serve at least one request"), cause)
        self.assertIn("EngineCore failed to start", cause_excerpt(run, cause))

    def test_wrapper_only_falls_back_to_it(self):
        self.assertEqual(root_cause("RuntimeError: Engine core initialization failed. See root cause above."),
                         "RuntimeError: Engine core initialization failed. See root cause above.")
        self.assertEqual(root_cause("all fine\n"), "")

    def test_dotted_exception_names(self):
        self.assertEqual(root_cause("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB"),
                         "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB")

    def test_kv_cache_hint(self):
        from perseus.model.launch import explain_crash
        hints = explain_crash(LOG, {"vllm": {"max_model_len": 131072}})
        self.assertIn("KV cache", hints[0])


if __name__ == "__main__":
    unittest.main()
