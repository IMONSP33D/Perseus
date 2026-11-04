"""Addons: self-contained folders that plug extra machinery into Perseus without touching its core.

An addon is everything a tool needs that a tool file isn't allowed to hold: a service it
talks to, the key for that service, and the process that owns both. Drop a folder in here,
restart Perseus, and it is found, configured, started, offered to the model and shown on the
Agent Tools page. No file outside the folder changes. The folder's name is the addon's name,
and that one name is also:

    its [section] in Config/tooling.toml      (the settings and the api_key the user edits)
    its capability in the tool registry       (tools say `requires="<name>"`)
    its attribute on the tool context         (tools reach its service as `ctx.<name>`)
    its log, logs/<name>.log                  (when it runs a process)

What a folder holds:

    addon.py          REQUIRED  `ADDON = Addon(...)`: the manifest below
    tools/*.py        REQUIRED  the tool files the model can call (same rules as perseus/tools/)
    tooling.toml      REQUIRED  the addon's [section] as it appears in Config/tooling.toml,
                                with every key on one commented line; `bash Config/configuration.sh`
                                merges it into the user's file
    dependencies.toml optional  Python packages the addon's own process needs, exactly pinned;
                                `bash preflight.sh` installs them (setup/addon_deps.py), and an addon
                                whose packages are missing is reported off at boot, not broken
    <anything>.py     optional  the client, the process, the provider API: whatever the addon needs
    tests/test_*.py   optional  picked up by `python -m unittest discover -s tests -t .`
    README.md         optional  how to set the addon up; linked from the main README's addon table

A folder whose name starts with "_" is a template or an example and is skipped (see _example/).
An addon whose addon.py fails to import, or whose manifest is incomplete, is skipped with its
reason recorded in `errors()`, printed at boot and shown on the Agent Tools page: a broken
addon never takes Perseus down.

The boot sequence for each addon (perseus.launcher, the same loop for all of them):

    plan = ADDON.plan(section_from_preflight, config_path, os.environ)   # start it, or say why not
    port, token = a free loopback port, a per-boot secret                 # if plan.start
    process = ADDON.process(plan, port, token, os.environ)               # the supervised process, if any
    client = ADDON.client(port, token)                                   # what tools get as ctx.<name>
    ok, detail = ADDON.connect(client, registry)                         # after the process is healthy:
                                                                         # registry.provide(name, ...) or not

The socket guard lets the engine reach only its own loopback ports, the addon's included; the
gateway refuses loopback, so fetch_url can never reach an addon; and every secret an addon's
process holds is scrubbed from anything the model reads. None of that is the addon's job.
"""

from __future__ import annotations

import importlib
import inspect
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

ADDONS_DIR = Path(__file__).resolve().parent
PACKAGE = "perseus.addons"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
TEMPLATE_FILE = "tooling.toml"
DEPS_FILE = "dependencies.toml"
PIN_RE = re.compile(r"^[0-9]+(\.[0-9]+)*([a-zA-Z0-9.+!-]*)$")     # an exact version, never an operator or a range
DIST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")   # a distribution name (PEP 508)
FORMERLY_RE = re.compile(r"^\s*#\s*formerly\s*:\s*(.+?)\s*$", re.M)   # in tooling.toml: old names of the section


@dataclass
class Plan:
    """What plan() decides: whether to start the addon, and the one-line reason shown at boot."""
    start: bool
    reason: str                                   # "starting (e2b)" or "off (no api_key set)"
    env: dict[str, str] = field(default_factory=dict)   # extra environment for the addon's process only


@dataclass
class Process:
    """A process the launcher supervises for the addon: restarted when it dies, stopped with Perseus."""
    command: list[str]
    env: dict[str, str]                           # its whole environment: build it, don't inherit os.environ
    ready_timeout_s: float = 60.0                 # how long the launcher waits for healthy() at boot
    healthy: Callable[[], bool] | None = None     # None: the client's healthy() is used


