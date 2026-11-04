"""Helpers every addon may use. Nothing here imports an addon, so no addon depends on another.

    AddonError      a failure of the addon's service itself (not of the user's program)
    fingerprint()   show enough of a key to recognise it, never enough to use it
    tooling_file()  where Config/tooling.toml is, given the Config folder (or a single-file config)
    read_section_secret()   one secret key out of one [section] of tooling.toml, for the addon's own
                            process; Perseus itself never calls this
"""

from __future__ import annotations

import tomllib
from pathlib import Path

TOOLING_FILE = "tooling.toml"


class AddonError(Exception):
    """A failure of the addon's service (not of the user's program). Addons translate their provider's
    exceptions into one of these codes, so every addon fails alike and the model reads one vocabulary."""

    CODES = ("unavailable", "auth", "quota", "config", "backend")

    def __init__(self, code: str, message: str):
        assert code in self.CODES, code
        super().__init__(message)
        self.code = code


def fingerprint(key: str) -> str:
    """Enough of a key to match it against the provider's dashboard, never enough to use it."""
    key = key.strip()
    if len(key) < 12:
        return "(too short to show)"
    prefix = key[:4] if key[3:4] == "_" else key[:2]
    return f"{prefix}…{key[-4:]}"


def tooling_file(config_path: str | Path) -> Path:
    """Config/tooling.toml for a Config folder; the file itself for a single-file config."""
    path = Path(config_path).expanduser()
    if path.is_dir() or path.suffix != ".toml":
        return path / TOOLING_FILE
    if path.name == "perseus.toml" and not path.exists() and (path.parent / "Config" / TOOLING_FILE).is_file():
        # a resolved file from before the split still names the old perseus.toml, which
        # configuration.sh has since moved into Config/: follow it there until preflight re-runs
        return path.parent / "Config" / TOOLING_FILE
    return path


def read_section_secret(config_path: str | Path, section: str, key: str = "api_key",
                        placeholders: tuple[str, ...] = ("...",), former_sections: tuple[str, ...] = ()) -> str:
    """[section] key from Config/tooling.toml, or "" when unset or still the template placeholder.

    Opens tooling.toml alone, so an addon's process never reads the Hugging Face token in provider.toml.
    A key still under one of `former_sections` (an old name of this addon, or "section:key" for a key
    that moved from elsewhere) is accepted until `bash Config/configuration.sh` moves it.
    Raises AddonError("config") when the file can't be read."""
    if not config_path:
        return ""
    path = tooling_file(config_path)
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AddonError("config", f"can't read {path} for [{section}] {key} ({type(e).__name__})")
    for name in (section, *former_sections):
        sec, _, k = name.partition(":")
        value = str((data.get(sec) or {}).get(k or key) or "").strip()
        if value:
            return "" if value in placeholders or value.endswith("...") else value
    return ""
