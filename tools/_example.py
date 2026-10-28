"""Template for your own tools. Copy to tools/<name>.py (no leading underscore) and edit.

Files in tools/ load at boot and reload before each answer when they change.
A file that imports network, process or FFI libraries is refused: tools reach
the web only through ctx.gateway (read-only, checked) and ctx.search.
Set lookup=True on any tool that reads the web, so it counts toward the
research depth and its results are labelled untrusted.
"""

from perseus.tools import ToolFailure, tool


@tool(description="Count the words in a piece of text.",
      parameters={"type": "object", "properties": {
          "text": {"type": "string", "maxLength": 100_000, "description": "The text to count."}},
          "required": ["text"]})
def word_count(ctx, text: str):
    return {"words": len(text.split()), "characters": len(text)}


@tool(description="Fetch a page and report its title and length.", lookup=True,
      parameters={"type": "object", "properties": {
          "url": {"type": "string", "pattern": "^https?://", "maxLength": 2048}},
          "required": ["url"]})
def page_size(ctx, url: str):
    page = ctx.gateway.fetch(url)          # every gateway rule applies
    if page.binary:
        raise ToolFailure("that URL isn't a text page")
    return {"url": page.url, "title": page.title, "words": len(page.text.split())}
