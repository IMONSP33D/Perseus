"""Install every addon's Python packages into Perseus's environment, from each addon's dependencies.toml.

    python -m setup.addon_deps list              one `name==version` per line, for every addon
    python -m setup.addon_deps install [--python PY]   install them with uv (into PY's environment)
    python -m setup.addon_deps check             exit 1 and say which are missing; nothing installed

An addon declares what its own process needs, exactly pinned, in perseus/addons/<name>/dependencies.toml:

    [packages]
    e2b = "2.62.0"

preflight.sh runs `install` after the pinned `uv sync` (which makes the environment exact to uv.lock, so
addon packages must go in afterwards, not before). Two addons pinning different versions of one
package is an error here, before anything is installed. The engine never imports these packages: they
belong to the addon's own process, which is why they don't have to be in pyproject.toml.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def requirements() -> tuple[list[str], list[str]]:
    """(['name==version', ...] sorted, [problem lines]). Skipped addons are reported, never installed."""
    from perseus import addons
    pins: dict[str, tuple[str, str]] = {}     # name -> (version, addon)
    problems = []
    for a in addons.discover():
        try:
            deps = a.dependencies()
        except ValueError as e:
            problems.append(f"{a.name}: {e}")
            continue
        for name, version in deps.items():
            key = name.lower().replace("_", "-")
            if key in pins and pins[key][0] != version:
                problems.append(f"{name}: {pins[key][1]} pins {pins[key][0]} but {a.name} pins {version}")
                continue
            pins[key] = (version, a.name)
    for folder, why in addons.errors().items():
        problems.append(f"{folder}: skipped ({why})")
    return sorted(f"{n}=={v}" for n, (v, _) in pins.items()), problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m setup.addon_deps")
    p.add_argument("action", choices=("list", "install", "check"))
    p.add_argument("--python", default=sys.executable, help="the interpreter whose environment to install into")
    a = p.parse_args(argv)
    reqs, problems = requirements()
    for line in problems:
        print(f"  ! addon {line}", file=sys.stderr)
    if a.action == "list":
        print("\n".join(reqs))
        return 1 if problems else 0
    if a.action == "check":
        from perseus import addons
        missing = []
        for ad in addons.discover():
            try:
                missing += [f"{ad.name}: {m}" for m in addons.missing_packages(ad.dependencies())]
            except ValueError:
                pass
        for line in missing:
            print(f"  ! {line}", file=sys.stderr)
        return 1 if missing or problems else 0
    if not reqs:
        print("  · addons: no packages to install")
        return 1 if problems else 0
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    cmd = [uv, "pip", "install", "-q", "--python", a.python, *reqs]
    env = {**os.environ, "UV_NO_PROGRESS": "1"}
    r = subprocess.run(cmd, env=env)
    if r.returncode:
        print(f"  ✗ uv pip install failed for: {' '.join(reqs)}", file=sys.stderr)
        return r.returncode
    print(f"  ✓ addon packages: {', '.join(reqs)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
