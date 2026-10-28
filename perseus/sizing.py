"""Choose tensor-parallel size, quantization, KV-cache dtype and context.

Pure functions over Hardware + ModelMeta + the [model] config: no I/O, so every
rule is unit-tested. The order follows the plan:

  1. weights = what the repo ships (file sizes), or params x 1 byte for online FP8
  2. TP      = smallest of 1/2/4/8 that holds the weights with headroom, where
               headroom means room for 32k tokens of context (or the model's
               limit); if no TP reaches that, the fit with the most context.
               TP must divide the attention heads.
  3. quant   = the repo's own; else bf16 if it fits; else FP8 on Ada/Hopper+;
               else (single GPU, opt-in) CPU offload; else stop and say what would fit
  4. context = the most tokens whose KV cache fits, capped at the model's limit
  5. small-GPU profile on one GPU when that context is under 32k: FP8 KV cache,
     tool-result budget and research depth scaled to the context

Tunables below are starting values for Phase 0 measurements.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .hardware import GIB, Hardware

GPU_MEM_UTIL = 0.90             # vLLM's default share of VRAM it may use
RESERVE_TP1 = 1.5 * GIB         # activations + CUDA graphs on one GPU
RESERVE_PER_EXTRA_GPU = 0.5 * GIB  # NCCL buffers grow with TP
WEIGHT_OVERHEAD = 1.02          # embeddings/norms not split evenly, allocator slack
MIN_CONTEXT = 4096              # below this a tool-calling session is useless
SMALL_GPU_CONTEXT = 32768
FP8_MIN_CC = (8, 9)             # Ada Lovelace / Hopper or newer
PCIE_GBPS = 25.0                # rough host->GPU bandwidth for offload estimates
DEPTHS = {"quick": 5, "standard": 20, "deep": 50}

BYTES_PER_PARAM = {"fp32": 4.0, "bf16": 2.0, "fp16": 2.0, "fp8": 1.0, "int8": 1.0, "int4": 0.5}


class SizingError(Exception):
    def __init__(self, message: str, suggestions: list[str] | None = None):
        self.suggestions = suggestions or []
        super().__init__(message)


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
    qk_rope_head_dim: int | None = None
    thinks: bool = False                 # chat template emits <think> blocks
    chat_template_tools: bool = True

    @property
    def is_mla(self) -> bool:
        return bool(self.kv_lora_rank)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Resolution:
    tensor_parallel: int
    weights: str                  # label: bf16 | fp8 (online) | repo:awq | ...
    quantization: str | None      # vLLM --quantization value; None = let vLLM read the repo
    dtype: str                    # vLLM --dtype
    kv_cache_dtype: str           # auto | fp8
    max_context: int
    context_capacity: int         # tokens the KV cache could hold (before the model cap)
    gpu_memory_utilization: float
    cpu_offload_gb: float
    weight_gib_per_gpu: float
    kv_gib_total: float
    small_gpu: bool
    small_gpu_reason: str
    tool_result_tokens: int | None
    research_lookups: dict[str, int]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def kv_bytes_per_token(meta: ModelMeta, tp: int, bytes_per_value: float) -> float:
    """Total KV bytes one token costs across all TP ranks.

    Standard attention: 2 (K and V) x layers x KV heads x head size x bytes.
    With TP > KV heads, vLLM replicates heads so each rank holds at least one.
    MLA caches one latent per layer, replicated on every rank.
    """
    if meta.is_mla:
        per_rank = meta.num_layers * (meta.kv_lora_rank + (meta.qk_rope_head_dim or 0)) * bytes_per_value
        return per_rank * tp
    heads = max(meta.num_kv_heads, tp)
    return 2 * meta.num_layers * heads * meta.head_dim * bytes_per_value


def tp_candidates(meta: ModelMeta, n_gpus: int, forced: int | str) -> list[int]:
    def ok(t: int) -> bool:
        if meta.num_attention_heads % t:
            return False
        kv = meta.num_kv_heads
        return meta.is_mla or kv % t == 0 or t % kv == 0

    if forced != "auto":
        t = int(forced)
        if t > n_gpus:
            raise SizingError(f"tensor_parallel = {t} but this box has {n_gpus} GPU(s)")
        if not ok(t):
            raise SizingError(f"tensor_parallel = {t} doesn't divide the model's "
                              f"{meta.num_attention_heads} attention / {meta.num_kv_heads} KV heads")
        return [t]
    return [t for t in (1, 2, 4, 8) if t <= n_gpus and ok(t)]


def _usable_per_gpu(hw: Hardware, tp: int) -> float:
    return hw.min_vram_bytes * GPU_MEM_UTIL - RESERVE_TP1 - RESERVE_PER_EXTRA_GPU * (tp - 1)


def _round_ctx(tokens: float) -> int:
    t = int(tokens)
    return t - t % 1024 if t >= 2048 else t


@dataclass
class _Option:
    label: str
    weight_bytes: float
    quantization: str | None
    dtype: str


def weight_options(meta: ModelMeta, hw: Hardware, choice: str) -> tuple[list[_Option], list[str]]:
    """Weight formats to try, in the plan's order, plus notes on what was skipped."""
    notes: list[str] = []
    fp8_ok = hw.min_compute_cap >= FP8_MIN_CC
    if meta.quant_method:
        method = meta.quant_method
        if choice in ("bf16", "fp8", "awq", "gptq") and choice != method:
            raise SizingError(f"quantization = {choice!r} but {meta.repo_id} is already {method}; "
                              "set quantization = \"auto\" or pick a different repo")
        if method == "fp8" and not fp8_ok:
            notes.append(f"repo is FP8 but GPUs are compute {_cc(hw)}; vLLM falls back to a slower "
                         "weight-only FP8 kernel (VERIFY on pinned vLLM)")
        return [_Option(f"repo:{method}", meta.weight_bytes, None, "auto")], notes

    if choice in ("awq", "gptq"):
        raise SizingError(f"quantization = {choice!r} needs a pre-quantized repo; {meta.repo_id} is "
                          f"{meta.native_dtype}", [f"point model.id at a {choice.upper()} build of this model"])
    params = meta.params_total or meta.weight_bytes / BYTES_PER_PARAM.get(meta.native_dtype, 2.0)
    # vLLM loads fp32 checkpoints as bf16; fp16/bf16 load as shipped.
    native = meta.weight_bytes if meta.native_dtype in ("bf16", "fp16") else params * 2
    dtype = "float16" if meta.native_dtype == "fp16" else "bfloat16"
    bf16 = _Option(meta.native_dtype if meta.native_dtype in ("bf16", "fp16") else "bf16", native, None, dtype)
    fp8 = _Option("fp8 (online)", params * 1.0, "fp8", dtype)
    if choice == "bf16":
        return [bf16], notes
    if choice == "fp8":
        if not fp8_ok:
            raise SizingError(f"quantization = \"fp8\" needs compute capability 8.9+ (Ada/Hopper); "
                              f"these GPUs are {_cc(hw)}", ["use bf16, or a pre-quantized AWQ/GPTQ repo"])
        return [fp8], notes
    if not fp8_ok:
        notes.append(f"FP8 skipped: GPUs are compute {_cc(hw)}, FP8 needs 8.9+")
    return [bf16] + ([fp8] if fp8_ok else []), notes


