# Review 3 of 4: app shell and web dashboard

Scope: `perseus/launcher.py`, `supervisor.py`, `config.py`, `console.py`, `progress.py`, `timing.py`, `pipelines/`, `web/` (server, dashboard, reach, static), `perseus.py`.

The most serious problems were in process supervision. Two code paths could each start a vLLM, and the first one was never stopped. Because vLLM runs in its own session, an orphan like that holds GPU memory until someone kills it by hand.

## Changes

| # | Severity | File | What was wrong | Fix |
|---|---|---|---|---|
| 1 | High | `supervisor.py` | If the dashboard restarted vLLM during a crash backoff, two processes were started. `restart()` spawned one, then the watcher woke up and spawned a second, overwriting `m.proc` without stopping the first. | A `_spawning` lock wraps terminate+spawn in `restart()` and the watcher's respawn. The watcher checks again, after the backoff, whether someone else has already replaced the process. |
| 2 | High | `web/server.py` | Clicking Apply twice started two concurrent restarts. The first one to finish also cleared `restarting` while the second was still loading. | `/api/model/apply` returns 409 while a restart is running. |
| 3 | High | `launcher.py` | A second Ctrl-C (or SIGTERM) during the up-to-30 s shutdown interrupted the `finally` block and left vLLM and SearXNG running. | The `finally` block ignores SIGINT, SIGTERM and SIGHUP before it stops the processes. |
| 4 | Medium | `supervisor.py` | The "same error twice in a row, so not retrying" check compared against crashes from any time earlier, even across a recovery or a deliberate restart. | `last_cause` is cleared when a process comes back up and on every `restart()`. |
| 5 | Medium | `launcher.py` | If a context change from the dashboard didn't fit, vLLM was left in `failed` with the new command, and the whole session died. | `restart_model` goes back to the previous context and command, then reports why the change failed. |
| 6 | Medium | `web/server.py` | Request handlers caught only a few exception types. A non-ASCII `?token=` hit `hmac.compare_digest`'s `TypeError`, which dropped the connection and printed a traceback. Any other unexpected error did the same. | Secrets are compared as bytes (`_same_secret`). A final `except Exception` logs one line and returns a 500. |
| 7 | Medium | `web/dashboard.py` | `/api/chats/<id>` and the export could serialise a message while the turn thread was adding keys to it (`ttft`, `duration`), raising `dictionary changed size during iteration`. The client then stopped polling without any message. | Every message key is created up front, so later updates only change existing values. |
| 8 | Medium (UX) | `web/dashboard.py`, `web/static/app.js` | See segment 2: tool load errors now appear on the Agent Functions page. The dashboard gets them through an explicit `tool_errors` callable. | |
| 9 | Low | `launcher.py` | The "public" link used `gethostbyname(gethostname())`, which returns `127.0.1.1` on stock Debian and Ubuntu. | The link uses the first non-loopback IPv4 address from `getaddrinfo`, and otherwise shows a placeholder. No packets are sent, so the socket guard isn't affected. |
| 10 | Low | `launcher.py` | The socket-guard refusal summary only printed in `--ask` / `--console` mode. | Moved into the `finally` block, so dashboard mode prints it too. |
| 11 | Low | `console.py` | If `run_turn` raised, the console crashed with `KeyError: 'r'`. | The error is caught in the thread and printed, and the console carries on. |
| 12 | Low | `web/static/app.js` | The new `closed: "steps"` reason from segment 1 had no label. | Added "Tool step limit reached · wrapped up". |

## Checked and sound

- **XSS.** `h()` only uses `textContent` and `setAttribute`, and markdown links must start with `http(s)`.
- **File downloads.** Names and mime types are sanitised, and files are always sent as attachments.
- **Auth and CSRF.** The cookie is `HttpOnly; SameSite=Strict`, writes need `X-Perseus: 1` and an Origin check, and the CSP allows no inline script or style.
- **Consistency.** The README API table and the stream event names match between server and client. Every config key read in code exists in `Config/templates/` and in `config.py`'s `SCHEMA`.

## Tests added

- `tests/test_server.py`: a non-ASCII token gets a 401 and not a crash; a second Apply during a restart gets a 409.
- `tests/test_supervisor_selftest.py`: a restart during a crash backoff leaves exactly one live process, the one `restart()` started.
