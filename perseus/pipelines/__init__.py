"""Development pipelines: a request worked through ordered stages, each a role with its own prompt and tools.

WIP: this module defines the format, checks it, and builds exactly what each stage would be given.
Nothing runs pipelines yet; the dashboard shows them so the format can be shaped before the runner.

A pipeline is one TOML file in this folder (files starting with "_" are skipped):

    [pipeline]
    name    = "Feature build"
    summary = "Plan, build, review and document a code change."
    status  = "draft"                     # draft | ready

    [[stage]]
    id        = "plan"                    # letters, digits, "_" and "-"
    role      = "Planner"                 # who the model is in this stage
    goal      = "One line: what this stage is for."
    prompt    = '''Instructions for the role.'''
    tools     = ["web_search", "fetch_url"]   # only these are offered; [] for none
    inputs    = ["request"]               # artifacts handed in: "request" or an earlier stage's output
    outputs   = ["plan"]                  # artifacts it must hand on (a work stage needs one or more)
    done_when = ["Every requirement has an acceptance check."]
    thinking  = "medium"                  # optional: off | low | medium | high | max

    [[stage]]
    id        = "review"
    kind      = "review"                  # work (default) | review
    on_revise = "build"                   # review only: the earlier stage that gets the work back
    max_rounds = 2                        # review only: revisions before the pipeline stops for a person
    ...

The rules that make the judgement unbiased:
  * Every stage starts in a new context window. It is given the system prompt from stage_prompt() and
    one user message from handoff_packet(): the request plus only the artifacts it lists as inputs.
    No earlier reasoning, tool results or chat comes along.
  * A stage finishes by writing a <handoff> block (format below); parse_handoff() reads it. Text outside
    the block is the stage's working, and is never passed on.
  * A review stage cannot edit the work. It returns a verdict (pass | revise) and feedback; on revise the
    on_revise stage runs again, fresh, with its earlier output and the feedback added to its inputs.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PIPELINES_DIR = Path(__file__).resolve().parent
KINDS = ("work", "review")
STATUSES = ("draft", "ready")
THINKING = ("off", "low", "medium", "high", "max")
REQUEST = "request"
REVIEW_OUTPUTS = ("verdict", "feedback")
VERDICTS = ("pass", "revise")
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_STAGE_KEYS = {"id", "role", "kind", "goal", "prompt", "tools", "inputs", "outputs", "done_when", "thinking",
               "on_revise", "max_rounds"}
_PIPELINE_KEYS = {"name", "summary", "status"}


class PipelineError(Exception):
    pass


@dataclass
class Stage:
    id: str
    role: str
    goal: str
    prompt: str
    kind: str = "work"
    tools: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=lambda: [REQUEST])
    outputs: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    thinking: str | None = None
    on_revise: str | None = None
    max_rounds: int = 2

    @property
    def produces(self) -> list[str]:
        return list(REVIEW_OUTPUTS) if self.kind == "review" else self.outputs


@dataclass
class Pipeline:
    id: str
    name: str
    summary: str
    stages: list[Stage]
    status: str = "draft"
    source: str = ""

    def stage(self, sid: str) -> Stage:
        for s in self.stages:
            if s.id == sid:
                return s
        raise KeyError(sid)

    def producer(self, artifact: str) -> Stage | None:
        return next((s for s in self.stages if artifact in s.produces), None)


# ---------------------------------------------------------------------- loading

def _strs(v, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise PipelineError(f"{where} must be a list of non-empty strings")
    return [x.strip() for x in v]


def _text(d: dict, key: str, where: str, required: bool = True) -> str:
    v = d.get(key)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or not v.strip():
        raise PipelineError(f"{where}: {key} is required and must be text")
    return v.strip()


def _reflow(text: str) -> str:
    """Join hard-wrapped lines from the file into paragraphs; blank lines and list items keep their breaks."""
    paras = []
    for para in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        out: list[str] = []
        for ln in lines:
            if out and not re.match(r"^([-*•]|\d+[.)])\s", ln):
                out[-1] += " " + ln
            else:
                out.append(ln)
        paras.append("\n".join(out))
    return "\n\n".join(paras)


def parse(doc: dict, pid: str, source: str = "") -> Pipeline:
    """Check a parsed TOML document and build the Pipeline. Raises PipelineError naming the first problem."""
    head = doc.get("pipeline")
    if not isinstance(head, dict):
        raise PipelineError("missing [pipeline] table")
    if extra := set(head) - _PIPELINE_KEYS:
        raise PipelineError(f"[pipeline]: unknown key {min(extra)!r}")
    status = head.get("status", "draft")
    if status not in STATUSES:
        raise PipelineError(f"[pipeline]: status must be one of {', '.join(STATUSES)}")
    raw = doc.get("stage")
    if not isinstance(raw, list) or not raw:
        raise PipelineError("needs at least one [[stage]]")
    if extra := set(doc) - {"pipeline", "stage"}:
        raise PipelineError(f"unknown table {min(extra)!r}")

    stages: list[Stage] = []
    available = {REQUEST}             # artifacts produced so far, in order
    for n, d in enumerate(raw, 1):
        where = f"stage {n}"
        if not isinstance(d, dict):
            raise PipelineError(f"{where} must be a table")
        sid = d.get("id")
        if not isinstance(sid, str) or not _ID.match(sid):
            raise PipelineError(f"{where}: id must be lowercase letters, digits, '_' or '-', starting with a letter")
        where = f"stage {sid!r}"
        if extra := set(d) - _STAGE_KEYS:
            raise PipelineError(f"{where}: unknown key {min(extra)!r}")
        if any(s.id == sid for s in stages):
            raise PipelineError(f"{where}: id used twice")
        kind = d.get("kind", "work")
        if kind not in KINDS:
            raise PipelineError(f"{where}: kind must be work or review")
        st = Stage(id=sid, role=_text(d, "role", where), goal=_text(d, "goal", where),
                   prompt=_reflow(_text(d, "prompt", where)), kind=kind,
                   tools=_strs(d.get("tools", []), f"{where}: tools"),
                   inputs=_strs(d.get("inputs", [REQUEST]), f"{where}: inputs"),
                   outputs=_strs(d.get("outputs", []), f"{where}: outputs"),
                   done_when=_strs(d.get("done_when", []), f"{where}: done_when"))
        if not st.inputs:
            raise PipelineError(f"{where}: inputs can't be empty")
        for a in st.inputs:
            if a not in available:
                raise PipelineError(f"{where}: input {a!r} isn't produced by an earlier stage")
        for a in st.outputs:
            if not _ID.match(a):
                raise PipelineError(f"{where}: output name {a!r} must look like an id")
            if a in available:
                raise PipelineError(f"{where}: output {a!r} is already produced earlier")
            if a in REVIEW_OUTPUTS:
                raise PipelineError(f"{where}: {a!r} is reserved for review stages")
        if "thinking" in d:
            if d["thinking"] not in THINKING:
                raise PipelineError(f"{where}: thinking must be one of {', '.join(THINKING)}")
            st.thinking = d["thinking"]
        if kind == "work":
            if not st.outputs:
                raise PipelineError(f"{where}: a work stage needs at least one output")
            if "on_revise" in d or "max_rounds" in d:
                raise PipelineError(f"{where}: on_revise and max_rounds are for review stages")
        else:
            if st.outputs:
                raise PipelineError(f"{where}: a review stage hands on a verdict and feedback, not outputs")
            target = d.get("on_revise")
            if not any(s.id == target and s.kind == "work" for s in stages):
                raise PipelineError(f"{where}: on_revise must name an earlier work stage")
            st.on_revise = target
            rounds = d.get("max_rounds", 2)
            if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 5:
                raise PipelineError(f"{where}: max_rounds must be a whole number from 1 to 5")
            st.max_rounds = rounds
            if not st.done_when:
                raise PipelineError(f"{where}: a review stage needs done_when criteria to judge against")
        stages.append(st)
        available.update(st.produces if kind == "work" else ())
    return Pipeline(id=pid, name=_text(head, "name", "[pipeline]"), summary=_text(head, "summary", "[pipeline]"),
                    stages=stages, status=status, source=source)


def load_file(path: Path) -> Pipeline:
    try:
        doc = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise PipelineError(f"not valid TOML: {e}") from None
    return parse(doc, path.stem, path.name)


def load_all(directory: Path = PIPELINES_DIR) -> tuple[list[Pipeline], dict[str, str]]:
    """(pipelines, {file name: problem}) for every *.toml in the folder. A broken file never hides the rest."""
    pipelines, errors = [], {}
    for p in sorted(directory.glob("*.toml")):
        if p.name.startswith("_"):
            continue
        try:
            pipelines.append(load_file(p))
        except (PipelineError, OSError) as e:
            errors[p.name] = str(e)
    return pipelines, errors


# ---------------------------------------------------------------------- what a stage is given

def stage_prompt(pl: Pipeline, st: Stage, tool_names: list[str] | None = None) -> str:
    """The system prompt for a stage's fresh context window."""
    n, total = pl.stages.index(st) + 1, len(pl.stages)
    offered = [t for t in st.tools if tool_names is None or t in tool_names]
    lines = [f"You are the {st.role} in the \"{pl.name}\" pipeline (stage {n} of {total}).", st.goal, "",
             st.prompt, "", "How this stage works:",
             ("- You start with a fresh context. You are given only what is in the message below, "
              "nothing else: not the earlier stages' reasoning, tool use or conversation.")]
    if st.kind == "review":
        lines.append("- You did not write this work and owe it nothing. Judge it only against the criteria below, "
                     "check claims rather than trusting them, and don't rewrite it yourself: say what must change.")
    lines.append(f"- Tools you may call: {', '.join(offered)}." if offered else "- You have no tools in this stage.")
    if st.done_when:
        lines.append("- You are done when:" if st.kind == "work" else "- Pass the work only if all of these hold:")
        lines += [f"  - {c}" for c in st.done_when]
    lines += ["", "Finish with this handoff block, exactly once, at the very end. Only what is inside it is passed on.",
              handoff_template(st)]
    return "\n".join(lines)