def _cc(hw: Hardware) -> str:
    major, minor = hw.min_compute_cap
    return f"{major}.{minor}"


def resolve(hw: Hardware, meta: ModelMeta, model_cfg: dict) -> Resolution:
    if not hw.gpus:
        raise SizingError("no NVIDIA GPU found; one GPU is the floor")
    n = len(hw.gpus)
    options, notes = weight_options(meta, hw, model_cfg.get("quantization", "auto"))
    tps = tp_candidates(meta, n, model_cfg.get("tensor_parallel", "auto"))
    if not tps:
        raise SizingError(f"no tensor-parallel size from 1/2/4/8 fits {n} GPU(s) and the model's "
                          f"{meta.num_attention_heads} attention heads")
    small_pref = model_cfg.get("small_gpu", "auto")
    min_ctx = min(MIN_CONTEXT, meta.max_positions)
    warnings = list(notes)

    def kv_dtype_for(tp: int, kv_free_total: float) -> tuple[str, bool, str]:
        """Small-GPU decision: one GPU and bf16-KV context under 32k (or the model limit)."""
        ctx16 = kv_free_total / kv_bytes_per_token(meta, tp, 2)
        threshold = min(SMALL_GPU_CONTEXT, meta.max_positions)
        detected = n == 1 and ctx16 < threshold
        if small_pref is True:
            on, why = True, "forced on in perseus.toml"
        elif small_pref is False:
            on, why = False, "forced off in perseus.toml"
        else:
            on = detected
            why = (f"{_k(min(ctx16, meta.max_positions))} tokens of context on 1 × {hw.gpus[0].vram_gib:.0f} GiB"
                   if n == 1 else f"{n} GPUs")
        return ("fp8" if on else "auto"), on, why

    target_ctx = min(SMALL_GPU_CONTEXT, meta.max_positions)  # "headroom": a useful context

    def attempt(offload: float) -> Resolution | None:
        for opt in options:
            fallback = None  # largest-TP fit that falls short of target_ctx
            for tp in tps:
                weights_gpu = (opt.weight_bytes * WEIGHT_OVERHEAD - offload * GIB) / tp
                kv_free = _usable_per_gpu(hw, tp) - weights_gpu
                if kv_free <= 0:
                    continue
                kv_total = kv_free * tp
                kv_dtype, small, why = kv_dtype_for(tp, kv_total)
                per_tok = kv_bytes_per_token(meta, tp, 1 if kv_dtype == "fp8" else 2)
                capacity = int(kv_total / per_tok)
                if capacity < min_ctx:
                    continue
                found = (opt, tp, kv_dtype, small, why, capacity, kv_total, weights_gpu, offload)
                if capacity >= target_ctx:
                    return _finish(*found)
                fallback = found
            if fallback:
                return _finish(*fallback)
        return None

    def _finish(opt, tp, kv_dtype, small, why, capacity, kv_total, weights_gpu, offload) -> Resolution:
        auto_ctx = _round_ctx(min(capacity, meta.max_positions))
        wanted = model_cfg.get("max_context", "auto")
        if wanted == "auto":
            ctx = auto_ctx
        else:
            ctx = int(wanted)
            if ctx > meta.max_positions:
                raise SizingError(f"max_context = {ctx} is above the model's own limit of {meta.max_positions}")
            if ctx > capacity:
                raise SizingError(f"max_context = {ctx} doesn't fit: the KV cache holds about {capacity} "
                                  f"tokens with {opt.label} weights on TP={tp}",
                                  [f"set max_context = {auto_ctx} or \"auto\"", "use more GPUs or smaller weights"])
        if tp > 1 and hw.interconnect == "pcie":
            warnings.append(f"TP={tp} runs over PCIe only (no NVLink): expect slower generation")
        if small and small_pref == "auto":
            ctx16 = _k(min(kv_total / kv_bytes_per_token(meta, tp, 2), meta.max_positions))
            why = (f"{ctx16} tokens of context with a bf16 KV cache on 1 × {hw.gpus[0].vram_gib:.0f} GiB; "
                   f"FP8 KV cache gives {_k(ctx)}")
        tool_cap, lookups = _research_budget(ctx, small)
        return Resolution(
            tensor_parallel=tp, weights=opt.label, quantization=opt.quantization, dtype=opt.dtype,
            kv_cache_dtype=kv_dtype, max_context=ctx, context_capacity=capacity,
            gpu_memory_utilization=GPU_MEM_UTIL, cpu_offload_gb=float(offload),
            weight_gib_per_gpu=round(weights_gpu / GIB, 1), kv_gib_total=round(kv_total / GIB, 1),
            small_gpu=small, small_gpu_reason=why, tool_result_tokens=tool_cap,
            research_lookups=lookups, warnings=warnings)

    res = attempt(0.0)
    if res:
        return res

    offload = float(model_cfg.get("cpu_offload_gb", 0) or 0)
    if offload > 0:
        if n != 1:
            raise SizingError(f"{meta.repo_id} doesn't fit on {hw.summary()}, and cpu_offload_gb is "
                              "for single-GPU boxes only", _suggestions(hw, meta))
        tps[:] = [1]
        need_ram = (offload + 8) * GIB
        if hw.ram_bytes < need_ram:
            raise SizingError(f"cpu_offload_gb = {offload:g} needs about {need_ram / GIB:.0f} GiB of RAM; "
                              f"this box has {hw.ram_bytes / GIB:.0f} GiB")
        res = attempt(offload)
        if res:
            tps_est = PCIE_GBPS / offload
            res.warnings.append(f"CPU offload of {offload:g} GB: every token re-reads those weights over PCIe, "
                                f"so expect at most ~{tps_est:.1f} tokens/s")
            return res

    forced = model_cfg.get("tensor_parallel", "auto")
    where = f"{hw.summary()} with tensor_parallel = {forced}" if forced != "auto" else hw.summary()
    hints = _suggestions(hw, meta)
    if forced != "auto":
        hints.insert(0, "set tensor_parallel = \"auto\" to let preflight pick")
    raise SizingError(f"{meta.repo_id} doesn't fit on {where}", hints)


