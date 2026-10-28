# Perseus

Turns a blank, rented Linux GPU box into a safe tool-calling model server in two commands. From one 24 GB card to an 8-GPU node, the same sizing rules decide everything.

```bash
git clone <this repo> perseus && cd perseus
cp perseus.example.toml perseus.toml      # set [model] id and [secrets] hf_token
bash preflight.sh && python3 perseus.py
```

**The model proposes, Perseus disposes.** The model never reaches the internet directly. Every tool call is parsed, validated against its schema, checked against the gateway rules and shown in the tool feed.

## Status

| Phase | What | State |
| --- | --- | --- |
| 0 Fast boot | preflight, sizing, download, vLLM launch, boot timings | built; needs its first real-GPU run |
| 1 Wire dashboard | the Perseus Chat design, streaming, token auth, dev view | built; tested in a browser against a stand-in model |
| 2 Safe tool loop | registry, validation, tool feed, offline tools | built, gate tests pass |
| 3 Web gateway | read-only gateway, SearXNG search, fetch, limits, kill switch, boot self-test | built, gate tests pass |
| 4 Developer harness | hot-reload tools, session export and replay | built (parser fixtures need a real model) |

`perseus.py` ends by serving the dashboard. `--console` gives the terminal console instead.

## What `perseus.py` does, in order

1. **Walls first.** Installs the socket guard, builds the gateway and runs the boot self-test: 21 refusal checks in about a second. Any failure stops Perseus before GPU time is spent (fails closed).
2. **Weights.** Waits for the download that preflight started, showing the same live panel as preflight: overall and per-file bars, current speed with a one-minute graph, ETA and free disk. In preflight, Ctrl-C pauses the download and the next run resumes it.
3. **Processes.** Starts SearXNG and vLLM under the supervisor. vLLM runs on 127.0.0.1, offline, with a per-boot key and no HF token.
4. **Proof.** Times the first token, then sends one test tool call. It must come back parsed, or Perseus stops and names the parser.
5. **Dashboard.** Prints a link with a one-time access token (see below). Ctrl-C, or `kill`, stops vLLM and SearXNG too.

Flags:

- `--ask "question"`: one answer, with the tool log printed.
- `--selftest`: the refusal checks only.
- `--replay <data>/logs/session-….json`: re-run a saved session offline.
- `--config FILE`, `--data DIR`: another config, or another data directory.
- `--console`: the terminal console instead of the dashboard.
- `--fast-start`, `--timings`, `--print-cmd`.

Console commands: `/depth quick|standard|deep`, `/web off|on` (kill switch), `/feed`, `/prompt` (the raw prompt after the chat template), `/export`, `/reset`, `/exit`. Ctrl-C during an answer is the Stop button; the partial answer is kept.

## The dashboard (`perseus/web/`)

A port of the "Perseus Chat Interface" design canvas into plain HTML, CSS and JavaScript (no build step, no framework), served by Perseus itself.

**Opening it.** `perseus.py` prints `http://127.0.0.1:8080/?token=…`. The box binds to loopback only, so forward the port from your own computer:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 8080:127.0.0.1:8080 user@<gpu box>
```

Run that on your own computer, not on the box, and keep it open while you use the dashboard. At startup Perseus checks the dashboard the way a browser would (health, sign-in with the token, the page and its script), checks that the box's SSH server allows tunnels, and prints the command above with your user and address filled in. When a browser arrives it says so: "✓ a browser signed in", or what went wrong ("without the token", "an old or wrong token"). Nothing printed means nothing came through the tunnel.

Then open the printed link. The token is exchanged for a session cookie, so later visits need only `http://127.0.0.1:8080/`. With `public = true` the dashboard binds `0.0.0.0` and the link carries the box's IP; anyone with the link can use the model.

**What each control does.**

