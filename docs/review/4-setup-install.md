# Review 4 of 4: setup, config and install

Scope: `setup/` (hardware, hub, sizing, download, check_config, install scripts, sandbox template), `Config/` (configuration.sh, update.py, templates) and `preflight.sh`.

The install flow is `configuration.sh` → edit → `preflight.sh --check` → `preflight.sh` → `perseus.py`. Every step should just work, and anything that fails should fail early with a sentence that says what to do next.

## Changes

| # | Severity | File | What was wrong | Fix |
|---|---|---|---|---|
| 1 | High | `setup/sizing.py` | A hand-set `max_context` was rejected even when a larger tensor-parallel size would hold it. The search stopped at the first TP with 32k tokens of headroom. Example: 8B bf16 on 2×4090 with `max_context = 98304` failed at TP=1 with "use more GPUs", though TP=2 holds it. | A numeric `max_context` raises the search target, so the smallest TP that holds it is chosen. |
| 2 | High | `preflight.sh`, `perseus/config.py` | `preflight.sh` and `perseus.py` read `./Config` from wherever they were run, while `configuration.sh` writes to the checkout's `Config/`. So `bash ~/perseus/preflight.sh` run from `~` created an empty `~/Config` and failed with "set model.id". | Both now use `./Config` when the current folder has one, and the checkout's `Config/` otherwise. The final "Start Perseus with:" hint gives the full path when you're outside the checkout. |
| 3 | Medium | `setup/hub.py` | Multimodal repos that leave keys out of `text_config` crashed `--check` with a `KeyError` traceback, or quietly fell back to a 4,096-token limit. | `text_config` is merged over the top-level config. A key that is still missing becomes a `HubError` naming the key. The `ModelMeta` construction moved into `_meta()` so the try/except is small. |
| 4 | Medium | `Config/update.py` | Renamed keys always counted as values you had set. Someone who never touched the old `internet = false` was migrated to `network = "none"`, while a fresh install gets `"packages"`. That contradicts the README's "defaults you never changed follow the new version". | If the old key still equals the old base default, the new template default is used. |
| 5 | Low | `setup/sandbox/Dockerfile` | The header still pointed at `[secrets] code_runner_api_key` in `perseus.toml`, which is now a config error. | Points at `[code_runner] api_key` in `Config/tooling.toml`. |
| 6 | Low | `setup/install_driver.sh`, `README.md` | Claimed Debian support, but `ubuntu-drivers-common` is Ubuntu-only, so it failed inside apt on Debian. | Checks `/etc/os-release` and says plainly that it is Ubuntu-only. The README says the same. |
| 7 | Low | `preflight.sh` | `--skip-download` was missing from `--help`. | Added. |

## Checked and sound

- Every key in the three templates matches `SCHEMA` in `perseus/config.py`, and every `SCHEMA` key is in a template.
- `pins.env` (Python 3.12) matches `[tool.perseus] python`. `driver.min` matches the vLLM 0.32 / torch 2.13 pins.
- File modes are right: config files, backups and `unused-keys.toml` are 600, and `.backup`, `.base` and `run/` are 700. Secrets are never printed, and `perseus.resolved.toml` stores only `api_key_set`.
- The KV-per-token and TP math is correct for standard, MLA and hybrid attention.
- Re-runs skip finished steps.

## Tests added

- `tests/test_sizing.py`: a fixed context picks the TP that holds it.
- `tests/test_hub.py`: a partial `text_config` falls back to top-level keys, and a missing key gives a clear `HubError`.
- `tests/test_config_update.py`: an untouched renamed key takes the new default. This test fails against the old code.
