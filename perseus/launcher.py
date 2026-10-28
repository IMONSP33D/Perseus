"""perseus.py: boot the box and serve the model with the safe tool loop.

Boot order, cheapest failure first:
  1. socket guard + gateway, then the refusal self-test (~1 s, fails closed)
     before any GPU time is spent
  2. wait for the download preflight started
  3. SearXNG and vLLM under the supervisor (restarts with backoff)
  4. first token (timed), then one test tool call that must come back parsed
  5. the terminal console (the dashboard replaces it in Phase 1)

Reads only perseus.resolved.toml (no secrets). vLLM runs on 127.0.0.1 with a
random per-boot API key in its environment, offline, with no HF token.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import LOG_DIR, RESOLVED_PATH, ROOT, STATE_DIR, searxng, selftest, socketguard
from .download import MARKER
from .engine import Engine, EngineSettings
from .feed import Feed
from .gateway import Gateway
from .llm import LLMClient, LLMError
from .searxng import SearchClient
from .session import Recorder
from .supervisor import ProcessSpec, Supervisor
from .timing import instance_age_seconds, record, report
from .tools import ToolContext, UserTools, load_builtin

STATUS_PATH = STATE_DIR / "download-status.json"
VLLM_LOG = LOG_DIR / "vllm.log"
SEARXNG_LOG = LOG_DIR / "searxng.log"
GATEWAY_LOG = LOG_DIR / "gateway.jsonl"
USER_TOOLS = ROOT / "tools"
SECRET_ENV = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_API_TOKEN")
DEPTHS = {"quick": 5, "standard": 20, "deep": 50}

TOOL_TEST = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current UTC time.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, *hints: str, code: int = 1) -> "NoReturn":  # noqa: F821
    print(f"  ✗ {msg}", file=sys.stderr)
    for h in hints:
        print(f"      → {h}", file=sys.stderr)
    sys.exit(code)


def load_resolved(path: Path = RESOLVED_PATH) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        die(f"{path.name} not found", "run bash preflight.sh first")


# --------------------------------------------------------------------------- vLLM process

def build_vllm_cmd(r: dict, port: int, fast_start: bool = False) -> list[str]:
    """`vllm serve` flags from the resolved config.

    VERIFY IN PHASE 0 against `vllm serve --help` on the pinned 0.32.0; every
    flag here has existed since vLLM 0.8 but names do get renamed.
    """
    v, m = r["vllm"], r["model"]
    vllm_bin = Path(sys.executable).with_name("vllm")
    cmd = [str(vllm_bin) if vllm_bin.exists() else "vllm", "serve", m["path"],
           "--served-model-name", m["id"],
           "--host", "127.0.0.1", "--port", str(port),
           "--tensor-parallel-size", str(v["tensor_parallel_size"]),
           "--max-model-len", str(v["max_model_len"]),
           "--gpu-memory-utilization", str(v["gpu_memory_utilization"]),
           "--dtype", v["dtype"],
           "--generation-config", "auto",
           "--enable-auto-tool-choice", "--tool-call-parser", v["tool_call_parser"]]
    if v.get("quantization"):
        cmd += ["--quantization", v["quantization"]]
    if v.get("kv_cache_dtype", "auto") != "auto":
        cmd += ["--kv-cache-dtype", v["kv_cache_dtype"]]
    if v.get("cpu_offload_gb"):
        cmd += ["--cpu-offload-gb", f"{v['cpu_offload_gb']:g}"]
    if v.get("reasoning_parser"):
        cmd += ["--reasoning-parser", v["reasoning_parser"]]
    if v.get("trust_remote_code"):
        cmd += ["--trust-remote-code"]
    if fast_start or r.get("backend", {}).get("fast_start"):
        cmd += ["--enforce-eager"]  # skip CUDA graph capture: faster load, slower decode
    return cmd


def vllm_env(api_key: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update({
        "VLLM_API_KEY": api_key,           # env, not argv: argv is visible in `ps`
        "HF_HUB_OFFLINE": "1",             # the weights are local; vLLM never talks to the Hub
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": str(STATE_DIR / "hf"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "VLLM_NO_USAGE_STATS": "1",
        "DO_NOT_TRACK": "1",
    })
    return env


def explain_crash(log_text: str, r: dict) -> list[str]:
    from .supervisor import OOM_MARKERS
    if any(m in log_text for m in OOM_MARKERS):
        return [f"vLLM ran out of GPU memory at context {r['vllm']['max_model_len']:,}",
                "set [model] max_context lower in perseus.toml, or smaller weights (quantization), "
                "then re-run bash preflight.sh --check"]
    if "parser" in log_text.lower() and "invalid choice" in log_text.lower():
        return ["vLLM rejected the tool or reasoning parser name",
                "set [model] tool_parser / reasoning_parser (see `vllm serve --help`)"]
    return []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def vllm_healthy(port: int, key: str) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


# --------------------------------------------------------------------------- waiting and progress

def wait_for_download(r: dict, poll: float = 2.0) -> None:
    model_dir = Path(r["model"]["path"])
    sha = r["model"]["revision_sha"]
    t0, shown_stale = time.monotonic(), False
    while True:
        try:
            if json.loads((model_dir / MARKER).read_text()).get("sha") == sha:
                if time.monotonic() - t0 > poll:
                    say("")
                say(f"  ✓ weights ready: {model_dir}")
                return
        except (OSError, ValueError):
            pass
        try:
            st = json.loads(STATUS_PATH.read_text())
        except (OSError, ValueError):
            die("the model hasn't been downloaded", "run bash preflight.sh")
        if st.get("state") == "failed":
            die(f"the download failed ({st.get('error')})", "re-run bash preflight.sh; it resumes")
        if st.get("state") == "done":
            die(f"no verified download for this model at {model_dir}",
                "the config changed since the download, or files were removed; re-run bash preflight.sh")
        age = time.time() - st.get("updated", 0)
        if age > 60 and not shown_stale:
            shown_stale = True
            say("\n  ! download status hasn't changed for a minute; is bash preflight.sh still running?")
        if age > 600:
            die("no download progress for 10 minutes", "re-run bash preflight.sh")
        have, total = st.get("bytes", 0), st.get("total") or 1
        eta = st.get("eta")
        eta_s = f"{int(eta // 60)}m{int(eta % 60):02d}s" if eta else "--"
        print(f"\r\033[K  waiting for download: {have / 1e9:.1f}/{total / 1e9:.1f} GB "
              f"({100 * have / total:.0f}%), ETA {eta_s}   ", end="", flush=True)
        time.sleep(poll)


def gpu_memory_used() -> list[int] | None:
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            return [pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(i)).used
                    for i in range(pynvml.nvmlDeviceGetCount())]
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None


def last_log_line(path: Path) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            lines = [l for l in f.read().decode(errors="replace").splitlines()
                     if l.strip() and not l.startswith("$ ")]
        return lines[-1].strip()[-100:] if lines else ""
    except OSError:
        return ""


def load_progress(expected_gib: float):
    def show(elapsed: float):
        used = gpu_memory_used()
        mem = ""
        if used and expected_gib:
            pct = min(100, 100 * (min(used) / 2 ** 30) / expected_gib)
            mem = f"  GPU memory {min(used) / 2 ** 30:.0f}/{expected_gib:.0f} GiB weights ({pct:.0f}%)"
        print(f"\r\033[K  loading {elapsed:5.0f}s{mem}  {last_log_line(VLLM_LOG)[:70]}", end="", flush=True)
    return show


# --------------------------------------------------------------------------- checks

def tool_call_check(llm: LLMClient, parser: str) -> tuple[bool, str]:
    """Fail closed: a model whose tool calls come back as plain text can't use any tool."""
    try:
        reply = llm.chat([{"role": "user", "content": "What time is it in UTC? Use the get_time tool."}],
                         tools=[TOOL_TEST], max_tokens=512)
    except LLMError as e:
        return False, f"test request failed: {e}"
    names = [c["function"]["name"] for c in reply.tool_calls]
    if names[:1] == ["get_time"]:
        return True, f"get_time() parsed by the {parser} parser"
    snippet = (reply.content or "").strip().replace("\n", " ")[:160]
    return False, (f"the call wasn't parsed by the {parser!r} parser; model replied: {snippet or '(empty)'}. "
                   "Set [model] tool_parser in perseus.toml")


