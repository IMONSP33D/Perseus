"""One config key's rules: the type(s) it takes, its default, its choices or check. Shared by
perseus.config (the core schema) and every addon's addon.py (its own section), so it lives apart
from both and neither imports the other to get it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Key:
    types: tuple[type, ...]
    default: Any = None
    required: bool = False
    choices: tuple | None = None
    check: Callable[[Any], str | None] | None = None
    secret: bool = False

    def validate(self, value: Any) -> str | None:
        # bool is a subclass of int in Python; never accept True for a number.
        if isinstance(value, bool) and bool not in self.types:
            return f"expected {self._type_names()}, got a boolean"
        if not isinstance(value, self.types):
            return f"expected {self._type_names()}, got {type(value).__name__} {value!r}"
        if self.choices is not None and value not in self.choices:
            return f"must be one of {', '.join(repr(c) for c in self.choices)}; got {value!r}"
        if self.check is not None:
            return self.check(value)
        return None

    def _type_names(self) -> str:
        names = {str: "a string", int: "a whole number", float: "a number",
                 bool: "true/false", list: "a list"}
        return " or ".join(names.get(t, t.__name__) for t in self.types)



def range_check(lo: int, hi: int) -> Callable[[Any], str | None]:
    def check(v: Any) -> str | None:
        return None if lo <= v <= hi else f"must be between {lo} and {hi}"
    return check


def api_key_check(v: str) -> str | None:
    if v and any(c.isspace() for c in v.strip()):
        return "must be the key alone, with no spaces"
    return None



__all__ = ["Key", "range_check", "api_key_check"]
