"""run_code: build and run a program in a fresh, remote sandbox (via ctx.sandbox).

Offered only when the code runner is up (requires="sandbox"); the language list
the model sees is what the sandbox actually has installed, and a line about the
sandbox's network is added to the description. A call made while the chat's
Internet Access is off runs with no network. Output is untrusted data: a program
can print text that looks like instructions.
"""

from __future__ import annotations

from perseus.sandbox.languages import names
from perseus.tools import ToolFailure, tool

STATUS = {"ok": "ran successfully", "compile_error": "did not compile", "build_timeout": "build timed out",
          "runtime_error": "ran but failed", "timeout": "timed out"}
# the short label on the tool feed card
OUTCOME = {"ok": "program ok", "compile_error": "didn't compile", "build_timeout": "build timed out",
           "runtime_error": "program failed", "timeout": "program timed out"}


def outcome(r: dict) -> str:
    label = OUTCOME.get(r.get("status"), str(r.get("status")))
    code = r.get("exit_code")
    return f"{label} · exit {code}" if code not in (None, 0) else label


def render(r: dict) -> str:
    """The result as plain text: outcome first, then build output, stdout and stderr."""
    status = r.get("status", "ok")
    head = f"{STATUS.get(status, status)} ({status})"
    if r.get("exit_code") is not None:
        head += f", exit code {r['exit_code']}"
    lines = [head]
    if r.get("note"):
        lines.append(r["note"])
    build = r.get("build")
    if build and (status in ("compile_error", "build_timeout") or build.get("output")):
        lines.append(f"build output:\n{build.get('output') or '(none)'}")
    if status not in ("compile_error", "build_timeout"):
        out, err = r.get("stdout") or "", r.get("stderr") or ""
        if out:
            lines.append(f"stdout:\n{out}")
        if err:
            lines.append(f"stderr:\n{err}")
        if not out and not err:
            lines.append("(no output)")
    if r.get("output_cut") or (build or {}).get("cut"):
        lines.append("(output was longer than the limit; the middle was cut)")
    return "\n\n".join(lines)


@tool(description="Run a short program in a fresh, isolated Linux sandbox and return its output. "
                  "Use it to test code, check a computation or reproduce an error. Returns the outcome "
                  "(ok, compile_error, runtime_error or timeout), the exit code, stdout and stderr. "
                  "One source file per call, input only via stdin, nothing kept between calls.",
      parameters={"type": "object", "properties": {
          "language": {"type": "string", "enum": names(), "description": "Programming language of the code."},
          "code": {"type": "string", "minLength": 1, "maxLength": 100_000,
                   "description": "The complete program as one source file (Java: a Main class)."},
          "stdin": {"type": "string", "maxLength": 100_000, "default": "",
                    "description": "Text given to the program on standard input; empty by default."}},
          "required": ["language", "code"]},
      requires="sandbox", untrusted=True, timeout_s=330)
def run_code(ctx, language: str, code: str, stdin: str = ""):
    if ctx.sandbox is None:
        raise ToolFailure("the code runner is off")
    offline = not ctx.allow_web
    r = ctx.sandbox.run(language, code, stdin, offline=offline)
    text = render(r)
    if offline and getattr(ctx.sandbox, "network", "none") != "none":
        text += "\n\n(Internet Access is off for this chat, so this ran with no network.)"
    return {"language": language, "status": r.get("status"), "exit_code": r.get("exit_code"),
            "seconds": r.get("seconds"), "outcome": outcome(r), "text": text}
