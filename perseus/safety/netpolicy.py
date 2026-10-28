"""The gateway's rules as pure functions: URL shape, address class, secrets.

Nothing here touches the network except `resolve`, which takes the resolver as
an argument so tests and the self-test can use a fake one.

Every refusal raises `Blocked` with a short machine code (shown in the tool feed
as "blocked: <code>") and a sentence for the model.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

MAX_URL_LENGTH = 2048
SCHEMES = {"http": 80, "https": 443}
PORTS = {80, 443}
METHODS = {"GET", "HEAD"}

# Names that are internal by definition, refused before any DNS lookup.
INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")
INTERNAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata",
                  "metadata.google.internal", "instance-data", "wpad"}

# Not covered by ipaddress.is_global in every Python version, refused explicitly.
EXTRA_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8", "2001:db8::/32", "100::/64",
)]
NAT64 = ipaddress.ip_network("64:ff9b::/96")
SIXTOFOUR = ipaddress.ip_network("2002::/16")

SECRET_PATTERNS = [
    ("Hugging Face token", re.compile(r"hf_[A-Za-z0-9]{20,}")),
    ("Anthropic/OpenAI-style key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("AWS access key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("Slack token", re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("GitLab token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}")),
    ("private key", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY")),
    # Long hex: 48+ characters. Git commit SHAs (40) stay allowed for research links.
    ("long hex string", re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{48,}(?![0-9a-fA-F])")),
]
# Long base64-ish run with upper, lower and digits mixed: how random keys look.
B64_RUN = re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}")


class Blocked(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str          # ASCII (IDNA) hostname or IP literal, no brackets
    port: int
    request_path: str  # path + query, never a fragment
    is_ip_literal: bool

    @property
    def origin(self) -> str:
        h = f"[{self.host}]" if ":" in self.host else self.host
        default = SCHEMES[self.scheme] == self.port
        return f"{self.scheme}://{h}" + ("" if default else f":{self.port}")

    @property
    def url(self) -> str:
        return self.origin + self.request_path


def check_method(method: str) -> str:
    m = method.upper()
    if m not in METHODS:
        raise Blocked("method", f"only GET and HEAD are allowed; {method!r} is refused")
    return m


def parse_url(url: str) -> Target:
    if not isinstance(url, str) or not url:
        raise Blocked("url", "no URL given")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise Blocked("url_length", f"URL is {len(url)} characters; the limit is {MAX_URL_LENGTH}")
    if re.search(r"[\x00-\x20\x7f\\]", url):
        raise Blocked("url", "URL contains spaces, control characters or backslashes")
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise Blocked("url", f"malformed URL ({e})")
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise Blocked("scheme", f"only http and https URLs are allowed; got {scheme or 'none'!r}")
    netloc = parts.netloc
    if "@" in netloc:
        raise Blocked("userinfo", "URLs with a user name or password (user@host) are refused")
    if not netloc:
        raise Blocked("url", "URL has no host")
    try:
        port = parts.port
    except ValueError:
        raise Blocked("port", "URL has an invalid port")
    port = port if port is not None else SCHEMES[scheme]
    if port not in PORTS:
        raise Blocked("port", f"only ports 80 and 443 are allowed; got {port}")

    raw_host = parts.hostname or ""
    host, is_ip = _normalize_host(raw_host, bracketed=netloc.lstrip().startswith("["))
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    request_path = path + (f"?{parts.query}" if parts.query else "")
    return Target(scheme, host, port, request_path, is_ip)


def _normalize_host(host: str, bracketed: bool) -> tuple[str, bool]:
    host = host.lower().rstrip(".")
    if not host:
        raise Blocked("url", "URL has no host")
    if bracketed or ":" in host:
        try:
            ip = ipaddress.IPv6Address(host.split("%", 1)[0])
        except ValueError:
            raise Blocked("url", f"invalid IPv6 address {host!r}")
        if "%" in host:
            raise Blocked("internal_address", "scoped IPv6 addresses are refused")
        return str(ip), True
    labels = host.split(".")
    if all(re.fullmatch(r"0x[0-9a-f]*|[0-9]+", lab) for lab in labels):
        # Looks like an IPv4 literal; resolvers accept many encodings
        # (2130706433, 0x7f000001, 0177.0.0.1, 127.1). Only canonical dotted quads pass.
        if len(labels) == 4 and all(re.fullmatch(r"0|[1-9][0-9]{0,2}", lab) and int(lab) <= 255 for lab in labels):
            return host, True
        raise Blocked("encoded_ip", f"{host!r} is a numeric-encoded address; use a normal hostname")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise Blocked("url", f"invalid hostname {host!r}")
    if ascii_host != host:
        # IDNA maps fullwidth digits and dots to ASCII ("１２７．０．０．１" -> "127.0.0.1"): check again
        return _normalize_host(ascii_host, bracketed=False)
    if len(ascii_host) > 253 or any(not lab or len(lab) > 63 for lab in ascii_host.split(".")):
        raise Blocked("url", f"invalid hostname {host!r}")
    if not re.fullmatch(r"[a-z0-9.\-_]+", ascii_host):
        raise Blocked("url", f"invalid characters in hostname {host!r}")
    if ascii_host in INTERNAL_NAMES or ascii_host.endswith(INTERNAL_SUFFIXES) or "." not in ascii_host:
        raise Blocked("internal_name", f"{ascii_host!r} is an internal name; only public sites are allowed")
    return ascii_host, False


def address_problem(ip_text: str) -> str | None:
    """Why this address is not public, or None if it is."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return f"unparseable address {ip_text!r}"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return address_problem(str(ip.ipv4_mapped))
        if ip in NAT64:
            return address_problem(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in SIXTOFOUR:
            return address_problem(str(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)))
        if ip.teredo:
            return "Teredo tunnel address"
    for net in EXTRA_BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return _label(ip)
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return _label(ip)
    return None


