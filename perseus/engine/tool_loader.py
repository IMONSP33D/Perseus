"""Loads tool files from perseus/tools/ and reloads them when they change.

Before a file is imported, a static check refuses network, process and FFI
libraries; a tool file that fails to import never takes the engine down.
Reloads run before each answer, so editing a tool takes effect on the next turn.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

from perseus.tools import REGISTRY, TOOLS_DIR, Registry

BANNED_IMPORTS = {"socket", "_socket", "ssl", "_ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp",
                  "subprocess", "_posixsubprocess", "asyncio", "multiprocessing", "ctypes", "cffi", "pycurl",
                  "websocket", "websockets", "ftplib", "smtplib", "telnetlib", "paramiko", "pty", "importlib",
                  "builtins"}
BANNED_OS_CALLS = re.compile(r"^(system|popen|exec\w*|spawn\w*|fork\w*|posix_spawn\w*)$")


class ToolLoader:
    def __init__(self, directory: Path = TOOLS_DIR, registry: Registry = REGISTRY, package: str = "perseus.tools"):
        self.dir, self.registry, self.package = directory, registry, package
        self._mtimes: dict[Path, float] = {}
        self.errors: dict[str, str] = {}

    def files(self) -> dict[Path, float]:
        if not self.dir.is_dir():
            return {}
        return {p: p.stat().st_mtime for p in sorted(self.dir.glob("*.py")) if not p.name.startswith("_")}

    def refresh(self) -> list[str]:
        """Load new and changed files, drop removed ones. Returns one line per change."""
        with self.registry.lock:
            return self._refresh()

    def _refresh(self) -> list[str]:
        changes = []
        files = self.files()
        for p in list(self._mtimes):
            if p not in files:
                self.registry.remove_source(self._modname(p))
                del self._mtimes[p]
                changes.append(f"removed tools from {p.name}")
        for p, mtime in files.items():
            if self._mtimes.get(p) == mtime:
                continue
            first = p not in self._mtimes
            self._mtimes[p] = mtime
            mod = self._modname(p)
            problem = import_problem(p)
            if problem:
                self.registry.remove_source(mod)
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
                changes.append(f"{'loaded' if first else 'reloaded'} {p.name}")
            except Exception as e:  # a broken tool never takes the engine down
                self.registry.remove_source(mod)
                self.errors[p.name] = f"{type(e).__name__}: {e}"
                changes.append(f"failed to load {p.name}: {type(e).__name__}: {e}")
        return changes

    def _modname(self, p: Path) -> str:
        return f"{self.package}.{p.stem}"


_default: ToolLoader | None = None


def default_loader() -> ToolLoader:
    global _default
    if _default is None:
        _default = ToolLoader()
    return _default


def import_problem(path: Path) -> str | None:
    """Static check: a tool file may not import network, process or FFI libraries."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        return f"syntax error line {e.lineno}"
    os_names = {"os"} | {a.asname for n in ast.walk(tree) if isinstance(n, ast.Import)
                         for a in n.names if a.name == "os" and a.asname}
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("__import__", "exec", "eval", "compile"):
            return f"line {node.lineno}: {node.func.id}() isn't allowed in tools"
        elif (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr" and node.args
              and isinstance(node.args[0], ast.Name) and node.args[0].id in os_names):
            return f"line {node.lineno}: getattr() on os isn't allowed in tools"
        elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in os_names
              and BANNED_OS_CALLS.match(node.attr)):
            return f"line {node.lineno}: os.{node.attr} runs programs; tools return data, they don't run commands"
        for n in names:
            root = n.split(".")[0]
            if n in BANNED_IMPORTS or root in BANNED_IMPORTS:
                return f"line {node.lineno}: imports {n!r}; tools reach the web only through ctx.gateway"
        if isinstance(node, ast.ImportFrom) and node.module == "os" and any(BANNED_OS_CALLS.match(a.name) for a in node.names):
            return f"line {node.lineno}: imports a process-running function from os"
    return None
