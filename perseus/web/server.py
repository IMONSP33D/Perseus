"""The dashboard's HTTP server (stdlib, one user).

Security, in order on every request:
  host      only IP literals and localhost in the Host header (DNS-rebinding guard)
  auth      an access token printed at boot; /?token=… swaps it for an HttpOnly,
            SameSite=Strict cookie. Everything except /healthz and /assets needs it.
  csrf      writes need the X-Perseus header and a same-origin Origin, if one is sent
  headers   strict CSP (no inline script or style), nosniff, no referrer, no framing
  limits    request bodies up to 1 MB
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import queue
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from perseus.web.dashboard import BadRequest, Busy, Dashboard

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "perseus_session"
MAX_BODY = 1024 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")

LOCKED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Perseus</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/assets/app.css"></head>
<body class="pz-light"><main class="locked"><img src="/assets/logo-full.svg" alt="Perseus" class="locked-logo">
<h1>Open the link from the terminal</h1><p>This dashboard needs the access link Perseus printed when it started
(it ends in <code>?token=…</code>). It is also saved on the box in <code>run/dashboard.url</code> inside
Perseus's data directory.</p></main></body></html>"""


class Server:
    def __init__(self, dash: Dashboard, bind: str = "127.0.0.1", port: int = 8080, token: str | None = None,
                 health: callable = None):
        self.dash = dash
        self.token = token or secrets.token_urlsafe(24)
        self.session = secrets.token_urlsafe(32)   # cookie value; the token itself never sits in the cookie
        self.health = health or (lambda: {"ok": True})
        handler = type("Handler", (_Handler,), {"app": self})
        self.httpd = _HTTPServer((bind, port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    def url(self, host: str = "127.0.0.1") -> str:
        h = f"[{host}]" if ":" in host else host
        return f"http://{h}:{self.port}/?token={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="dashboard")
        self._thread.start()

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a browser tab closed mid-request
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    app: Server
    server_version = "Perseus"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the terminal for Perseus's own output
        pass

    # ------------------------------------------------------------------ plumbing
    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None):
        self.send_response(code)
        if not self._consumed and int(self.headers.get("Content-Length") or 0):
            # answered without reading the body: close, or its bytes would be read as the next request
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, headers: dict | None = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store", **(headers or {})})

    def _error(self, code: int, message: str):
        self._json(code, {"error": message})

    def _security_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        m = re.fullmatch(r"\[([0-9a-f:.]+)\](?::\d+)?|([^:]+)(?::\d+)?", host)
        if not m:
            return False
        name = m.group(1) or m.group(2)
        if name in ("localhost",):
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return False

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), self.app.token):
            return True
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except Exception:
            return False
        c = jar.get(COOKIE)
        return bool(c) and hmac.compare_digest(c.value, self.app.session)

    def _same_origin_write(self) -> bool:
        if self.headers.get("X-Perseus") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            if n <= 8 * MAX_BODY:   # read and discard, so the client gets the 400 instead of a broken pipe
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                self._consumed = True
            raise BadRequest("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        self._consumed = True
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise BadRequest("body must be JSON")
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    # ------------------------------------------------------------------ dispatch
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        self._consumed = False
        try:
            if not self._host_ok():
                return self._error(421, "unexpected Host header; open the dashboard by IP or localhost")
            url = urlsplit(self.path)
            path = url.path
            if path == "/healthz":
                return self._json(200, self.app.health())
            if path.startswith("/assets/") and method == "GET":
                return self._static(path[len("/assets/"):])
            if path == "/" and method == "GET":
                return self._index(parse_qs(url.query))
            if not self._authed():
                return self._error(401, "not signed in: open the link Perseus printed at startup")
            if method != "GET" and not self._same_origin_write():
                return self._error(403, "cross-site request refused")
            return self._api(method, path, parse_qs(url.query))
        except BadRequest as e:
            self._error(400, str(e))
        except Busy as e:
            self._error(409, str(e))
        except KeyError:
            self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _index(self, query: dict):
        token = (query.get("token") or [""])[0]
        if token:
            if not hmac.compare_digest(token, self.app.token):
                return self._send(401, LOCKED_PAGE.encode(), "text/html; charset=utf-8")
            cookie = f"{COOKIE}={self.app.session}; HttpOnly; SameSite=Strict; Path=/"
            return self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie,
                                                        "Cache-Control": "no-store"})
        if not self._authed():
            return self._send(401, LOCKED_PAGE.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
        self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _static(self, name: str):
        if not re.fullmatch(r"[A-Za-z0-9_\-]+\.(css|js|svg|png|woff2|ico)", name):
            return self._error(404, "not found")
        p = STATIC_DIR / name
        if not p.is_file():
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or name.endswith(".js"):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ API
    def _api(self, method: str, path: str, query: dict):
        d = self.app.dash
        parts = [p for p in path.split("/") if p]
        if parts[:1] != ["api"]:
            return self._error(404, "not found")
        route = (method, *parts[1:])

        if route == ("GET", "state"):
            return self._json(200, d.state())
        if route == ("PATCH", "settings"):
            return self._json(200, {"settings": d.update_settings(self._body())})
        if route == ("POST", "model", "apply"):
            body = self._body()
            tokens = body.get("context")
            threading.Thread(target=self._apply_context, args=(tokens,), daemon=True).start() \
                if tokens is not None else None
            if tokens is not None:
                d.restarting = True   # visible to the next /api/state poll right away
            return self._json(202, {"restarting": tokens is not None})
        if route == ("GET", "chats"):
            return self._json(200, {"chats": d.list_chats()})
        if route == ("POST", "chats"):
            return self._json(201, d.new_chat().full())
        if route == ("DELETE", "chats"):
            d.delete_all()
            return self._json(200, {"chats": []})
        if route == ("GET", "export"):
            body = json.dumps(d.export_all(), ensure_ascii=False, indent=1).encode()
            name = time.strftime("perseus-chats-%Y%m%d-%H%M%S.json")
            return self._send(200, body, "application/json; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
        if len(parts) >= 3 and parts[1] == "chats":
            cid = parts[2]
            rest = tuple(parts[3:])
            if method == "GET" and rest == ():
                return self._json(200, d.get(cid).full())
            if method == "DELETE" and rest == ():
                d.delete(cid)
                return self._json(200, {"deleted": cid})
            if method == "POST" and rest == ("messages",):
                return self._stream(d.start_turn(cid, self._body().get("text", "")))
            if method == "POST" and rest == ("stop",):
                d.stop(cid)
                return self._json(200, {"stopping": True})
            if method == "GET" and len(rest) == 2 and rest[0] == "raw":
                return self._json(200, d.raw(cid, rest[1]))
        return self._error(404, "not found")

    def _apply_context(self, tokens):
        try:
            self.app.dash.apply_context(tokens)
        except (BadRequest, Busy) as e:
            self.app.dash.restarting = False
            self.app.dash.info["last_restart_error"] = str(e)
        except Exception as e:
            self.app.dash.restarting = False
            self.app.dash.info["last_restart_error"] = f"{type(e).__name__}: {e}"

    def _stream(self, q: "queue.Queue"):
        """Server-sent events for one answer. Text pieces are batched every ~40 ms."""
        self.send_response(200)
        self._consumed = True
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        self.close_connection = True
        alive = True
        pending: list[dict] = []

        def flush():
            nonlocal alive
            if not pending or not alive:
                pending.clear()
                return
            # merge consecutive text pieces of the same kind into one event
            merged: list[dict] = []
            for ev in pending:
                if merged and ev["type"] == "text" and merged[-1]["type"] == "text" and merged[-1]["thought"] == ev["thought"]:
                    merged[-1] = {**merged[-1], "text": merged[-1]["text"] + ev["text"]}
                else:
                    merged.append(ev)
            pending.clear()
            try:
                self.wfile.write("".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in merged).encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                alive = False   # the answer keeps running; the dashboard reloads the chat when it reconnects

        last = time.monotonic()
        while True:
            try:
                ev = q.get(timeout=0.04)
            except queue.Empty:
                flush()
                last = time.monotonic()
                continue
            if ev is None:
                break
            pending.append(ev)
            if ev["type"] != "text" or time.monotonic() - last > 0.04:
                flush()
                last = time.monotonic()
        flush()