@dataclass
class Addon:
    """The manifest an addon.py exports as ADDON. Every field without a default is required; `name` must
    equal the folder name. See _example/addon.py for a commented copy."""
    name: str
    label: str              # how boot lines and the dashboard name it, e.g. "Windows VM"
    summary: str            # one line for the Agent Tools page: what this addon gives the model
    agent_use: str          # what the MODEL is told about using this addon's tools: when to reach for them,
                            # what they can and can't do, how results come back. Appended to every tool's
                            # description while the addon is up, so write it to the model, not the user.
    config: dict[str, Any]  # the [name] section's keys: {key: perseus.keys.Key(...)}; secret=True keys are
                            # read only by the addon's own process, never by perseus.py or the model
    plan: Callable[[dict, str, MutableMapping[str, str]], Plan]
    client: Callable[[int, str], Any]                                    # (port, token) -> ctx.<name>
    connect: Callable[[Any, Any], tuple[bool, str]]                      # (client, registry) -> (ok, detail)
    process: Callable[[Plan, int, str, Mapping[str, str]], Process | None] | None = None
    former_names: tuple[str, ...] = ()   # earlier names of the [section]; a config still using one gets a
                                         # clear error naming the new section (configuration.sh renames it)
    renamed_keys: dict[str, str] = field(default_factory=dict)   # old key in this section -> what replaced it
    folder: Path = field(default=None, repr=False)   # set by discover()
    package: str = field(default=PACKAGE, repr=False)   # the Python package the folder sits in; set by discover()

    @property
    def tools_dir(self) -> Path:
        return self.folder / "tools"

    @property
    def tools_package(self) -> str:
        return f"{self.package}.{self.name}.tools"

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(k for k, spec in self.config.items() if getattr(spec, "secret", False))

    def template(self) -> str:
        """The addon's tooling.toml fragment, as merged into Config/tooling.toml."""
        return (self.folder / TEMPLATE_FILE).read_text()

    def dependencies(self) -> dict[str, str]:
        """{distribution: exact version} from dependencies.toml, or {} without one."""
        return read_dependencies(self.folder)


_ADDONS: dict[Path, list[Addon]] = {}
_ERRORS: dict[Path, dict[str, str]] = {}