def _label(ip) -> str:
    if ip.is_loopback:
        return f"{ip} is a loopback address (this machine)"
    if str(ip) in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200"):
        return f"{ip} is the cloud metadata endpoint"
    if ip.is_link_local:
        return f"{ip} is a link-local address"
    if ip.is_private:
        return f"{ip} is a private network address"
    if ip.is_multicast:
        return f"{ip} is a multicast address"
    return f"{ip} is not a public internet address"


def check_ip_literal(target: Target) -> None:
    if target.is_ip_literal:
        problem = address_problem(target.host)
        if problem:
            raise Blocked("internal_address", problem)


def resolve(target: Target, resolver=socket.getaddrinfo) -> str:
    """Resolve once; refuse if ANY answer is internal; return the address to pin."""
    if target.is_ip_literal:
        check_ip_literal(target)
        return target.host
    try:
        infos = resolver(target.host, target.port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise Blocked("dns", f"couldn't resolve {target.host} ({e})")
    addrs = list(dict.fromkeys(info[4][0] for info in infos))
    if not addrs:
        raise Blocked("dns", f"{target.host} has no addresses")
    for a in addrs:
        problem = address_problem(a)
        if problem:
            raise Blocked("internal_address", f"{target.host} resolves to {problem}")
    v4 = [a for a in addrs if ":" not in a]
    return (v4 or addrs)[0]


def secret_problem(url: str, exact: tuple[str, ...] = ()) -> str | None:
    """Name of the secret-like string in this URL, or None. Checks raw and %-decoded forms."""
    forms = {url, unquote(url), unquote(unquote(url))}
    for form in forms:
        for s in exact:
            if s and s in form:
                return "a Perseus key"
        for name, pat in SECRET_PATTERNS:
            if pat.search(form):
                return name
        for run in B64_RUN.findall(form):
            body = run.rstrip("=")
            if (re.search(r"[A-Z]", body) and re.search(r"[a-z]", body) and re.search(r"[0-9]", body)
                    and len(set(body)) >= 16):
                return "long random-looking string"
    return None


def check_secrets(url: str, exact: tuple[str, ...] = ()) -> None:
    found = secret_problem(url, exact)
    if found:
        raise Blocked("secret", f"URL contains what looks like a {found}; requests can't carry keys or tokens")


def redact(url: str) -> str:
    """For logs: keep the origin, drop a path/query that may hold a secret."""
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.hostname or '?'}/[redacted]"
    except ValueError:
        return "[redacted]"
