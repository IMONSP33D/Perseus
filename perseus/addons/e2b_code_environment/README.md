# e2b_code_environment: the code runner

This addon ships with Perseus in `perseus/addons/e2b_code_environment/`. Its settings are the `[e2b_code_environment]` section of `Config/tooling.toml` (added there by `bash preflight.sh`); the general rules for addons are in [`docs/addons.md`](../../../docs/addons.md).

`run_code` lets the model test its code before giving it to you. Each run happens in a brand-new virtual machine on [E2B](https://e2b.dev), which is deleted straight afterwards. Nothing runs on your GPU server.

To set it up:

1. Create an API key at [e2b.dev](https://e2b.dev) and add it to `Config/tooling.toml`:
   ```toml
   [e2b_code_environment]
   api_key = "e2b_..."
   ```
2. Build the sandbox image with all the compilers. You only do this once, it builds on E2B's side, and it needs no Docker. (Preflight installs uv in `~/.local/bin` without adding it to your PATH, hence the full path.)
   ```bash
   ~/.local/bin/uv run python -m perseus.addons.e2b_code_environment.setup.build_template
   ```
3. Tell Perseus to use that image, in the same section of `Config/tooling.toml`:
   ```toml
   e2b_template = "perseus-code"
   ```
4. Run `bash preflight.sh`, then `python3 perseus.py`. On startup you'll see `✓ code runner: e2b · python, javascript, …`, or a line saying why it's off.

If you skip step 2, you get E2B's standard image, which has Python, JavaScript and Bash only. With the full image you also get C, C++, Rust, Go and Java. Perseus checks which languages are really there and only offers those.

What programs can reach is set by `network` in `[e2b_code_environment]`:

| `network` | Programs can reach |
| --- | --- |
| `"none"` | Nothing at all |
| `"packages"` (default) | Package registries only (pip, npm, cargo, Go and Maven), so installs work |
| `"full"` | The whole internet |

Perseus tests the network from inside a real sandbox at startup. If programs can reach more than the setting allows, it turns the code runner off.

To check where your code actually runs, run `~/.local/bin/uv run python -m perseus.addons.e2b_code_environment.doctor`. It creates one sandbox with your key, shows its E2B id and what it reports about itself, then deletes it. Every run is also logged in `logs/e2b_code_environment.log`, without the code or its output.


The tests run with the rest of the addons' suites: `python3 -m unittest discover -s perseus/addons -t .`. The E2B conformance test needs a real key: `PERSEUS_TEST_E2B=1 ~/.local/bin/uv run python -m unittest perseus.addons.e2b_code_environment.tests.test_sandbox_conformance`.