def handoff_template(st: Stage) -> str:
    if st.kind == "review":
        return ("<handoff>\n<verdict>pass or revise</verdict>\n<feedback>\nWhat holds, what fails and exactly what "
                "must change.\n</feedback>\n</handoff>")
    body = "\n".join(f'<artifact name="{a}">\n…\n</artifact>' for a in st.outputs)
    return f"<handoff>\n{body}\n</handoff>"


def handoff_packet(pl: Pipeline, st: Stage, request: str, artifacts: dict[str, str],
                   revision: tuple[int, str] | None = None) -> str:
    """The one user message that opens a stage's context: the request and only its declared inputs.

    revision = (round, feedback) when a review sent the work back; the stage then also gets its own
    earlier outputs, so it revises instead of starting over.
    """
    n = pl.stages.index(st) + 1
    parts = [f"Pipeline: {pl.name} · stage {n} of {len(pl.stages)} · {st.role}", ""]
    for a in st.inputs:
        if a == REQUEST:
            parts += ["<request>", request.strip(), "</request>", ""]
        else:
            src = pl.producer(a)
            parts += [f'<artifact name="{a}" from="{src.id if src else "?"}">',
                      (artifacts.get(a) or "(missing)").strip(), "</artifact>", ""]
    if revision:
        rnd, feedback = revision
        reviewer = next((s for s in pl.stages if s.on_revise == st.id), None)
        limit = reviewer.max_rounds if reviewer else rnd
        parts += [f"Revision {rnd} of {limit}: a reviewer sent your work back.", ""]
        for a in st.outputs:
            parts += [f'<previous name="{a}">', (artifacts.get(a) or "(missing)").strip(), "</previous>", ""]
        parts += ["<feedback>", feedback.strip(), "</feedback>", ""]
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------- reading a stage's answer

