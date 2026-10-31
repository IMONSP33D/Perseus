# Review 1 of 4: engine and model

Scope: `perseus/engine/` (loop, prompts, schema, feed, session, tool_loader) and `perseus/model/` (client, thinksplit, parsers, launch, meta, prompt_view).

Method: the code-review skill's process, adapted for a whole-codebase audit. There is no PR, so the bug reviewers read the full segment instead of a diff. Each finding was checked against the code, and only verified findings were changed. The guiding test was whether the change makes Perseus a better agent harness for self-hosted models, which tend to emit messier tool calls than frontier APIs do.

## Changes

| # | Severity | File | What was wrong | Fix |
|---|---|---|---|---|
| 1 | High | `engine/loop.py` | A tool call with malformed JSON arguments went into the history as-is. vLLM re-parses the arguments of every earlier call on the next request, so one bad call could break that request and every later turn in the chat. | `_history_message()` replaces bad arguments with `"{}"` in the history. The feed still shows the raw text the model sent. |
| 2 | High | `engine/loop.py` | Small models often write a tool call as plain text (`<tool_call>…`, `[TOOL_CALLS]`, `<function=…>`) and the parser misses it. That text was returned to the user as the answer. | The loop now detects these markers and retries once, telling the model to use the proper format. The feed records an `unparsed_call` event. |
| 3 | High | `engine/loop.py` | The context guard always reserved 2,048 reply tokens, while the dashboard sends `max_tokens` up to half the context. Long chats went over `max_model_len` and vLLM rejected them. If compaction wasn't enough, the over-long prompt was sent anyway. | `_fit_context(reserve=…)` reserves the request's real `max_tokens`, capped at half the context. If compacting old tool results isn't enough, it drops the oldest whole turns. The current answer is never dropped. |
| 4 | Med-High | `engine/loop.py` | On small GPUs, compaction removed the `<<<UNTRUSTED CONTENT … >>>` wrapper from old web and `run_code` results. The model then read that text unlabelled. | `compact_result()` compacts what is inside the wrapper and keeps the wrapper. |
| 5 | Medium | `engine/loop.py` | At the step limit, the last step still offered tools. The results were thrown away and the turn ended with "(stopped: too many tool steps)". | The last step is now a wrap-up: no tools, an explicit "stop calling tools" note and the notes so far (`closed: "steps"`), the same way Close out works. The dashboard shows a label for this. The independent check caught a first version that sent a plain tool-less request; a model could answer that with tool-call markup. |
| 6 | Medium | `engine/loop.py` | Results that weren't dicts or strings (lists, `None`, numbers) reached the model as Python repr, and only a dict's `text` field was size-capped. | Every non-string result is serialised as JSON. Every result body is capped; dicts get a little extra room for their title and links. A cut plain-string result now says it was cut. |
| 7 | Med-Low | `model/client.py` | When vLLM failed mid-stream it sent an `error` object, which was ignored. The turn ended with an empty answer and no error message. | The client raises `LLMError("vLLM stream error: …")`, and the dashboard shows it. |
| 8 | Low | `engine/loop.py` | If Stop arrived during a tool step, the planning text ("Let me search…") came back as the answer. Every other stop path already skipped that text. | Uses `_last_text(new)`, like every other path. |
| 9 | Low | `engine/loop.py` | The Close out wrap-up counted thinking as characters ÷ 3.5. The main loop uses `stream_tokens()`, whose docstring says that formula overcounts. | Both now use `stream_tokens()`. |
| 10 | Low | `engine/loop.py` | Injection patterns starting with `^` were compiled once, then compiled again on every call with `re.M`. | All patterns are compiled once with `re.I \| re.M`. |
| 11 | Low-Med | `tools/__init__.py`, `engine/tool_loader.py` | A hot reload in one chat could change the registry while another chat's turn was reading it, which could raise `dictionary changed size during iteration`. | `Registry.lock` (an RLock) covers changes and reads, and `ToolLoader.refresh()` holds it for the whole reload. |

## Considered and left alone

- **Showing a plain exception's message to the model.** `test_errors_never_leak_tracebacks` deliberately keeps a crashing tool's message out of the model's context, because it can contain paths and internals. `ToolFailure` remains the way to send the model a message. `_example.py` now teaches that pattern.
- **`parsers.py` "VERIFY IN PHASE 0" notes.** These are real pending checks against the pinned vLLM, not code issues.

## Tests added

- `tests/test_engine.py`
  - `ContextFit`: old turns are dropped on overflow, and a cut plain-string result is marked.
  - `Recovery`: malformed arguments are kept out of the history, a tool call written as text is retried, the step limit wraps up, and compacted results stay labelled untrusted.
- `tests/test_clients.py`: a mid-stream error is raised.
