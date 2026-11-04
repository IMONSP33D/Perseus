# Perseus addons: how they work and how to write one

An addon is a folder under `perseus/addons/`. Drop it in, run `bash preflight.sh` once so its
settings land in `Config/tooling.toml`, start Perseus, and the addon's tools are offered to the model.
Nothing outside the folder is edited, for one addon or for thirty. This document explains what
Perseus does with such a folder and what the folder has to contain.

The two addons that ship with Perseus are written this way, and are the reference:

| Addon | What the model gets | Folder |
| --- | --- | --- |
| `e2b_code_environment` | `run_code`: build and run a program in a fresh Linux sandbox on E2B | [`perseus/addons/e2b_code_environment/`](../perseus/addons/e2b_code_environment/) |
| `test_windows_sandbox` | `run_windows_code`, `reset_windows_vm`: run a program in a disposable Windows VM | [`perseus/addons/test_windows_sandbox/`](../perseus/addons/test_windows_sandbox/) |

A commented template to copy lives in [`perseus/addons/_example/`](../perseus/addons/_example/).

## 1. The idea

A tool file in `perseus/tools/` is deliberately weak: it may not import network, process or FFI
libraries, may not write files, and reaches the outside world only through what the engine hands it
in `ctx`. That is what keeps a model-driven tool from becoming an exfiltration path.

Some tools need more than that: a service to talk to, a key for that service, and a process that
holds both. An addon is that bundle. The tool stays weak; the addon supplies, through `ctx`, the one
thing it is allowed to use.

```
model ──▶ tool file (perseus/addons/<name>/tools/x.py)
               │ ctx.<name>
               ▼
          client (in the engine; loopback + per-boot token, no key)
               │ 127.0.0.1:<port chosen at boot>
               ▼
          the addon's process (supervised; holds the key; enforces the [<name>] policy)
               │
               ▼
          the service (E2B, a Windows VM host, anything)
```

