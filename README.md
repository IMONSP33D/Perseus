# Perseus

Turns a blank, rented Linux GPU box into a safe tool-calling model server in two commands. From one 24 GB card to an 8-GPU node, the same sizing rules decide everything.

```bash
git clone <this repo> perseus && cd perseus
cp perseus.example.toml perseus.toml      # set [model] id and [secrets] hf_token
bash preflight.sh && python perseus.py
```

**The model proposes, Perseus disposes.** The model never reaches the internet directly. Every tool call is parsed, validated against its schema, checked against the gateway rules and shown in the tool feed.

## Status

| Phase | What | State |
| --- | --- | --- |
| 0 Fast boot | preflight, sizing, download, vLLM launch, boot timings | built; needs its first real-GPU run |
| 1 Wire dashboard | the existing dashboard, streaming, token auth, dev view | backend pieces built (streaming client, feed events, prompt rendering); waiting on the dashboard files |
| 2 Safe tool loop | registry, validation, tool feed, offline tools | built, gate tests pass |
| 3 Web gateway | read-only gateway, SearXNG search, fetch, limits, kill switch, boot self-test | built, gate tests pass |
| 4 Developer harness | hot-reload tools, session export and replay | built (parser fixtures need a real model) |

Until the dashboard is wired, `perseus.py` ends in a terminal console that runs the full tool loop.

## What `perseus.py` does, in order

1. **Walls first.** Installs the socket guard, builds the gateway and runs the boot self-test: 21 refusal checks in about a second. Any failure stops Perseus before GPU time is spent (fails closed).
2. **Weights.** Waits for the download that preflight started, showing progress.
3. **Processes.** Starts SearXNG and vLLM under the supervisor. vLLM runs on 127.0.0.1, offline, with a per-boot key and no HF token.
4. **Proof.** Times the first token, then sends one test tool call. It must come back parsed, or Perseus stops and names the parser.
5. **Console.** Runs the safe tool loop in the terminal. The dashboard replaces this in Phase 1.

Flags:

- `--ask "question"`: one answer, with the tool log printed.
- `--selftest`: the refusal checks only.
- `--replay logs/session-….json`: re-run a saved session offline.
- `--no-repl`, `--fast-start`, `--timings`, `--print-cmd`.

Console commands: `/depth quick|standard|deep`, `/web off|on` (kill switch), `/feed`, `/prompt` (the raw prompt after the chat template), `/export`, `/reset`, `/exit`. Ctrl-C during an answer is the Stop button; the partial answer is kept.

## The tool loop (`perseus/engine.py`)

