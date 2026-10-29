"""Build setup/sandbox/Dockerfile into an E2B template, using E2B's builders (no local Docker needed).

    uv run python -m setup.sandbox.build_template --key-file ~/.config/perseus/e2b.key [--name perseus-code]

Then set [code_runner] e2b_template = "<name>" in perseus.toml. Rebuild after editing the Dockerfile.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent / "Dockerfile"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m setup.sandbox.build_template")
    p.add_argument("--key-file", default="", help="file holding your E2B API key (default: $E2B_API_KEY)")
    p.add_argument("--name", default="perseus-code", help="template name to create or update")
    a = p.parse_args(argv)
    try:
        from e2b import Template
    except ImportError:
        sys.exit("the e2b package isn't installed: run bash preflight.sh (or uv sync --extra sandbox)")
    from perseus.sandbox.backends import SandboxError
    from perseus.sandbox.backends.e2b import read_key

    try:
        key = read_key({"key_file": a.key_file}, os.environ)
    except SandboxError as e:
        sys.exit(str(e))
    print(f"Building {DOCKERFILE} as E2B template {a.name!r} (several minutes on first build)…", flush=True)
    info = Template.build(Template().from_dockerfile(str(DOCKERFILE)), a.name, api_key=key,
                          on_build_logs=lambda entry: print(f"  {entry}", flush=True))
    print(f"\n✓ built: {info}\n  set [code_runner] e2b_template = \"{a.name}\" in perseus.toml")


if __name__ == "__main__":
    main()
