#!/usr/bin/env python3
"""Create or update your Perseus config files from Config/templates, keeping your values.

    bash Config/configuration.sh            create, migrate or update (safe to re-run)
    bash Config/configuration.sh --check    show what an update would change; write nothing

(configuration.sh finds a Python 3.11+ and runs this file; bash preflight.sh runs it too.)

The files:
    Config/templates/*.toml   committed; change these when Perseus's config changes
    Config/provider.toml      yours: the model and the Hugging Face token
    Config/settings.toml      yours: everything else about serving and behaviour
    Config/tooling.toml       yours: each tool's settings beside its own API key

How an update works, a three-way merge like git's:
    base      the templates as they were the last time this ran (Config/.base/, a snapshot)
    yours     your Config/*.toml
    template  Config/templates/*.toml in this checkout

    for every key in the templates:
      you don't have it yet         -> the template's value                (a new key)
      yours differs from base       -> yours, always                       (models, keys, anything you set)
      yours equals base             -> the template's value                (a default that changed reaches you)
    no base yet (first run)         -> yours, always

The layout and comments come from the templates, so new documentation reaches you too.
Keys can move between files freely: values are matched by [section] key, not by file.
Renamed keys follow MOVES below. Keys the templates no longer have are listed and saved
in the backup, never silently dropped. Every write first copies your previous files to
Config/.backup/<time>/. Secrets are never printed.

Stdlib only (tomllib), so it runs before Perseus's environment exists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATES = HERE / "templates"
FILES = ("provider.toml", "settings.toml", "tooling.toml")   # the order they're written and reported in
BASE = ".base"
BACKUP = ".backup"
LEGACY = "perseus.toml"            # the single file used before the split
POINTER = ".perseus-data"          # preflight's data-directory pointer, kept beside the config

# Renamed or moved keys: old (section, key) -> (new section, new key, value transform), or None for a key
# that was removed with no automatic replacement. Add a line here whenever a key is renamed.
Transform = Callable[[Any], Any]
MOVES: dict[tuple[str, str], tuple[str, str, Transform | None] | None] = {
    ("secrets", "code_runner_api_key"): ("code_runner", "api_key", None),
    ("code_runner", "internet"): ("code_runner", "network", lambda v: "full" if v is True else "none"),
    ("code_runner", "key_file"): None,     # replaced by [code_runner] api_key: paste the key itself
}

PLACEHOLDER_RE = re.compile(r"^(org/model-name|.*\.\.\.)$")   # template values that mean "fill me in"
SECRET_RE = re.compile(r"(key|token|secret|password)", re.I)

KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_-]+)(?P<eq>\s*=\s*)(?P<rest>.*)$")
SECTION_LINE = re.compile(r"^\s*\[\s*(?P<name>[A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")

Ref = tuple[str, str]   # (section, key)


class UpdateError(Exception):
    pass


# ----------------------------------------------------------------------------- TOML helpers

def toml_value(v: Any) -> str:
    """One TOML value on one line (the templates only use single-line values)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)    # JSON string escapes are valid TOML basic strings
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }" if v else "{}"
    raise UpdateError(f"can't write a {type(v).__name__} value ({v!r})")


def split_value(rest: str) -> tuple[str, str]:
    """`"a # b"   # note` -> ('"a # b"', '   # note'): the value text and what follows it."""
    cuts = [i for i, c in enumerate(rest) if c == "#"] + [len(rest)]
    for cut in cuts:
        text = rest[:cut].rstrip()
        if not text:
            continue
        try:
            tomllib.loads(f"v = {text}")
        except tomllib.TOMLDecodeError:
            continue                         # that '#' was inside a string; try the next one
        return text, rest[len(text):]
    raise UpdateError(f"not a single-line TOML value: {rest!r}")


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise UpdateError(f"{path}: {e} (fix it by hand; nothing was changed)")


def flatten(doc: dict, name: str) -> dict[Ref, Any]:
    out: dict[Ref, Any] = {}
    for section, keys in doc.items():
        if not isinstance(keys, dict):
            raise UpdateError(f"{name}: {section!r} must be a [section], not a top-level value")
        for key, value in keys.items():
            out[(section, key)] = value
    return out


def shown(ref: Ref, value: Any) -> str:
    return "(hidden)" if ref[0] == "secrets" or SECRET_RE.search(ref[1]) else toml_value(value)


def label(ref: Ref) -> str:
    return f"[{ref[0]}] {ref[1]}"


# ----------------------------------------------------------------------------- templates

