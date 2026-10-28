"""Launching vLLM: the command line, its environment, health and the boot tool-call test.

vLLM runs on 127.0.0.1 with a random per-boot API key in its environment
(argv is visible in `ps`), offline, with no HF token, its caches on the data disk.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from perseus.model.client import LLMClient, LLMError

SECRET_ENV = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_API_TOKEN")
OOM_MARKERS = ("CUDA out of memory", "OutOfMemoryError", "No available memory for the cache blocks",
               "insufficient memory", "larger than the available KV cache", "out of memory")

TOOL_TEST = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current UTC time.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def build_vllm_cmd(r: dict, port: int, fast_start: bool = False) -> list[str]:
    """`vllm serve` flags from the resolved config.

    VERIFY IN PHASE 0 against `vllm serve --help` on the pinned 0.32.0; every
    flag here has existed since vLLM 0.8 but names do get renamed.
    """
    v, m = r["vllm"], r["model"]
    vllm_bin = Path(sys.executable).with_name("vllm")
    cmd = [str(vllm_bin) if vllm_bin.exists() else "vllm", "serve", m["path"],
           "--served-model-name", m["id"],
           "--host", "127.0.0.1", "--port", str(port),
           "--tensor-parallel-size", str(v["tensor_parallel_size"]),
           "--max-model-len", str(v["max_model_len"]),
           "--gpu-memory-utilization", str(v["gpu_memory_utilization"]),
           "--dtype", v["dtype"],
           "--generation-config", "auto",
           "--enable-auto-tool-choice", "--tool-call-parser", v["tool_call_parser"]]
    if v.get("quantization"):
        cmd += ["--quantization", v["quantization"]]
    if v.get("kv_cache_dtype", "auto") != "auto":
        cmd += ["--kv-cache-dtype", v["kv_cache_dtype"]]
    if v.get("cpu_offload_gb"):
        cmd += ["--cpu-offload-gb", f"{v['cpu_offload_gb']:g}"]
    if v.get("reasoning_parser"):
        cmd += ["--reasoning-parser", v["reasoning_parser"]]
    if v.get("trust_remote_code"):
        cmd += ["--trust-remote-code"]
    if fast_start or r.get("backend", {}).get("fast_start"):
        cmd += ["--enforce-eager"]  # skip CUDA graph capture: faster load, slower decode
    return cmd


def vllm_env(api_key: str, run_dir: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update({
        "VLLM_API_KEY": api_key,           # env, not argv: argv is visible in `ps`
        "HF_HUB_OFFLINE": "1",             # the weights are local; vLLM never talks to the Hub
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "VLLM_NO_USAGE_STATS": "1",
        "DO_NOT_TRACK": "1",
    })
    if run_dir is not None:                # compile caches can be GBs: keep them on the data disk
        env["HF_HOME"] = str(run_dir / "hf")
        env["VLLM_CACHE_ROOT"] = str(run_dir / "vllm-cache")
    return env


def explain_crash(log_text: str, r: dict) -> list[str]:
    if any(m in log_text for m in OOM_MARKERS):
        return [f"vLLM ran out of GPU memory at context {r['vllm']['max_model_len']:,}",
                "set [model] max_context lower in perseus.toml, or smaller weights (quantization), "
                "then re-run bash preflight.sh --check"]
    if "parser" in log_text.lower() and "invalid choice" in log_text.lower():
        return ["vLLM rejected the tool or reasoning parser name",
                "set [model] tool_parser / reasoning_parser (see `vllm serve --help`)"]
    return []


def vllm_healthy(port: int, key: str) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def tool_call_check(llm: LLMClient, parser: str) -> tuple[bool, str]:
    """Fail closed: a model whose tool calls come back as plain text can't use any tool."""
    try:
        reply = llm.chat([{"role": "user", "content": "What time is it in UTC? Use the get_time tool."}],
                         tools=[TOOL_TEST], max_tokens=512)
    except LLMError as e:
        return False, f"test request failed: {e}"
    names = [c["function"]["name"] for c in reply.tool_calls]
    if names[:1] == ["get_time"]:
        return True, f"get_time() parsed by the {parser} parser"
    snippet = (reply.content or "").strip().replace("\n", " ")[:160]
    return False, (f"the call wasn't parsed by the {parser!r} parser; model replied: {snippet or '(empty)'}. "
                   "Set [model] tool_parser in perseus.toml")


def precision_label(v: dict) -> str:
    w = str(v.get("weights", ""))
    names = {"bf16": "BF16", "fp16": "FP16", "fp8 (online)": "FP8 (quantized at load)"}
    label = names.get(w) or (w.split(":", 1)[1].upper() + " (from the repo)" if w.startswith("repo:") else w.upper())
    if v.get("kv_cache_dtype") == "fp8":
        label += " · FP8 KV cache"
    if v.get("cpu_offload_gb"):
        label += f" · {v['cpu_offload_gb']:g} GB offloaded to RAM"
    return label


def gpu_memory_used() -> list[int] | None:
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            return [pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(i)).used
                    for i in range(pynvml.nvmlDeviceGetCount())]
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None
