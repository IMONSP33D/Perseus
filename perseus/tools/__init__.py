"""Tool registry: the only Perseus API a tool imports.

Each built-in tool is one file in this folder. A tool is a plain Python
function registered with @tool; its docstring is the description and its
type hints are the schema:

    from perseus.tools import tool

    @tool
    def add(a: float, b: float) -> dict:
        "Add two numbers."
        return {"sum": a + b}

Pass `parameters=` (a JSON Schema) instead when you need limits such as
maxLength or pattern, and take `ctx` as the first parameter to use the gateway,
search or sandbox.

Rules every tool follows (the loader and tests/test_tool_imports.py check the first one;
the socket guard is the wall behind it):
  * no network or process libraries: web access only through `ctx.gateway`,
    search only through `ctx.search`, code execution only through `ctx.sandbox`
    (or `ctx.<addon>` for an addon's own service, named after its folder)
  * no file writes; return data, don't act
  * return a dict, list or string; raise ToolFailure with a message for the model
    (other exceptions reach the model only as their type name)

Reserved keys in a returned dict:
  text     the main body; capped to the context budget
  url      the source, named in the untrusted-content label
  status   an int is shown as the HTTP status in the feed
  feed     a card view for the tool feed (see engine.feed.clean_feed); never sent to the model
  file     a file handed to the user (see engine.feed.clean_file); never sent to the model
  outputs  files the tool brought back, [{"ref": "<origin>:dist/app.exe", "data": b"..."}]: the engine keeps
           the bytes for this chat (engine.outputs) so output_file(source=ref) can deliver them; never sent
           to the model, so say each file's ref in the result text instead

A tool that needs an engine-provided capability says so with `requires=` (for
example `requires="sandbox"`). It is offered to the model, listed in the
dashboard and callable only after the launcher calls `REGISTRY.provide(...)`
for that capability, so a box without the capability never shows the tool.

Files here are loaded, and hot-reloaded when they change, by
perseus.engine.tool_loader; files starting with "_" are skipped (see _example.py).
Addons keep their tool files in perseus/addons/<name>/tools/, loaded the same way (see perseus.addons).
"""

from __future__ import annotations

import copy
import functools
import inspect
import threading
import types
import typing
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


class ToolContext:
    """What a tool may use. Built by the engine; tools never construct clients.

    Fixed attributes are the engine's own services. Every addon that is up adds one more, named after
    the addon: a tool that says `requires="my_addon"` reaches its service as `ctx.my_addon`.
    An addon that is off reads as None."""

    FIXED = ("gateway", "search", "limits", "engines", "outputs")

    def __init__(self, gateway: Any = None, search: Any = None, limits: dict | None = None,
                 engines: list[str] | None = None, outputs: Any = None, **addons: Any):
        self.gateway = gateway          # perseus.safety.gateway.Gateway
        self.search = search            # perseus.search.SearchClient
        self.limits = limits if limits is not None else {}
        self.engines = engines          # search engines chosen in the dashboard; None = all configured
        self.outputs = outputs          # perseus.engine.outputs.Outputs: this chat's collected files (set per call)
        self.addons: dict[str, Any] = dict(addons)   # addon name -> its client, or None when the addon is off

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in self.FIXED:
            raise AttributeError(name)
        return self.__dict__.get("addons", {}).get(name)

    def with_outputs(self, outputs: Any) -> "ToolContext":
        c = ToolContext(self.gateway, self.search, self.limits, self.engines, outputs, **self.addons)
        return c


