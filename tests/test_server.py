"""Dashboard server: auth, CSRF/Host checks, chats API, streaming turns, settings, restart, raw view."""

import http.client
import json
import threading
import time
import unittest

from perseus.web.dashboard import Dashboard
from perseus.engine.feed import Feed
from perseus.model.client import Reply
from perseus.web.server import COOKIE, Server
from perseus.tools import Registry, ToolContext, tool


class StreamingLLM:
    """Streams reasoning then content; calls `calc` once when the prompt asks for math."""

    def __init__(self):
        self.requests = []

    def chat(self, messages, tools=None, tool_choice="auto", max_tokens=None, on_text=None, should_stop=None, extra=None):
        self.requests.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []],
                              "max_tokens": max_tokens, "extra": extra})
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if "slow" in user:
            for _ in range(200):
                if should_stop and should_stop():
                    return Reply(content="partial", finish_reason="stopped")
                time.sleep(0.02)
        if "math" in user and messages[-1]["role"] != "tool":
            return Reply(tool_calls=[{"id": "c1", "type": "function",
                                      "function": {"name": "calc", "arguments": '{"x": 2}'}}],
                         finish_reason="tool_calls", usage={"prompt_tokens": 100, "completion_tokens": 5})
        for piece, thought in (("Let me think.", True), ("Hello ", False), ("**world**", False)):
            if on_text:
                on_text(piece, thought)
        return Reply(content="Hello **world**", reasoning="Let me think.", finish_reason="stop",
                     usage={"prompt_tokens": 120, "completion_tokens": 7}, first_token_s=0.01)


def make(**over):
    reg = Registry()

    @tool(description="double", registry=reg, parameters={"type": "object", "properties": {"x": {"type": "number"}},
                                                          "required": ["x"]})
    def calc(ctx, x):
        return {"y": x * 2}

    @tool(description="web", registry=reg, lookup=True, parameters={"type": "object", "properties": {}})
    def fetch_url(ctx):
        return {}

    restarts = []
    info = {"model": "Qwen/Qwen3-32B", "hardware": "1 × RTX 4090 (24 GiB)", "precision": "AWQ", "context": 32768,
            "capacity": 40960, "thinks": True}
    llm = StreamingLLM()
    dash = Dashboard(info=info, llm=llm, registry=reg, ctx=ToolContext(), depths={"quick": 2, "standard": 8, "deep": 20},
                     engine_settings={"max_context": 32768}, engine_list=["duckduckgo", "wikipedia"],
                     status=lambda: {"vLLM": {"state": "up", "restarts": 0}},
                     restart_model=lambda n: (restarts.append(n), time.sleep(0.2)),
                     raw_prompt=lambda msgs, tools: ("<|im_start|>system ...", 321), **over)
    return dash, llm, restarts


