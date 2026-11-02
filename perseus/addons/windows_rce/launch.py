"""Boot-time decisions for the RCE addon, kept out of the launcher so they can be tested.

    plan()     before processes start: start the relay or not, and why
    connect()  after the relay is up: offer run_windows_code, or say why not
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

from perseus.addons.windows_rce.upstream import SECTION, check_url
from perseus.sandbox.backends import SandboxError

CAPABILITY = "windows_rce"
TOKEN_ENV = "PERSEUS_RCE_TOKEN"
SETTINGS_ENV = "PERSEUS_RCE_SETTINGS"
PROCESS = "Windows VM"


@dataclass
class Plan:
    start: bool
    reason: str
    env: dict = field(default_factory=dict)      # extra environment for the relay process only


def plan(cfg: dict, config_path: str = "") -> Plan:
    """Decide whether to start the relay. The key lives in Config/tooling.toml ([windows_rce] api_key);
    Perseus only learns from preflight *whether* one is set (api_key_set), and the relay reads the value."""
    if not cfg:
        return Plan(False, f"off (no [{SECTION}] section; run bash preflight.sh to refresh the config)")
    if not cfg.get("enabled", True):
        return Plan(False, f"off ([{SECTION}] enabled = false)")
    if not cfg.get("api_key_set") or not config_path:
        return Plan(False, f"off (no [{SECTION}] api_key in Config/tooling.toml; add it, then run bash preflight.sh)")
    try:
        check_url(str(cfg.get("url") or ""))
    except SandboxError as e:
        return Plan(False, f"off ({e})")
    settings = {k: v for k, v in cfg.items() if k != "api_key_set"}
    settings["config_path"] = config_path
    return Plan(True, f"starting ({cfg.get('url')})", {SETTINGS_ENV: json.dumps(settings)})


# all the relay inherits from Perseus's environment: enough to run Python, nothing else
RELAY_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")


def relay_command(port: int) -> list[str]:
    return [sys.executable, "-m", "perseus.addons.windows_rce.relay", "--port", str(int(port))]


def relay_env(environ, p: Plan, token: str) -> dict:
    env = {k: environ[k] for k in RELAY_ENV_KEEP if k in environ}
    env.update(p.env)
    env[TOKEN_ENV] = token
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def note(info: dict) -> str:
    """What the model is told about this VM, appended to the tool's description."""
    limits = info.get("limits") or {}
    bits = [f"Runs are limited to {limits.get('run_timeout_s', 120)} s."]
    bits.append("The VM can reach the internet when you set network=true (it is off otherwise)."
                if info.get("allow_network") else
                "The VM has no network: pip, npm and NuGet installs fail, so use only what is installed.")
    bits.append("Set keep_vm_state=true to keep installs and files for your next call, and call reset_windows_vm "
                "when you are done." if info.get("allow_keep_state") else
                "Every run starts from a clean snapshot.")
    return " ".join(bits)


def connect(client, registry) -> tuple[bool, str]:
    """Read the relay's start-up result and provide the capability, or withdraw it and say why.

    An API that isn't reachable yet still gets the tool: the tunnel or start-api.ps1 often comes
    up after Perseus, and a run then fails with a plain message the model can pass on."""
    info = client.info()
    err = info.get("error")
    if err:
        registry.withdraw(CAPABILITY)
        return False, f"off ({err.get('code')}: {err.get('message')})"
    registry.provide(CAPABILITY, note=note(info),
                     enums={"language": list(info.get("languages") or []),
                            "network": [False, True] if info.get("allow_network") else [False],
                            "keep_vm_state": [False, True] if info.get("allow_keep_state") else [False]})
    ident = info.get("identity") or {}
    parts = [f"{ident.get('api_url', '?')} (key {ident.get('key', '?')})", ", ".join(info.get("languages") or [])]
    parts.append("network allowed" if info.get("allow_network") else "network off")
    if info.get("reachable"):
        vm = info.get("vm") or {}
        state = vm.get("vm_state") or vm.get("state")
        parts.append(f"VM {state}" if state else "API reachable")
    else:
        parts.append("! API not reachable yet; start-api.ps1 and the tunnel must be up before a run")
    return True, " · ".join(parts)