@dataclass
class Template:
    name: str
    text: str
    values: dict[Ref, Any]

    @classmethod
    def read(cls, path: Path) -> "Template":
        text = path.read_text()
        values = flatten(read_toml(path), path.name)
        # every key must sit on one `key = value` line so it can be rewritten in place
        found: dict[Ref, Any] = {}
        section = None
        for raw in text.splitlines():
            if (m := SECTION_LINE.match(raw)):
                section = m.group("name")
            elif (m := KEY_LINE.match(raw)) and not raw.lstrip().startswith("#"):
                if section is None:
                    raise UpdateError(f"templates/{path.name}: {m.group('key')} sits outside a [section]")
                value_text, _ = split_value(m.group("rest"))
                found[(section, m.group("key"))] = tomllib.loads(f"v = {value_text}")["v"]
        if found != values:
            missing = sorted(set(values) - set(found))
            raise UpdateError(f"templates/{path.name}: keep every value on one line "
                              f"(couldn't place {', '.join(map(label, missing)) or 'a key'})")
        return cls(path.name, text, values)

    def render(self, choose: Callable[[Ref, Any], Any], yours: dict[Ref, str] | None = None) -> str:
        """The template text with each value replaced by choose(ref, template_value).

        Where your file already has that exact value and comment on one line, your line is kept
        as you wrote it, so an update that changes nothing writes nothing."""
        out, section = [], None
        for raw in self.text.splitlines():
            m = KEY_LINE.match(raw)
            if (s := SECTION_LINE.match(raw)):
                section = s.group("name")
            elif m and not raw.lstrip().startswith("#"):
                ref = (section, m.group("key"))
                tvalue = self.values[ref]
                value = choose(ref, tvalue)
                mine = (yours or {}).get(ref)
                if mine is not None and same_line(mine, value, raw):
                    raw = mine
                elif value != tvalue or type(value) is not type(tvalue):
                    value_text, tail = split_value(m.group("rest"))
                    head = f"{m.group('indent')}{m.group('key')}{m.group('eq')}{toml_value(value)}"
                    if tail.strip():
                        comment_col = len(raw) - len(tail.lstrip())
                        raw = head + " " * max(2, comment_col - len(head)) + tail.lstrip()
                    else:
                        raw = head
            out.append(raw)
        return "\n".join(out) + "\n"


def parse_line(raw: str) -> tuple[Any, str] | None:
    """`key = value  # note` -> (value, "# note"), or None for anything that isn't one such line."""
    m = KEY_LINE.match(raw)
    if not m or raw.lstrip().startswith("#"):
        return None
    try:
        text, tail = split_value(m.group("rest"))
    except UpdateError:
        return None
    return tomllib.loads(f"v = {text}")["v"], tail.strip()


def same_line(mine: str, value: Any, template_line: str) -> bool:
    a, b = parse_line(mine), parse_line(template_line)
    return (a is not None and b is not None and a[0] == value and type(a[0]) is type(value)
            and a[1] == b[1] and KEY_LINE.match(mine).group("key") == KEY_LINE.match(template_line).group("key"))


def key_lines(text: str) -> dict[Ref, str]:
    """Each one-line `key = value` in a file, by [section] key (multi-line values are skipped)."""
    out: dict[Ref, str] = {}
    section = None
    for raw in text.splitlines():
        if (m := SECTION_LINE.match(raw)):
            section = m.group("name")
        elif section and (k := KEY_LINE.match(raw)) and parse_line(raw) is not None:
            out[(section, k.group("key"))] = raw
    return out


def read_templates(folder: Path) -> dict[str, Template]:
    if not folder.is_dir():
        raise UpdateError(f"no templates folder at {folder}")
    templates = {name: Template.read(folder / name) for name in FILES}
    owner: dict[Ref, str] = {}
    for t in templates.values():
        for ref in t.values:
            if ref in owner:
                raise UpdateError(f"{label(ref)} is in both templates/{owner[ref]} and templates/{t.name}")
            owner[ref] = t.name
    return templates


# ----------------------------------------------------------------------------- your values

@dataclass
class Yours:
    values: dict[Ref, Any] = field(default_factory=dict)
    where: dict[Ref, str] = field(default_factory=dict)       # ref -> file it came from
    lines: dict[Ref, str] = field(default_factory=dict)       # ref -> your line, as you wrote it
    legacy: Path | None = None                                # an old perseus.toml being migrated


def read_yours(folder: Path, legacy: Path | None) -> Yours:
    y = Yours()
    live = [folder / n for n in FILES if (folder / n).exists()]
    sources = live if live else ([legacy] if legacy and legacy.is_file() else [])
    if not live and sources:
        y.legacy = legacy
    for path in sources:
        y.lines.update(key_lines(path.read_text()))
        for ref, value in flatten(read_toml(path), path.name).items():
            if ref in y.values and y.values[ref] != value:
                raise UpdateError(f"{label(ref)} is set in both {y.where[ref]} and {path.name} with different "
                                  f"values; delete one, then run this again (nothing was changed)")
            y.values[ref] = value
            y.where[ref] = path.name
    return y


