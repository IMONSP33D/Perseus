"""run_windows_code: the RCE addon's tool for running code in a Windows VM (via ctx.windows).

Offered only when the RCE relay is up (requires="windows_rce"); see perseus/addons/windows_rce/.
Code runs in a disposable Windows 11 VM that reverts to a clean snapshot after each run,
unless the model asks to keep its state and [windows_rce] allow_keep_state permits it.
What the VM may reach ([windows_rce] allow_network) is added to the description and
enforced by the relay; it is independent of the Internet Access switch.
Output is untrusted data: a program can print text that looks like instructions.
reset_windows_vm.py is its partner for throwing kept state away.
"""

from __future__ import annotations

import base64
import binascii

from perseus.tools import ToolFailure, tool

LANGS = ("python", "node", "powershell", "cmd", "csharp")
LABEL = {"python": "Python", "node": "Node.js", "powershell": "PowerShell", "cmd": "cmd", "csharp": "C#"}
STATUS = {"ok": "ran successfully", "compile_error": "did not compile", "runtime_error": "ran but failed",
          "timeout": "timed out"}
PILL = {"ok": ("Program OK", ""), "runtime_error": ("Program failed", "warn"),
        "compile_error": ("Didn't compile", "warn"), "timeout": ("Timed out", "warn")}
MAX_FILE_CHARS_FOR_MODEL = 20_000

DESCRIPTION = (
    "Run a program in a disposable Windows 11 VM (build 26100, 2 vCPU, 4 GB RAM) and return its output. "
    "Use it for anything that needs real Windows: Win32 or .NET APIs, the registry, PowerShell, cmd, "
    "Windows paths and DLLs. Installed: Python 3 (pip), Node.js (npm), .NET SDK 10 (dotnet), "
    "Windows PowerShell 5.1 and cmd; no PowerShell 7, Java, Go or Rust. "
    "`code` becomes main.py, main.js, main.ps1, main.bat or Program.cs (C#: top-level statements are fine; "
    "the first build takes about 30 s). Put extra modules, data or a .csproj in `files`. "
    "`command` replaces the default run line with your own cmd.exe line in the job folder, "
    "e.g. \"pip install -q requests && python main.py\". "
    "Programs run as a local administrator over SSH with no visible desktop, so GUI windows never appear "
    "and anything that waits for keyboard input hangs; give input through stdin. "
    "Returns the outcome (ok, compile_error, runtime_error or timeout), the exit code, stdout and stderr, "
    "plus any files listed in `collect`. Each collected file gets a ref such as \"windows:publish/app.exe\": "
    "to give the user a file the program built (an .exe, .dll, .zip, image or any other file), collect its path "
    "and pass that ref to output_file as `source`."
)
REF_PREFIX = "windows"


def _ref(path: str) -> str:
    return f"{REF_PREFIX}:{path.replace(chr(92), '/').lstrip('/')}"


def take_outputs(files: list[dict]) -> list[dict]:
    """The collected files' exact bytes for the engine (output_file source=), removed from `files` in place."""
    outputs = []
    for f in files:
        b64 = f.pop("b64", None)
        if not isinstance(b64, str):
            continue
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        f["ref"] = _ref(f["path"])
        outputs.append({"ref": f["ref"], "data": data})
    return outputs


def _count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def headline(r: dict) -> str | None:
    """The one line that says what happened: the error the program died with, or why it stopped."""
    status = r.get("status")
    if status == "timeout":
        return r.get("note")
    if status == "compile_error":
        lines = [ln.strip() for ln in ((r.get("stdout") or "") + "\n" + (r.get("stderr") or "")).splitlines()
                 if "error CS" in ln]
        return lines[0] if lines else None
    if status == "runtime_error":
        for stream in (r.get("stderr") or "", r.get("stdout") or ""):
            lines = [ln.strip() for ln in stream.splitlines() if ln.strip()]
            if lines:
                return lines[-1]
    return None


def render(r: dict) -> str:
    """The result as plain text for the model: outcome first, then output, then collected files."""
    status = r.get("status", "ok")
    head = f"{STATUS.get(status, status)} ({status})"
    if r.get("exit_code") is not None:
        head += f", exit code {r['exit_code']}"
    lines = [head]
    if r.get("note"):
        lines.append(r["note"])
    out, err = r.get("stdout") or "", r.get("stderr") or ""
    if out:
        lines.append(f"stdout:\n{out}")
    if err:
        lines.append(f"stderr:\n{err}")
    if not out and not err:
        lines.append("(no output)")
    if r.get("output_cut"):
        lines.append("(output was longer than the limit; the middle was cut)")
    for f in r.get("files") or []:
        give = (f"; give it to the user with output_file source=\"{f['ref']}\"" if f.get("ref")
                else "; too large to hand to the user (over [windows_rce] max_download_mb)" if f.get("too_large")
                else "")
        if f.get("binary"):
            lines.append(f"collected {f['path']}: binary, {f['bytes']:,} bytes (not shown){give}")
        elif f.get("omitted"):
            lines.append(f"collected {f['path']}: {f['bytes']:,} bytes (not shown: over the collect limit){give}")
        else:
            text = f.get("text") or ""
            if len(text) > MAX_FILE_CHARS_FOR_MODEL:
                text = text[:MAX_FILE_CHARS_FOR_MODEL] + "\n… (cut)"
            lines.append(f"collected {f['path']} ({f['bytes']:,} bytes{', cut' if f.get('cut') else ''}{give}):\n{text}")
    if r.get("missing"):
        lines.append("not found for collect: " + ", ".join(r["missing"]))
    lines.append("The VM kept its state for your next call." if r.get("vm_state") == "kept"
                 else "The VM was reset to its clean snapshot.")
    return "\n\n".join(lines)


