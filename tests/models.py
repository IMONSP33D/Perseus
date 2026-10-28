"""Realistic model shapes and boxes for sizing tests (values from the repos' config.json)."""

from setup.hardware import GIB, GPU, Hardware
from perseus.model.meta import ModelMeta


def box(n, vram_gib, cc=(9, 0), name="H100", interconnect=None, ram_gib=256):
    gpus = [GPU(i, name, int(vram_gib * GIB), cc) for i in range(n)]
    return Hardware(gpus, "580.65.06", "13.0", interconnect or ("single" if n == 1 else "nvlink"),
                    32, int(ram_gib * GIB), [])


RTX4090 = dict(vram_gib=23.99, cc=(8, 9), name="RTX 4090")
L40S = dict(vram_gib=44.99, cc=(8, 9), name="L40S")
A100_80 = dict(vram_gib=79.25, cc=(8, 0), name="A100 80GB")
H100 = dict(vram_gib=79.65, cc=(9, 0), name="H100 80GB")
H200 = dict(vram_gib=139.8, cc=(9, 0), name="H200")


def llama31_8b():
    return ModelMeta("meta-llama/Llama-3.1-8B-Instruct", "a" * 40, 16_060_522_496, 8_030_261_248, "bf16", None,
                     32, 32, 8, 128, 131072, "llama", ["LlamaForCausalLM"])


def qwen25_32b_awq():
    return ModelMeta("Qwen/Qwen2.5-32B-Instruct-AWQ", "b" * 40, 19_340_000_000, None, "int4", "awq",
                     64, 40, 8, 128, 32768, "qwen2", ["Qwen2ForCausalLM"])


def llama33_70b():
    return ModelMeta("meta-llama/Llama-3.3-70B-Instruct", "c" * 40, 141_107_000_000, 70_553_706_496, "bf16", None,
                     80, 64, 8, 128, 131072, "llama", ["LlamaForCausalLM"])


def qwen3_235b():
    return ModelMeta("Qwen/Qwen3-235B-A22B", "d" * 40, 470_190_000_000, 235_093_634_560, "bf16", None,
                     94, 64, 4, 128, 40960, "qwen3_moe", ["Qwen3MoeForCausalLM"], thinks=True)


def deepseek_v3():
    return ModelMeta("deepseek-ai/DeepSeek-V3", "e" * 40, 688_590_000_000, 684_531_386_000, "fp8", "fp8",
                     61, 128, 128, 192, 163840, "deepseek_v3", ["DeepseekV3ForCausalLM"],
                     kv_lora_rank=512, qk_rope_head_dim=64)