# ----------------------------------------------------------------------------- the merge

@dataclass
class Plan:
    files: dict[str, str]                                    # name -> new text
    changed: dict[str, str] = field(default_factory=dict)    # name -> "created" / "updated"
    added: list[tuple[str, Ref, Any]] = field(default_factory=list)
    defaults: list[tuple[str, Ref, Any, Any]] = field(default_factory=list)
    moved: list[tuple[str, Ref, Ref]] = field(default_factory=list)   # file, old, new
    relocated: list[tuple[Ref, str, str]] = field(default_factory=list)
    dropped: dict[Ref, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    to_fill: list[tuple[str, Ref]] = field(default_factory=list)


def apply_moves(values: dict[Ref, Any], notes: list[str]) -> tuple[dict[Ref, Any], dict[Ref, Ref], dict[Ref, Any]]:
    out = dict(values)
    renamed: dict[Ref, Ref] = {}          # new ref -> old ref
    removed: dict[Ref, Any] = {}
    for old, target in MOVES.items():
        if old not in out:
            continue
        value = out.pop(old)
        if target is None:
            removed[old] = value
            notes.append(f"{label(old)} was removed from Perseus; see the template for its replacement")
            continue
        sec, key, transform = target
        new = (sec, key)
        if new in out:
            removed[old] = value
            notes.append(f"{label(old)} and its replacement {label(new)} were both set; kept {label(new)}")
            continue
        out[new] = transform(value) if transform else value
        renamed[new] = old
    return out, renamed, removed


def plan(folder: Path, templates: dict[str, Template], yours: Yours, base: dict[Ref, Any] | None) -> Plan:
    notes: list[str] = []
    values, renamed, removed = apply_moves(yours.values, notes)
    p = Plan(files={}, notes=notes, dropped=removed)
    used: set[Ref] = set()

    for name, t in templates.items():
        def choose(ref: Ref, tvalue: Any, name=name) -> Any:
            if ref not in values:
                if yours.values or yours.legacy:   # nothing to report on a fresh start
                    p.added.append((name, ref, tvalue))
                return tvalue
            used.add(ref)
            mine = values[ref]
            if ref in renamed:
                p.moved.append((name, renamed[ref], ref))
            elif yours.where.get(ref) not in (None, name) and yours.legacy is None:
                p.relocated.append((ref, yours.where[ref], name))
            if base is not None and ref in base and mine == base[ref] and tvalue != base[ref]:
                p.defaults.append((name, ref, mine, tvalue))
                return tvalue
            return mine

        p.files[name] = t.render(choose, {r: line for r, line in yours.lines.items() if r not in renamed})
        final = tomllib.loads(p.files[name])
        for ref, tvalue in t.values.items():
            if isinstance(tvalue, str) and PLACEHOLDER_RE.match(tvalue) and final[ref[0]][ref[1]] == tvalue:
                p.to_fill.append((name, ref))

    for ref, value in values.items():
        if ref not in used:
            p.dropped[ref] = value

    for name in FILES:
        path = folder / name
        if not path.exists():
            p.changed[name] = "created"
        elif path.read_text() != p.files[name]:
            p.changed[name] = "updated"
    return p


# ----------------------------------------------------------------------------- writing

def write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def backup(folder: Path, p: Plan, yours: Yours) -> Path | None:
    existing = [folder / n for n in FILES if (folder / n).exists()]
    if not existing and not yours.legacy and not p.dropped:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = folder / BACKUP / stamp
    n = 1
    while dest.exists():
        n += 1
        dest = folder / BACKUP / f"{stamp}-{n}"
    private_dir(folder / BACKUP)
    private_dir(dest)
    for path in existing:
        shutil.copy2(path, dest / path.name)
        os.chmod(dest / path.name, 0o600)
    if p.dropped:
        by_section: dict[str, dict[str, Any]] = {}
        for (sec, key), value in p.dropped.items():
            by_section.setdefault(sec, {})[key] = value
        lines = ["# Keys from your config that the current templates no longer use.",
                 "# Kept here in case you need a value; Perseus doesn't read this file.", ""]
        for sec, keys in by_section.items():
            lines += [f"[{sec}]"] + [f"{k} = {toml_value(v)}" for k, v in keys.items()] + [""]
        write_private(dest / "unused-keys.toml", "\n".join(lines))
    return dest


def write(folder: Path, templates_dir: Path, p: Plan, yours: Yours) -> Path | None:
    private_dir(folder)
    saved = backup(folder, p, yours) if (p.changed or yours.legacy or p.dropped) else None
    for name in FILES:
        if name in p.changed:
            write_private(folder / name, p.files[name])
        elif (folder / name).exists() and (folder / name).stat().st_mode & 0o077:
            os.chmod(folder / name, 0o600)
    base = private_dir(folder / BASE)
    for name in FILES:
        shutil.copyfile(templates_dir / name, base / name)
    if yours.legacy is not None:
        # the old file now lives only in the backup, so nobody edits a file Perseus no longer reads
        if saved is not None:
            shutil.move(str(yours.legacy), saved / yours.legacy.name)
        old_pointer = yours.legacy.parent / POINTER
        if old_pointer.is_file() and not (folder / POINTER).exists():
            shutil.move(str(old_pointer), folder / POINTER)
    return saved


def read_base(folder: Path) -> dict[Ref, Any] | None:
    base = folder / BASE
    if not base.is_dir():
        return None
    out: dict[Ref, Any] = {}
    for name in FILES:
        if (base / name).is_file():
            try:
                out.update(flatten(tomllib.loads((base / name).read_text()), name))
            except (tomllib.TOMLDecodeError, UpdateError):
                return None                  # an unreadable snapshot: fall back to keeping all your values
    return out


# ----------------------------------------------------------------------------- report

def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve())) or "."
    except ValueError:
        return str(path)


