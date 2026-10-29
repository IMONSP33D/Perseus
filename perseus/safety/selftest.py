"""Boot self-test: prove the gateway refuses what it must before anyone can chat.

Runs against the live gateway and socket guard in this process, in about a
second. Any refusal check that fails stops Perseus with the case named: it
fails closed, never open. A failed reachability check only warns, so an
offline box still serves chat without web tools.

These are the same cases as tests/test_gateway.py (the Phase 3 suite).
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from perseus.safety import socketguard
from perseus.safety.gateway import Gateway, GatewayError
from perseus.safety.netpolicy import Blocked
from perseus.engine.tool_loader import import_problem
from perseus.tools import Registry

REBIND_NAME = "rebind-to-loopback.perseus-selftest.example"


@dataclass
class Case:
    name: str
    ok: bool
    detail: str
    critical: bool = True


@dataclass
class Report:
    cases: list[Case] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.cases if c.critical)

    @property
    def failures(self) -> list[Case]:
        return [c for c in self.cases if c.critical and not c.ok]

    @property
    def warnings(self) -> list[Case]:
        return [c for c in self.cases if not c.critical and not c.ok]

    def summary(self) -> str:
        n = sum(c.critical for c in self.cases)
        ok = sum(c.ok for c in self.cases if c.critical)
        return f"{ok}/{n} refusal checks passed in {self.seconds:.1f}s"


def refusal_urls(ports: dict[str, int]) -> list[tuple[str, str]]:
    cases = [("cloud metadata", "http://169.254.169.254/latest/meta-data/")]
    for label, port in ports.items():
        cases.append((f"127.0.0.1 on the {label} port", f"http://127.0.0.1:{port}/"))
    cases += [
        ("loopback on port 80", "http://127.0.0.1/"),
        ("localhost", "http://localhost/"),
        ("IPv6 loopback", "http://[::1]/"),
        ("0.0.0.0", "http://0.0.0.0/"),
        ("private address", "http://10.0.0.1/"),
        ("IPv4-mapped loopback", "http://[::ffff:127.0.0.1]/"),
        ("port 8443", "https://example.com:8443/"),
        ("numeric-encoded IP", "http://2130706433/"),
        ("userinfo URL", "http://user@example.com/"),
        ("key-shaped string in URL", "https://example.com/?q=hf_" + "AbCdEfGh12" * 3),
        ("DNS name resolving to loopback", f"http://{REBIND_NAME}/"),
    ]
    return cases


def run(gateway: Gateway, ports: dict[str, int], registry: Registry | None = None,
        reach_url: str | None = "https://example.com/", parse_check: Callable[[], tuple[bool, str]] | None = None,
        tool_dirs: list[Path] = ()) -> Report:
    t0 = time.monotonic()
    rep = Report()

    # A copy of the gateway whose resolver maps one test name to loopback:
    # exercises the after-DNS address check without depending on outside DNS.
    rebinding = Gateway(resolver=_rebind_resolver(gateway.resolver), exact_secrets=gateway.exact_secrets,
                        timeout_s=gateway.timeout_s)

    for label, url in refusal_urls(ports):
        g = rebinding if REBIND_NAME in url else gateway
        try:
            g.check(url, "GET")
            rep.cases.append(Case(f"refuse {label}", False, f"{url} was allowed"))
        except Blocked as e:
            rep.cases.append(Case(f"refuse {label}", True, f"blocked: {e.code}"))

    for method in ("POST", "PUT", "DELETE", "PATCH"):
        try:
            gateway.check("https://example.com/", method)
            rep.cases.append(Case(f"refuse {method}", False, f"{method} was allowed"))
        except Blocked as e:
            rep.cases.append(Case(f"refuse {method}", True, f"blocked: {e.code}"))

    # Socket guard: a direct connection outside the gateway must be refused before it is made.
    if not socketguard.installed():
        rep.cases.append(Case("socket guard installed", False, "the in-process guard isn't active"))
    else:
        try:
            s = socket.create_connection(("1.1.1.1", 443), timeout=2)
            s.close()
            rep.cases.append(Case("refuse direct socket", False, "a direct connection to 1.1.1.1:443 succeeded"))
        except socketguard.GuardRefused:
            rep.cases.append(Case("refuse direct socket", True, "socket guard refused 1.1.1.1:443"))
        except OSError as e:
            rep.cases.append(Case("refuse direct socket", False, f"guard didn't catch it ({type(e).__name__})"))

    # Tool sources import no network or process libraries.
    sources = set()
    if registry:
        import sys
        for n in registry.all_names():   # tools whose capability is off are still checked
            mod = sys.modules.get(registry.loaded(n).source)
            if mod is not None and getattr(mod, "__file__", None):
                sources.add(Path(mod.__file__))
    for d in tool_dirs:
        sources |= set(Path(d).glob("*.py"))
    bad = [(p.name, prob) for p in sorted(sources) if (prob := import_problem(p))]
    rep.cases.append(Case("tools import no network libraries", not bad,
                          "; ".join(f"{n}: {p}" for n, p in bad) or f"{len(sources)} tool file(s) checked"))

    if parse_check:
        ok, detail = parse_check()
        rep.cases.append(Case("model tool call parsed", ok, detail))

    if reach_url:
        try:
            r = Gateway(timeout_s=5, exact_secrets=gateway.exact_secrets, resolver=gateway.resolver,
                        connection_factory=gateway._factory).fetch(reach_url, "HEAD")
            rep.cases.append(Case("reach a public page", True, f"{reach_url} → {r.status}", critical=False))
        except (Blocked, GatewayError) as e:
            rep.cases.append(Case("reach a public page", False,
                                  f"{reach_url}: {e} (web tools will fail; chat still works)", critical=False))
    rep.seconds = time.monotonic() - t0
    return rep


def _rebind_resolver(real):
    def resolver(host, port, type=0, **kw):
        if host == REBIND_NAME:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real(host, port, type=type, **kw)
    return resolver
