<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
  <img src="perseus/web/static/logo-header.svg" alt="Perseus" width="440">
</picture>

**A self-hosted agent harness for open-weight models.**<br>
Turn a rented Linux GPU server into a private, sandboxed tool-calling assistant with web research, in two commands.

![Python 3.11–3.14](https://img.shields.io/badge/python-3.11%E2%80%933.14-2B2D42)
![vLLM 0.32](https://img.shields.io/badge/vLLM-0.32.0-2B2D42)
![Tests](https://img.shields.io/badge/tests-191%20passing-16A34A)
![License](https://img.shields.io/badge/license-proprietary-D90429)

[Quick start](#quick-start) · [Configuration](#configuration) · [Dashboard](#the-dashboard) · [Pipelines](#development-pipelines-preview) · [Tools](#writing-tools) · [Security](#how-perseus-stays-safe) · [Project structure](#project-structure)

</div>

---

<p align="center">
  <img src="docs/screenshot-chat.png" alt="The Perseus dashboard: a chat with the thinking panel open, an answer with a table, and the tool feed showing a calculator call" width="100%">
</p>

## What it is

Perseus takes a blank GPU box and a Hugging Face model id and gives you back a working assistant you reach through an SSH tunnel. It handles the whole stack:

- **Sizing and serving.** Reads your GPUs and the model, then picks the quantization, tensor parallelism and context length that fit, from one 24 GB card to an 8-GPU node. Downloads the weights, launches vLLM with the right tool-call and reasoning parsers, and proves tool calling works before you use it.
- **The agent loop.** Every tool call is parsed, validated against its schema, executed with a timeout and shown in a live tool feed. Thinking is kept separate from the answer, with a budget you control.
- **Safe web research.** A private SearXNG search engine and a read-only web gateway. The model never touches the network directly.
- **A dashboard.** Streaming chat, thinking panel, tool feed, settings, and a raw view of exactly what the model was sent. Plain HTML, CSS and JavaScript, served by Perseus itself.
- **Development pipelines (preview).** Multi-stage workflows where each stage is a role with its own prompt and tools, and every handoff starts a fresh context window for unbiased review.

> **The model proposes, Perseus disposes.** Nothing the model asks for runs until Perseus has checked it.

## Requirements

| | |
| --- | --- |
| **Machine** | Linux (tested on Ubuntu and Debian) with one or more NVIDIA GPUs, 24 GB or more recommended |
| **Driver** | NVIDIA driver 580 or newer (checked by preflight; `--install-driver` can install it on a VM) |
| **Disk** | Room for the model's weights, plus about 10 GB for the Python environment; preflight picks the fastest local disk |
| **Account** | A [Hugging Face access token](https://huggingface.co/settings/tokens), needed for gated models |
| **Software** | `bash`, `git` and `python3`. Preflight installs uv, Python 3.12, vLLM and SearXNG itself |

## Quick start

**1. Clone and configure** (on the GPU box)

```bash
git clone https://github.com/IMONSP33D/Perseus.git perseus && cd perseus
cp perseus.example.toml perseus.toml
nano perseus.toml          # set [model] id and [secrets] hf_token
```

**2. Prepare the box.** Preflight checks everything cheap first, so a bad config fails in seconds, not after a 400 GB download. It is safe to re-run: finished steps are skipped, and an interrupted download resumes.

```bash
bash preflight.sh          # or --check to validate without installing anything heavy
```

**3. Start Perseus**

```bash
python3 perseus.py
```

It runs the safety self-test, starts SearXNG and vLLM, times the first token, sends a test tool call, and then prints a sign-in link and the exact SSH command for your machine.

**4. Open the dashboard.** Perseus binds to `127.0.0.1` only, so forward the port from **your own computer** and keep the tunnel open:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 8080:127.0.0.1:8080 user@<gpu-box>
```

Then open the printed `http://127.0.0.1:8080/?token=…` link. The token is exchanged for a session cookie, so later visits only need `http://127.0.0.1:8080/`. Perseus tells you in its terminal when a browser signs in, or what went wrong.

Press **Ctrl-C** in the terminal to shut everything down, vLLM and SearXNG included.

## Configuration

All settings live in **`perseus.toml`**, a copy of [`perseus.example.toml`](perseus.example.toml) that documents every key. It is git-ignored, and preflight keeps it readable only by you (mode 600). If it's missing, preflight creates it from the example and stops so you can fill it in.

**How values are decided**

- Any key set to `"auto"` is worked out by preflight from your hardware and the model. A value you type wins.
- The results are saved to `perseus.resolved.toml` in the data directory, so you can see what was picked.
- One key can be overridden for a single run from the command line: `bash preflight.sh --model.id Qwen/Qwen3-32B-AWQ`.
- Change the model or sizing keys, then re-run `bash preflight.sh` before `perseus.py`.

**The keys you will usually touch**

| Section | Key | What it does |
| --- | --- | --- |
| `[model]` | `id` | Any Hugging Face repo with safetensors weights, e.g. `"Qwen/Qwen3-32B-AWQ"` |
| | `revision` | `"main"`, or a commit hash for repeatable boots (required with `trust_remote_code`) |
| | `quantization` | `auto`, `fp8`, `bf16`, `awq` or `gptq` |
| | `max_context` | `auto` or a token count |
| | `tensor_parallel` | `auto`, `1`, `2`, `4` or `8` GPUs |
| | `tool_parser`, `reasoning_parser` | `auto`, or a vLLM parser name if auto picks wrong (`reasoning_parser = "none"` turns it off) |
| `[secrets]` | `hf_token` | Read by preflight only. `perseus.py` never loads it |
| `[storage]` | `data_dir` | `auto` (fastest disk with room) or an absolute path outside the checkout |
| `[server]` | `port`, `public` | Dashboard port. `public = true` binds `0.0.0.0`: anyone with the link can use the model |
| `[search]` | `engines` | The SearXNG engines asked for each search |
| `[limits]` | `research_depth` | `quick` (5), `standard` (20) or `deep` (50) web lookups per answer |

**Example: a 32B model on a single 24 GB card**

```toml
[model]
id = "Qwen/Qwen3-32B-AWQ"
revision = "main"
quantization = "auto"      # uses the repo's own AWQ weights
max_context = "auto"       # as many tokens as the KV cache allows

[secrets]
hf_token = "hf_..."
```

<details>
<summary><b>Every key</b></summary>

| Section | Key | Default | Notes |
| --- | --- | --- | --- |
| `[model]` | `trust_remote_code` | `false` | `true` requires `revision` pinned to a commit |
| | `cpu_offload_gb` | `0` | Single GPU only: spill this many GB of weights to RAM (slower) |
| | `small_gpu` | `auto` | The small-GPU profile (see [How sizing works](#under-the-hood)) |
| `[server]` | `bind` | `127.0.0.1` | |
| `[search]` | `port` | `8888` | Local only; never reachable by `fetch_url` |
| `[backend]` | `max_restarts` | `3` | Crash restarts per process, with backoff |
| | `fast_start` | `false` | Skip CUDA graph capture: faster load, slower generation |
| `[limits]` | `fetch_timeout_s` | `15` | Per page read |
| | `max_response_kb` | `2048` | Response size cap |
| | `warn_download_minutes` | `20` | Warn when the download is expected to take longer |

Both entry points read `./perseus.toml` from the folder they run in. `--config FILE` points somewhere else.

</details>

## The dashboard

The sidebar holds a new chat button, quick toggles (Internet Access, Show Thinking), the Development section, and your recent chats. The header carries the per-chat controls; the gear opens Settings.

| Control | What it does |
| --- | --- |
| **Internet Access** | Off: web tools are neither offered to the model nor run, and the gateway's kill switch is on. |
| **Thinking: Off / Low / Medium / High / Max** | For models that think. Each level is a thinking budget on top of the answer length: 4K, 16K, 32K tokens, or everything one request can hold. Past it, Perseus wraps up and says so. The thinking panel shows the count live. |
| **Show Thinking** | Shows or hides the thinking panel (display only). |
| **Context ring** | Tokens the last answer used out of the current context. |
| **Close out** | While an answer runs: stop thinking and skip remaining tool calls, then write the final answer from the notes so far. |
| **Send / Stop** | While an answer runs, the send button stops it and keeps the partial answer. |
| **Tool feed** | Every call with its status (OK, Blocked, Refused, Error, Cached, Stopped), its reason, and the exact arguments and result. Earlier questions fold into one row each. |
| **View raw** | Under each answer: the prompt after the chat template, token counts and the model's raw output per step. |
| **Attach** | Text files up to 200 KB each, five per message. |

<details>
<summary><b>Settings</b></summary>

| Setting | What it does |
| --- | --- |
| System prompt | Engineer, Planner, Researcher or your own text. Placed before Perseus's safety rules, never instead of them. |
| Memory 8K / 32K / 128K / Max | The context length. Options that don't fit this box's KV cache are disabled. Applying restarts vLLM. |
| Creativity | Temperature 0.2, 0.7 or 1.0. |
| Answer length | Output cap of 1K, 4K or 16K tokens, never more than half the context. |
| Research depth | Lookup limit per answer (scaled down on small GPUs). |
| Search engines | Which SearXNG engines each search asks. |
| Model server | Live state from the supervisor. Apply & restart appears only when Memory changed. |

Chats live in memory on the box and are gone at shutdown. **Export all chats** in Settings downloads everything as JSON. The theme, Show Thinking and the feed panel are remembered per browser.

</details>

### Thinking stays out of the answer

vLLM's reasoning parser splits thinking from the answer, but it misses some cases: a second thinking block mid-answer, a closing tag with no opening one, or a template that ignores `enable_thinking: false`. Perseus catches these as the answer streams and moves the text back into the thinking panel, so the answer and the saved chat history stay clean. Think tags inside code blocks are left alone.

## Development pipelines (preview)

<p align="center">
  <img src="docs/screenshot-pipelines.png" alt="The Development Pipelines page: four stage cards connected by handoff arrows, a dashed loop from the review gate back to the Engineer, and the selected stage's details" width="100%">
</p>

A pipeline works a request through ordered stages. Each stage is a **role** with its own instructions, tools and completion criteria. When a stage finishes, its work goes to the next stage in a **new context window**: the next stage receives the request and only the artifacts it declared, never the earlier reasoning, tool results or chat. **Review gates** judge the work against their criteria without seeing how it was made, and either pass it on or send it back with feedback, up to a set number of rounds.

> **Status:** the format, validation and dashboard are built. The runner that executes pipelines is not, so the page is a preview.

Open **Development Pipelines** in the sidebar to see each pipeline as a flow. Click a stage to see its role, what it receives and hands on, its tools, and the exact system prompt and opening message it would get. **Tools** (nested underneath) lists every tool the model can call; hover or focus one for its description and arguments.

**Writing a pipeline.** Add one `.toml` file to `perseus/pipelines/`. It appears the next time you open the page, and a broken file is listed with the reason.

```toml
[pipeline]
name    = "Feature build"
summary = "Plan, build, review and document a code change."
status  = "draft"                          # draft | ready

[[stage]]
id        = "build"
role      = "Engineer"
goal      = "Implement the plan as working code."
prompt    = '''Implement the plan step by step and include tests.'''
tools     = ["calculator"]                 # only these are offered; [] for none
inputs    = ["request", "plan"]            # "request" or an earlier stage's output
outputs   = ["code", "test_notes"]         # what it must hand on
done_when = ["Tests cover each acceptance check."]
thinking  = "high"                         # optional: off | low | medium | high | max

[[stage]]
id         = "review"
role       = "Code Reviewer"
kind       = "review"                      # a gate: returns pass or revise
goal       = "Judge the code against the plan."
prompt     = '''Review the code as if seeing it for the first time.'''
inputs     = ["plan", "code", "test_notes"]
on_revise  = "build"                       # where the work goes back to
max_rounds = 2                             # then a person decides
done_when  = ["No correctness or security bugs remain."]
```

Every stage ends its answer with a `<handoff>` block; only what is inside it is passed on. The full specification is at the top of [`perseus/pipelines/__init__.py`](perseus/pipelines/__init__.py). Two starter pipelines ship with Perseus: **Feature build** and **Research brief**.

## Writing tools

Each tool is one Python file in `perseus/tools/`. Copy [`_example.py`](perseus/tools/_example.py) to `perseus/tools/<name>.py` and edit it. Files load at boot and reload before each answer when they change, so there's no restart.

```python
from perseus.tools import ToolFailure, tool

@tool(description="Count the words in a piece of text.",
      parameters={"type": "object", "properties": {
          "text": {"type": "string", "maxLength": 100_000, "description": "The text to count."}},
          "required": ["text"]})
def word_count(ctx, text: str):
    return {"words": len(text.split()), "characters": len(text)}
```

- Return a dict or a string. Raise `ToolFailure("message")` to give the model a clean error.
- Tools may not import network, process or FFI libraries (`socket`, `urllib`, `requests`, `subprocess`, `ctypes`, `os.system` and others). A file that does is refused at load.
- Reach the web only through `ctx.gateway.fetch(url)`, and search only through `ctx.search`.
- Mark web-reading tools `lookup=True`, so they count toward research depth and their results are labelled untrusted.

Built-in tools: `calculator`, `current_time`, `web_search` and `fetch_url`.

## How Perseus stays safe

The safety layers start before any GPU time is spent. Perseus runs 21 refusal checks at boot and stops if any fails.

1. **The tool loop** refuses calls with malformed or unexpected arguments, enforces the research-depth limit, and wraps web results as untrusted content.
2. **The web gateway** is read-only and re-checks every redirect. It blocks private and internal addresses, secrets in URLs and DNS rebinding.
3. **The socket guard** refuses every other outbound connection from the Perseus process.
4. **vLLM** runs on loopback, offline, with a per-boot key and no Hugging Face token.
5. **The dashboard** needs a session cookie or bearer token for everything except `/healthz` and static assets. It enforces same-origin writes and a strict content-security policy, and model or web text only ever reaches the page as plain text.

<details>
<summary><b>The tool loop, step by step</b></summary>

| Step | Rule | If it fails |
| --- | --- | --- |
| Parse | Arguments must be a JSON object. | Refused, not run |
| Validate | The tool must exist; arguments must match its schema (unknown keys refused). | Refused, not run |
| Depth | Web tools count toward Quick 5 / Standard 20 / Deep 50 lookups (scaled down on small GPUs). At the limit, web tools are withdrawn and the model is told to answer. | Refused |
| Cache | An identical call in the same answer returns the earlier result. | — |
| Execute | Runs with a timeout. Errors reach the model as one clean line, never a traceback. | Error |
| Label | Web results are wrapped as untrusted content (a page can't fake the end marker). Instruction-like phrases get a warning badge in the feed. | — |
| Budget | Each result is capped in tokens. On small GPUs older results shrink to title, URL and excerpt; the feed and export keep the full text. | — |

</details>

<details>
<summary><b>The web gateway rules</b></summary>

Every rule is re-applied to every redirect hop.

| Rule | Behaviour |
| --- | --- |
| Method | GET and HEAD only. No other method exists in the client. |
| Secrets | The URL (raw and %-decoded) may not carry a key-shaped string (Hugging Face, OpenAI, Anthropic, GitHub, AWS, Slack, Google, JWT, long random strings) or this boot's vLLM key. Blocked URLs are redacted in the log. |
| Shape | http/https on ports 80 and 443 only. No `user@host`, no numeric-encoded IPs (`2130706433`, `0x7f.1`, `127.1`, fullwidth digits), no internal names (`localhost`, `*.internal`, single-label names). URLs are capped at 2,048 characters. |
| Address | DNS is resolved once, and the URL is refused if any answer is loopback, private, link-local, metadata, CGNAT, multicast, reserved, or IPv4 hidden inside IPv6. The connection goes to that pinned IP while TLS verifies the real hostname. |
| Request | No body, cookies or auth headers; an honest User-Agent; proxies are never used. |
| Limits | Per-call timeout, response size cap (gzip bombs included), per-domain rate limit, kill switch. |
| Response | HTML becomes text with scripts and styles stripped, plus links. Binaries are reported, never returned. |

`web_search` never takes a URL: it sends a query string to SearXNG's one fixed local address. `fetch_url`, the only tool that takes a URL, refuses every loopback address, SearXNG's included.

</details>

## Under the hood

<details>
<summary><b>What <code>perseus.py</code> does at startup</b></summary>

1. **Walls first.** Installs the socket guard, builds the gateway and runs the boot self-test (21 refusal checks, about a second). Any failure stops Perseus.
2. **Weights.** Waits for the download that preflight started, with a live panel: per-file bars, speed, ETA and free disk.
3. **Processes.** Starts SearXNG and vLLM under a supervisor that restarts them on a crash.
4. **Proof.** Times the first token, then sends one test tool call. It must come back parsed, or Perseus stops and names the parser.
5. **Dashboard.** Checks the dashboard the way a browser would, checks that the box's SSH server allows tunnels, and prints the sign-in link and SSH command.

</details>

<details>
<summary><b>Command-line options</b></summary>

**`preflight.sh`**

| Option | Effect |
| --- | --- |
| `--check` | Validate config, hardware, Hub access and sizing only; install nothing heavy |
| `--config FILE` | Use another config file |
| `--install-driver` | VM only: install the NVIDIA driver, then reboot and re-run |
| `--section.key value` | Override one config key for this run |

**`perseus.py`**

| Option | Effect |
| --- | --- |
| `--console` | A terminal console instead of the dashboard |
| `--ask "question"` | Answer one question, print the tool log, exit |
| `--selftest` | Run the gateway refusal checks only |
| `--replay FILE.json` | Re-run a saved session offline (no GPU, no network) |
| `--config FILE`, `--data DIR` | Another config, or another data directory |
| `--fast-start` | Skip CUDA graph capture |
| `--timings` | Print the latest boot timings |
| `--print-cmd` | Print the vLLM command and exit |

**Console commands** (with `--console`): `/depth quick|standard|deep`, `/web off|on`, `/feed`, `/prompt`, `/export`, `/reset`, `/exit`. Ctrl-C during an answer stops it and keeps the partial answer.

</details>

<details>
<summary><b>How sizing works</b></summary>

From [`setup/sizing.py`](setup/sizing.py):

1. **Weights** are what the repo ships, or parameters × 1 byte for online FP8.
2. **Quantization** is the repo's own if it has one; else bf16 if it fits; else FP8 on compute capability 8.9+; else CPU offload (opt-in, single GPU); else Perseus stops and prints what would fit.
3. **Tensor parallelism** is the smallest of 1, 2, 4 or 8 that divides the attention heads and leaves room for 32K tokens of context (or the model's limit).
4. **Context** is the most tokens whose KV cache fits, capped at the model's limit:
   `KV bytes per token = 2 × layers × KV heads × head size × bytes per value`. MLA models (DeepSeek) store one latent per layer instead.
5. **The small-GPU profile** turns on for a single GPU when the context comes out under 32K. It switches the KV cache to FP8 and scales tool-result size and research depth to the context.

</details>

<details>
<summary><b>HTTP API</b></summary>

For scripts, send `Authorization: Bearer <token>`. Writes also need an `X-Perseus: 1` header.

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/state` | Model, settings, status and chat list |
| `PATCH` | `/api/settings` | Change settings |
| `POST` | `/api/model/apply` | `{"context": N}`: restart vLLM with a new context length |
| `GET` `POST` `DELETE` | `/api/chats` | List, create or delete all chats |
| `GET` `DELETE` | `/api/chats/<id>` | One chat |
| `POST` | `/api/chats/<id>/messages` | `{"text": …}`: a server-sent-events stream of `start`, `text`, `retract`, `phase`, `tool` and `done` events |
| `POST` | `/api/chats/<id>/stop`, `/closeout` | Stop, or close out, the running answer |
| `GET` | `/api/chats/<id>/raw/<message id>` | The raw prompt and output |
| `GET` | `/api/pipelines`, `/api/tools` | Pipeline definitions and available tools |
| `GET` | `/api/export` | All chats as JSON |
| `GET` | `/healthz` | Health check (no auth) |

</details>

## Project structure

Perseus keeps two trees apart: **the checkout** you cloned (code and config) and **a data directory** that preflight creates on the fastest local disk (weights, logs, run state). Nothing large or secret lands in the checkout.

```
perseus/                         the checkout (this repo)
├── perseus.example.toml         config template documenting every key
├── perseus.toml                 your config (git-ignored)
├── preflight.sh                 step 1: prepare the box
├── perseus.py                   step 2: start Perseus
├── setup/                       everything preflight runs
│   ├── check_config.py          config, Hub and sizing checks; picks the data directory
│   ├── sizing.py                tensor parallelism, dtype, context, small-GPU profile
│   ├── hardware.py              GPUs, driver, NVLink/PCIe, RAM, disks
│   ├── hub.py                   Hugging Face metadata, file list, template checks
│   ├── download.py              resumable download with a live panel
│   └── install_searxng.sh       private search engine
├── perseus/                     the Python package
│   ├── launcher.py              boot order
│   ├── config.py                loads perseus.toml and the resolved settings
│   ├── model/                   vLLM launch, parser choice, streaming client, thinking splitter
│   ├── engine/                  tool loop, validation, budgets, prompts, tool loader, replay
│   ├── tools/                   one file per tool (registry in __init__.py, template in _example.py)
│   ├── pipelines/               pipeline format and validation, plus the .toml definitions
│   ├── safety/                  web gateway, URL and secret rules, socket guard, self-test
│   ├── web/                     HTTP server, dashboard state; static/ holds the page
│   ├── search.py                SearXNG client (one fixed loopback address)
│   ├── supervisor.py            keeps vLLM and SearXNG running
│   └── console.py               terminal front end (--console)
├── docs/                        README images
└── tests/                       191 tests; no GPU or network needed
```

```
<fastest disk>/perseus/          the data directory, e.g. /mnt/nvme/perseus
├── weights/<org>/<model>/       the download (Hugging Face token removed afterwards)
├── perseus.resolved.toml        what preflight decided
├── logs/                        preflight, vLLM, SearXNG, gateway, boot times, sessions
├── searxng/                     its source, venv and settings
└── run/                         PIDs, per-boot keys, dashboard link (removed at shutdown)
```

The one large thing left in the checkout is the Python environment (`.venv`, about 10 GB with vLLM). Set `UV_PROJECT_ENVIRONMENT` before running both entry points to move it.

## Development

```bash
python3 -m unittest discover -s tests -t .      # 191 tests, about 20 s; no GPU, network or extra packages
```

Dry-run the whole preflight check against a recorded GPU and model:

```bash
PERSEUS_FAKE_HW=tests/fixtures/hw_1x4090.json \
  python3 -m setup.check_config check --meta-file tests/fixtures/meta_qwen25_32b_awq.json
```

Install the pre-commit hook once per clone. It blocks commits that contain tokens or your `perseus.toml`:

```bash
uvx pre-commit install
```

## Secrets

`perseus.toml` is git-ignored, kept at mode 600, and read only by preflight. `perseus.py` never loads the Hugging Face token, and the token is scrubbed from caches after the download. The pre-commit hook above blocks commits that contain tokens or the config file.

## License

Proprietary. All rights reserved.
