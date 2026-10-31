# Codebase review: four segments

This is a consistency and best-practice review of Perseus, split into four segments. It follows the process in the `code-review` skill, adapted to a whole-codebase audit because there is no PR diff:

1. An independent reviewer read each segment.
2. Each finding was checked against the code before anything changed.
3. Only verified issues were fixed, each with a regression test where one made sense.
4. A separate reviewer then checked the combined diff.

The guiding goal is the project's own: a frontier-capable harness for self-hosted models, with tools that are plug-and-play simple.

| Segment | Doc | Main points |
|---|---|---|
| 1. Engine and model | [1-engine-model.md](1-engine-model.md) | Recovery from malformed or text-only tool calls; a context guard that actually prevents overflows; compaction that keeps untrusted labels; mid-stream vLLM errors reported |
| 2. Tools, safety, sandbox | [2-tools-safety-sandbox.md](2-tools-safety-sandbox.md) | **`@tool` on a plain typed function**: the schema comes from type hints and the description from the docstring; tool load errors shown in the dashboard; HTML extraction fixes; tool-name clashes refused |
| 3. App shell and web | [3-app-web.md](3-app-web.md) | No more orphaned vLLM processes (restart races, double Apply, second Ctrl-C); failed context changes roll back; the server returns 500 instead of dropping the connection |
| 4. Setup and install | [4-setup-install.md](4-setup-install.md) | Sizing honours a fixed `max_context`; one config location for every entry point; partial multimodal configs handled; migrated defaults follow the new template |

The test suite went from 332 to 348 tests, all passing. In-code comments were kept to a minimum; the reasoning lives in these docs.
