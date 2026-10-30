"""Build setup/sandbox/Dockerfile into an E2B template, using E2B's builders (no local Docker needed).

    uv run python -m setup.sandbox.build_template [--config Config] [--name perseus-code]

Uses [code_runner] api_key from Config/tooling.toml (or E2B_API_KEY). Then set
[code_runner] e2b_template = "<name>" in Config/tooling.toml. Rebuild after editing the Dockerfile.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent / "Dockerfile"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m setup.sandbox.build_template")
    p.add_argument("--config", default="Config", help="the Config folder (its tooling.toml holds [code_runner] api_key)")
    p.add_argument("--name", default="perseus-code", help="template name to create or update")
    a = p.parse_args(argv)
    try:
        from e2b import Template
    except ImportError:
        sys.exit("the e2b package isn't installed: run bash preflight.sh (or uv sync --extra sandbox)")
    from perseus.sandbox.backends import SandboxError, config_secret, tooling_file
    from perseus.sandbox.backends.e2b import read_key

    try:
        found = config_secret(a.config) if tooling_file(a.config).is_file() else ""
        key = read_key({"api_key": found}, os.environ)
    except SandboxError as e:
        sys.exit(str(e))
    print(f"Building {DOCKERFILE} as E2B template {a.name!r} (several minutes on first build)…", flush=True)
    info = Template.build(Template().from_dockerfile(str(DOCKERFILE)), a.name, api_key=key,
                          on_build_logs=lambda entry: print(f"  {entry}", flush=True))
    print(f"\n✓ built: {info}\n  set [code_runner] e2b_template = \"{a.name}\" in Config/tooling.toml")


if __name__ == "__main__":
    main()
