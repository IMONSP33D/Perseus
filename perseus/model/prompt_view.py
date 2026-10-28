"""Developer view backend: the exact prompt the model sees, after its chat template.

Rendered locally from the downloaded tokenizer files (no network), the same
template vLLM applies, so a dev can see how tools and tool results are laid
out for this model family and how many tokens they cost.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def _tokenizer(model_dir: str):
    from transformers import AutoTokenizer  # installed with vLLM
    return AutoTokenizer.from_pretrained(model_dir, local_files_only=True)


def render_prompt(model_dir: Path, messages: list[dict], tools: list[dict] | None = None) -> tuple[str, int]:
    """(prompt text, token count) for `messages` as the next request would send them."""
    tok = _tokenizer(str(model_dir))
    text = tok.apply_chat_template(messages, tools=tools or None, tokenize=False, add_generation_prompt=True)
    return text, len(tok(text, add_special_tokens=False)["input_ids"])