| Step | Rule | If it fails |
| --- | --- | --- |
| Parse | Arguments must be a JSON object. | refused, not run |
| Validate | The tool must exist; arguments must match its schema (unknown keys refused). | refused, not run |
| Depth | Web tools count toward Quick 5 / Standard 20 / Deep 50 lookups (scaled down on small GPUs). At the limit, web tools are withdrawn and the model is told to answer. | refused |
| Cache | An identical call in the same answer returns the earlier result. | — |
| Execute | Runs with a timeout. Errors reach the model as one clean line, never a traceback. | error |
| Label | Web results are wrapped as untrusted content (a page can't fake the end marker). Instruction-like phrases get a warning badge in the feed. | — |
| Budget | Each result is capped in tokens. On small GPUs older results shrink to title, URL and excerpt for the model; the feed and export keep the full text. | — |

## The web gateway (`perseus/gateway.py`, `perseus/netpolicy.py`)

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

Copy `tools/_example.py` to `tools/<name>.py`. Files there load at boot and reload before each answer when they change.

- A file that imports network, process or FFI libraries (`socket`, `urllib`, `requests`, `subprocess`, `ctypes`, `os.system`, …) is refused.
- Tools reach the web only through `ctx.gateway`, and search only through `ctx.search`.
- Mark web-reading tools `lookup=True`. They then count toward research depth and their results are labelled untrusted.

## Sizing rules (`perseus/sizing.py`)

1. Weights are what the repo ships (file sizes), or params × 1 byte for online FP8.
2. Quantization: the repo's own if it has one; else bf16 if it fits; else FP8 on compute 8.9+; else CPU offload (opt-in, single GPU); else stop and print what would fit.
3. TP is the smallest of 1/2/4/8 that divides the attention heads and leaves room for 32k tokens of context (or the model's limit). If none does, TP is the fit with the most context.
4. Context is the most tokens whose KV cache fits, capped at the model's limit:

   KV bytes per token = 2 × layers × KV heads × head size × bytes per value

   MLA models (DeepSeek) store one latent per layer instead.
5. The small-GPU profile turns on for one GPU when the context comes out under 32k. It switches the KV cache to FP8 and scales tool-result size and research depth to the context.

## Verify on the first rented box

These were written against vLLM 0.32.0 and SearXNG without running them on a GPU; check each one once:

- [ ] `vllm serve --help` still has every flag in `launcher.build_vllm_cmd`, and `VLLM_API_KEY` is still read from the environment.
- [ ] The parser names in `perseus/parsers.py` are valid choices.
- [ ] Streamed reasoning arrives as `reasoning_content` or `reasoning` (`perseus/llm.py` reads both).
- [ ] `requirements/driver.min` matches the CUDA runtime in torch 2.13.0.
- [ ] SearXNG starts with `python -m searx.webapp` and answers `/healthz` (`perseus/searxng.py`).
- [ ] Download progress counts Xet's in-progress bytes (otherwise the ETA starts late).
- [ ] Measured KV-cache size is close to preflight's estimate; tune `RESERVE_*` if not.
- [ ] Record three cold boots: `python perseus.py --timings`.

## Layout

```
preflight.sh            bash entry: ordering, installs, timings
perseus.py              entry: re-runs itself under .venv, then perseus.launcher
perseus/config.py       perseus.toml schema and validation
perseus/hardware.py     GPUs, driver, NVLink/PCIe, RAM, disks
perseus/hub.py          Hub metadata, exact file list, remote-code and template checks
perseus/sizing.py       TP, quantization, context, small-GPU profile
perseus/parsers.py      tool-call / reasoning parser per model family
perseus/download.py     Xet download, progress, verification, token scrub
perseus/preflight.py    check / smoke / searxng / download stages
perseus/launcher.py     boot order, vLLM command, parse check
perseus/supervisor.py   restarts with backoff, OOM explanation, process groups
perseus/selftest.py     boot refusal checks, fail closed
perseus/netpolicy.py    URL, address and secret rules (pure functions)
perseus/gateway.py      the read-only HTTP client with pinned IPs
perseus/socketguard.py  in-process egress guard
perseus/htmltext.py     HTML to text
perseus/searxng.py      local search client and process command
perseus/tools/          registry, schema validation, built-in tools, hot reload
perseus/engine.py       the tool loop
perseus/feed.py         tool feed events and export
perseus/llm.py          vLLM streaming client (tool calls reassembled)
perseus/session.py      recording, export, offline replay
perseus/console.py      terminal front end (until the dashboard)
perseus/devtools.py     raw prompt after the chat template
tools/_example.py       template for your own tools
tests/                  python3 -m unittest discover -s tests -t .
```

Tests need no GPU, no network and no extra packages (109 tests, about 5 s). To dry-run the whole preflight check:

```bash
PERSEUS_FAKE_HW=tests/fixtures/hw_1x4090.json python3 -m perseus.preflight check --meta-file tests/fixtures/meta_qwen25_32b_awq.json
```

## Secrets

`perseus.toml` is git-ignored and mode 600, and only preflight reads it. `perseus.py` never loads the HF token. A pre-commit hook (`uvx pre-commit install`) blocks commits containing tokens or the config file.