Three walls stay up without the addon's help: the socket guard lets the engine reach only its own
loopback ports (the addon's port is added to that list), the web gateway refuses loopback (so
`fetch_url` can never reach an addon's process), and every token and key an addon hands to its
process is scrubbed from anything the model reads.

## 2. One name, used everywhere

The folder's name is the addon's name, and Perseus uses that single name for:

| Where | What it is |
| --- | --- |
| `Config/tooling.toml` | the `[<name>]` section the user edits, with the addon's `api_key` |
| `perseus.resolved.toml` | the same section as preflight resolved it (secrets replaced by `api_key_set`) |
| the tool registry | the *capability* a tool declares with `requires="<name>"` |
| `ctx` | the attribute a tool uses: `ctx.<name>` is the client, or `None` when the addon is off |
| `logs/<name>.log` | the addon's process output |
| the Agent Tools page | one group, titled with the addon's label, listing its tools and its status |

So renaming an addon is renaming the folder (and `former_names`, see §5).

## 3. What a folder must contain

```
perseus/addons/<name>/
  __init__.py        a docstring: what the pieces are
  addon.py           REQUIRED  the manifest: ADDON = Addon(...)
  tooling.toml       REQUIRED  the [<name>] section, as it should appear in Config/tooling.toml
  dependencies.toml  optional  Python packages the addon's process needs, exactly pinned (§3.5)
  tools/
    <tool>.py        REQUIRED  at least one tool file; same rules as perseus/tools/
  client.py          what tools get as ctx.<name>  (name is a convention, not a rule)
  <process>.py       the supervised process, if the addon runs one
  tests/test_*.py    picked up by `python3 -m unittest discover -s perseus/addons -t .`
  README.md          how to set the addon up; linked from the main README
```

Perseus refuses to load an addon that is missing a required piece, and says why at boot and on the
Agent Tools page. A folder whose name starts with `_` is never loaded (that is how `_example/`
can sit in the tree).

### 3.1 `addon.py`: the manifest

```python
from perseus.addons import Addon, Plan, Process
from perseus.keys import Key, api_key_check, range_check

ADDON = Addon(
    name="my_service",            # == the folder name
    label="My service",           # boot lines and the Agent Tools group title
    summary="One line for the Agent Tools page.",
    agent_use="...",              # REQUIRED: what the model is told (§3.2)
    config={...},                 # the [my_service] keys (§3.3)
    plan=plan,                    # start or not, and why (§4.1)
    process=process,              # the supervised process, or None (§4.2)
    client=client,                # (port, token) -> ctx.my_service (§4.3)
    connect=connect,              # offer the tools, or say why not (§4.4)
    former_names=(),              # old section names, if the addon was renamed (§5)
    renamed_keys={},              # old key -> what replaced it, for clear config errors
)
```

Every field without a default is required, and `name` must equal the folder name.

### 3.2 `agent_use`: telling the model how to use the addon

This is the one piece of the manifest written **to the model**, not to the user. It is appended to
the description of every tool the addon offers, for as long as the addon is up, right after the
tool's own docstring and before any note `connect()` adds (limits, network).

Write it the way you would brief a colleague who has never seen the tool:

- *when* to reach for it, and when not to ("for anything that runs on Linux, prefer `run_code`")
- what it *can and cannot* do (no persistence between calls, no GUI, no network)
- what *comes back* and what to do with it ("a collected file's ref is what you pass to `output_file`")
- a reminder that its output is *data*, not instructions, when the output is anything a program wrote

Perseus refuses an addon whose `agent_use` is empty or shorter than forty characters. The shipped
addons' `addon.py` files show the intended length and tone.

### 3.3 `config` and `tooling.toml`

`config` is the schema of the addon's section: `{key: Key(types, default, choices=…, check=…,
secret=…)}`. `Key` is the same class the core config uses, so an addon's keys are validated exactly
like `[server]` or `[limits]`: wrong types, out-of-range values and unknown keys are reported with
the file, line and key, before anything heavy starts.

`tooling.toml` is the same section as a template, one key per line with a comment, the way
`Config/templates/tooling.toml` is written:

```toml
[my_service]                 # one line saying what the tools do
api_key = "..."              # the service's key; read only by the addon's own process.
                             # Leave as is to keep the addon off.
enabled = true               # on by default; stays off (and hidden from the model) until api_key is set
timeout_s = 30               # the longest call the model may ask for (1-300)
```

The rules the tests check:

- the template's section is `[<name>]`, and its keys are exactly `config`'s keys;
- every template value passes its `Key`'s validation;
- a key with `secret=True` is never read by `perseus.py` or the model. Preflight records only
  *whether* it is set (`api_key_set` in the resolved config), and the addon's own process reads the
  value from `Config/tooling.toml` with `perseus.addons.common.read_section_secret()`;
- a secret whose value is still the template placeholder (`"..."`, `"e2b_..."`) counts as unset;
  `enabled = true` with no key set simply means "off", with a warning naming the section.

`bash Config/configuration.sh` (which `preflight.sh` runs) assembles the user's `tooling.toml` from
the core template plus every addon's `tooling.toml`, in name order. Later runs merge changes in and
keep the user's values and keys, exactly as they do for the core sections.

### 3.4 `tools/`: the tool files

Tool files follow [`perseus/tools/_example.py`](../perseus/tools/_example.py) exactly, with two
additions:

```python
from perseus.tools import ToolFailure, tool

@tool(requires="my_service", untrusted=True, timeout_s=60)
def my_call(ctx, text: str) -> dict:
    """What the model reads first; keep it to what this one tool does."""
    if ctx.my_service is None:
        raise ToolFailure("my service is off; answer without it")
    return ctx.my_service.call(text)