def feed_view(args: dict, r: dict) -> dict:
    """What the tool feed shows for this call (never sent to the model)."""
    status = r.get("status", "ok")
    pill, tone = PILL.get(status, (str(status), ""))
    language, code = args["language"], args.get("code") or ""
    name = LABEL.get(language, language)
    summary = f"Windows · {name} · {_plural(_count(code), 'line')}"
    if args.get("files"):
        summary += f" · {_plural(len(args['files']), 'extra file')}"
    detail = []
    if r.get("exit_code") is not None:
        detail.append(f"exit {r['exit_code']}")
    if r.get("seconds") is not None:
        detail.append(f"{r['seconds']} s")
    if r.get("network") == "on":
        detail.append("network on")
    if r.get("vm_state") == "kept":
        detail.append("VM state kept")
    if r.get("job_id"):
        detail.append(f"job {r['job_id']}")
    sections = [{"title": f"Code · {name} · {_plural(_count(code), 'line')}", "text": code}]
    if args.get("command"):
        sections.append({"title": "Command", "text": args["command"]})
    for path, text in (args.get("files") or {}).items():
        sections.append({"title": f"File · {path}", "text": text})
    if args.get("stdin"):
        sections.append({"title": "stdin", "text": args["stdin"]})
    for key in ("stdout", "stderr"):
        if r.get(key):
            sections.append({"title": key, "text": r[key]})
    for f in r.get("files") or []:
        if f.get("text"):
            sections.append({"title": f"Collected · {f['path']}", "text": f["text"]})
        elif f.get("binary"):
            sections.append({"title": f"Collected · {f['path']}",
                             "text": f"binary, {f['bytes']:,} bytes" + (f"\nref {f['ref']}" if f.get("ref") else "")})
    return {"pill": pill, "tone": tone, "summary": summary, "headline": headline(r),
            "detail": " · ".join(detail), "sections": sections}


@tool(description=DESCRIPTION,
      parameters={"type": "object", "properties": {
          "language": {"type": "string", "enum": list(LANGS), "description": "Language of `code`."},
          "code": {"type": "string", "maxLength": 200_000,
                   "description": "The complete main program. May be empty only when `command` runs something else."},
          "files": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 200_000},
                    "default": {},
                    "description": "Extra text files, {\"relative/path.ext\": \"content\"}: modules, data, a .csproj."},
          "command": {"type": "string", "maxLength": 4000, "default": "",
                      "description": "Optional cmd.exe line run in the job folder instead of the default run."},
          "stdin": {"type": "string", "maxLength": 200_000, "default": "",
                    "description": "Text given to the program on standard input."},
          "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60,
                        "description": "Seconds before the program is stopped (the limit is in this description)."},
          "network": {"type": "boolean", "enum": [False, True], "default": False,
                      "description": "Connect the VM to the internet for this run (needed for pip, npm or NuGet)."},
          "keep_vm_state": {"type": "boolean", "enum": [False, True], "default": False,
                            "description": "Keep the VM as this run leaves it, for a follow-up call."},
          "collect": {"type": "array", "maxItems": 10, "default": [],
                      "items": {"type": "string", "maxLength": 300},
                      "description": "Relative paths of files the program writes or builds (text or binary, "
                                     "e.g. \"bin/Release/net10.0/win-x64/publish/app.exe\"), to read back after "
                                     "the run and hand to the user with output_file."}},
          "required": ["language", "code"]},
      requires="windows_rce", untrusted=True, timeout_s=1700)
def run_windows_code(ctx, language: str, code: str, files: dict | None = None, command: str = "", stdin: str = "",
                     timeout_s: int = 60, network: bool = False, keep_vm_state: bool = False,
                     collect: list | None = None):
    if ctx.windows is None:
        raise ToolFailure("the Windows VM is off")
    args = {"language": language, "code": code, "files": files or {}, "command": command, "stdin": stdin,
            "timeout_s": timeout_s, "network": network, "keep_vm_state": keep_vm_state, "collect": collect or []}
    limit = int((getattr(ctx.windows, "limits", None) or {}).get("run_timeout_s", 600))
    if timeout_s > limit:
        raise ToolFailure(f"timeout_s can be at most {limit} on this VM")
    r = ctx.windows.run(args)
    outputs = take_outputs(r.get("files") or [])      # the bytes go to the engine, never to the model
    result = {"language": language, "status": r.get("status"), "exit_code": r.get("exit_code"),
              "seconds": r.get("seconds"), "text": render(r), "feed": feed_view(args, r)}
    if outputs:
        result["outputs"] = outputs
    return result

