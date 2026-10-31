import unittest

from setup.sizing import SizingError, kv_bytes_per_token, resolve, tp_candidates
from tests.models import (A100_80, H100, H200, L40S, RTX4090, box, deepseek_v3, llama31_8b, llama33_70b,
                          qwen3_235b, qwen25_32b_awq)

AUTO = {"quantization": "auto", "tensor_parallel": "auto", "max_context": "auto",
        "cpu_offload_gb": 0, "small_gpu": "auto"}


def cfg(**kw):
    return {**AUTO, **kw}


class KVMath(unittest.TestCase):
    def test_llama8b_kv_per_token(self):
        # 2 x 32 layers x 8 KV heads x 128 x 2 bytes = 128 KiB
        self.assertEqual(kv_bytes_per_token(llama31_8b(), 1, 2), 131072)

    def test_tp_above_kv_heads_replicates(self):
        # Qwen3-235B has 4 KV heads; at TP=8 each rank still holds one -> 8 heads' worth
        m = qwen3_235b()
        self.assertEqual(kv_bytes_per_token(m, 8, 2), 2 * 94 * 8 * 128 * 2)

    def test_mla_is_one_latent_per_layer_per_rank(self):
        m = deepseek_v3()
        self.assertEqual(kv_bytes_per_token(m, 1, 2), 61 * (512 + 64) * 2)
        self.assertEqual(kv_bytes_per_token(m, 8, 2), 8 * 61 * (512 + 64) * 2)

    def test_tp_must_divide_heads(self):
        m = qwen25_32b_awq()  # 40 heads
        self.assertEqual(tp_candidates(m, 8, "auto"), [1, 2, 4, 8])
        m.num_attention_heads = 28
        m.num_kv_heads = 4
        self.assertEqual(tp_candidates(m, 8, "auto"), [1, 2, 4])
        with self.assertRaises(SizingError):
            tp_candidates(m, 8, 8)
        with self.assertRaises(SizingError):
            tp_candidates(m, 2, 4)  # more TP than GPUs


class SingleGPU(unittest.TestCase):
    def test_8b_on_24gb_is_bf16_without_small_profile(self):
        # ~4.8 GiB left for KV / 128 KiB per token = ~38k tokens: above the 32k line
        r = resolve(box(1, **RTX4090), llama31_8b(), cfg())
        self.assertEqual((r.tensor_parallel, r.weights, r.quantization), (1, "bf16", None))
        self.assertFalse(r.small_gpu)
        self.assertEqual(r.kv_cache_dtype, "auto")
        self.assertTrue(32768 < r.max_context < 45000)
        self.assertEqual(r.max_context % 1024, 0)

    def test_small_gpu_forced_on_doubles_context(self):
        off = resolve(box(1, **RTX4090), llama31_8b(), cfg())
        on = resolve(box(1, **RTX4090), llama31_8b(), cfg(small_gpu=True))
        self.assertEqual(on.kv_cache_dtype, "fp8")
        self.assertAlmostEqual(on.context_capacity / off.context_capacity, 2, delta=0.01)
        self.assertEqual(on.small_gpu_reason, "forced on in Config/settings.toml")

    def test_32b_awq_on_24gb_fits_with_small_profile(self):
        r = resolve(box(1, **RTX4090), qwen25_32b_awq(), cfg())
        self.assertEqual(r.weights, "repo:awq")
        self.assertIsNone(r.quantization)       # vLLM reads the repo's quant config
        self.assertTrue(r.small_gpu)
        self.assertGreaterEqual(r.max_context, 4096)
        self.assertLess(r.max_context, 32768)
        self.assertLess(r.research_lookups["standard"], 20)
        self.assertGreaterEqual(r.research_lookups["quick"], 2)
        self.assertIsNotNone(r.tool_result_tokens)

    def test_small_gpu_forced_off(self):
        r = resolve(box(1, **RTX4090), qwen25_32b_awq(), cfg(small_gpu=False))
        self.assertFalse(r.small_gpu)
        self.assertEqual(r.kv_cache_dtype, "auto")
        self.assertLess(r.max_context, 32768)
        self.assertEqual(r.research_lookups["standard"], 20)

    def test_70b_on_one_24gb_card_stops_with_suggestions(self):
        with self.assertRaises(SizingError) as e:
            resolve(box(1, **RTX4090), llama33_70b(), cfg())
        text = " ".join(e.exception.suggestions)
        self.assertIn("4-bit", text)
        self.assertIn("cpu_offload_gb", text)

    def test_70b_offload_needs_ram_and_warns_slow(self):
        meta = llama33_70b()
        with self.assertRaises(SizingError):
            resolve(box(1, **RTX4090, ram_gib=64), meta, cfg(cpu_offload_gb=130))
        r = resolve(box(1, **RTX4090, ram_gib=256), meta, cfg(cpu_offload_gb=130))
        self.assertEqual(r.cpu_offload_gb, 130)
        self.assertTrue(any("tokens/s" in w for w in r.warnings))

    def test_70b_on_48gb_uses_fp8_on_ada(self):
        # bf16 (131 GiB) can't fit 45 GiB; FP8 (~66 GiB) doesn't either -> stop
        with self.assertRaises(SizingError):
            resolve(box(1, **L40S), llama33_70b(), cfg())

    def test_fp8_skipped_on_ampere(self):
        with self.assertRaises(SizingError) as e:
            resolve(box(1, **A100_80), llama33_70b(), cfg())
        self.assertIn("doesn't fit", str(e.exception))
        with self.assertRaises(SizingError):
            resolve(box(1, **A100_80), llama31_8b(), cfg(quantization="fp8"))