| Control | Effect |
| --- | --- |
| Internet Access (toggle, header pill) | Off: web tools are neither offered to the model nor run, and the gateway's kill switch is on. |
| Show Thinking | Shows or hides the Thinking Process panel (display only). Opened while the model thinks, the panel follows the newest text; scroll up to read and it holds still, with "Jump to latest" to follow again. |
| Sandbox Mode | Disabled: local code execution comes with the VM sandbox in a later release. |
| Thinking: Off / Low / Medium / High | Shown only for models whose chat template thinks. Off sends `enable_thinking: false`; for gpt-oss the level is also sent as `reasoning_effort`. Each level is a thinking budget on top of Length (Low 2k, Medium 8k, High 32k tokens): past it Perseus wraps up (as Close out does) and the answer says "Thinking budget reached". |
| Context ring | Tokens used by the last answer (prompt plus output, as vLLM reported) out of the current context. |
| System prompt | Engineer, Planner, Researcher or your own text, placed before Perseus's safety rules (never instead of them). |
| Memory 8K / 32K / 128K / Max | The context length. Options that don't fit this box's KV cache are disabled. Applying restarts vLLM. |
| Creativity | Temperature 0.2 / 0.7 / 1.0. |
| Answer length | Output cap of 1K / 4K / 16K tokens, never more than half the context. |
| Research depth | Lookup limit per answer (scaled down by the small-GPU profile). |
| Search engines | The SearXNG engines asked for each search. |
| Model server | Live state from the supervisor. Apply & restart is offered only when Memory changed. |
| Tool feed | Every call with its status (OK, Blocked, Refused, Error, Cached, Stopped), reason, lookup count and warning badge. Details shows the exact arguments and the text the model received. |
| Tool feed grouping | The current question's calls are listed in full; each earlier question folds into one row (its text, number of calls, how many weren't OK) the moment a new question is sent. Click a row to open it. |
| Clear (tool feed) | Hides the calls listed so far from the feed, in this browser only; "Show them" brings them back. The chat, the model's history and "Export all chats" keep every call. |
| View raw (under each answer) | The prompt after the chat template, token counts, and the model's raw output for every step. |
| Attach | Text files up to 200 KB each (5 per message), added to the message as fenced blocks. |
| Send / Stop | While an answer runs, the send button stops it; the partial answer is kept. |
| Close out | While an answer runs: stop thinking and skip any remaining tool calls, then one last request with tools and thinking off and the notes so far. The answer gets the full Length budget and no time limit (Stop still ends it); if a model's template ignores the thinking switch, Perseus cuts the new thinking off after 512 tokens and says so. The answer is marked "Closed out". An answer that runs out of tokens while thinking wraps up the same way; one cut off mid-answer says "Cut off at the length limit". |

Chats live in memory on the box and vanish at teardown; "Export all chats" downloads everything as JSON. The theme, Show Thinking and the feed panel are remembered per browser.

**Security.**

- Every page and API call needs the session cookie (HttpOnly, SameSite=Strict) or the token as a Bearer header. Only `/healthz` and `/assets/*` are open.
- Writes need an `X-Perseus` header and a same-origin `Origin`.
- Host headers other than an IP or `localhost` are refused, which guards against DNS rebinding.
- A strict content-security policy allows no inline script or style.
- Model and web text reaches the page only as text nodes; links are http(s) only and open with `noreferrer`.

**API** (for scripts; `Authorization: Bearer <token>`): `GET /api/state` · `PATCH /api/settings` · `POST /api/model/apply {"context": N}` · `GET|POST|DELETE /api/chats` · `GET|DELETE /api/chats/<id>` · `POST /api/chats/<id>/messages {"text"}` (a server-sent-events stream of `start`, `text`, `tool` and `done` events) · `POST /api/chats/<id>/stop` · `GET /api/chats/<id>/raw/<message id>` · `GET /api/export` · `GET /healthz`.

## The tool loop (`perseus/engine/`)

