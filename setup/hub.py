"""Read a model's size and shape from the Hugging Face Hub without downloading weights.

Only small files (config.json, tokenizer_config.json, the safetensors index,
the chat template) are fetched, into a throwaway cache. Everything goes through
`HubClient` so tests can swap in a fake.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from perseus.model.meta import ModelMeta

AUX_FILES = {
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "tokenizer.model", "vocab.json", "merges.txt", "added_tokens.json",
    "chat_template.jinja", "chat_template.json", "preprocessor_config.json", "processor_config.json",
    "model.safetensors.index.json",
}
DTYPES = {"bfloat16": "bf16", "float16": "fp16", "float32": "fp32", "bf16": "bf16", "fp16": "fp16",
          "BF16": "bf16", "F16": "fp16", "F32": "fp32", "F8_E4M3": "fp8", "I8": "int8", "I32": "int4"}


class HubError(Exception):
    def __init__(self, message: str, hint: str = ""):
        self.hint = hint
        super().__init__(message)


@dataclass
class RepoFiles:
    sha: str
    sizes: dict[str, int]        # filename -> bytes
    gated: bool | str
    params_total: int | None
    params_by_dtype: dict[str, int]


class HubClient:
    """Thin adapter over huggingface_hub, so its API surface is in one place."""

    def __init__(self, token: str | None):
        self.token = token or None
        self._tmp = tempfile.TemporaryDirectory(prefix="perseus-hub-")

    def whoami(self) -> str:
        from huggingface_hub import HfApi
        try:
            info = HfApi(token=self.token).whoami()
        except Exception as e:
            raise HubError("HF token rejected by huggingface.co", f"check [secrets] hf_token ({_cls(e)})")
        return info.get("name", "?") if isinstance(info, dict) else "?"

    def files(self, repo: str, revision: str) -> RepoFiles:
        from huggingface_hub import HfApi
        try:
            info = HfApi(token=self.token).model_info(repo, revision=revision, files_metadata=True)
        except Exception as e:
            raise _translate(e, repo, revision)
        sizes = {s.rfilename: int(s.size or 0) for s in (info.siblings or [])}
        st = getattr(info, "safetensors", None)
        params = getattr(st, "parameters", None) or {}
        return RepoFiles(sha=info.sha, sizes=sizes, gated=getattr(info, "gated", False) or False,
                         params_total=getattr(st, "total", None) or (sum(params.values()) or None),
                         params_by_dtype=dict(params))

    def read(self, repo: str, sha: str, filename: str) -> str:
        from huggingface_hub import hf_hub_download
        try:
            path = hf_hub_download(repo, filename, revision=sha, token=self.token, cache_dir=self._tmp.name)
        except Exception as e:
            raise _translate(e, repo, sha)
        return Path(path).read_text()


def _cls(e: Exception) -> str:
    return type(e).__name__


def _translate(e: Exception, repo: str, revision: str) -> HubError:
    name = _cls(e)
    url = f"https://huggingface.co/{repo}"
    if name == "GatedRepoError":
        return HubError(f"{repo} is gated and this token hasn't been granted access",
                        f"accept the terms at {url} with the account that owns the token")
    if name == "RepositoryNotFoundError":
        return HubError(f"{repo} not found (or private and the token can't see it)", f"check model.id; {url}")
    if name == "RevisionNotFoundError":
        return HubError(f"revision {revision!r} not found in {repo}", "check model.revision")
    if name in ("EntryNotFoundError", "RemoteEntryNotFoundError"):
        return HubError(f"a required file is missing from {repo}", str(e).splitlines()[0])
    return HubError(f"couldn't reach the Hub for {repo}: {name}", str(e).splitlines()[0] if str(e) else "")


@dataclass
class Inspection:
    meta: ModelMeta
    download: dict[str, int]      # exact files to fetch -> bytes
    remote_code_files: list[str]
    chat_template: str
    warnings: list[str]


def inspect(client: HubClient, repo: str, revision: str, trust_remote_code: bool) -> Inspection:
    rf = client.files(repo, revision)
    names = set(rf.sizes)
    warnings: list[str] = []

    weights = _weight_files(client, repo, rf)
    config = json.loads(client.read(repo, rf.sha, "config.json"))
    text = {**config, **(config.get("text_config") or {})}  # multimodal repos nest the LM config, often partially

    remote = sorted(n for n in names if n.endswith(".py"))
    if config.get("auto_map"):
        if not trust_remote_code:
            raise HubError(f"{repo} needs custom code to load ({', '.join(remote) or 'auto_map in config.json'})",
                           "read those files, then set trust_remote_code = true with revision pinned to a commit")
        warnings.append(f"trust_remote_code: will run {', '.join(remote)} from {repo}@{rf.sha[:12]}")
    else:
        remote = []

    template = _chat_template(client, repo, rf.sha, names)
    if not template:
        raise HubError(f"{repo} has no chat template", "tool calling needs one; pick an instruct/chat repo")
    tools_ok = "tools" in template
    if not tools_ok:
        warnings.append("chat template never mentions tools: tool calling may not work with this repo")

    quant = _quant_method(config)
    try:
        meta = _meta(repo, rf, config, text, quant, template, sum(rf.sizes[w] for w in weights))
    except KeyError as e:
        raise HubError(f"{repo}: config.json lacks {e.args[0]!r}, which sizing needs",
                       "pick a standard transformers repo, or report the model so its layout can be supported")

    download = {n: rf.sizes[n] for n in names if n in AUX_FILES}
    download.update({w: rf.sizes[w] for w in weights})
    download.update({r: rf.sizes[r] for r in remote})
    return Inspection(meta, download, remote, template, warnings)


def _meta(repo: str, rf: RepoFiles, config: dict, text: dict, quant: str | None, template: str,
          weight_bytes: int) -> ModelMeta:
    return ModelMeta(
        repo_id=repo,
        revision_sha=rf.sha,
        weight_bytes=weight_bytes,
        params_total=rf.params_total,
        native_dtype=_native_dtype(config, rf, quant),
        quant_method=quant,
        num_layers=int(text["num_hidden_layers"]),
        attention_layers=_attention_layers(text),
        num_attention_heads=int(text["num_attention_heads"]),
        num_kv_heads=int(text.get("num_key_value_heads") or text["num_attention_heads"]),
        head_dim=int(text.get("head_dim") or text["hidden_size"] // text["num_attention_heads"]),
        max_positions=int(text.get("max_position_embeddings") or config.get("max_position_embeddings") or 4096),
        model_type=str(text.get("model_type") or config.get("model_type") or ""),
        architectures=list(config.get("architectures") or []),
        kv_lora_rank=text.get("kv_lora_rank"),
        qk_rope_head_dim=text.get("qk_rope_head_dim"),
        thinks="<think>" in template or "enable_thinking" in template,
        chat_template_tools="tools" in template,
    )


def _weight_files(client: HubClient, repo: str, rf: RepoFiles) -> list[str]:
    """Exactly the safetensors shards the model loads; never .bin/.pth/original/ duplicates."""
    if "model.safetensors.index.json" in rf.sizes:
        index = json.loads(client.read(repo, rf.sha, "model.safetensors.index.json"))
        files = sorted(set(index.get("weight_map", {}).values()))
        missing = [f for f in files if f not in rf.sizes]
        if missing:
            raise HubError(f"{repo}'s safetensors index lists files that aren't in the repo: {missing[:3]}")
        return files
    if "model.safetensors" in rf.sizes:
        return ["model.safetensors"]
    root = sorted(n for n in rf.sizes if n.endswith(".safetensors") and "/" not in n
                  and not n.startswith("consolidated"))
    if root:
        return root
    other = [n for n in rf.sizes if n.endswith((".bin", ".pth", ".gguf"))]
    raise HubError(f"{repo} has no safetensors weights" + (f" (only {other[0].rsplit('.', 1)[1]} files)" if other else ""),
                   "GGUF files are for llama.cpp; vLLM needs the safetensors release of the model: often the "
                   "same name without -GGUF, or the FP8/base model the GGUF card says it was converted from")


def _chat_template(client: HubClient, repo: str, sha: str, names: set[str]) -> str:
    if "chat_template.jinja" in names:
        return client.read(repo, sha, "chat_template.jinja")
    if "chat_template.json" in names:
        data = json.loads(client.read(repo, sha, "chat_template.json"))
        return data.get("chat_template", "") if isinstance(data, dict) else ""
    if "tokenizer_config.json" in names:
        tpl = json.loads(client.read(repo, sha, "tokenizer_config.json")).get("chat_template", "")
        if isinstance(tpl, list):  # named templates: [{"name": "default", "template": ...}, ...]
            tpl = next((t.get("template", "") for t in tpl if t.get("name") in ("tool_use", "default")), "")
        return tpl or ""
    return ""


def _attention_layers(text: dict) -> int | None:
    """Full-attention layer count for hybrid models (Qwen3-Next / Qwen3.5 style); None for ordinary ones."""
    types = text.get("layer_types")
    if isinstance(types, list) and types and any("linear" in str(t) for t in types):
        return sum(1 for t in types if str(t) in ("full_attention", "attention"))
    interval = text.get("full_attention_interval")
    if isinstance(interval, int) and interval > 1:
        return int(text["num_hidden_layers"]) // interval
    return None


def _quant_method(config: dict) -> str | None:
    q = config.get("quantization_config") or (config.get("text_config") or {}).get("quantization_config")
    if not q:
        return None
    method = str(q.get("quant_method", "")).lower()
    if method == "compressed-tensors":
        groups = q.get("config_groups") or {}
        w = next(iter(groups.values()), {}).get("weights", {}) if groups else {}
        bits, kind = w.get("num_bits"), w.get("type")
        if bits == 8 and kind == "float":
            return "fp8"
        if bits == 4:
            return "int4"
        return "compressed-tensors"
    return method or "unknown"


def _native_dtype(config: dict, rf: RepoFiles, quant: str | None) -> str:
    if quant in ("fp8",):
        return "fp8"
    if quant in ("awq", "gptq", "int4", "mxfp4", "bitsandbytes"):
        return "int4"
    raw = config.get("torch_dtype") or config.get("dtype") or (config.get("text_config") or {}).get("torch_dtype")
    if raw in DTYPES:
        return DTYPES[raw]
    if rf.params_by_dtype:
        top = max(rf.params_by_dtype, key=rf.params_by_dtype.get)
        return DTYPES.get(top, "bf16")
    return "bf16"
