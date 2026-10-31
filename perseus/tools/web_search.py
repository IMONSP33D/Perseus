"""web_search: query the local SearXNG instance through ctx.search.

Takes a query string only, never a URL: the search client sends it to one
fixed loopback address. Reading a result goes through fetch_url and the gateway.
"""

from __future__ import annotations

from perseus.tools import ToolFailure, tool


@tool(description="Search the web. Returns titles, URLs and snippets from several search engines. "
                  "Read a result with fetch_url.",
      parameters={"type": "object", "properties": {
          "query": {"type": "string", "minLength": 1, "maxLength": 400, "description": "Search terms."},
          "max_results": {"type": "integer", "minimum": 1, "maximum": 15, "default": 8}},
          "required": ["query"]},
      lookup=True, timeout_s=20)
def web_search(ctx, query: str, max_results: int = 8):
    if ctx.search is None:
        raise ToolFailure("search unavailable right now; fetch_url still works if you know a URL")
    return ctx.search.search(query, max_results, engines=ctx.engines)
