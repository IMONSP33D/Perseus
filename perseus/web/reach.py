"""Is the dashboard reachable? Checked once it starts, and explained in the terminal.

The box serves on loopback only, so a browser elsewhere needs an SSH tunnel. Three things
can break that, and this module checks or explains each:
  1. the dashboard itself (health, the token login, the page and its assets): self_check()
  2. the box's SSH server refusing port forwarding (AllowTcpForwarding no): ssh_forwarding()
  3. the tunnel command run in the wrong place or with a busy local port: tunnel_command()
Visits from a browser are reported as they happen (VisitReporter), so "it doesn't open"
becomes either "nothing arrived" (the tunnel) or "arrived without the token" (the link).
"""

from __future__ import annotations

import getpass
import http.client
import ipaddress
import json
import os
import re
from pathlib import Path

SELF = {"X-Perseus-Selfcheck": "1"}


def _get(port: int, path: str, headers: dict | None = None, host: str = "127.0.0.1") -> http.client.HTTPResponse:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Host": f"{host}:{port}", **SELF, **(headers or {})})
    r = c.getresponse()
    r.body = r.read()
    c.close()
    return r


def self_check(port: int, token: str) -> list[tuple[bool, str]]:
    """What a browser at the other end of the tunnel would get, step by step."""
    out: list[tuple[bool, str]] = []
    try:
        r = _get(port, "/healthz")
        ok = r.status == 200 and json.loads(r.body or b"{}").get("ok") is not False
        out.append((ok, f"health check: HTTP {r.status}"))
    except (OSError, ValueError) as e:
        return [(False, f"nothing answers on 127.0.0.1:{port} ({e})")]
    try:
        r = _get(port, f"/?token={token}")
        cookie = (r.getheader("Set-Cookie") or "").split(";", 1)[0]
        out.append((r.status == 303 and bool(cookie), f"sign-in with the link's token: HTTP {r.status}"))
        for host in ("127.0.0.1", "localhost"):       # what the browser sends, depending on the link used
            r = _get(port, "/", {"Cookie": cookie}, host=host)
            out.append((r.status == 200 and b"app.js" in r.body, f"dashboard page via {host}: HTTP {r.status}"))
        r = _get(port, "/assets/app.js")
        out.append((r.status == 200, f"dashboard script: HTTP {r.status}"))
    except OSError as e:
        out.append((False, f"request failed: {e}"))
    return out


def ssh_forwarding(config_dir: Path = Path("/etc/ssh")) -> str | None:
    """A reason the box's sshd refuses tunnels, if its config says so (first setting wins, like sshd)."""
    seen: dict[str, str] = {}
    lines: list[tuple[str, str]] = []

    def read(path: Path):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(" ")
            key, value = key.lower(), value.strip().lower()
            if key == "match":
                return                              # Match blocks apply conditionally; stop at the first
            if key == "include":
                for pat in value.split():
                    base = Path(pat) if pat.startswith("/") else config_dir / pat
                    for f in sorted(base.parent.glob(base.name)):
                        read(f)
                continue
            lines.append((key, value))

    read(config_dir / "sshd_config")          # Ubuntu's includes sshd_config.d/*.conf at the top
    for key, value in lines:
        seen.setdefault(key, value)
    if seen.get("disableforwarding") == "yes":
        return "the box's SSH server has DisableForwarding yes"
    if seen.get("allowtcpforwarding") in ("no", "remote"):
        return f"the box's SSH server has AllowTcpForwarding {seen['allowtcpforwarding']}"
    return None


def tunnel_command(port: int) -> str:
    """The command to run on your own computer, filled in as far as this box can tell."""
    user = os.environ.get("USER") or getpass.getuser()
    conn = os.environ.get("SSH_CONNECTION", "").split()      # client_ip client_port server_ip server_port
    host = "<the address you ssh to>"
    ssh_port = ""
    if len(conn) == 4:
        try:
            ip = ipaddress.ip_address(conn[2])
            if ip.is_global:                                  # behind cloud NAT this is a private address
                host = conn[2]
        except ValueError:
            pass
        if conn[3] != "22":
            ssh_port = f" -p {conn[3]}"
    return (f"ssh -N -o ExitOnForwardFailure=yes -L {port}:127.0.0.1:{port}{ssh_port} {user}@{host}")


def in_ssh_session() -> bool:
    return bool(os.environ.get("SSH_CONNECTION"))


class VisitReporter:
    """Prints, once each, what reached the dashboard: proof the tunnel works, or what's wrong."""

    MESSAGES = {
        "signed_in": "✓ a browser signed in to the dashboard",
        "page": None,
        "no_token": "! a browser reached the dashboard without the token: open the full link, ending in ?token=…",
        "bad_token": "! a browser used an old or wrong token: open the link printed above (it changes every start)",
        "bad_host": "! a browser was refused for its address ({detail}): open http://127.0.0.1:… or localhost, "
                    "not a hostname",
    }

    def __init__(self, say):
        self.say, self.seen = say, set()

    def __call__(self, kind: str, detail: str = "") -> None:
        msg = self.MESSAGES.get(kind)
        if not msg or kind in self.seen:
            return
        self.seen.add(kind)
        self.say("  " + msg.format(detail=re.sub(r"[^\w.:\[\]-]", "", detail)[:60]))
