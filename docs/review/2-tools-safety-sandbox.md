# Review 2 of 4: tools, safety and sandbox

Scope: `perseus/tools/` (the registry and the built-in tools), `perseus/safety/` (gateway, netpolicy, socketguard, htmltext, selftest), `perseus/sandbox/` and `perseus/search.py`.

The main question for this segment was how easy it is to add a tool. Before this review, a new tool needed a hand-written JSON Schema, even for a one-argument function. Now a typed function with a docstring is enough.

## Headline change: tools from a plain typed function

```python
from typing import Annotated
from perseus.tools import tool

@tool
def word_count(text: Annotated[str, "The text to count."]) -> dict:
    """Count the words in a piece of text."""
    return {"words": len(text.split())}
```

- **Bare `@tool`.** Also works as `@tool(lookup=True, timeout_s=…)`. The description comes from the docstring and the schema from the type hints (`schema_from_signature`).
- **Supported hints.**
  - Basic types: `str`, `int`, `float`, `bool`, `dict`, `list[...]`.
  - Choices: `Literal[...]` becomes an enum.
  - Optional values: `X | None` accepts `null`.
  - Help text: `Annotated[T, "help"]` becomes the argument's description.
  - Defaults become schema defaults.
  - `*args` / `**kwargs`, and hints with no JSON equivalent, fail at load with a clear message.
- **`ctx` is optional.** Take `ctx` as the first parameter only when you need the gateway, search or sandbox. With an explicit `parameters=`, a first parameter that isn't one of the schema's arguments still gets the context, whatever it is called. That keeps every existing tool working.
- **Fully backwards compatible.** `@tool(description=..., parameters={...})` works as before, and every built-in tool is unchanged. Use `parameters=` when you need `maxLength`, `pattern`, `minimum` and the like.
- **`_example.py` and the README** now teach the short form first and the explicit schema second.

## Fixes

| # | Severity | File | What was wrong | Fix |
|---|---|---|---|---|
| 1 | High | `tools/calculator.py`, `engine/loop.py` | `(-8)**(1/3)` produced a complex number. `json.dumps` then raised outside the engine's error handling, which aborted the turn. | The calculator refuses complex results with a `ToolFailure`. The engine also catches render errors, so a tool that returns something it can't serialise gets an error event instead of crashing the turn. |
| 2 | High | `safety/htmltext.py` | Real pages came back empty. HTML5 lets a page leave out `</head>`, which made everything after it count as head. ASP.NET wraps the whole page in `<form>`, which was skipped. An inline SVG `<title>` was added to the page title. | `form` is no longer skipped. A body-level tag ends `<head>`. Only the first `<title>` outside skipped content counts. |
| 3 | High | `tools/__init__.py` | Two files defining the same tool name silently replaced each other. Deleting the copy then removed the original tool until restart. | `Registry.add` refuses a name already defined by another file. The loader reports this as a load error. |
| 4 | High (UX) | `web/dashboard.py`, `web/static/app.js`, `launcher.py` | A tool file that failed to load was only reported in the terminal, so tool authors never saw the reason on the dashboard. | `/api/tools` returns `errors`, and the Agent Functions page shows each failed file and why, the same way the pipelines page does. |
| 5 | Medium | `tools/run_code.py` | `timeout_s=330` was below the longest run the config allows (build 180 s + run 120 s + 90 s overhead = 390 s). Long builds were reported as timeouts while the broker kept running them. | `timeout_s=400`. |
| 6 | Medium | `engine/tool_loader.py` | The import check could be bypassed with `import os as o; o.system()`, `getattr(os, "system")` or `import builtins`. | Aliases of `os` are tracked, `getattr` on `os` is refused, and `builtins` is banned. The docstring now says this is a static check with the socket guard as the wall behind it, which matches what it actually is. |
| 7 | Medium | `tools/__init__.py` | The return keys with special meaning (`text`, `url`, `status`, `feed`, `file`) were documented nowhere, yet `run_code` and `output_file` depend on them. | Documented in the registry docstring. Segment 1 also caps the whole body, not just `text`. |
| 8 | Low | `sandbox/client.py` | Non-ASCII code was JSON-escaped (6–12 bytes per character), so large inputs went over the broker's 1 MB limit and got a vague error. | `ensure_ascii=False`, plus a clear message for HTTP 413. |
| 9 | Low | `search.py` | The result list was cut to `max_results` before non-http URLs were filtered out, so the model could get fewer results than it asked for. | Filter first, then stop at `max_results`. |
| 10 | Low | `tools/fetch_url.py` | A binary page that was also truncated lost its "binary content" note. | The notes are combined. |
| 11 | Low | `tools/calculator.py`, `tools/run_code.py`, `tools/web_search.py`, `tools/_example.py` | Descriptions and messages didn't match behaviour: missing functions, a missing `build_timeout` outcome, two wordings for "search unavailable", and the example skipped the `ctx.gateway is None` check. | Aligned. |

## Checked and sound

I found no SSRF bypass in the gateway:
- Each redirect hop is re-checked.
- The IP is pinned after one DNS lookup.
- Encoded and IPv4-mapped addresses are refused.
- The decompression size cap holds.
- The socket guard and the sandbox broker's token auth are consistent.

## Tests added

- `tests/test_tool_imports.py`:
  - `SignatureSchemas` covers inferred schemas, enums, optional values and `Annotated` descriptions.
  - It checks that a missing description is rejected, that a name clash is refused, and that `_example.py` loads as a real tool file.
  - The new import-ban bypasses are added to the banned-imports test.
- `tests/test_gateway_refusals.py`: `HtmlText` covers a missing `</head>`, `<form>` pages, SVG titles and complex calculator results.
