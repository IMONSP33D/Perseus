"""Client for vLLM's OpenAI-compatible endpoint on loopback.

Streams by default and reassembles tool calls from their deltas, so the same
call feeds a live dashboard (on_text) and the tool loop (the finished message).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


class LLMError(Exception):
    pass


@dataclass
class Reply:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # OpenAI shape: {id, type, function: {name, arguments}}
    finish_reason: str | None = None
    first_token_s: float | None = None
    usage: dict = field(default_factory=dict)

    def message(self) -> dict:
        """The assistant message to append to the conversation."""
        m: dict = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            m["tool_calls"] = self.tool_calls
        return m


class LLMClient:
    def __init__(self, port: int, key: str, model: str, timeout_s: float = 600):
        self.base = f"http://127.0.0.1:{int(port)}"
        self.key, self.model, self.timeout_s = key, model, timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path: str, body: dict, timeout: float):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/json"})
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read(2000).decode(errors="replace")
            raise LLMError(f"vLLM returned {e.code}: {detail[:300]}")
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"vLLM unreachable: {e}")

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str = "auto",
             max_tokens: int | None = None, on_text: Callable[[str, bool], None] | None = None,
             should_stop: Callable[[], bool] | None = None, extra: dict | None = None) -> Reply:
        body: dict = {"model": self.model, "messages": messages, "stream": True,
                      "stream_options": {"include_usage": True}}
        if tools:
            body["tools"], body["tool_choice"] = tools, tool_choice
        if max_tokens:
            body["max_tokens"] = max_tokens
        body.update(extra or {})
        t0 = time.monotonic()
        reply = Reply()
        calls: dict[int, dict] = {}
        with self._post("/v1/chat/completions", body, self.timeout_s) as resp:
            for raw in resp:
                if should_stop and should_stop():
                    reply.finish_reason = "stopped"
                    break
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    reply.usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    thought = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if (text or thought or delta.get("tool_calls")) and reply.first_token_s is None:
                        reply.first_token_s = time.monotonic() - t0
                    if thought:
                        reply.reasoning += thought
                        if on_text:
                            on_text(thought, True)
                    if text:
                        reply.content += text
                        if on_text:
                            on_text(text, False)
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)),
                                                {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        slot["function"]["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        reply.finish_reason = choice["finish_reason"]
        reply.tool_calls = [calls[i] for i in sorted(calls)]
        for n, c in enumerate(reply.tool_calls):
            c["id"] = c["id"] or f"call_{n}"
        return reply
