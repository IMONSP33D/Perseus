"""Pick vLLM's tool-call and reasoning parsers for a model family.

VERIFY IN PHASE 0: these names match vLLM releases up to mid-2026; check them
against `vllm serve --help` (--tool-call-parser / --reasoning-parser choices)
for the pinned 0.32.0. The boot tool-call test catches a wrong pick either way,
and [model] tool_parser / reasoning_parser in perseus.toml override auto.
"""

from __future__ import annotations

from dataclasses import dataclass

from .sizing import ModelMeta


@dataclass(frozen=True)
class Rule:
    match: tuple[str, ...]        # substrings of model_type, architecture or repo id (lowercase)
    tool: str
    reasoning: str | None = None  # used only when the chat template thinks


# First match wins: put specific families above general ones.
RULES: tuple[Rule, ...] = (
    Rule(("qwen3_coder", "qwen3-coder"), "qwen3_coder"),
    Rule(("qwen3",), "hermes", "qwen3"),
    Rule(("qwq",), "hermes", "deepseek_r1"),
    Rule(("qwen2",), "hermes"),
    Rule(("gpt_oss", "gpt-oss"), "openai", "openai_gptoss"),
    Rule(("deepseek_v32", "deepseek-v3.2"), "deepseek_v32", "deepseek_r1"),
    Rule(("deepseek-v3.1", "deepseek_v31"), "deepseek_v31", "deepseek_r1"),
    Rule(("deepseek_v3", "deepseek-r1"), "deepseek_v3", "deepseek_r1"),
    Rule(("glm4_moe", "glm-4.5", "glm-4.6", "glm4.5"), "glm45", "glm45"),
    Rule(("kimi_k2", "kimi-k2"), "kimi_k2"),
    Rule(("minimax",), "minimax_m2", "minimax_m2"),
    Rule(("llama4",), "llama4_pythonic"),
    Rule(("llama",), "llama3_json"),
    Rule(("mistral", "mixtral", "magistral", "devstral"), "mistral", "mistral"),
    Rule(("granite",), "granite", "granite"),
    Rule(("hermes",), "hermes"),
)


class ParserError(Exception):
    pass


def pick(meta: ModelMeta, tool_override: str = "auto", reasoning_override: str = "auto") -> tuple[str, str | None]:
    haystack = " ".join([meta.model_type, *meta.architectures, meta.repo_id]).lower()
    rule = next((r for r in RULES if any(m in haystack for m in r.match)), None)

    if tool_override != "auto":
        tool = tool_override
    elif rule:
        tool = rule.tool
    else:
        raise ParserError(f"no known tool-call parser for {meta.repo_id} (model_type {meta.model_type!r}); "
                          "set [model] tool_parser in perseus.toml (see `vllm serve --help`)")

    if reasoning_override == "none":
        reasoning = None
    elif reasoning_override != "auto":
        reasoning = reasoning_override
    else:
        reasoning = rule.reasoning if (rule and meta.thinks) else None
    return tool, reasoning