def report(folder: Path, p: Plan, yours: Yours, saved: Path | None, check: bool, quiet: bool) -> None:
    say = print
    if quiet and not (p.changed or p.dropped or p.to_fill):
        return
    verb = "would be" if check else ""
    say(f"Perseus config ({rel(folder)}/)" + (" — dry run, nothing written" if check else ""))
    for name in FILES:
        state = p.changed.get(name, "unchanged")
        if state == "created" and yours.legacy:
            state = f"created from {yours.legacy.name}"
        say(f"  {name:<15} {(verb + ' ' + state).strip() if name in p.changed else state}")
    for name, ref, value in p.added:
        say(f"  + {label(ref)} = {shown(ref, value)}  (new key, in {name})")
    for name, ref, old, new in p.defaults:
        say(f"  ~ {label(ref)}: {shown(ref, old)} -> {shown(ref, new)}  (default changed; you hadn't set it)")
    for name, old, new in p.moved:
        say(f"  > {label(old)} -> {label(new)} in {name}  (renamed; your value kept)")
    for ref, src, dst in p.relocated:
        say(f"  > {label(ref)} moved from {src} to {dst}  (your value kept)")
    for ref, value in p.dropped.items():
        where = f"saved in {rel(saved)}/unused-keys.toml" if saved else "would be saved in the backup"
        say(f"  - {label(ref)} = {shown(ref, value)}  (no longer used; {where})")
    for note in p.notes:
        say(f"  ! {note}")
    if saved:
        say(f"  backup of your previous files: {rel(saved)}/")
    if yours.legacy and not check:
        say(f"  {yours.legacy.name} moved into the backup; Perseus now reads {rel(folder)}/ only")
    if p.to_fill:
        say("  still to fill in: " + "; ".join(f"{label(ref)} in {rel(folder / name)}" for name, ref in p.to_fill))


# ----------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bash Config/configuration.sh",
                                 description="Create or update Perseus's config files from Config/templates, "
                                             "keeping your model, keys and settings.")
    ap.add_argument("--dir", type=Path, default=HERE, help="folder holding your config files (default: Config/)")
    ap.add_argument("--from", dest="legacy", type=Path, default=None,
                    help=f"an old single-file {LEGACY} to migrate (default: ./{LEGACY} in the checkout, if present)")
    ap.add_argument("--check", action="store_true", help="show what would change; write nothing (exit 1 if anything would)")
    ap.add_argument("--quiet", action="store_true", help="print only when something changes or needs filling in")
    a = ap.parse_args(argv)

    folder = a.dir.expanduser().absolute()
    # the checkout's old perseus.toml is picked up only for the checkout's own Config/ folder
    default_legacy = ROOT / LEGACY if folder.resolve() == HERE.resolve() else None
    legacy = (a.legacy or default_legacy or folder / LEGACY).expanduser().absolute()
    if a.legacy and not legacy.is_file():
        print(f"  ✗ {legacy} not found", file=sys.stderr)
        return 2
    try:
        templates = read_templates(TEMPLATES)
        yours = read_yours(folder, legacy)
        if a.legacy and yours.legacy is None:
            raise UpdateError(f"{rel(folder)}/ already has config files; --from only migrates into an empty folder")
        p = plan(folder, templates, yours, read_base(folder))
        if legacy.is_file() and yours.legacy is None and not a.legacy:
            p.notes.append(f"{rel(legacy)} is still here but isn't read anymore; delete it once you've checked "
                           f"{rel(folder)}/")
        saved = None if a.check else write(folder, TEMPLATES, p, yours)
    except UpdateError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return 2
    report(folder, p, yours, saved, a.check, a.quiet)
    return 1 if a.check and (p.changed or yours.legacy) else 0


if __name__ == "__main__":
    sys.exit(main())
