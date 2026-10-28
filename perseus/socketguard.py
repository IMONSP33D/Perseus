"""In-process egress guard: the second wall behind the gateway.

Once installed, every Python socket connect in the Perseus process is refused
unless it is either
  * the gateway's own connection to the address it just checked and pinned
    (granted per call with `permit(ip, port)`), or
  * a loopback service Perseus runs itself (vLLM, SearXNG), by port.

A tool that smuggled in its own HTTP client would hit this and be logged.
Scope: Python code in this process. vLLM and SearXNG are separate processes;
a kernel-level lock (nftables) returns with the VM sandbox.
"""

from __future__ import annotations

import contextvars
import ipaddress
import socket
import threading
from contextlib import contextmanager
from typing import Callable

_permit: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar("perseus_permit", default=None)
_lock = threading.Lock()
_state: dict = {"installed": False, "loopback_ports": set(), "on_refuse": None, "orig": None}


class GuardRefused(PermissionError):
    pass


def _norm(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return str(ip)
    except ValueError:
        return host


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(_norm(host)).is_loopback
    except ValueError:
        return False


def allowed(family: int, address) -> bool:
    if family == getattr(socket, "AF_UNIX", object()):
        return True
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host, port = _norm(str(address[0])), int(address[1])
    p = _permit.get()
    if p is not None and p == (host, port):
        return True
    return _is_loopback(host) and port in _state["loopback_ports"]


def _guarded(orig: Callable, ex: bool):
    def connect(self, address):
        if not allowed(self.family, address):
            target = f"{address[0]}:{address[1]}" if isinstance(address, tuple) and len(address) >= 2 else str(address)
            cb = _state["on_refuse"]
            if cb:
                try:
                    cb(target)
                except Exception:
                    pass
            err = GuardRefused(f"outbound connection to {target} refused: only the web gateway may reach the internet")
            if ex:
                return 13  # EACCES, as connect_ex reports errors
            raise err
        return orig(self, address)
    connect.__perseus_guard__ = True
    return connect


def install(loopback_ports: set[int] | list[int], on_refuse: Callable[[str], None] | None = None) -> None:
    with _lock:
        _state["loopback_ports"] = set(loopback_ports)
        _state["on_refuse"] = on_refuse
        if _state["installed"]:
            return
        _state["orig"] = (socket.socket.connect, socket.socket.connect_ex)
        socket.socket.connect = _guarded(_state["orig"][0], ex=False)
        socket.socket.connect_ex = _guarded(_state["orig"][1], ex=True)
        _state["installed"] = True


def allow_loopback_port(port: int) -> None:
    with _lock:
        _state["loopback_ports"].add(int(port))


def uninstall() -> None:
    """Tests only."""
    with _lock:
        if _state["installed"]:
            socket.socket.connect, socket.socket.connect_ex = _state["orig"]
            _state["installed"] = False


def installed() -> bool:
    return bool(_state["installed"]) and getattr(socket.socket.connect, "__perseus_guard__", False)


@contextmanager
def permit(ip: str, port: int):
    """Allow exactly one destination for the duration of the block (this thread/task only)."""
    token = _permit.set((_norm(ip), int(port)))
    try:
        yield
    finally:
        _permit.reset(token)