def print_selftest(rep: selftest.Report, verbose: bool = False) -> None:
    say(f"  {'✓' if rep.passed else '✗'} self-test: {rep.summary()}")
    for c in rep.cases:
        if verbose or not c.ok:
            mark = "✓" if c.ok else ("✗" if c.critical else "!")
            say(f"      {mark} {c.name}: {c.detail}")


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python perseus.py")
    p.add_argument("--ask", metavar="QUESTION", help="answer one question through the engine and gateway, then exit")
    p.add_argument("--selftest", action="store_true", help="run the gateway refusal checks and exit")
    p.add_argument("--replay", metavar="EXPORT.json", help="re-run a saved session offline (no GPU, no network)")
    p.add_argument("--fast-start", action="store_true", help="skip CUDA graph capture (faster load)")
    p.add_argument("--console", action="store_true", help="terminal console instead of the dashboard")
    p.add_argument("--no-repl", action="store_true", help=argparse.SUPPRESS)  # old name for the default: serve
    p.add_argument("--timings", action="store_true", help="print the latest boot timings and exit")
    p.add_argument("--print-cmd", action="store_true", help="print the vLLM command and exit")
    args = p.parse_args(argv)

    if args.timings:
        return say(report())
    if args.replay:
        return run_replay(Path(args.replay))
    r = load_resolved()
    vllm_port = free_port()
    if args.print_cmd:
        return say(" ".join(build_vllm_cmd(r, vllm_port, args.fast_start)))

    key = secrets.token_urlsafe(32)  # per boot, held only by this process and vLLM
    search_port = int(r["search"]["port"])
    limits = r.get("limits", {})
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 1. the walls first ------------------------------------------------------
    gateway = Gateway(timeout_s=limits.get("fetch_timeout_s", 15), max_response_kb=limits.get("max_response_kb", 2048),
                      exact_secrets=(key,), log_path=GATEWAY_LOG)
    refused: list[str] = []
    socketguard.install({vllm_port, search_port}, on_refuse=refused.append)
    registry = load_builtin()
    user_tools = UserTools(USER_TOOLS, registry)
    for change in user_tools.refresh():
        say(f"  tools: {change}")
    ports = {"vLLM": vllm_port, "SearXNG": search_port, "Perseus": int(r["server"]["port"])}
    rep = selftest.run(gateway, ports, registry, reach_url="https://example.com/", tool_dirs=[USER_TOOLS])
    refused.clear()  # the self-test's own probe isn't a real refusal
    if args.selftest:
        print_selftest(rep, verbose=True)
        sys.exit(0 if rep.passed else 1)

    model = r["model"]["id"]
    say(f"\033[1mPerseus\033[0m · {model} on {r['hardware']['summary']} · TP={r['vllm']['tensor_parallel_size']}, "
        f"{r['vllm']['weights']}, context {r['vllm']['max_model_len']:,}")
    print_selftest(rep)
    if not rep.passed:
        die("the web gateway failed its refusal checks; Perseus won't start (fails closed)",
            *[f"{c.name}: {c.detail}" for c in rep.failures])
    if r["profile"]["small_gpu"]:
        say(f"  Small-GPU profile: on — {r['profile']['small_gpu_reason']}")
    for w in r.get("notes", {}).get("warnings", []):
        say(f"  ! {w}")

    # 2. weights --------------------------------------------------------------
    t_wait = time.monotonic()
    wait_for_download(r)
    if time.monotonic() - t_wait > 5:
        record("wait_for_download", time.monotonic() - t_wait)

    # 3. processes ------------------------------------------------------------
    last_state: dict[str, str] = {}

    def on_state(name, m):
        try:
            _report_state(name, m)
        finally:
            last_state[name] = m.state

    def _report_state(name, m):
        if m.state == "restarting" and m.intentional:
            if last_state.get(name) != "restarting":
                say(f"\n  ↻ {name} restarting with new settings")
        elif m.state == "restarting":
            say(f"\n  ! {name} restarting (attempt {m.restarts} of {m.spec.max_restarts}): {m.reason}")
        elif m.state == "up" and m.restarts:
            say(f"\n  ✓ {name} back up")
        elif m.state == "failed":
            say(f"\n  ✗ {name} failed: {m.reason}\n{m.tail}")

    sup = Supervisor(on_state=on_state)

    def interrupt(signum, frame):
        raise KeyboardInterrupt  # SIGTERM / SIGHUP unwind into the finally below, which stops vLLM and SearXNG

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    max_restarts = int(r.get("backend", {}).get("max_restarts", 3))
    search = SearchClient(search_port)
    sx_cmd, sx_env = searxng.command(ROOT)
    have_searxng = Path(sx_cmd[0]).exists()
    if have_searxng:
        sup.add(ProcessSpec("SearXNG", sx_cmd, {**os.environ, **sx_env}, SEARXNG_LOG, search.healthy,
                            ready_timeout_s=90, max_restarts=max_restarts))
    sup.add(ProcessSpec("vLLM", build_vllm_cmd(r, vllm_port, args.fast_start), vllm_env(key), VLLM_LOG,
                        lambda: vllm_healthy(vllm_port, key), cwd=ROOT, max_restarts=max_restarts,
                        explain=lambda log: explain_crash(log, r)))
    try:
        if have_searxng:
            sup.start("SearXNG")
        sup.start("vLLM")
        say(f"  starting vLLM (log: {VLLM_LOG.relative_to(ROOT)})")
        t_load = time.monotonic()
        m = sup.wait_ready("vLLM", load_progress(r["vllm"].get("weight_gib_per_gpu", 0)))
        say("")
        if m.state != "up":
            die(f"vLLM didn't start: {m.reason}", *m.hints[1:], code=1)
        load_s = time.monotonic() - t_load
        record("model_load", load_s)
        say(f"  ✓ vLLM ready in {load_s:.0f}s")
        if have_searxng:
            sx = sup.wait_ready("SearXNG")
            say("  ✓ SearXNG up" if sx.state == "up" else f"  ! SearXNG not up ({sx.reason}); web_search unavailable")
        else:
            say("  ! SearXNG isn't installed (run bash preflight.sh); web_search unavailable")

        # 4. first token and the parse check -----------------------------------
        llm = LLMClient(vllm_port, key, model)
        first = llm.chat([{"role": "user", "content": "Reply with one word: ready"}], max_tokens=256)
        ttft = first.first_token_s or 0.0
        record("first_token", ttft)
        age = instance_age_seconds()
        if age is not None:
            record("boot_to_first_token", age)
        say(f"  ✓ first token in {ttft:.2f}s" + (f" · {age / 60:.1f} min since the instance started" if age else ""))
        ok, detail = tool_call_check(llm, r["vllm"]["tool_call_parser"])
        if not ok:
            die(f"tool-call test: {detail}")
        say(f"  ✓ tool-call test: {detail}")
        say("\n" + report())

        # 5. the engine ---------------------------------------------------------
        prof = r["profile"]
        depths = {k: int(prof.get(f"lookups_{k}", v)) for k, v in DEPTHS.items()}
        recorder = Recorder(meta={"model": model, "hardware": r["hardware"]["summary"], "vllm": r["vllm"]})
        engine = Engine(llm, registry, ToolContext(gateway=gateway, search=search, limits=limits), Feed(),
                        EngineSettings(lookup_limit=depths[limits.get("research_depth", "standard")],
                                       tool_result_tokens=prof.get("tool_result_tokens") or None,
                                       max_context=int(r["vllm"]["max_model_len"]), small_gpu=bool(prof["small_gpu"])),
                        recorder=recorder)
        if args.ask or args.console:
            from .console import Console
            console = Console(engine, depths, limits.get("research_depth", "standard"), LOG_DIR, gateway,
                              user_tools.refresh, recorder, model_dir=Path(r["model"]["path"]))
            if args.ask:
                print("model › ", end="", flush=True)
                console.ask(args.ask)
                say(f"  saved {console.export()}")
            else:
                console.loop()
                if console.history:
                    say(f"  saved {console.export()}")
        else:
            serve_dashboard(r, sup, llm, registry, engine.ctx, gateway, depths, user_tools, rep,
                            lambda: build_vllm_cmd, args.fast_start, vllm_port)
        if refused:
            say(f"  ! socket guard refused {len(refused)} connection(s) outside the gateway: {', '.join(refused[:5])}")
    except KeyboardInterrupt:
        pass
    finally:
        say("  stopping…")
        sup.stop_all()


