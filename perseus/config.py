"""Load and validate perseus.toml.

Every error names the file, line and key so a bad value is caught in seconds,
not after a long download. Unknown keys are errors too (typos silently ignored
are the worst kind of config bug).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HF_TOKEN_RE = re.compile(r"^hf_[A-Za-z0-9]{20,}$")


class ConfigError(Exception):
    """One or more problems in the config; `problems` holds one line each."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass(frozen=True)
class Key:
    types: tuple[type, ...]
    default: Any = None
    required: bool = False
    choices: tuple | None = None
    check: Callable[[Any], str | None] | None = None
    secret: bool = False

    def validate(self, value: Any) -> str | None:
        # bool is a subclass of int in Python; never accept True for a number.
        if isinstance(value, bool) and bool not in self.types:
            return f"expected {self._type_names()}, got a boolean"
        if not isinstance(value, self.types):
            return f"expected {self._type_names()}, got {type(value).__name__} {value!r}"
        if self.choices is not None and value not in self.choices:
            return f"must be one of {', '.join(repr(c) for c in self.choices)}; got {value!r}"
        if self.check is not None:
            return self.check(value)
        return None

    def _type_names(self) -> str:
        names = {str: "a string", int: "a whole number", float: "a number",
                 bool: "true/false", list: "a list"}
        return " or ".join(names.get(t, t.__name__) for t in self.types)


def _repo_id(v: str) -> str | None:
    if v in ("", "org/model-name"):
        return "set this to a Hugging Face repo id, e.g. \"Qwen/Qwen3-32B\""
    if not REPO_ID_RE.match(v):
        return f"{v!r} is not a repo id of the form \"org/name\""
    return None


def _auto_or_positive_int(v: Any) -> str | None:
    if isinstance(v, str):
        return None if v == "auto" else f"must be \"auto\" or a number of tokens; got {v!r}"
    return None if v >= 1024 else "must be at least 1024 tokens"


def _auto_or_bool(v: Any) -> str | None:
    if isinstance(v, str) and v != "auto":
        return f"must be \"auto\", true or false; got {v!r}"
    return None


def _non_negative(v: float) -> str | None:
    return None if v >= 0 else "must be 0 or more"


def _port(v: int) -> str | None:
    return None if 1 <= v <= 65535 else "must be a port number from 1 to 65535"


def _positive(v: float) -> str | None:
    return None if v > 0 else "must be more than 0"


def _hf_token(v: str) -> str | None:
    if v in ("", "hf_..."):
        return None  # allowed for public repos; preflight warns
    return None if HF_TOKEN_RE.match(v) else "doesn't look like a Hugging Face token (hf_ followed by letters and digits)"


def _model_dir(v: str) -> str | None:
    return None if v == "auto" or v.startswith("/") else "must be \"auto\" or an absolute path"


def _engines(v: list) -> str | None:
    if not v:
        return "list at least one search engine"
    if not all(isinstance(e, str) and e for e in v):
        return "every engine must be a non-empty string"
    return None


SCHEMA: dict[str, dict[str, Key]] = {
    "model": {
        "id": Key((str,), required=True, check=_repo_id),
        "revision": Key((str,), "main"),
        "quantization": Key((str,), "auto", choices=("auto", "fp8", "bf16", "awq", "gptq")),
        "max_context": Key((str, int), "auto", check=_auto_or_positive_int),
        "tensor_parallel": Key((str, int), "auto", choices=("auto", 1, 2, 4, 8)),
        "trust_remote_code": Key((bool,), False),
        "cpu_offload_gb": Key((int, float), 0, check=_non_negative),
        "small_gpu": Key((str, bool), "auto", check=_auto_or_bool),
        "tool_parser": Key((str,), "auto"),
        "reasoning_parser": Key((str,), "auto"),
    },
    "secrets": {
        "hf_token": Key((str,), "", check=_hf_token, secret=True),
    },
    "storage": {
        "model_dir": Key((str,), "auto", check=_model_dir),
    },
    "server": {
        "bind": Key((str,), "127.0.0.1"),
        "port": Key((int,), 8080, check=_port),
        "public": Key((bool,), False),
    },
    "search": {
        "port": Key((int,), 8888, check=_port),
        "engines": Key((list,), ["duckduckgo", "wikipedia", "github", "arxiv", "stackoverflow", "mojeek"],
                       check=_engines),
    },
    "backend": {
        "max_restarts": Key((int,), 3, check=_non_negative),
        "fast_start": Key((bool,), False),
    },
    "limits": {
        "research_depth": Key((str,), "standard", choices=("quick", "standard", "deep")),
        "fetch_timeout_s": Key((int, float), 15, check=_positive),
        "max_response_kb": Key((int,), 2048, check=_positive),
        "warn_download_minutes": Key((int, float), 20, check=_positive),
    },
}