def discover(root: Path = ADDONS_DIR, package: str = PACKAGE, refresh: bool = False) -> list[Addon]:
    """Every valid addon under `root`, sorted by name. Cached per root; `refresh=True` scans again.
    Problems go to errors(root) instead of raising, one line per folder."""
    if root in _ADDONS and not refresh:
        return _ADDONS[root]
    found, errors = [], {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        if not (folder / "addon.py").is_file():
            continue                      # a plain package (common helpers, say), not an addon
        try:
            found.append(_load(folder, package))
        except Exception as e:  # a broken addon never takes Perseus down
            errors[folder.name] = _reason(e)
    _ADDONS[root], _ERRORS[root] = found, errors
    return found


def errors(root: Path = ADDONS_DIR) -> dict[str, str]:
    """{folder name: why it was skipped} for the last discover() of `root`."""
    if root not in _ERRORS:
        discover(root)
    return _ERRORS[root]


def by_name(name: str, root: Path = ADDONS_DIR) -> Addon | None:
    return next((a for a in discover(root) if a.name == name), None)


def _load(folder: Path, package: str) -> Addon:
    name = folder.name
    if not NAME_RE.match(name):
        raise ValueError("folder name must be lower-case letters, digits and underscores, 2-40 characters")
    mod = importlib.import_module(f"{package}.{name}.addon")
    a = getattr(mod, "ADDON", None)
    if not isinstance(a, Addon):
        raise TypeError("addon.py must export ADDON = Addon(...)")
    problems = check(a, folder)
    if problems:
        raise ValueError("; ".join(problems))
    a.folder, a.package = folder, package
    return a


def check(a: Addon, folder: Path, expect_name: str | None = None) -> list[str]:
    """Everything a manifest must get right, as one line per problem (empty when it's fine).
    `expect_name` lets a template folder (_example) be checked as if it had been renamed."""
    out = []
    if a.name != (expect_name or folder.name):
        out.append(f"name {a.name!r} must equal the folder name {folder.name!r}")
    for f in ("label", "summary", "agent_use"):
        v = getattr(a, f, None)
        if not isinstance(v, str) or not v.strip():
            out.append(f"{f} is required: a non-empty string")
    if isinstance(a.agent_use, str) and 0 < len(a.agent_use.strip()) < 40:
        out.append("agent_use must actually tell the model how to use the addon (40+ characters)")
    if not isinstance(a.config, dict):
        out.append("config must be a dict of {key: Key(...)}")
    else:
        for k, spec in a.config.items():
            if not re.match(r"^[a-z][a-z0-9_]*$", str(k)) or not hasattr(spec, "validate"):
                out.append(f"config[{k!r}] must be a perseus.keys.Key")
    for f in ("plan", "client", "connect"):
        if not callable(getattr(a, f, None)):
            out.append(f"{f} must be a function")
    if a.process is not None and not callable(a.process):
        out.append("process must be a function or None")
    if not (folder / "tools").is_dir() or not list((folder / "tools").glob("[!_]*.py")):
        out.append("tools/ must hold at least one tool file")
    if not (folder / TEMPLATE_FILE).is_file():
        out.append(f"{TEMPLATE_FILE} (the [section] template) is missing")
    else:
        text = (folder / TEMPLATE_FILE).read_text()
        if f"[{a.name}]" not in text:
            out.append(f"{TEMPLATE_FILE} must start its section with [{a.name}]")
        formerly = tuple(x.strip() for m in FORMERLY_RE.findall(text) for x in m.split(",") if x.strip())
        if formerly != tuple(a.former_names):
            out.append(f"{TEMPLATE_FILE}'s '# formerly:' line must list former_names {list(a.former_names)}")
    for f in ("name", "label", "summary"):
        if "\n" in str(getattr(a, f, "")):
            out.append(f"{f} must be one line")
    try:
        read_dependencies(folder)
    except ValueError as e:
        out.append(str(e))
    return out


def read_dependencies(folder: Path) -> dict[str, str]:
    """The [packages] table of a folder's dependencies.toml: {distribution: exact version}.
    Raises ValueError for anything but exact pins, so a loose range never reaches an install."""
    f = folder / DEPS_FILE
    if not f.is_file():
        return {}
    import tomllib
    try:
        doc = tomllib.loads(f.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{DEPS_FILE}: {e}")
    table = doc.get("packages")
    if not isinstance(table, dict) or set(doc) - {"packages"}:
        raise ValueError(f"{DEPS_FILE} must hold one [packages] table of name = \"version\" lines")
    out = {}
    for name, version in table.items():
        if not DIST_RE.match(str(name)):
            raise ValueError(f"{DEPS_FILE}: {name!r} is not a package name")
        if not isinstance(version, str) or not PIN_RE.match(version):
            raise ValueError(f"{DEPS_FILE}: {name} must be pinned to one exact version, e.g. \"2.62.0\" "
                             f"(got {version!r}); Perseus installs exact pins only")
        out[str(name)] = version
    return out


def missing_packages(deps: dict[str, str]) -> list[str]:
    """Which of these pins aren't installed at that exact version, as 'name (want X, have Y)' lines."""
    from importlib import metadata
    out = []
    for name, want in deps.items():
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            out.append(f"{name} (want {want}, not installed)")
            continue
        if have != want:
            out.append(f"{name} (want {want}, have {have})")
    return out


def _reason(e: Exception) -> str:
    if isinstance(e, (ValueError, TypeError)) and not isinstance(e, ImportError):
        return str(e)
    tb = traceback.extract_tb(e.__traceback__)
    last = next((fr for fr in reversed(tb) if "/addons/" in fr.filename), tb[-1] if tb else None)
    where = f" ({Path(last.filename).name}:{last.lineno})" if last else ""
    return f"{type(e).__name__}: {e}{where}"


def signature_ok(fn: Callable, n: int) -> bool:
    """For tests: the callable takes `n` positional arguments."""
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return True
    return len(params) == n
