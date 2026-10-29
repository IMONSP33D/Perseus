"""Boot-time decisions for the code runner, kept out of the launcher so they can be tested.

    plan()     before processes start: start the broker or not, and why
    connect()  after the broker is up: offer run_code, or say why not (fails closed)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from perseus.sandbox.backends import CONFIGURABLE

KEY_ENV = "E2B_API_KEY"


@dataclass
class Plan:
    start: bool
    reason: str
    env: dict = field(default_factory=dict)      # extra environment for the broker process only


def plan(cfg: dict, environ) -> Plan:
    """Decide whether to start the broker. Always removes E2B_API_KEY from `environ` (this process's
    environment), so tools, vLLM and SearXNG never inherit it; only the broker gets it back."""
    env_key = (environ.pop(KEY_ENV, "") or "").strip()
    if not cfg:
        return Plan(False, "off (no [code_runner] section; run bash preflight.sh to refresh the config)")
    if not cfg.get("enabled", True):
        return Plan(False, "off ([code_runner] enabled = false)")
    backend = cfg.get("backend", "e2b")
    if backend not in CONFIGURABLE:
        return Plan(False, f"off (unknown backend {backend!r})")
    settings = {**cfg, "backend": backend}
    env = {"PERSEUS_SANDBOX_SETTINGS": json.dumps(settings)}
    if backend == "e2b":
        if importlib.util.find_spec("e2b") is None:
            return Plan(False, "off (the e2b package isn't installed; run bash preflight.sh)")
        key_file = (cfg.get("key_file") or "").strip()
        if key_file:
            if not Path(key_file).expanduser().is_file():
                return Plan(False, f"off ([code_runner] key_file {key_file} not found)")
        elif env_key:
            env[KEY_ENV] = env_key
        else:
            return Plan(False, "off (no E2B key: set [code_runner] key_file, see README › Code runner)")
    return Plan(True, f"starting ({backend})", env)


# all the broker inherits from Perseus's environment: enough to run Python and reach the provider
# (proxies, CA bundles), and nothing else, so an exported HF_TOKEN or other key never reaches it
BROKER_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
                   "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                   "no_proxy")


def broker_command(port: int) -> list[str]:
    return [sys.executable, "-m", "perseus.sandbox.broker", "--port", str(int(port))]


def broker_env(environ, p: Plan, token: str) -> dict:
    env = {k: environ[k] for k in BROKER_ENV_KEEP if k in environ}
    env.update(p.env)
    env["PERSEUS_SANDBOX_TOKEN"] = token
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def connect(client, registry) -> tuple[bool, str]:
    """Read the broker's probe and provide the "sandbox" capability, or withdraw it and say why."""
    info = client.info()
    err = info.get("error")
    if err:
        registry.withdraw("sandbox")
        return False, f"off ({err.get('code')}: {err.get('message')})"
    langs = info.get("languages") or []
    if not langs:
        registry.withdraw("sandbox")
        return False, "off (no language toolchains found in the sandbox)"
    if not info.get("internet") and info.get("network_blocked") is not True:
        registry.withdraw("sandbox")   # fail closed: never offer a sandbox whose network we couldn't close
        return False, "off (the sandbox could reach the internet although [code_runner] internet = false)"
    registry.provide("sandbox", reaches_web=bool(info.get("internet")), enums={"language": langs})
    missing = info.get("missing") or []
    net = "internet on" if info.get("internet") else "no network"
    return True, (f"{info.get('backend')} · {', '.join(langs)} · {net}"
                  + (f" · not installed: {', '.join(missing)}" if missing else ""))