@dataclass
class Capability:
    """Something the engine provides to tools that declare `requires=<name>`."""
    name: str
    reaches_web: bool = False    # tools using it are web tools: gated by the Internet Access switch
    enums: dict[str, list[str]] = field(default_factory=dict)   # narrows these enum parameters, e.g. languages
    note: str = ""               # appended to the description of tools using it (e.g. the sandbox's network)
    agent_use: str = ""          # the addon's standing guidance to the model, appended before `note`


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._caps: dict[str, Capability] = {}
        self.lock = threading.RLock()

    def add(self, t: Tool) -> Tool:
        check_schema(t.parameters)
        if not t.name.replace("_", "").isalnum():
            raise ValueError(f"tool name {t.name!r}: letters, digits and underscores only")
        with self.lock:
            clash = self._tools.get(t.name)
            if clash is not None and clash.source != t.source:
                raise ValueError(f"tool name {t.name!r} is already defined in {clash.source}")
            self._tools[t.name] = t
        return t

    def remove_source(self, source: str) -> None:
        with self.lock:
            for name in [n for n, t in self._tools.items() if t.source == source]:
                del self._tools[name]

    # capabilities ------------------------------------------------------------
    def provide(self, name: str, *, reaches_web: bool = False, enums: dict[str, list[str]] | None = None,
                note: str = "") -> None:
        agent_use = self._caps[name].agent_use if name in self._caps else ""
        self._caps[name] = Capability(name, reaches_web, dict(enums or {}), note, agent_use)

    def describe(self, name: str, agent_use: str) -> None:
        """Attach an addon's agent_use text to its capability; kept across provide() calls."""
        if name in self._caps:
            self._caps[name].agent_use = agent_use.strip()

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
        with self.lock:
            return sorted(n for n, t in self._tools.items() if self.available(t))

    def all_names(self) -> list[str]:
        """Every loaded tool, usable or not (import checks, the dev view)."""
        with self.lock:
            return sorted(self._tools)

    def loaded(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def description(self, t: Tool) -> str:
        cap = self._caps.get(t.requires) if t.requires else None
        if not cap:
            return t.description
        return " ".join(x for x in (t.description, cap.agent_use, cap.note) if x)

    def specs(self, only: set[str] | None = None) -> list[dict]:
        out = []
        with self.lock:
            tools = sorted(self._tools.items())
        for n, t in tools:
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

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


def _json_type(hint: Any) -> dict:
    meta = ""
    if typing.get_origin(hint) is typing.Annotated:
        hint, *extra = typing.get_args(hint)
        meta = next((e for e in extra if isinstance(e, str)), "")
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        real = [a for a in args if a is not type(None)]
        out = _json_type(real[0]) if len(real) == 1 else {}
        if len(real) < len(args) and isinstance(out.get("type"), str):
            out["type"] = [out["type"], "null"]
    elif origin is typing.Literal:
        out = {"enum": list(args)}
        kinds = {_JSON_TYPES.get(type(a)) for a in args}
        if len(kinds) == 1 and None not in kinds:
            out["type"] = kinds.pop()
    elif origin is list:
        out = {"type": "array"}
        if args:
            out["items"] = _json_type(args[0])
    elif origin is dict:
        out = {"type": "object"}
    elif hint in _JSON_TYPES:
        out = {"type": _JSON_TYPES[hint]}
    elif hint is inspect.Parameter.empty or hint is Any:
        out = {}
    else:
        raise TypeError(f"type hint {hint!r} has no JSON Schema equivalent; pass parameters= instead")
    if meta:
        out["description"] = meta
    return out


def schema_from_signature(fn: Callable) -> dict:
    """A parameters schema from the function's type hints; `Annotated[str, "help"]` adds a description."""
    hints = typing.get_type_hints(fn, include_extras=True)
    props, required = {}, []
    for i, (pname, p) in enumerate(inspect.signature(fn).parameters.items()):
        if i == 0 and pname == "ctx":
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise TypeError(f"{fn.__name__}: *args/**kwargs can't be offered to a model; name each argument")
        prop = _json_type(hints.get(pname, p.annotation))
        if p.default is p.empty:
            required.append(pname)
        elif p.default is not None:
            prop["default"] = p.default
        props[pname] = prop
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _takes_ctx(fn: Callable, parameters: dict | None) -> bool:
    """A first parameter named `ctx`, or (explicit schema) one that isn't a schema argument, gets the context."""
    first = next(iter(inspect.signature(fn).parameters), None)
    if first is None:
        return False
    if parameters is not None:
        return first not in (parameters.get("properties") or {})
    return first == "ctx"


def tool(name: str | Callable | None = None, *, description: str | None = None, parameters: dict | None = None,
         lookup: bool = False, timeout_s: float = 30.0, registry: Registry | None = None,
         requires: str | None = None, untrusted: bool | None = None):
    """Register a function as a tool. All of these work:

        @tool
        def shout(text: str) -> str:
            "Upper-case some text."
            return text.upper()

        @tool(lookup=True)
        def title(ctx, url: Annotated[str, "Full http(s) URL."]): ...

        @tool(description="...", parameters={...JSON Schema...})
        def anything(ctx, **schema_args): ...

    The description defaults to the docstring and the schema to the type hints.
    A first parameter named `ctx` receives the ToolContext; without it the tool gets only its arguments.
    With an explicit `parameters=`, a first parameter that isn't one of its arguments gets the context.
    """
    def deco(fn):
        desc = description or inspect.getdoc(fn)
        if not desc:
            raise ValueError(f"tool {fn.__name__!r}: give it a docstring or description=")
        params = parameters if parameters is not None else schema_from_signature(fn)
        call = fn if _takes_ctx(fn, parameters) else functools.wraps(fn)(lambda _ctx, **kw: fn(**kw))
        (registry or REGISTRY).add(Tool(name if isinstance(name, str) else fn.__name__, desc.strip(), params,
                                        call, lookup, timeout_s, fn.__module__, requires, untrusted))
        return fn
    if callable(name):
        return deco(name)
    return deco


def load_builtin() -> Registry:
    """Load every tool file here and in each addon's tools/ into REGISTRY (once; later calls reload changed files)."""
    from perseus.engine.tool_loader import default_loader
    default_loader().refresh()
    return REGISTRY
