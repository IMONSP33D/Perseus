"""Tool registry: the only Perseus API a tool imports.

Each built-in tool is one file in this folder. A tool is a plain Python
function with a JSON Schema, registered with @tool:

    from perseus.tools import ToolFailure, tool

    @tool(description="Add two numbers.",
          parameters={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                      "required": ["a", "b"]})
    def add(ctx, a, b):
        return {"sum": a + b}

Rules every tool follows (the loader and tests/test_tool_imports.py enforce the first two):
  * no network or process libraries: web access only through `ctx.gateway`,
    search only through `ctx.search`
  * no file writes; return data, don't act
  * return a dict or a string; raise ToolFailure with a message for the model

Files here are loaded, and hot-reloaded when they change, by
perseus.engine.tool_loader; files starting with "_" are skipped (see _example.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from perseus.engine.schema import check_schema, validate, with_defaults

TOOLS_DIR = Path(__file__).resolve().parent


class ToolFailure(Exception):
    """Raised by a tool to return a clean error message to the model."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
    lookup: bool = False        # counts toward the research-depth limit (web tools)
    timeout_s: float = 30.0
    source: str = ""            # module the tool came from (shown in the dev view)

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class ToolContext:
    """What a tool may use. Built by the engine; tools never construct clients."""
    gateway: Any = None          # perseus.safety.gateway.Gateway
    search: Any = None           # perseus.search.SearchClient
    limits: dict = field(default_factory=dict)
    engines: list[str] | None = None   # search engines chosen in the dashboard; None = all configured


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def add(self, t: Tool) -> Tool:
        check_schema(t.parameters)
        if not t.name.replace("_", "").isalnum():
            raise ValueError(f"tool name {t.name!r}: letters, digits and underscores only")
        self._tools[t.name] = t
        return t

    def remove_source(self, source: str) -> None:
        for name in [n for n, t in self._tools.items() if t.source == source]:
            del self._tools[name]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, only: set[str] | None = None) -> list[dict]:
        return [t.spec() for n, t in sorted(self._tools.items()) if only is None or n in only]

    def validate(self, name: str, args: Any) -> tuple[Tool | None, dict, list[str]]:
        """(tool, args with defaults, problems). A non-empty problem list means: refuse."""
        t = self.get(name)
        if t is None:
            known = ", ".join(self.names()) or "none"
            return None, {}, [f"no tool named {name!r} (available: {known})"]
        if not isinstance(args, dict):
            return t, {}, [f"arguments must be a JSON object, got {type(args).__name__}"]
        problems = validate(args, t.parameters)
        return t, with_defaults(args, t.parameters), problems


REGISTRY = Registry()


def tool(name: str | None = None, *, description: str, parameters: dict, lookup: bool = False,
         timeout_s: float = 30.0, registry: Registry | None = None):
    def deco(fn):
        (registry or REGISTRY).add(Tool(name or fn.__name__, description.strip(), parameters, fn, lookup,
                                        timeout_s, fn.__module__))
        return fn
    return deco


def load_builtin() -> Registry:
    """Load every tool file in this folder into REGISTRY (once; later calls reload changed files)."""
    from perseus.engine.tool_loader import default_loader
    default_loader().refresh()
    return REGISTRY
