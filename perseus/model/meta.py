"""What preflight learns about a model from the Hub, shared by setup/ (sizing) and perseus/ (parsers)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ModelMeta:
    repo_id: str
    revision_sha: str
    weight_bytes: int                    # sum of the safetensors files we will download
    params_total: int | None             # from safetensors metadata (MoE: total, not active)
    native_dtype: str                    # bf16 | fp16 | fp32 | fp8 | int8 | int4
    quant_method: str | None             # repo is pre-quantized: fp8 | awq | gptq | mxfp4 | ...
    num_layers: int
    num_attention_heads: int
    num_kv_heads: int
    head_dim: int
    max_positions: int
    model_type: str = ""
    architectures: list[str] = field(default_factory=list)
    kv_lora_rank: int | None = None      # MLA (DeepSeek-style) latent KV
    attention_layers: int | None = None  # hybrid models: layers with a KV cache (linear-attention layers have none)
    qk_rope_head_dim: int | None = None
    thinks: bool = False                 # chat template emits <think> blocks
    chat_template_tools: bool = True

    @property
    def is_mla(self) -> bool:
        return bool(self.kv_lora_rank)

    def to_dict(self) -> dict:
        return asdict(self)