def _research_budget(ctx: int, small: bool) -> tuple[int | None, dict[str, int]]:
    if not small:
        return None, dict(DEPTHS)
    cap = max(800, min(6000, int(ctx * 0.06)))
    scale = ctx / SMALL_GPU_CONTEXT
    return cap, {k: max(2, min(v, round(v * scale))) for k, v in DEPTHS.items()}


def _suggestions(hw: Hardware, meta: ModelMeta) -> list[str]:
    params = meta.params_total or meta.weight_bytes / BYTES_PER_PARAM.get(meta.native_dtype, 2.0)
    per_gpu = hw.min_vram_bytes * GPU_MEM_UTIL - RESERVE_TP1 - 2 * GIB  # leave room for some KV
    out = []
    for label, bpp in (("bf16", 2.0), ("FP8", 1.0), ("4-bit AWQ/GPTQ", 0.55)):
        need = params * bpp * WEIGHT_OVERHEAD
        gpus = next((t for t in (1, 2, 4, 8) if need / t <= per_gpu - RESERVE_PER_EXTRA_GPU * (t - 1)), None)
        size = f"{hw.gpus[0].vram_gib:.0f} GiB" if hw.gpus else "these"
        if gpus:
            fp8_note = " (needs Ada/Hopper or newer)" if label == "FP8" else ""
            out.append(f"{label}: about {need / GIB:.0f} GiB of weights, fits {gpus} × {size} GPU(s){fp8_note}")
        else:
            out.append(f"{label}: about {need / GIB:.0f} GiB of weights, more than 8 × {size} GPUs")
    if len(hw.gpus) == 1:
        out.append("single GPU: set cpu_offload_gb to spill part of the weights to RAM (much slower)")
    out.append("or choose a smaller model repo")
    return out


def _k(tokens: float) -> str:
    return f"{int(tokens) // 1000}k" if tokens >= 1000 else str(int(tokens))
