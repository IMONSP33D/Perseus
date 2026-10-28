import http.server
import json
import threading
import unittest

from perseus.llm import LLMClient, LLMError
from perseus.searxng import SearchClient, SearchUnavailable


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/search"):
            body = json.dumps({"results": [
                {"title": "vLLM", "url": "https://github.com/vllm-project/vllm", "content": "  fast   inference ",
                 "engines": ["github", "duckduckgo"]},
                {"title": "bad", "url": "javascript:alert(1)", "content": "x", "engines": ["mojeek"]},
                {"title": "Paper", "url": "https://arxiv.org/abs/2309.06180", "content": "PagedAttention",
                 "engine": "arxiv"}],
                "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.end_headers()

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer k":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"bad key"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [{"choices": [{"delta": {"reasoning_content": "thinking…"}}]},
                  {"choices": [{"delta": {"content": "Let me "}}]},
                  {"choices": [{"delta": {"content": "check."}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "web_",
                                                                                              "arguments": '{"qu'}}]}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "search",
                                                                                   "arguments": 'ery": "vllm"}'}}]}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"name": "calculator",
                                                                                   "arguments": '{"expression":"1+1"}'}}]}}]},
                  {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                  {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 9}}]
        self.server.last = req
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class Clients(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_stream_reassembles_tool_calls(self):
        seen = []
        r = LLMClient(self.port, "k", "m").chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}],
                                                on_text=lambda t, thought: seen.append((t, thought)))
        self.assertEqual(r.content, "Let me check.")
        self.assertEqual(r.reasoning, "thinking…")
        self.assertEqual([c["function"]["name"] for c in r.tool_calls], ["web_search", "calculator"])
        self.assertEqual(json.loads(r.tool_calls[0]["function"]["arguments"]), {"query": "vllm"})
        self.assertEqual([c["id"] for c in r.tool_calls], ["call_a", "call_1"])  # missing id filled in
        self.assertEqual(r.finish_reason, "tool_calls")
        self.assertEqual(r.usage["completion_tokens"], 9)
        self.assertEqual(seen[0], ("thinking…", True))
        self.assertEqual(self.srv.last["tool_choice"], "auto")
        self.assertEqual(r.message()["tool_calls"], r.tool_calls)

    def test_bad_key_is_an_error(self):
        with self.assertRaises(LLMError) as e:
            LLMClient(self.port, "wrong", "m").chat([{"role": "user", "content": "x"}])
        self.assertIn("401", str(e.exception))

    def test_search_results(self):
        out = SearchClient(self.port).search("vllm", 5)
        self.assertEqual([r["url"] for r in out["results"]],
                         ["https://github.com/vllm-project/vllm", "https://arxiv.org/abs/2309.06180"])
        self.assertEqual(out["results"][0]["snippet"], "fast inference")
        self.assertEqual(out["results"][1]["engines"], ["arxiv"])
        self.assertEqual(out["engines_answered"], ["arxiv", "duckduckgo", "github"])
        self.assertEqual(out["engines_unresponsive"], ["duckduckgo"])

    def test_search_down(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        with self.assertRaises(SearchUnavailable):
            SearchClient(free, timeout_s=1).search("x")


if __name__ == "__main__":
    unittest.main()
