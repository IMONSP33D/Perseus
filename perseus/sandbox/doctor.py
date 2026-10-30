"""Check the code runner end to end and show exactly where code runs.

    uv run python -m perseus.sandbox.doctor [--config Config]

Uses the same key, template and network level as Perseus (from Config/tooling.toml) and prints:
  1. which key it uses (as a fingerprint, e.g. e2b_…3f9a), where it came from, and the API host
  2. any E2B_* environment variables that would redirect the SDK
  3. a real sandbox: the id E2B returns, and how long it took to create
  4. whether E2B's own API, asked with the same key, lists that sandbox
  5. what the sandbox reports about itself (pwd, hostname, user, kernel)
  6. that it was destroyed

Then you can look for that sandbox id in the E2B dashboard. If the dashboard doesn't show it, the
dashboard is open on a different account or team than the one that owns this key.
It never prints the key itself. One sandbox for a few seconds costs a fraction of a cent.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tomllib
from pathlib import Path

from perseus.sandbox.backends import SandboxError, SessionSpec, config_secret, fingerprint, tooling_file
from perseus.sandbox.backends.e2b import SDK_ENV

OK, BAD, WARN = "✓", "✗", "!"


def say(mark: str, msg: str) -> None:
    print(f"  {mark} {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m perseus.sandbox.doctor")
    p.add_argument("--config", default="Config", help="the Config folder (or a single .toml) to read (default: ./Config)")
    a = p.parse_args(argv)
    print("Perseus code runner check\n")

    # 1. configuration and key ---------------------------------------------------------------
    path = tooling_file(a.config)
    cfg: dict = {}
    if path.is_file():
        cfg = (tomllib.loads(path.read_text()).get("code_runner") or {})
        say(OK, f"config: {path.resolve()}")
    else:
        say(WARN, f"no {path} here; using defaults and E2B_API_KEY")
    backend = cfg.get("backend", "e2b")
    if backend != "e2b":
        say(BAD, f"[code_runner] backend = {backend!r}; this check covers e2b only")
        return 1
    try:
        key = config_secret(str(path)) if path.is_file() else ""
    except SandboxError as e:
        say(BAD, str(e))
        return 1
    source = "tooling.toml [code_runner] api_key" if key else "E2B_API_KEY environment variable"
    key = key or (os.environ.get("E2B_API_KEY") or "").strip()
    if not key:
        say(BAD, "no key: set [code_runner] api_key in Config/tooling.toml")
        return 1
    say(OK, f"key {fingerprint(key)} from {source}  (match the last 4 characters in E2B's dashboard › API Keys)")
    if source.startswith("tooling.toml") and os.environ.get("E2B_API_KEY", "").strip() not in ("", key):
        say(WARN, f"E2B_API_KEY in this shell is a DIFFERENT key ({fingerprint(os.environ['E2B_API_KEY'])}); "
                  "Perseus uses the tooling.toml one")

    # 2. environment that would redirect the SDK ----------------------------------------------
    present = [n for n in SDK_ENV if n != "E2B_API_KEY" and os.environ.get(n)]
    if present:
        say(WARN, f"set in this shell: {', '.join(f'{n}={os.environ[n]}' for n in present)}. Perseus's broker "
                  "doesn't pass these on, but this check will use them, so unset them to test what Perseus does")
    else:
        say(OK, "no E2B_DOMAIN / E2B_API_URL / E2B_DEBUG overrides in this shell")

    try:
        from e2b import Sandbox
        from perseus.sandbox.backends.e2b import E2BBackend
    except ImportError:
        say(BAD, "the e2b package isn't installed: run bash preflight.sh (or uv sync --extra sandbox)")
        return 1
    b = E2BBackend({"api_key": key, "e2b_template": cfg.get("e2b_template", "")}, environ=os.environ)
    ident = b.describe()
    say(OK, f"E2B SDK {ident['sdk']} · API {ident['api_url']} · template {ident['template']}")
    level = cfg.get("network") or "packages"

    # 3. a real sandbox ------------------------------------------------------------------------
    t0 = time.monotonic()
    try:
        session = b.open(SessionSpec(network=level, lifetime_s=120))
    except SandboxError as e:
        say(BAD, f"E2B refused to create a sandbox ({e.code}): {e}")
        if e.code == "auth":
            print("\n  The key was rejected: check it in E2B's dashboard › API Keys.")
        return 1
    say(OK, f"E2B created sandbox {session.id} in {(time.monotonic() - t0):.1f} s (network {level})")

    ok = True
    try:
        # 4. does E2B's API, asked with the same key, see it? -------------------------------
        try:
            info = session.sbx.get_info()
            say(OK, f"E2B's API reports it: template {info.template_id}, state {getattr(info.state, 'value', info.state)}, "
                    f"started {info.started_at:%Y-%m-%d %H:%M:%S} UTC")
        except Exception as e:
            ok = False
            say(BAD, f"couldn't read it back from E2B's API ({type(e).__name__}: {e})")
        try:
            listed = [s.sandbox_id for s in Sandbox.list(api_key=key, limit=100).next_items()]
            if session.id in listed:
                say(OK, f"listed among this key's team's {len(listed)} running sandbox(es)")
            else:
                ok = False
                say(BAD, f"NOT in the list of this key's running sandboxes ({len(listed)} listed)")
        except Exception as e:
            say(WARN, f"couldn't list sandboxes ({type(e).__name__}: {e})")

        # 5. where are we? ----------------------------------------------------------------
        r = session.exec("echo \"pwd=$(pwd)\"; echo \"hostname=$(hostname)\"; echo \"user=$(id -un)\"; "
                         "echo \"kernel=$(uname -sr)\"; echo \"os=$(. /etc/os-release 2>/dev/null; echo $PRETTY_NAME)\"", 30)
        facts = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        if r.exit_code == 0 and facts:
            say(OK, "inside the sandbox: " + " · ".join(f"{k} {v}" for k, v in facts.items()))
            if facts.get("pwd") != "/home/user/job":
                ok = False
                say(WARN, "pwd isn't /home/user/job, the folder Perseus's E2B backend uses")
        else:
            ok = False
            say(BAD, f"a command inside the sandbox failed (exit {r.exit_code}): {(r.stderr or r.stdout)[:300]}")
    finally:
        # 6. destroy it -----------------------------------------------------------------------
        session.close()
        say(OK if session.killed else WARN,
            "destroyed" if session.killed else "couldn't destroy it; it expires on its own within 2 minutes")

    print()
    if ok:
        print(f"  Code runs on E2B, in sandboxes like {session.id}, under the team that owns key {fingerprint(key)}.")
        print("  If E2B's dashboard shows no sandboxes, it's open on a different account or team: check the team")
        print("  switcher, and that API Keys there lists a key ending in", key[-4:] + ".")
    else:
        print("  Something above didn't check out; send this output.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