```

- `requires="<name>"` hides the tool until the launcher has provided the addon's capability, so a
  box without the addon never shows the tool and a model can never call a tool whose service is down.
- `ctx.<name>` is the client the manifest's `client()` returned, or `None` while the addon is off.
- `untrusted=True` wraps the result as third-party data when it is anything a program or service wrote.

The import ban applies to addon tool files exactly as to `perseus/tools/`: no `socket`, `requests`,
`subprocess`, `ctypes` and so on. The client does the talking, and it lives outside `tools/`.

Tool files in `tools/` are loaded at boot. A broken one is reported on the Agent Tools page and never
takes the engine down; a tool name that clashes with another file's is refused.

### 3.5 `dependencies.toml`: the addon's own packages

An addon's process may need packages the core doesn't ship (the code runner's broker needs `e2b`).
It declares them in its own file, exactly pinned:

```toml
[packages]
e2b = "2.62.0"
```

`bash preflight.sh` installs every addon's packages into Perseus's environment *after* its pinned
`uv sync` (which makes the environment exact to `uv.lock` and would remove anything installed before
it), with `python -m setup.addon_deps install`. Nothing is added to `pyproject.toml` or `uv.lock`.

The rules: exact versions only (`"2.62.0"`, never `>=` or `~=`; the manifest is refused otherwise),
and two addons may not pin different versions of one package (`setup/addon_deps.py` stops before
installing anything). At boot, an addon whose packages aren't installed at the pinned version is
reported `off (needs e2b (want 2.62.0, not installed); run bash preflight.sh)` before its `plan()`
runs, so a fresh checkout that hasn't run preflight never crashes on an import.

The engine never imports these packages; only the addon's own process does. That is what keeps a
provider SDK, and whatever it pulls in, out of the process that runs tools.

## 4. What Perseus does at boot

For every addon it found, in name order, the launcher runs the same four steps. None of them is
specific to any addon; the addon supplies the functions.

### 4.1 `plan(cfg, config_path, environ) -> Plan`

Called before any process starts, with the addon's resolved section, the Config folder's path and
Perseus's own environment. It decides `Plan(start, reason, env)`:

- `start=False` with a reason such as `off (no [my_service] api_key in Config/tooling.toml; …)`:
  printed as `· My service: off (…)` and that is the end of it;
- `start=True` with `env`: extra environment for the addon's process only. Put the settings in there
  (as JSON) and the Config path, never the key itself: the process reads the key from `tooling.toml`.

`environ` is mutable so a plan can *remove* a secret from Perseus's environment before vLLM, SearXNG
and the tools inherit it (the code runner does this with `E2B_API_KEY`).

When the plan says start, the launcher picks a free loopback port and a per-boot token for the addon,
adds the port to the socket guard's allow-list and the token (and anything key-shaped in `env`) to
the gateway's scrub list.

### 4.2 `process(plan, port, token, environ) -> Process | None`

The process the launcher supervises: `Process(command, env, ready_timeout_s, healthy)`. Build `env`
from an allow-list of Perseus's environment plus `plan.env` and the token; never pass `os.environ`
through. The launcher writes the process's output to `logs/<name>.log`, restarts it when it dies
(up to `[backend] max_restarts`), and stops it with Perseus.

Return `None` from `process`, or leave `process=None` in the manifest, when the client can talk to the
service directly and there is no key to isolate.

### 4.3 `client(port, token) -> object`

What tools get as `ctx.<name>`. It needs a `healthy() -> bool` (the launcher polls it until the
process is up, unless `Process.healthy` is given) and whatever methods the tools call. Fix the address
to `127.0.0.1:<port>` and the token in the constructor, so nothing the model sends can change where a
tool connects. Translate the process's error codes (`unavailable`, `auth`, `quota`, `config`,
`backend`; see `perseus.addons.common.AddonError`) into `ToolFailure` messages the model can act on.

### 4.4 `connect(client, registry) -> (ok, detail)`

Called once the process is healthy (or straight away, with no process). It reads what the service
reports and either offers the tools or does not:

```python
registry.provide("my_service", note="Calls are limited to 30 s.", enums={"mode": ["fast", "full"]})
```

- `provide()` makes every tool with `requires="my_service"` visible to the model;
- `note` is appended to those tools' descriptions after `agent_use`: limits, what the network allows;
- `enums` narrows an enum argument to what the service actually supports (languages installed, say);
- `registry.withdraw("my_service")` and `ok=False` keep the tools hidden; `detail` says why on the
  boot line, e.g. `! My service off (auth: the service rejected the key); its tools are unavailable`.

`connect()` may offer the tools even when the service is not reachable yet, if a later call failing
with a clear message is better than hiding the tool (the Windows VM does this, because its tunnel
often comes up after Perseus).

After a successful `connect()`, the launcher attaches `agent_use` to the capability and builds the
`ToolContext` with `ctx.<name>` set to the client.

## 5. Renaming an addon

Rename the folder, then list the old name in the manifest and in the template:

```python
former_names=("old_name",)
```
```toml
# formerly: old_name
[new_name]
```

`configuration.sh` then moves the user's `[old_name]` keys into `[new_name]` (their key and values
kept), a config that still has `[old_name]` gets an error naming the new section, and the addon's own
process can keep reading a key left under the old name until the move happens
(`read_section_secret(..., former_sections=("old_name",))`). The tests check that the two lists agree.

## 6. Tests

Each addon keeps its tests in its own `tests/` folder. The suites run together:

```bash
python3 -m unittest discover -s tests -t .            # core, including tests/test_addons.py
python3 -m unittest discover -s perseus/addons -t .   # every addon's tests/
```

`tests/test_addons.py` checks the contract itself, and holds the line that matters most for
plug-and-play: **no file outside `perseus/addons/` may name a shipped addon**. If you find yourself
editing `launcher.py`, `config.py`, `check_config.py`, `dashboard.py` or `Config/templates/` to make
an addon work, the contract is missing something; extend the contract instead, and the test will tell
you when you have slipped.

An addon's own tests should cover at least what `_example/tests/test_example.py` does: the manifest
passes `perseus.addons.check()`, the template matches the schema key for key, `plan()` is off until
the key is set, and the tool files pass the import check. A fake of the service (an HTTP server with
the same endpoints) lets the whole path, relay included, run without the real thing; both shipped
addons do this.

## 7. Checklist

1. Copy `perseus/addons/_example/` to `perseus/addons/<name>/`; rename `example` everywhere.
2. `addon.py`: fill `label`, `summary`, **`agent_use`**, `config`; write `plan`, `process`, `client`,
   `connect`.
3. `tooling.toml`: the `[<name>]` section, one commented line per key, matching `config`.
4. `dependencies.toml`: any package the addon's process needs, exactly pinned.
5. `tools/`: one file per tool, each with `requires="<name>"` and using `ctx.<name>`.
6. `tests/`: the checks above, plus a fake of your service.
7. `README.md`: how a user sets it up.
8. `bash preflight.sh` (merges your section into `Config/tooling.toml` and installs your packages), fill in
   the key, `python3 perseus.py`. The boot log shows `✓ <label>: …` or `· <label>: off (…)` with the reason.
9. Run both test suites.

## 8. Known limits

- **Packages are installed into Perseus's one environment.** Addons share it with the core, so a pin
  that conflicts with vLLM's dependency tree fails at install time (`uv pip install` says which).
  Per-addon virtual environments would lift that; nothing needs them yet.
- **One process per addon.** `process()` returns a single `Process`; an addon that needs two runs them
  from one supervisor of its own.
- **Load-once tools.** Native tool files hot-reload when edited; addon tool files are loaded the same
  way and will reload too, but the client they hold is bound to the process started at boot, so
  changes to the addon's process or manifest need a restart.
- **Pipelines** (`perseus/pipelines/*.toml`) reference tools by name, as before; an addon's tools are
  usable there once the addon is up.