def precision_label(v: dict) -> str:
    w = str(v.get("weights", ""))
    names = {"bf16": "BF16", "fp16": "FP16", "fp8 (online)": "FP8 (quantized at load)"}
    label = names.get(w) or (w.split(":", 1)[1].upper() + " (from the repo)" if w.startswith("repo:") else w.upper())
    if v.get("kv_cache_dtype") == "fp8":
        label += " · FP8 KV cache"
    if v.get("cpu_offload_gb"):
        label += f" · {v['cpu_offload_gb']:g} GB offloaded to RAM"
    return label


def serve_dashboard(r: dict, sup, llm, registry, ctx, gateway, depths, user_tools, selftest_report,
                    cmd_builder, fast_start: bool, vllm_port: int) -> None:
    """Serve the dashboard until Ctrl-C (or until vLLM fails for good)."""
    from .dashboard import Dashboard
    from .server import Server

    v, prof = r["vllm"], r["profile"]
    model_dir = Path(r["model"]["path"])
    info = {"model": r["model"]["id"], "hardware": r["hardware"]["summary"], "precision": precision_label(v),
            "context": int(v["max_model_len"]), "capacity": int(v.get("context_capacity") or v["max_model_len"]),
            "max_positions": int(r["model"].get("max_positions") or v["max_model_len"]),
            "thinks": bool(r["model"].get("thinks")), "reasoning_effort": v.get("reasoning_parser") == "openai_gptoss",
            "small_gpu": bool(prof.get("small_gpu")), "tensor_parallel": int(v["tensor_parallel_size"])}

    def restart_model(tokens: int) -> None:
        r["vllm"]["max_model_len"] = tokens
        sup.restart("vLLM", cmd_builder()(r, vllm_port, fast_start))
        m = sup.wait_ready("vLLM")
        if m.state != "up":
            raise RuntimeError(m.reason or "vLLM didn't come back")

    def raw_prompt(messages, tools):
        from .devtools import render_prompt
        return render_prompt(model_dir, messages, tools)

    def status():
        return {name: {"state": st["state"], "restarts": st["restarts"], **({"reason": st["reason"]} if st.get("reason") else {})}
                for name, st in sup.status().items()}

    engine_settings = {"tool_result_tokens": prof.get("tool_result_tokens") or None,
                       "max_context": int(v["max_model_len"]), "small_gpu": bool(prof.get("small_gpu"))}
    dash = Dashboard(info=info, llm=llm, registry=registry, ctx=ctx, depths=depths, engine_settings=engine_settings,
                     engine_list=list(r["search"]["engines"]), status=status, restart_model=restart_model,
                     raw_prompt=raw_prompt, refresh_tools=user_tools.refresh, gateway=gateway)
    dash.update_settings({"depth": r.get("limits", {}).get("research_depth", "standard")})

    def health():
        st = status()
        return {"ok": st.get("vLLM", {}).get("state") == "up", "processes": st, "selftest": selftest_report.summary()}

    srv = r["server"]
    try:
        server = Server(dash, srv["bind"], int(srv["port"]), health=health)
    except OSError as e:
        die(f"can't open the dashboard on {srv['bind']}:{srv['port']} ({e.strerror or e})",
            "another program uses that port: set [server] port in perseus.toml")
    server.start()
    port = server.port
    say("")
    if srv.get("public"):
        host = _public_ip()
        say(f"  ✓ dashboard (public): {server.url(host)}")
        say("  ! the dashboard is reachable from the internet; anyone with this link can use the model")
    else:
        say(f"  ✓ dashboard: {server.url()}")
        say(f"    from your computer: ssh -N -L {port}:127.0.0.1:{port} <user>@<this box>, then open the link")
    say("    Ctrl-C stops Perseus.")
    try:
        while sup.procs["vLLM"].state != "failed":
            time.sleep(1)
        die("vLLM failed and restarts ran out", *sup.procs["vLLM"].hints)
    finally:
        server.stop()


def _public_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "<this box's IP>"


def run_replay(path: Path) -> None:
    from .session import replay

    def make(llm, override):
        reg = load_builtin()
        return Engine(llm, reg, ToolContext(), Feed(), EngineSettings(), tool_override=override)

    rows = replay(path, make)
    for i, row in enumerate(rows, 1):
        mark = "=" if row["same"] else "≠"
        say(f"{i:3}. {mark} {row['user']}")
        if not row["same"]:
            say(f"       recorded: {row['recorded_answer']}\n       replayed: {row['replayed_answer']}")
        if row["refused"] or row["blocked"]:
            say(f"       refused {row['refused']}, blocked {row['blocked']}")
    same = sum(r["same"] for r in rows)
    say(f"\n{same}/{len(rows)} turns replayed with the same answer")