@dataclass
class Handoff:
    artifacts: dict[str, str] = field(default_factory=dict)
    verdict: str | None = None
    feedback: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


_BLOCK = re.compile(r"<handoff>(.*?)</handoff>", re.DOTALL)
_ARTIFACT = re.compile(r'<artifact\s+name="([^"]+)"\s*>(.*?)</artifact>', re.DOTALL)
_TAG = {k: re.compile(rf"<{k}>(.*?)</{k}>", re.DOTALL) for k in REVIEW_OUTPUTS}


def parse_handoff(st: Stage, text: str) -> Handoff:
    """Read the stage's last <handoff> block. Problems say what a runner should send back to the stage."""
    h = Handoff()
    blocks = _BLOCK.findall(text or "")
    if not blocks:
        h.problems.append("no <handoff> block at the end of the answer")
        return h
    body = blocks[-1]
    if st.kind == "review":
        v = _TAG["verdict"].search(body)
        verdict = v.group(1).strip().lower() if v else ""
        if verdict not in VERDICTS:
            h.problems.append("verdict must be pass or revise")
        else:
            h.verdict = verdict
        f = _TAG["feedback"].search(body)
        h.feedback = f.group(1).strip() if f else ""
        if h.verdict == "revise" and not h.feedback:
            h.problems.append("a revise verdict needs feedback saying what must change")
        return h
    for name, content in _ARTIFACT.findall(body):
        if name in st.outputs:
            h.artifacts[name] = content.strip()
    for a in st.outputs:
        if not h.artifacts.get(a):
            h.problems.append(f"artifact {a!r} is missing or empty")
    return h


# ---------------------------------------------------------------------- for the dashboard

def describe(pl: Pipeline, tool_names: list[str]) -> dict:
    """JSON for the dashboard: stages with their compiled prompt and packet, and any tools not installed."""
    stages = []
    for st in pl.stages:
        sample = {a: f"(the {a} from {pl.producer(a).role if pl.producer(a) else '?'})" for a in st.inputs}
        stages.append({
            "id": st.id, "role": st.role, "kind": st.kind, "goal": st.goal, "prompt": st.prompt,
            "tools": st.tools, "missing_tools": [t for t in st.tools if t not in tool_names],
            "inputs": [{"name": a, "from": (pl.producer(a).id if pl.producer(a) else None)} for a in st.inputs],
            "outputs": st.produces, "done_when": st.done_when, "thinking": st.thinking,
            "on_revise": st.on_revise, "max_rounds": st.max_rounds if st.kind == "review" else None,
            "system_prompt": stage_prompt(pl, st, tool_names),
            "packet": handoff_packet(pl, st, "(the request you send)", sample),
        })
    return {"id": pl.id, "name": pl.name, "summary": pl.summary, "status": pl.status, "source": pl.source,
            "stages": stages}
