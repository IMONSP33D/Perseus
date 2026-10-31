"""Template for your own tools. Copy to perseus/tools/<name>.py (no leading underscore) and edit.

Files in perseus/tools/ load at boot and reload before each answer when they change.
The docstring is what the model reads; the type hints become the argument schema.
A file that imports network, process or FFI libraries is refused: tools reach
the web only through ctx.gateway (read-only, checked) and ctx.search.
Set lookup=True on any tool that reads the web, so it counts toward the
research depth and its results are labelled untrusted.
"""

from typing import Annotated, Literal

from perseus.tools import ToolFailure, tool


@tool
def word_count(text: Annotated[str, "The text to count."],
               unit: Literal["words", "characters"] = "words") -> dict:
    """Count the words or characters in a piece of text."""
    return {unit: len(text.split()) if unit == "words" else len(text)}


@tool(lookup=True)
def page_size(ctx, url: Annotated[str, "Full http(s) URL."]) -> dict:
    """Fetch a page and report its title and length."""
    if ctx.gateway is None:
        raise ToolFailure("web access unavailable")
    page = ctx.gateway.fetch(url)
    if page.binary:
        raise ToolFailure("that URL isn't a text page")
    return {"url": page.url, "title": page.title, "words": len(page.text.split())}


@tool(description="Repeat a short phrase.",
      parameters={"type": "object", "properties": {
          "phrase": {"type": "string", "maxLength": 200},
          "times": {"type": "integer", "minimum": 1, "maximum": 10, "default": 2}},
          "required": ["phrase"]})
def repeat(phrase: str, times: int = 2) -> str:
    return " ".join([phrase] * times)