class Client:
    def __init__(self, port, cookie=None, host=None):
        self.port, self.cookie, self.host = port, cookie, host

    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"X-Perseus": "1"}
        if self.cookie:
            h["Cookie"] = f"{COOKIE}={self.cookie}"
        if self.host:
            h["Host"] = self.host
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        if raw:
            return r, data
        return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else data)

    def stream(self, cid, text):
        r, data = self.req("POST", f"/api/chats/{cid}/messages", {"text": text}, raw=True)
        events = [json.loads(chunk[5:]) for chunk in data.decode().split("\n\n") if chunk.startswith("data:")]
        return r.status, events


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.dash, self.llm, self.restarts = make()
        self.srv = Server(self.dash, "127.0.0.1", 0, token="tok-123")
        self.srv.start()
        self.anon = Client(self.srv.port)
        self.c = Client(self.srv.port, self.srv.session)

    def tearDown(self):
        self.srv.stop()

    # ---------------------------------------------------------------- security
    def test_token_login_sets_cookie_and_redirects(self):
        r, _ = self.anon.req("GET", "/?token=tok-123", raw=True)
        self.assertEqual(r.status, 303)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("tok-123", cookie)            # the token never sits in the cookie
        self.assertEqual(r.getheader("Location"), "/")
        r, _ = self.anon.req("GET", "/?token=wrong", raw=True)
        self.assertEqual(r.status, 401)

    def test_everything_needs_auth_except_health_and_assets(self):
        self.assertEqual(self.anon.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.anon.req("POST", "/api/chats", {})[0], 401)
        r, page = self.anon.req("GET", "/", raw=True)
        self.assertEqual(r.status, 401)
        self.assertIn(b"Open the link from the terminal", page)
        self.assertEqual(self.anon.req("GET", "/healthz")[0], 200)
        self.assertEqual(self.anon.req("GET", "/assets/app.css", raw=True)[0].status, 200)
        self.assertEqual(Client(self.srv.port).req(
            "GET", "/api/state", headers={"Authorization": "Bearer tok-123"})[0], 200)

    def test_csrf_and_host_checks(self):
        st, _ = self.c.req("POST", "/api/chats", {}, headers={"X-Perseus": ""})
        self.assertEqual(st, 403)
        st, _ = self.c.req("POST", "/api/chats", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _ = Client(self.srv.port, self.srv.session, host="evil.example:8080").req("GET", "/api/state")
        self.assertEqual(st, 421)           # DNS-rebinding guard
        st, _ = Client(self.srv.port, self.srv.session, host="localhost:8080").req("GET", "/api/state")
        self.assertEqual(st, 200)

    def test_security_headers_and_static_paths(self):
        r, _ = self.c.req("GET", "/", raw=True)
        self.assertEqual(r.status, 200)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        for bad in ("/assets/../server.py", "/assets/%2e%2e/server.py", "/assets/x.py", "/assets/"):
            self.assertEqual(self.anon.req("GET", bad, raw=True)[0].status, 404, bad)

    # ---------------------------------------------------------------- chats and turns
    def test_turn_streams_text_tools_and_done(self):
        st, chat = self.c.req("POST", "/api/chats", {})
        self.assertEqual(st, 201)
        st, events = self.c.stream(chat["id"], "say hello")
        self.assertEqual(st, 200)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "done")
        thought = "".join(e["text"] for e in events if e["type"] == "text" and e["thought"])
        content = "".join(e["text"] for e in events if e["type"] == "text" and not e["thought"])
        self.assertEqual((thought, content), ("Let me think.", "Hello **world**"))
        done = events[-1]
        self.assertEqual(done["message"]["content"], "Hello **world**")
        self.assertEqual(done["context_used"], 127)
        self.assertFalse(done["message"]["streaming"])
        st, full = self.c.req("GET", f"/api/chats/{chat['id']}")
        self.assertEqual(full["title"], "say hello")
        self.assertEqual([m["role"] for m in full["messages"]], ["user", "assistant"])
        self.assertFalse(full["busy"])

    def test_tool_events_reach_the_stream_and_feed(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        _, events = self.c.stream(chat["id"], "do math")
        tools = [e["event"] for e in events if e["type"] == "tool"]
        self.assertEqual([t["status"] for t in tools], ["running", "ok"])
        self.assertEqual(tools[-1]["tool"], "calc")
        _, full = self.c.req("GET", f"/api/chats/{chat['id']}")
        self.assertEqual(len(full["feed"]), 1)
        self.assertEqual(full["messages"][1]["tool_ids"], [tools[-1]["id"]])

    def test_stop_and_busy(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        out = {}
        t = threading.Thread(target=lambda: out.update(r=self.c.stream(chat["id"], "slow please")))
        t.start()
        time.sleep(0.3)
        st, _ = self.c.req("POST", f"/api/chats/{chat['id']}/messages", {"text": "again"})
        self.assertEqual(st, 409)                          # one answer at a time per chat
        self.assertEqual(self.c.req("POST", f"/api/chats/{chat['id']}/stop", {})[0], 200)
        t.join(5)
        done = out["r"][1][-1]
        self.assertTrue(done["message"]["stopped"])

    def test_close_out_wraps_up(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        self.assertEqual(self.c.req("POST", f"/api/chats/{chat['id']}/closeout", {})[0], 400)   # nothing running
        out = {}
        t = threading.Thread(target=lambda: out.update(r=self.c.stream(chat["id"], "slow please")))
        t.start()
        time.sleep(0.3)
        st, body = self.c.req("POST", f"/api/chats/{chat['id']}/closeout", {})
        self.assertEqual((st, body["seconds"]), (200, 10.0))
        t.join(5)
        events = out["r"][1]
        self.assertIn("closing", [e.get("phase") for e in events if e["type"] == "phase"])
        done = events[-1]["message"]
        self.assertEqual(done["closed"], "user")
        self.assertFalse(done["stopped"])
        self.assertIn("world", done["content"])                          # the wrap-up answer streamed in
        wrap = self.llm.requests[-1]
        self.assertEqual(wrap["tools"], [])                               # no tools while wrapping up
        self.assertFalse(wrap["extra"]["chat_template_kwargs"]["enable_thinking"])

    def test_thinking_gets_its_own_budget(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        self.c.stream(chat["id"], "hello")                 # defaults: Length medium, Thinking medium
        self.assertEqual(self.llm.requests[-1]["max_tokens"], 4096 + 8192)
        self.c.req("PATCH", "/api/settings", {"thinking": "off"})
        self.c.stream(chat["id"], "hello")
        self.assertEqual(self.llm.requests[-1]["max_tokens"], 4096)

    def test_settings_shape_the_next_request(self):
        self.c.req("PATCH", "/api/settings", {"internet": False, "persona": "engineer", "creativity": "precise",
                                               "length": "short", "thinking": "off", "engines": {"wikipedia": False}})
        _, chat = self.c.req("POST", "/api/chats", {})
        self.c.stream(chat["id"], "hello")
        req = self.llm.requests[-1]
        self.assertNotIn("fetch_url", req["tools"])                         # internet off: no web tools
        self.assertIn("senior software engineer", req["messages"][0]["content"])
        self.assertEqual(req["extra"]["temperature"], 0.2)
        self.assertEqual(req["extra"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(req["max_tokens"], 1024)
        self.assertEqual(self.dash.turn_options().engines, ["duckduckgo"])
        for bad in ({"thinking": "max"}, {"nope": 1}, {"engines": {"google": True}}, {"internet": "yes"},
                    {"custom_prompt": "x" * 9000}):
            self.assertEqual(self.c.req("PATCH", "/api/settings", bad)[0], 400, bad)

    def test_custom_prompt(self):
        self.c.req("PATCH", "/api/settings", {"persona": "custom", "custom_prompt": "Answer like a pirate."})
        _, chat = self.c.req("POST", "/api/chats", {})
        self.c.stream(chat["id"], "hi")
        self.assertTrue(self.llm.requests[-1]["messages"][0]["content"].startswith("Answer like a pirate."))

    def test_context_options_and_restart(self):
        _, st = self.c.req("GET", "/api/state")
        opts = {o["id"]: o for o in st["context_options"]}
        self.assertTrue(opts["32k"]["current"])
        self.assertFalse(opts["128k"]["available"])           # above this box's 40,960-token capacity
        self.assertEqual(opts["max"]["tokens"], 40960)
        st, body = self.c.req("POST", "/api/model/apply", {"context": 8192})
        self.assertEqual((st, body["restarting"]), (202, True))
        self.assertTrue(self.c.req("GET", "/api/state")[1]["restarting"])
        deadline = time.time() + 5
        while self.c.req("GET", "/api/state")[1]["restarting"] and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.restarts, [8192])
        self.assertEqual(self.c.req("GET", "/api/state")[1]["info"]["context"], 8192)
        # small-GPU case: preflight rounded its pick below the KV capacity, so no step matches
        self.dash.info.update(context=13312, capacity=14081)
        opts = self.c.req("GET", "/api/state")[1]["context_options"]
        cur = [o for o in opts if o["current"]]
        self.assertEqual([(o["id"], o["tokens"]) for o in cur], [("max", 13312)])   # Max rounded like preflight
        # a hand-set max_context that isn't a step shows as its own option
        self.dash.info.update(context=20480, capacity=40960)
        opts = self.c.req("GET", "/api/state")[1]["context_options"]
        cur = [o for o in opts if o["current"]]
        self.assertEqual([(o["id"], o["label"], o["tokens"]) for o in cur], [("current", "20K", 20480)])
        self.dash.info.update(context=8192, capacity=40960)
        self.c.req("POST", "/api/model/apply", {"context": 999_999})
        time.sleep(0.2)
        self.assertIn("between", self.c.req("GET", "/api/state")[1]["info"]["last_restart_error"])

    def test_raw_view_and_export_and_delete(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        _, events = self.c.stream(chat["id"], "do math")
        mid = events[-1]["message"]["id"]
        st, raw = self.c.req("GET", f"/api/chats/{chat['id']}/raw/{mid}")
        self.assertEqual(st, 200)
        self.assertEqual(raw["prompt_tokens"], 321)
        self.assertEqual(raw["steps"], 2)
        self.assertEqual(raw["outputs"][0]["tool_calls"][0]["function"]["name"], "calc")
        r, body = self.c.req("GET", "/api/export", raw=True)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        doc = json.loads(body)
        self.assertEqual(len(doc["chats"]), 1)
        self.assertEqual(doc["chats"][0]["tool_feed"][0]["tool"], "calc")
        self.assertEqual(self.c.req("DELETE", f"/api/chats/{chat['id']}")[0], 200)
        self.assertEqual(self.c.req("GET", f"/api/chats/{chat['id']}")[0], 404)
        self.c.req("POST", "/api/chats", {})
        self.c.req("DELETE", "/api/chats")
        self.assertEqual(self.c.req("GET", "/api/chats")[1]["chats"], [])

    def test_bad_requests(self):
        _, chat = self.c.req("POST", "/api/chats", {})
        self.assertEqual(self.c.req("POST", f"/api/chats/{chat['id']}/messages", {"text": "  "})[0], 400)
        self.assertEqual(self.c.req("POST", "/api/chats/nope/messages", {"text": "hi"})[0], 404)
        st, body = self.c.req("PATCH", "/api/settings", {"custom_prompt": "x" * (1024 * 1024 + 10)})
        self.assertEqual((st, body["error"]), (400, "request too large"))
        self.assertEqual(self.c.req("GET", "/api/nothing")[0], 404)


if __name__ == "__main__":
    unittest.main()
