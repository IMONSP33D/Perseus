"""The egress gateway: the only path from the model to the internet.

Rules, in order, re-applied to every redirect hop:
  method    GET or HEAD; no other method exists in this client
  secrets   URL (raw and %-decoded) carries no key-shaped string or Perseus key
  shape     http/https on 80/443, no user@host, no numeric-encoded IPs, length cap
  address   DNS resolved once; refused if ANY answer is non-public; the connection
            goes to that pinned IP (TLS still verifies the real hostname), so DNS
            rebinding can't swap it
  request   no body, no cookies, no auth headers, a fixed honest User-Agent
  limits    per-call timeout, response size cap, per-domain rate limit, kill switch
  response  HTML -> text, scripts stripped; binaries reported, never returned

Built on the stdlib http.client instead of httpx: it lets the connection go to
the checked IP while keeping the hostname for TLS, which is the point of pinning.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from . import __version__, socketguard
from .htmltext import html_to_text
from .netpolicy import Blocked, Target, check_method, check_secrets, parse_url, redact, resolve

USER_AGENT = f"Perseus/{__version__} (read-only research agent; GET only)"
REDIRECTS = {301, 302, 303, 307, 308}
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml", "application/rss+xml",
              "application/atom+xml", "application/ld+json", "application/javascript")


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class FetchResult:
    url: str                     # final URL after redirects
    status: int
    content_type: str
    bytes: int                   # body bytes received (after decompression)
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False
    binary: bool = False
    redirects: list[str] = field(default_factory=list)
    ip: str = ""
    elapsed_s: float = 0.0


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        with socketguard.permit(self._pinned_ip, self.port):
            self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    default_port = 443

    def __init__(self, host, port, ip, timeout, context: ssl.SSLContext):
        super().__init__(host, port, ip, timeout)
        self._context = context

    def connect(self):
        super().connect()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _RateLimiter:
    """Token bucket per host: `rate` requests/second, bursts of `burst`."""

    def __init__(self, rate: float = 1.0, burst: int = 3, max_wait: float = 3.0):
        self.rate, self.burst, self.max_wait = rate, burst, max_wait
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, host: str, now=time.monotonic, sleep=time.sleep) -> None:
        with self._lock:
            tokens, last = self._buckets.get(host, (float(self.burst), now()))
            t = now()
            tokens = min(self.burst, tokens + (t - last) * self.rate)
            wait = 0.0 if tokens >= 1 else (1 - tokens) / self.rate
            if wait > self.max_wait:
                raise Blocked("rate_limit", f"too many requests to {host}; wait a few seconds")
            self._buckets[host] = (tokens - 1 + wait * self.rate, t + wait)
        if wait:
            sleep(wait)


class Gateway:
    def __init__(self, *, timeout_s: float = 15, max_response_kb: int = 2048, max_redirects: int = 3,
                 exact_secrets: tuple[str, ...] = (), resolver=socket.getaddrinfo, log_path: Path | None = None,
                 rate: float = 1.0, burst: int = 3, connection_factory=None):
        self.timeout_s = timeout_s
        self.max_bytes = max_response_kb * 1024
        self.max_redirects = max_redirects
        self.exact_secrets = tuple(s for s in exact_secrets if s)
        self.resolver = resolver
        self.log_path = log_path
        self.limiter = _RateLimiter(rate, burst)
        self.disabled = False
        self._ssl = ssl.create_default_context()
        self._factory = connection_factory  # tests: replaces the pinned connection classes
        self._log_lock = threading.Lock()

    # ---------------------------------------------------------------- kill switch
    def disable(self) -> None:
        self.disabled = True

    def enable(self) -> None:
        self.disabled = False

    def add_secret(self, s: str) -> None:
        if s:
            self.exact_secrets = self.exact_secrets + (s,)

    # ---------------------------------------------------------------- policy only
    def check(self, url: str, method: str = "GET") -> tuple[Target, str]:
        """Apply every rule up to the connection without connecting. Returns (target, pinned ip)."""
        if self.disabled:
            raise Blocked("disabled", "web access is switched off (kill switch)")
        check_method(method)
        check_secrets(url, self.exact_secrets)
        target = parse_url(url)
        return target, resolve(target, self.resolver)

    # ---------------------------------------------------------------- fetch
    def fetch(self, url: str, method: str = "GET") -> FetchResult:
        t0 = time.monotonic()
        method = method.upper()
        current, hops = url, []
        try:
            for hop in range(self.max_redirects + 1):
                target, ip = self.check(current, method)
                self.limiter.acquire(target.host)
                status, headers, body, truncated = self._request(target, ip, method, deadline=t0 + self.timeout_s * 2)
                location = headers.get("location")
                if status in REDIRECTS and location:
                    if hop == self.max_redirects:
                        raise Blocked("redirects", f"more than {self.max_redirects} redirects")
                    current = urljoin(target.url, location.strip())
                    hops.append(current)
                    continue  # the next hop is checked from the top: secrets, shape, address
                result = self._result(target, ip, status, headers, body, truncated, method, hops)
                result.elapsed_s = round(time.monotonic() - t0, 2)
                self._log(url, method, "ok", status=status, bytes=result.bytes, ms=int(result.elapsed_s * 1000),
                          final=result.url if hops else None)
                return result
        except Blocked as e:
            self._log(url, method, f"blocked:{e.code}", hops=len(hops), secret=e.code == "secret")
            raise
        except GatewayError as e:
            self._log(url, method, f"error:{e.code}")
            raise
        raise AssertionError("unreachable")

    def _request(self, target: Target, ip: str, method: str, deadline: float):
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, identity",
                   "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
                   "Accept-Language": "en;q=0.9, *;q=0.5", "Connection": "close"}
        if self._factory:
            conn = self._factory(target, ip, self.timeout_s)
        elif target.scheme == "https":
            conn = _PinnedHTTPS(target.host, target.port, ip, self.timeout_s, self._ssl)
        else:
            conn = _PinnedHTTP(target.host, target.port, ip, self.timeout_s)
        try:
            conn.request(method, target.request_path, headers=headers)  # no body argument: GET/HEAD only
            resp = conn.getresponse()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            if method == "HEAD" or resp.status in REDIRECTS:
                return resp.status, hdrs, b"", False
            body, truncated = self._read(resp, hdrs, deadline)
            return resp.status, hdrs, body, truncated
        except socketguard.GuardRefused as e:
            raise GatewayError("guard", str(e))
        except (socket.timeout, TimeoutError):
            raise GatewayError("timeout", f"{target.host} didn't answer within {self.timeout_s:g}s")
        except ssl.SSLError as e:
            raise GatewayError("tls", f"TLS failed for {target.host}: {e.reason or e}")
        except (ConnectionError, OSError, http.client.HTTPException) as e:
            raise GatewayError("connect", f"couldn't fetch from {target.host}: {type(e).__name__}")
        finally:
            conn.close()

    def _read(self, resp, hdrs: dict, deadline: float) -> tuple[bytes, bool]:
        enc = hdrs.get("content-encoding", "").lower().strip()
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip" else None
        if enc not in ("", "identity", "gzip"):
            raise GatewayError("encoding", f"unsupported content encoding {enc!r}")
        out = bytearray()
        while len(out) <= self.max_bytes:
            if time.monotonic() > deadline:
                raise GatewayError("timeout", "response took too long to download")
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            if dec:
                # cap decompressed output too: a small gzip can expand enormously
                chunk = dec.decompress(chunk, self.max_bytes + 1 - len(out))
            out += chunk
        truncated = len(out) > self.max_bytes
        return bytes(out[: self.max_bytes]), truncated

    def _result(self, target, ip, status, hdrs, body, truncated, method, hops) -> FetchResult:
        ctype_full = hdrs.get("content-type", "")
        ctype = ctype_full.split(";", 1)[0].strip().lower()
        r = FetchResult(url=target.url, status=status, content_type=ctype or "unknown", bytes=len(body),
                        truncated=truncated, redirects=hops, ip=ip)
        if method == "HEAD" or not body:
            return r
        sniff_html = not ctype and body[:512].lstrip().lower().startswith((b"<!doctype html", b"<html"))
        if not (ctype.startswith(TEXT_TYPES) or sniff_html):
            r.binary = True
            return r
        text = body.decode(_charset(ctype_full, body), errors="replace")
        if ctype in ("text/html", "application/xhtml+xml") or sniff_html:
            r.title, r.text, r.links = html_to_text(text, target.url)
        else:
            r.text = text
        return r

    def _log(self, url: str, method: str, outcome: str, secret: bool = False, **fields) -> None:
        if not self.log_path:
            return
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "method": method,
                "url": redact(url) if secret else url[:2048], "outcome": outcome,
                **{k: v for k, v in fields.items() if v is not None}}
        with self._log_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(line) + "\n")


def _charset(ctype: str, body: bytes) -> str:
    m = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", body[:2048], re.I)
        name = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    else:
        name = m.group(1)
    try:
        "".encode(name)
        return name
    except LookupError:
        return "utf-8"
