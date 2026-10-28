# Perseus

Turns a blank, rented Linux GPU box into a tool-calling model server in two commands. From one 24 GB card to an 8-GPU node, the same sizing rules decide everything.

```bash
git clone <this repo> perseus && cd perseus
cp perseus.example.toml perseus.toml      # set [model] id and [secrets] hf_token
bash preflight.sh && python perseus.py
```

**Status: Phase 0 (Fast boot).** `perseus.py` starts vLLM, proves it answers and that tool calls parse, then opens a terminal chat. The dashboard, tool engine and web gateway arrive in Phases 1–3.

## What the two commands do

`preflight.sh` orders its work so a mistake costs seconds, not a 400 GB download:

| Step | What | Typical time |
| --- | --- | --- |
| 1 | `perseus.toml` exists (created from the example if not, mode 600); NVIDIA driver ≥ `requirements/driver.min` | < 1 s |
| 2 | git/curl if missing, pinned `uv`, Python 3.12, base env from `uv.lock` | seconds |
| 3 | Config schema (line-level errors), hardware, Hub metadata (no weights), sizing, disk → `perseus.resolved.toml` | seconds |
| 4 | vLLM from `uv.lock`, then a GPU smoke test (torch sees every GPU, allocates on each) | minutes |
| 5 | SearXNG at a pinned commit, in its own venv, settings for loopback only | ~1 min |
| 6 | Exactly the files vLLM loads, Xet parallel download, live MB/s and ETA, size-verified; HF token removed from Hugging Face caches | the long one |

`bash preflight.sh --check` runs steps 1–3 only. Re-running is safe: finished steps are skipped and the download resumes.

`perseus.py` reads only `perseus.resolved.toml`, which holds no secrets. It then:

- waits for the download,
- starts vLLM on 127.0.0.1 with a random per-boot key, offline and with no token in its environment,
- streams load progress (GPU memory filled versus expected weights),
- measures first-token time and instance-start-to-first-token,
- sends one test tool call and stops if it doesn't come back parsed.

Useful flags: `--no-repl`, `--fast-start` (skip CUDA graphs), `--timings`, `--print-cmd`.

## Sizing rules (`perseus/sizing.py`)

1. Weights are what the repo ships (file sizes), or params × 1 byte for online FP8.
2. Quantization: the repo's own if it has one; else bf16 if it fits; else FP8 on compute 8.9+ (Ada/Hopper or newer); else CPU offload (opt-in, single GPU); else stop and print what would fit.
3. TP is the smallest of 1/2/4/8 that divides the attention heads and leaves room for 32k tokens of context (or the model's limit). If none does, TP is the fit with the most context.
4. Context is the most tokens whose KV cache fits, capped at the model's limit:

   KV bytes per token = 2 × layers × KV heads × head size × bytes per value

   MLA models (DeepSeek) store one latent per layer instead.
5. The small-GPU profile turns on for one GPU when the context comes out under 32k. It switches the KV cache to FP8 and scales tool-result size and research depth to the context.

Tunables (VRAM share, reserves) are at the top of the file, as starting values for Phase 0 measurements.

## Verify on the first rented box (Phase 0)

These were written against vLLM 0.32.0 without running it; check each one once on a real GPU:

- [ ] `vllm serve --help` still has every flag in `perseus/launcher.py::build_vllm_cmd`, and `VLLM_API_KEY` is still read from the environment.
- [ ] The tool and reasoning parser names in `perseus/parsers.py` are valid choices.
- [ ] `requirements/driver.min` matches the CUDA runtime bundled in torch 2.13.0.
- [ ] Download progress counts Xet's in-progress bytes (otherwise ETA starts late).
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
perseus/launcher.py     vLLM start, health, first token, tool-call test
tests/                  python3 -m unittest discover -s tests -t .
```

Tests need no GPU and no extra packages. `PERSEUS_FAKE_HW=tests/fixtures/hw_1x4090.json python3 -m perseus.preflight check --meta-file tests/fixtures/meta_qwen25_32b_awq.json` dry-runs the whole check.

## Secrets

`perseus.toml` is git-ignored and mode 600, and only preflight reads it. A pre-commit hook (`uvx pre-commit install`) blocks commits containing tokens or the config file.
