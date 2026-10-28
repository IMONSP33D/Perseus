"""A tiny TOML writer for flat sections (stdlib has a reader, not a writer)."""

from __future__ import annotations

import json
from typing import Any


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def dumps(sections: dict[str, dict[str, Any]], header: str = "") -> str:
    out = [f"# {line}" for line in header.splitlines()] + ([""] if header else [])
    for name, keys in sections.items():
        out.append(f"[{name}]")
        for k, v in keys.items():
            if v is None:
                continue
            out.append(f"{k} = {_value(v)}")
        out.append("")
    return "\n".join(out)
