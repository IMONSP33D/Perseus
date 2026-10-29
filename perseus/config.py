"""perseus.toml (the config), perseus.resolved.toml (what preflight decided) and the data directory.

Two trees are kept apart: the checkout you cloned (code and perseus.toml) and
a data directory preflight creates on the fastest local disk (weights, logs,
SearXNG, run state). Preflight records the data directory in a one-line
pointer file beside perseus.toml, so perseus.py never guesses.

Config validation:

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
    if v.startswith(("hf://", "https://", "http://")) or v.endswith((".gguf", ".safetensors")):
        return (f"{v!r} is a file or URL; set the repo id only, e.g. \"org/name\" "
                "(Perseus downloads the right files itself)")
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


def _data_dir(v: str) -> str | None:
    return None if v == "auto" or v.startswith("/") else "must be \"auto\" or an absolute path"


def _engines(v: list) -> str | None:
    if not v:
        return "list at least one search engine"
    if not all(isinstance(e, str) and e for e in v):
        return "every engine must be a non-empty string"
    return None


def _range(lo: int, hi: int) -> Callable[[Any], str | None]:
    def check(v: Any) -> str | None:
        return None if lo <= v <= hi else f"must be between {lo} and {hi}"
    return check


def _api_key(v: str) -> str | None:
    if v and any(c.isspace() for c in v.strip()):
        return "must be the key alone, with no spaces"
    return None


def _languages(v: list) -> str | None:
    from perseus.sandbox.languages import LANGUAGES
    bad = [x for x in v if x not in LANGUAGES]
    return f"unknown language(s) {', '.join(map(str, bad))} (known: {', '.join(LANGUAGES)})" if bad else None


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
        "code_runner_api_key": Key((str,), "", check=_api_key, secret=True),
    },
    "storage": {
        "data_dir": Key((str,), "auto", check=_data_dir),
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
    "code_runner": {
        "enabled": Key((bool,), True),
        "backend": Key((str,), "e2b", choices=("e2b",)),
        "e2b_template": Key((str,), ""),
        "internet": Key((bool,), False),
        "languages": Key((list,), [], check=_languages),
        "build_timeout_s": Key((int,), 120, check=_range(5, 180)),
        "run_timeout_s": Key((int,), 30, check=_range(1, 120)),
        "max_output_kb": Key((int,), 64, check=_range(4, 1024)),
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
        key = data["secrets"]["code_runner_api_key"].strip()
        data["secrets"]["code_runner_api_key"] = "" if key in ("", "e2b_...") else key
        if data["code_runner"]["enabled"] and not data["secrets"]["code_runner_api_key"]:
            warnings.append("no [secrets] code_runner_api_key set: the code runner (run_code) stays off")

    if problems:
        raise ConfigError(problems)
    return Config(data=data, path=path, warnings=warnings)


def _closest(key: str, known: dict) -> str | None:
    import difflib
    match = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
    return match[0] if match else None


# ---------------------------------------------------------------------------- TOML writer
# (stdlib reads TOML but can't write it; resolved files only need flat sections)

def _toml_value(v: Any) -> str:
    import json
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def dumps(sections: dict[str, dict[str, Any]], header: str = "") -> str:
    out = [f"# {line}" for line in header.splitlines()] + ([""] if header else [])
    for name, keys in sections.items():
        out.append(f"[{name}]")
        for k, v in keys.items():
            if v is None:
                continue
            out.append(f"{k} = {_toml_value(v)}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------- the data directory

DATA_POINTER = ".perseus-data"
WEIGHTS_MARKER = ".perseus-complete.json"   # written beside the weights once every file is verified
RESOLVED_NAME = "perseus.resolved.toml"


@dataclass(frozen=True)
class Paths:
    """Everything Perseus writes at run time, under one directory on the fastest disk."""
    data: Path

    @property
    def weights(self) -> Path:
        return self.data / "weights"

    @property
    def resolved(self) -> Path:
        return self.data / RESOLVED_NAME

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def searxng(self) -> Path:
        return self.data / "searxng"

    @property
    def run(self) -> Path:
        """PIDs, the per-boot vLLM key and dashboard link (mode 600), download status, caches."""
        return self.data / "run"

    def model_dir(self, repo_id: str) -> Path:
        org, _, name = repo_id.partition("/")
        return self.weights / org / name

    def ensure(self) -> "Paths":
        import os
        for d in (self.data, self.weights, self.logs, self.searxng, self.run):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run, 0o700)
        return self

    def write_secret(self, name: str, text: str) -> Path:
        """A run/ file only this user can read (vLLM key, dashboard link)."""
        import os
        path = self.run / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text + "\n")
        os.chmod(path, 0o600)
        return path


def default_config_path() -> Path:
    """Both entry points read ./perseus.toml from the directory they run in."""
    return Path.cwd() / "perseus.toml"


def pointer_path(config_path: Path) -> Path:
    return config_path.resolve().parent / DATA_POINTER


def record_data_dir(config_path: Path, data_dir: Path) -> Path:
    p = pointer_path(config_path)
    p.write_text(str(data_dir.resolve()) + "\n")
    return p


def find_paths(config_path: Path, override: Path | None = None) -> Paths:
    """The data directory preflight recorded for this config (or --data DIR)."""
    if override is not None:
        return Paths(override.resolve())
    p = pointer_path(config_path)
    try:
        data = Path(p.read_text().strip())
    except OSError:
        raise ConfigError([f"no data directory recorded for {config_path.name} yet: run bash preflight.sh"
                           + (f" --config {config_path}" if config_path.resolve() != default_config_path() else "")])
    if not data.is_dir():
        raise ConfigError([f"the data directory {data} is gone (new instance?): run bash preflight.sh again"])
    return Paths(data)


def load_resolved(paths: Paths) -> dict:
    try:
        return tomllib.loads(paths.resolved.read_text())
    except FileNotFoundError:
        raise ConfigError([f"{paths.resolved} not found: run bash preflight.sh"])
