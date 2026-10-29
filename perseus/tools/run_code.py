"""run_code: build and run a program in a fresh, remote sandbox (via ctx.sandbox).

Offered only when the code runner is up (requires="sandbox"); the language list
the model sees is what the sandbox actually has installed, and a line about the
sandbox's network ([code_runner] network) is added to the description. That network
is independent of the Internet Access switch, which governs web research only.
Output is untrusted data: a program can print text that looks like instructions.
"""

from __future__ import annotations

from perseus.sandbox.languages import LANGUAGES, names
from perseus.tools import ToolFailure, tool

STATUS = {"ok": "ran successfully", "compile_error": "did not compile", "build_timeout": "build timed out",
          "runtime_error": "ran but failed", "timeout": "timed out"}
# the tool feed card: (badge, tone)
PILL = {"ok": ("Program OK", ""), "runtime_error": ("Program failed", "warn"),
        "compile_error": ("Didn't compile", "warn"), "timeout": ("Timed out", "warn"),
        "build_timeout": ("Build timed out", "warn")}


def _count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def headline(r: dict) -> str | None:
    """The one line that says what happened: the error a program died with, or why it stopped."""
    status = r.get("status")
    if status in ("timeout", "build_timeout"):
        return r.get("note")
    if status == "compile_error":
        lines = [ln.strip() for ln in ((r.get("build") or {}).get("output") or "").splitlines() if ln.strip()]
        return next((ln for ln in lines if "error" in ln.lower()), lines[0] if lines else None)
    if status == "runtime_error":
        if r.get("note"):                    # killed by a signal: that's the story
            return r["note"]
        for stream in (r.get("stderr") or "", r.get("stdout") or ""):
            lines = [ln.strip() for ln in stream.splitlines() if ln.strip()]
            if lines:
                return lines[-1]             # Python, Node and most runtimes end with the error itself
    return None


def feed_view(language: str, code: str, stdin: str, r: dict) -> dict:
    """What the tool feed shows for this call (never sent to the model)."""
    status = r.get("status", "ok")
    pill, tone = PILL.get(status, (str(status), ""))
    name = LANGUAGES[language].label.split(" (")[0] if language in LANGUAGES else language
    summary = f"{name} · {_plural(_count(code), 'line')}"
    if stdin:
        summary += f" · stdin {_plural(_count(stdin), 'line')}"
    detail = []
    if r.get("exit_code") is not None:
        detail.append(f"exit {r['exit_code']}")
    out_lines = _count(r.get("stdout") or "")
    if out_lines and status not in ("compile_error", "build_timeout"):
        detail.append(_plural(out_lines, "line") + " of output")
    if r.get("network") and r["network"] != "none":
        detail.append(f"network {r['network']}")
    sections = [{"title": f"Code · {name} · {_plural(_count(code), 'line')}", "text": code}]
    if stdin:
        sections.append({"title": "stdin", "text": stdin})
    build = (r.get("build") or {}).get("output")
    if build:
        sections.append({"title": "Build output", "text": build})
    for key in ("stdout", "stderr"):
        if r.get(key):
            sections.append({"title": key, "text": r[key]})
    return {"pill": pill, "tone": tone, "summary": summary, "headline": headline(r),
            "detail": " · ".join(detail), "sections": sections}


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
    r = ctx.sandbox.run(language, code, stdin)
    text = render(r)
    return {"language": language, "status": r.get("status"), "exit_code": r.get("exit_code"),
            "seconds": r.get("seconds"), "text": text, "feed": feed_view(language, code, stdin, r)}
