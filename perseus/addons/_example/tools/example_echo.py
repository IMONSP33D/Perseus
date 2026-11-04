"""example_echo: the addon's tool, loaded like any file in perseus/tools/ (see perseus/tools/_example.py).

    requires="example"   offered to the model only once the launcher has provided the addon's capability
    ctx.example          the client from addon.py's client(); None when the addon is off
No network, process or FFI imports here: the client does the talking, the service holds the key.
"""

from __future__ import annotations

from typing import Annotated

from perseus.tools import ToolFailure, tool


@tool(requires="example", untrusted=True, timeout_s=330)
def example_echo(ctx, text: Annotated[str, "The text to send."],
                 timeout_s: Annotated[int, "Seconds to wait, up to the configured limit."] = 30) -> dict:
    """Send text to the example service and return its answer."""
    if ctx.example is None:
        raise ToolFailure("the example service is off; answer without it")
    r = ctx.example.echo(text, timeout_s)
    return {"text": r.get("text", ""),
            "feed": {"badge": "Echoed", "rows": [("length", str(len(r.get("text", ""))))]}}