| Step | Rule | If it fails |
| --- | --- | --- |
| Parse | Arguments must be a JSON object. | refused, not run |
| Validate | The tool must exist; arguments must match its schema (unknown keys refused). | refused, not run |
| Depth | Web tools count toward Quick 5 / Standard 20 / Deep 50 lookups (scaled down on small GPUs). At the limit, web tools are withdrawn and the model is told to answer. | refused |
| Cache | An identical call in the same answer returns the earlier result. | — |
| Execute | Runs with a timeout. Errors reach the model as one clean line, never a traceback. | error |
| Label | Web results are wrapped as untrusted content (a page can't fake the end marker). Instruction-like phrases get a warning badge in the feed. | — |
| Budget | Each result is capped in tokens. On small GPUs older results shrink to title, URL and excerpt for the model; the feed and export keep the full text. | — |

## The web gateway (`perseus/safety/`)

Every rule is re-applied to every redirect hop:

| Rule | Behaviour |
| --- | --- |
| Method | GET and HEAD only. No other method exists in the client. |
| Secrets | The URL (raw and %-decoded) may not carry a key-shaped string (HF, OpenAI/Anthropic, GitHub, AWS, Slack, Google, JWT, long random strings) or this boot's vLLM key. Blocked URLs are redacted in the log. |
| Shape | http/https on 80/443 only. No `user@host`, no numeric-encoded IPs (`2130706433`, `0x7f.1`, `127.1`, fullwidth digits), no internal names (`localhost`, `*.internal`, single-label names). URLs are capped at 2,048 characters. |
| Address | DNS is resolved once, and the URL is refused if any answer is loopback, private, link-local, metadata, CGNAT, multicast, reserved, or IPv4 hidden inside IPv6. The connection goes to that pinned IP while TLS verifies the real hostname, so DNS rebinding can't swap it. |
| Request | No body, cookies or auth headers; an honest User-Agent; proxies are never used. |
| Limits | Per-call timeout, response size cap (gzip bombs included), per-domain rate limit, kill switch. |
| Response | HTML becomes text with scripts and styles stripped, plus links. Binaries are reported, never returned. |

Behind the gateway, the **socket guard** refuses any other outbound connection from the Perseus process. Allowed are only the gateway's pinned request and the loopback ports of vLLM and SearXNG. `web_search` never takes a URL: it sends a query string to SearXNG's one fixed local address. `fetch_url`, the only tool that takes a URL, refuses every loopback address, SearXNG's included.

## Writing tools

Each tool is one file in `perseus/tools/`. Copy `perseus/tools/_example.py` to `perseus/tools/<name>.py`. Files there load at boot and reload before each answer when they change.

- A file that imports network, process or FFI libraries (`socket`, `urllib`, `requests`, `subprocess`, `ctypes`, `os.system`, …) is refused.
- Tools reach the web only through `ctx.gateway`, and search only through `ctx.search`.
- Mark web-reading tools `lookup=True`. They then count toward research depth and their results are labelled untrusted.

## Sizing rules (`setup/sizing.py`)

1. Weights are what the repo ships (file sizes), or params × 1 byte for online FP8.
2. Quantization: the repo's own if it has one; else bf16 if it fits; else FP8 on compute 8.9+; else CPU offload (opt-in, single GPU); else stop and print what would fit.
3. TP is the smallest of 1/2/4/8 that divides the attention heads and leaves room for 32k tokens of context (or the model's limit). If none does, TP is the fit with the most context.
4. Context is the most tokens whose KV cache fits, capped at the model's limit:

   KV bytes per token = 2 × layers × KV heads × head size × bytes per value

   MLA models (DeepSeek) store one latent per layer instead.
5. The small-GPU profile turns on for one GPU when the context comes out under 32k. It switches the KV cache to FP8 and scales tool-result size and research depth to the context.

## Verify on the first rented box

These were written against vLLM 0.32.0 and SearXNG without running them on a GPU; check each one once:

- [ ] `vllm serve --help` still has every flag in `perseus/model/launch.py`, and `VLLM_API_KEY` is still read from the environment.
- [ ] The parser names in `perseus/model/parsers.py` are valid choices.
- [ ] Streamed reasoning arrives as `reasoning_content` or `reasoning` (`perseus/model/client.py` reads both).
- [ ] `setup/driver.min` matches the CUDA runtime in torch 2.13.0.
- [ ] SearXNG starts with `python -m searx.webapp` and answers `/healthz` (`perseus/search.py`).
- [ ] Download progress counts Xet's in-progress bytes (otherwise the ETA starts late).
- [ ] Measured KV-cache size is close to preflight's estimate; tune `RESERVE_*` if not.
- [ ] Thinking controls: Qwen3 honours `chat_template_kwargs.enable_thinking`, and gpt-oss honours `reasoning_effort` (`perseus/web/dashboard.py`).
- [ ] Close out on the real model: the wrap-up answer starts within a second or two and reads as a conclusion, not a fresh start (`perseus/engine/loop.py`, `CLOSE_OUT` in `prompts.py`).
- [ ] View raw renders the prompt with the model's own tokenizer (needs `transformers`, installed with vLLM).
- [ ] Record three cold boots: `python3 perseus.py --timings`.

## Layout

Two trees are kept apart: the checkout you cloned (code and config), and a data directory that preflight creates on the fastest local disk (weights, logs, run state). Nothing large or secret lands in the checkout.

**The checkout**

```
perseus/                     # project root (the git repo)
├── perseus.toml             # your config: git-ignored, mode 600
├── perseus.example.toml     # committed template documenting every key
├── .perseus-data            # git-ignored: one line, where preflight put the data directory
├── preflight.sh             # entry point 1
├── perseus.py               # entry point 2: thin, runs the perseus/ package
├── uv.lock                  # pinned Python dependencies
├── setup/                   # everything preflight runs
│   ├── check_config.py      # schema + Hub checks, sizing, data directory (--check); GPU smoke test
│   ├── sizing.py            # TP, dtype, context, small-GPU profile
│   ├── hardware.py          # GPUs, driver, NVLink/PCIe, RAM, disks
│   ├── hub.py               # Hub metadata, exact file list, remote-code and template checks
│   ├── download.py          # Xet download, live panel, pause/resume, verification, token scrub
│   ├── install_searxng.sh
│   ├── install_driver.sh    # opt-in, VM only
│   ├── searxng.settings.yml # template, filled in per box
│   ├── pins.env             # uv, Python and SearXNG pins
│   └── driver.min
├── perseus/                 # the Python package
│   ├── config.py            # loads perseus.toml + perseus.resolved.toml; the data directory
│   ├── launcher.py          # boot order (what perseus.py runs)
│   ├── model/               # launch vLLM, parser selection, health, client, raw prompt view
│   ├── engine/              # tool loop, validation, budgets, depth limit, feed, replay, tool loader
│   ├── tools/               # one file per tool; no network imports
│   │   ├── __init__.py      # registry + @tool decorator
│   │   ├── calculator.py
│   │   ├── current_time.py
│   │   ├── web_search.py
│   │   ├── fetch_url.py
│   │   └── _example.py      # template; files starting with _ aren't loaded
│   ├── safety/              # gateway, URL and secret rules, socket guard, HTML to text, selftest
│   ├── search.py            # SearXNG client (one fixed loopback address)
│   ├── supervisor.py
│   ├── console.py           # terminal front end (--console)
│   ├── progress.py          # the live download panel (preflight and perseus.py)
│   ├── timing.py            # boot timings
│   └── web/                 # HTTP routes, SSE, dashboard state; static/ holds the page
└── tests/                   # python3 -m unittest discover -s tests -t .
    ├── test_gateway_refusals.py
    ├── test_tool_imports.py
    └── …
```

**The data directory** (created by preflight)

```
<fastest disk>/perseus/      # e.g. /mnt/nvme/perseus; ../perseus-data if the checkout's disk is the best
├── weights/<org>/<model>/   # the download; HF token removed afterwards
├── perseus.resolved.toml    # auto-detected sizing + this data path
├── logs/                    # preflight.log, vllm.log, searxng.log, gateway.jsonl, boot-times.jsonl, sessions
├── searxng/                 # its source, venv and settings
└── run/                     # PIDs, per-boot vLLM key and dashboard link (mode 600, removed at shutdown),
                             # download plan and status, Hugging Face and vLLM caches
```

- `[storage] data_dir = "auto"` picks the fastest disk with room: known NVMe first, then the most free space. An explicit path works too, but one inside the checkout is refused.
- Re-running preflight on the same box keeps the data directory it chose before, as long as it still fits.
- Both entry points read `./perseus.toml` from the folder they run in. `--config FILE` points elsewhere; the data directory for that config is recorded beside it. `perseus.py --data DIR` overrides the recorded one.
- The Python environment (`.venv`, about 10 GB with vLLM) is the one large thing left in the checkout. preflight needs it before it can choose the data directory. Set `UV_PROJECT_ENVIRONMENT` before running both entry points to move it.

Tests need no GPU, no network and no extra packages (128 tests, about 12 s). To dry-run the whole preflight check:

```bash
PERSEUS_FAKE_HW=tests/fixtures/hw_1x4090.json python3 -m setup.check_config check --meta-file tests/fixtures/meta_qwen25_32b_awq.json
```

## Secrets

`perseus.toml` is git-ignored and mode 600, and only preflight reads it. `perseus.py` never loads the HF token. A pre-commit hook (`uvx pre-commit install`) blocks commits containing tokens or the config file.
