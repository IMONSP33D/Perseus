"""A small, strict JSON Schema validator for tool arguments.

Supports the subset tool schemas use: type, properties, required,
additionalProperties, enum, const, minimum/maximum, minLength/maxLength,
pattern, items, minItems/maxItems, default. Objects refuse unknown keys
unless additionalProperties is true: an argument the tool didn't declare is a
sign the model invented it.
"""

from __future__ import annotations

import re
from typing import Any

TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict, path: str = "") -> list[str]:
    errors: list[str] = []
    where = path or "arguments"
    t = schema.get("type")
    if t is not None:
        allowed = t if isinstance(t, list) else [t]
        if not any(TYPES[x](value) for x in allowed):
            return [f"{where}: expected {' or '.join(allowed)}, got {_kind(value)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: must be one of {', '.join(repr(e) for e in schema['enum'])}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: doesn't match the expected format")
    if TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: must be ≥ {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: must be ≤ {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: at most {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing required {key!r}")
        extra = schema.get("additionalProperties", False)
        for key, v in value.items():
            if key in props:
                errors += validate(v, props[key], f"{path + '.' if path else ''}{key}")
            elif extra is False:
                known = ", ".join(props) or "none"
                errors.append(f"{where}: unknown argument {key!r} (accepted: {known})")
            elif isinstance(extra, dict):
                errors += validate(v, extra, f"{path + '.' if path else ''}{key}")
    return errors


def with_defaults(value: dict, schema: dict) -> dict:
    out = dict(value)
    for key, sub in schema.get("properties", {}).items():
        if key not in out and "default" in sub:
            out[key] = sub["default"]
    return out


def check_schema(schema: dict) -> None:
    """Registry-time sanity check, so a broken tool fails at load, not mid-turn."""
    if schema.get("type") != "object":
        raise ValueError("a tool's parameters schema must be type: object")
    for key, sub in schema.get("properties", {}).items():
        t = sub.get("type")
        for x in (t if isinstance(t, list) else [t]):
            if x is not None and x not in TYPES:
                raise ValueError(f"property {key!r} has unknown type {x!r}")
    for key in schema.get("required", []):
        if key not in schema.get("properties", {}):
            raise ValueError(f"required {key!r} isn't in properties")


def _kind(v: Any) -> str:
    for name, test in TYPES.items():
        if test(v):
            return name
    return type(v).__name__
