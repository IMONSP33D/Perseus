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
    search only through `ctx.search`, code execution only through `ctx.sandbox`
  * no file writes; return data, don't act
  * return a dict or a string; raise ToolFailure with a message for the model

A tool that needs an engine-provided capability says so with `requires=` (for
example `requires="sandbox"`). It is offered to the model, listed in the
dashboard and callable only after the launcher calls `REGISTRY.provide(...)`
for that capability, so a box without the capability never shows the tool.

Files here are loaded, and hot-reloaded when they change, by
perseus.engine.tool_loader; files starting with "_" are skipped (see _example.py).
"""

from __future__ import annotations

import copy
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
    requires: str | None = None  # engine capability this tool needs, e.g. "sandbox"
    untrusted: bool | None = None  # output wrapped as untrusted data; None = same as `lookup`

    def __post_init__(self):
        if self.untrusted is None:
            self.untrusted = self.lookup

    def spec(self, parameters: dict | None = None) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": parameters or self.parameters}}


@dataclass
class ToolContext:
    """What a tool may use. Built by the engine; tools never construct clients."""
    gateway: Any = None          # perseus.safety.gateway.Gateway
    search: Any = None           # perseus.search.SearchClient
    limits: dict = field(default_factory=dict)
    engines: list[str] | None = None   # search engines chosen in the dashboard; None = all configured
    sandbox: Any = None          # perseus.sandbox.client.SandboxClient, or None when the code runner is off
    allow_web: bool = True       # this chat's Internet Access switch, set per call by the engine


@dataclass
class Capability:
    """Something the engine provides to tools that declare `requires=<name>`."""
    name: str
    reaches_web: bool = False    # tools using it are web tools: gated by the Internet Access switch
    enums: dict[str, list[str]] = field(default_factory=dict)   # narrows these enum parameters, e.g. languages
    note: str = ""               # appended to the description of tools using it (e.g. the sandbox's network)


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._caps: dict[str, Capability] = {}

    def add(self, t: Tool) -> Tool:
        check_schema(t.parameters)
        if not t.name.replace("_", "").isalnum():
            raise ValueError(f"tool name {t.name!r}: letters, digits and underscores only")
        self._tools[t.name] = t
        return t

    def remove_source(self, source: str) -> None:
        for name in [n for n, t in self._tools.items() if t.source == source]:
            del self._tools[name]

    # capabilities ------------------------------------------------------------
    def provide(self, name: str, *, reaches_web: bool = False, enums: dict[str, list[str]] | None = None,
                note: str = "") -> None:
        self._caps[name] = Capability(name, reaches_web, dict(enums or {}), note)

    def withdraw(self, name: str) -> None:
        self._caps.pop(name, None)

    def available(self, t: Tool) -> bool:
        return t.requires is None or t.requires in self._caps

    def is_web(self, t: Tool) -> bool:
        """Web tools are neither offered nor run while Internet Access is off."""
        return t.lookup or (t.requires in self._caps and self._caps[t.requires].reaches_web)

    def parameters(self, t: Tool) -> dict:
        """The tool's schema as offered right now: enum parameters narrowed by its capability."""
        cap = self._caps.get(t.requires) if t.requires else None
        if not cap or not cap.enums:
            return t.parameters
        params = copy.deepcopy(t.parameters)
        for prop, values in cap.enums.items():
            p = params.get("properties", {}).get(prop)
            if p is not None and "enum" in p:
                p["enum"] = [v for v in p["enum"] if v in values]
                if p.get("default") not in p["enum"]:
                    p.pop("default", None)
        return params

    # lookups -----------------------------------------------------------------
    def get(self, name: str) -> Tool | None:
        t = self._tools.get(name)
        return t if t is not None and self.available(t) else None

    def names(self) -> list[str]:
        """Tools usable right now (their capability, if any, is provided)."""
        return sorted(n for n, t in self._tools.items() if self.available(t))

    def all_names(self) -> list[str]:
        """Every loaded tool, usable or not (import checks, the dev view)."""
        return sorted(self._tools)

    def loaded(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def description(self, t: Tool) -> str:
        cap = self._caps.get(t.requires) if t.requires else None
        return f"{t.description} {cap.note}" if cap and cap.note else t.description

    def specs(self, only: set[str] | None = None) -> list[dict]:
        out = []
        for n, t in sorted(self._tools.items()):
            if self.available(t) and (only is None or n in only):
                spec = t.spec(self.parameters(t))
                spec["function"]["description"] = self.description(t)
                out.append(spec)
        return out

    def validate(self, name: str, args: Any) -> tuple[Tool | None, dict, list[str]]:
        """(tool, args with defaults, problems). A non-empty problem list means: refuse."""
        t = self.get(name)
        if t is None:
            known = ", ".join(self.names()) or "none"
            return None, {}, [f"no tool named {name!r} (available: {known})"]
        if not isinstance(args, dict):
            return t, {}, [f"arguments must be a JSON object, got {type(args).__name__}"]
        params = self.parameters(t)
        problems = validate(args, params)
        return t, with_defaults(args, params), problems


REGISTRY = Registry()


def tool(name: str | None = None, *, description: str, parameters: dict, lookup: bool = False,
         timeout_s: float = 30.0, registry: Registry | None = None, requires: str | None = None,
         untrusted: bool | None = None):
    def deco(fn):
        (registry or REGISTRY).add(Tool(name or fn.__name__, description.strip(), parameters, fn, lookup,
                                        timeout_s, fn.__module__, requires, untrusted))
        return fn
    return deco


def load_builtin() -> Registry:
    """Load every tool file in this folder into REGISTRY (once; later calls reload changed files)."""
    from perseus.engine.tool_loader import default_loader
    default_loader().refresh()
    return REGISTRY
