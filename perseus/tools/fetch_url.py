"""fetch_url: read one public page through ctx.gateway (GET only, every gateway rule applies)."""

from __future__ import annotations

from perseus.tools import ToolFailure, tool


@tool(description="Read a public web page (HTTP GET, read-only) and return its text, title and links. "
                  "Only public http/https sites; internal addresses and URLs carrying keys are refused.",
      parameters={"type": "object", "properties": {
          "url": {"type": "string", "minLength": 8, "maxLength": 2048, "pattern": "^https?://",
                  "description": "Full http(s) URL."}},
          "required": ["url"]},
      lookup=True, timeout_s=45)
def fetch_url(ctx, url: str):
    if ctx.gateway is None:
        raise ToolFailure("web access unavailable")
    r = ctx.gateway.fetch(url)  # Blocked / GatewayError propagate to the engine, which labels them
    out = {"url": r.url, "status": r.status, "content_type": r.content_type, "title": r.title}
    if r.binary:
        out["note"] = f"binary content ({r.content_type}, {r.bytes:,} bytes) not shown"
    else:
        out["text"] = r.text
        if r.links:
            out["links"] = [{"text": t, "url": u} for t, u in r.links[:15]]
    if r.truncated:
        out["note"] = "page was larger than the response limit and was cut off"
    if r.redirects:
        out["redirected_from"] = url
    return out