@dataclass
class Config:
    data: dict[str, dict[str, Any]]
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]

    def public(self) -> dict[str, dict[str, Any]]:
        """Every section except [secrets], for files that leave preflight."""
        return {s: dict(v) for s, v in self.data.items() if s != "secrets"}


def find_line(text: str, section: str, key: str | None = None) -> int | None:
    """Best-effort line number of `[section]` or of `key` inside it."""
    current = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^\[\s*([\w.-]+)\s*\]$", line)
        if m:
            current = m.group(1)
            if key is None and current == section:
                return n
            continue
        if key is not None and current == section and re.match(rf"^{re.escape(key)}\s*=", line):
            return n
    return None


def parse_override_value(raw: str) -> Any:
    """`--model.max_context 32768` -> 32768; anything that isn't TOML is a string."""
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def split_overrides(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Pull `--section.key value` / `--section.key=value` pairs out of argv."""
    rest: list[str] = []
    overrides: dict[str, Any] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        m = re.match(r"^--([a-z_]+)\.([a-z_]+)(?:=(.*))?$", arg)
        if m:
            name = f"{m.group(1)}.{m.group(2)}"
            if m.group(3) is not None:
                overrides[name] = parse_override_value(m.group(3))
            elif i + 1 < len(argv):
                overrides[name] = parse_override_value(argv[i + 1])
                i += 1
            else:
                raise ConfigError([f"{arg}: missing a value"])
        else:
            rest.append(arg)
        i += 1
    return rest, overrides


def load(path: Path, overrides: dict[str, Any] | None = None) -> Config:
    """Parse, validate and fill defaults. Raises ConfigError listing every problem."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError([f"{path.name} not found: copy perseus.example.toml to perseus.toml and fill it in"])
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"{path.name}: {e}"])
    return validate(raw, text=text, name=path.name, overrides=overrides, path=path)


def validate(raw: dict, *, text: str = "", name: str = "perseus.toml",
             overrides: dict[str, Any] | None = None, path: Path | None = None) -> Config:
    problems: list[str] = []
    warnings: list[str] = []

    def where(section: str, key: str | None = None) -> str:
        line = find_line(text, section, key) if text else None
        loc = f"{name}:{line}" if line else name
        return f"{loc} [{section}]" + (f" {key}" if key else "")

    for section, value in raw.items():
        if section not in SCHEMA:
            problems.append(f"{where(section)}: unknown section (known: {', '.join(SCHEMA)})")
        elif not isinstance(value, dict):
            problems.append(f"{where(section)}: must be a [section], not a value")
        else:
            for key in value:
                if key not in SCHEMA[section]:
                    hint = _closest(key, SCHEMA[section])
                    problems.append(f"{where(section, key)}: unknown key" + (f" (did you mean {hint!r}?)" if hint else ""))

    for name_, value in (overrides or {}).items():
        section, _, key = name_.partition(".")
        if section not in SCHEMA or key not in SCHEMA[section]:
            problems.append(f"--{name_}: unknown key")
            continue
        raw.setdefault(section, {})[key] = value

    data: dict[str, dict[str, Any]] = {}
    for section, keys in SCHEMA.items():
        given = raw.get(section, {}) if isinstance(raw.get(section, {}), dict) else {}
        out: dict[str, Any] = {}
        for key, spec in keys.items():
            if key in given:
                err = spec.validate(given[key])
                if err:
                    shown = "(hidden)" if spec.secret else ""
                    problems.append(f"{where(section, key)}: {err}{' ' + shown if shown else ''}")
                out[key] = given[key]
            elif spec.required:
                problems.append(f"{where(section)}: missing required key {key!r}")
            else:
                out[key] = list(spec.default) if isinstance(spec.default, list) else spec.default
        data[section] = out

    if not problems:
        m = data["model"]
        if m["trust_remote_code"] and not COMMIT_RE.match(m["revision"]):
            problems.append(f"{where('model', 'revision')}: trust_remote_code = true requires revision "
                            "pinned to a full 40-character commit hash, not a branch")
        if m["cpu_offload_gb"] and m["tensor_parallel"] not in ("auto", 1):
            problems.append(f"{where('model', 'cpu_offload_gb')}: CPU offload is single-GPU only; "
                            "set tensor_parallel = 1 or cpu_offload_gb = 0")
        if data["server"]["public"] and data["server"]["bind"] == "127.0.0.1":
            data["server"]["bind"] = "0.0.0.0"
        if data["server"]["port"] == data["search"]["port"]:
            problems.append(f"{where('search', 'port')}: must differ from [server] port")
        if data["secrets"]["hf_token"] in ("", "hf_..."):
            data["secrets"]["hf_token"] = ""
            warnings.append("no HF token set: fine for public repos; gated or private repos will fail the check")

    if problems:
        raise ConfigError(problems)
    return Config(data=data, path=path, warnings=warnings)


def _closest(key: str, known: dict) -> str | None:
    import difflib
    match = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
    return match[0] if match else None