class MultiGPU(unittest.TestCase):
    def test_235b_bf16_takes_all_eight_h100(self):
        r = resolve(box(8, **H100), qwen3_235b(), cfg())
        self.assertEqual((r.tensor_parallel, r.weights), (8, "bf16"))
        self.assertEqual(r.max_context, 40960)  # capped at the model's limit
        self.assertFalse(r.small_gpu)

    def test_235b_fp8_fits_four(self):
        r = resolve(box(4, **H100), qwen3_235b(), cfg())
        self.assertEqual((r.tensor_parallel, r.weights, r.quantization), (4, "fp8 (online)", "fp8"))

    def test_smallest_tp_that_fits(self):
        r = resolve(box(8, **H100), llama31_8b(), cfg())
        self.assertEqual(r.tensor_parallel, 1)
        self.assertFalse(r.small_gpu)  # 8-GPU box: profile is single-GPU only
        # TP=2 holds 70B bf16 but leaves ~17k tokens; TP=4 is the smallest with 32k+ headroom
        r = resolve(box(8, **H100), llama33_70b(), cfg())
        self.assertEqual((r.tensor_parallel, r.weights, r.max_context), (4, "bf16", 131072))
        # on a 2-GPU box TP=2 is the only fit, so it's taken with the shorter context
        r = resolve(box(2, **H100), llama33_70b(), cfg())
        self.assertEqual(r.tensor_parallel, 2)
        self.assertTrue(4096 <= r.max_context < 32768)

    def test_pcie_warning(self):
        r = resolve(box(8, **H100, interconnect="pcie"), llama33_70b(), cfg())
        self.assertTrue(any("PCIe" in w for w in r.warnings))
        r = resolve(box(8, **H100, interconnect="pcie"), llama31_8b(), cfg())
        self.assertFalse(any("PCIe" in w for w in r.warnings))  # TP=1: no interconnect involved

    def test_deepseek_fp8_needs_h200(self):
        with self.assertRaises(SizingError):
            resolve(box(8, **H100), deepseek_v3(), cfg())
        r = resolve(box(8, **H200), deepseek_v3(), cfg())
        self.assertEqual((r.tensor_parallel, r.weights), (8, "repo:fp8"))

    def test_explicit_values(self):
        r = resolve(box(8, **H100), llama33_70b(), cfg(tensor_parallel=4, max_context=65536))
        self.assertEqual((r.tensor_parallel, r.max_context), (4, 65536))
        with self.assertRaises(SizingError):
            resolve(box(8, **H100), llama33_70b(), cfg(max_context=262144))  # above the model limit
        with self.assertRaises(SizingError):
            resolve(box(2, **H100), llama33_70b(), cfg(max_context=131072))  # KV doesn't hold it

    def test_a_fixed_context_picks_the_tp_that_holds_it(self):
        self.assertEqual(resolve(box(2, **RTX4090), llama31_8b(), cfg()).tensor_parallel, 1)
        r = resolve(box(2, **RTX4090), llama31_8b(), cfg(max_context=98304))
        self.assertEqual((r.tensor_parallel, r.max_context), (2, 98304))

    def test_quantization_conflicts(self):
        with self.assertRaises(SizingError):
            resolve(box(1, **RTX4090), qwen25_32b_awq(), cfg(quantization="bf16"))
        with self.assertRaises(SizingError):
            resolve(box(8, **H100), llama33_70b(), cfg(quantization="awq"))

    def test_offload_refused_on_multi_gpu(self):
        with self.assertRaises(SizingError):
            resolve(box(2, **RTX4090), llama33_70b(), cfg(cpu_offload_gb=100))


if __name__ == "__main__":
    unittest.main()
