"""Tool registry.

A tool is a plain Python function with a JSON Schema, registered with @tool:

    @tool(description="Add two numbers.",
          parameters={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                      "required": ["a", "b"]})
    def add(ctx, a, b):
        return {"sum": a + b}

Rules every tool follows (a test enforces the first two):
  * no network or process libraries: web access only through `ctx.gateway`,
    search only through `ctx.search`
  * no file writes; return data, don't act
  * return a dict or a string; raise ToolFailure with a message for the model
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .schema import check_schema, validate, with_defaults


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
    gateway: Any = None          # perseus.gateway.Gateway
    search: Any = None           # perseus.searxng.SearchClient
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
    from . import builtin  # noqa: F401  registers on import
    return REGISTRY


class UserTools:
    """Hot reload for tools/*.py in the clone: re-imported when a file changes (checked before each turn)."""

    def __init__(self, directory: Path, registry: Registry = REGISTRY):
        self.dir, self.registry = directory, registry
        self._mtimes: dict[Path, float] = {}
        self.errors: dict[str, str] = {}

    def refresh(self) -> list[str]:
        """Reload changed files. Returns a line per change for the tool feed."""
        changes = []
        files = {p: p.stat().st_mtime for p in sorted(self.dir.glob("*.py")) if not p.name.startswith("_")} \
            if self.dir.is_dir() else {}
        for p in list(self._mtimes):
            if p not in files:
                self.registry.remove_source(_modname(p))
                del self._mtimes[p]
                changes.append(f"removed tools from {p.name}")
        for p, mtime in files.items():
            if self._mtimes.get(p) == mtime:
                continue
            self._mtimes[p] = mtime
            mod = _modname(p)
            problem = import_problem(p)
            if problem:
                self.errors[p.name] = problem
                changes.append(f"refused {p.name}: {problem}")
                continue
            self.registry.remove_source(mod)
            try:
                spec = importlib.util.spec_from_file_location(mod, p)
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod] = module
                spec.loader.exec_module(module)
                self.errors.pop(p.name, None)
                changes.append(f"loaded {p.name}")
            except Exception as e:  # a broken user tool never takes the engine down
                self.registry.remove_source(mod)
                self.errors[p.name] = f"{type(e).__name__}: {e}"
                changes.append(f"failed to load {p.name}: {type(e).__name__}: {e}")
        return changes


def _modname(p: Path) -> str:
    return f"perseus_user_tools.{p.stem}"


BANNED_IMPORTS = {"socket", "_socket", "ssl", "_ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp",
                  "subprocess", "_posixsubprocess", "asyncio", "multiprocessing", "ctypes", "cffi", "pycurl",
                  "websocket", "websockets", "ftplib", "smtplib", "telnetlib", "paramiko", "pty", "importlib"}
BANNED_OS_CALLS = re.compile(r"^(system|popen|exec\w*|spawn\w*|fork\w*|posix_spawn\w*)$")


def import_problem(path: Path) -> str | None:
    """Static check: a tool file may not import network, process or FFI libraries."""
    import ast
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        return f"syntax error line {e.lineno}"
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("__import__", "exec", "eval", "compile"):
            return f"line {node.lineno}: {node.func.id}() isn't allowed in tools"
        elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os"
              and BANNED_OS_CALLS.match(node.attr)):
            return f"line {node.lineno}: os.{node.attr} runs programs; tools return data, they don't run commands"
        for n in names:
            root = n.split(".")[0]
            if n in BANNED_IMPORTS or root in BANNED_IMPORTS:
                return f"line {node.lineno}: imports {n!r}; tools reach the web only through ctx.gateway"
        if isinstance(node, ast.ImportFrom) and node.module == "os" and any(BANNED_OS_CALLS.match(a.name) for a in node.names):
            return f"line {node.lineno}: imports a process-running function from os"
    return None
