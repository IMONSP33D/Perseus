"""perseus.py: boot the box and serve the model with the safe tool loop.

Boot order, cheapest failure first:
  1. socket guard + gateway, then the refusal self-test (~1 s, fails closed)
     before any GPU time is spent
  2. wait for the download preflight started
  3. SearXNG and vLLM under the supervisor (restarts with backoff)
  4. first token (timed), then one test tool call that must come back parsed
  5. the dashboard (or the terminal console with --console)

Reads ./perseus.toml only to find the data directory preflight recorded, then
<data>/perseus.resolved.toml (no secrets). Everything it writes goes to the
data directory: logs/ and run/ (PIDs, the per-boot vLLM key and the dashboard
link, mode 600, removed at shutdown).
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
from pathlib import Path

from perseus import timing
from perseus.config import WEIGHTS_MARKER, ConfigError, Paths, default_config_path, find_paths, load_resolved
from perseus.engine.feed import Feed
from perseus.engine.loop import Engine, EngineSettings
from perseus.engine.session import Recorder
from perseus.engine.tool_loader import default_loader
from perseus.model.client import LLMClient
from perseus.model.launch import (build_vllm_cmd, explain_crash, gpu_memory_used, precision_label, tool_call_check,
                                  vllm_env, vllm_healthy)
from perseus.safety import selftest, socketguard
from perseus.safety.gateway import Gateway
from perseus.search import SearchClient
from perseus.search import command as searxng_command
from perseus.supervisor import ProcessSpec, Supervisor
from perseus.tools import TOOLS_DIR, ToolContext, load_builtin

DEPTHS = {"quick": 5, "standard": 20, "deep": 50}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, *hints: str, code: int = 1) -> "NoReturn":  # noqa: F821
    print(f"  ✗ {msg}", file=sys.stderr)
    for h in hints:
        print(f"      → {h}", file=sys.stderr)
    sys.exit(code)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- waiting and progress

def wait_for_download(r: dict, paths: Paths, poll: float = 2.0) -> None:
    model_dir = Path(r["model"]["path"])
    sha = r["model"]["revision_sha"]
    status_path = paths.run / "download-status.json"
    t0, shown_stale = time.monotonic(), False
    while True:
        try:
            if json.loads((model_dir / WEIGHTS_MARKER).read_text()).get("sha") == sha:
                if time.monotonic() - t0 > poll:
                    say("")
                say(f"  ✓ weights ready: {model_dir}")
                return
        except (OSError, ValueError):
            pass
        try:
            st = json.loads(status_path.read_text())
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


def load_progress(expected_gib: float, log: Path):
    def show(elapsed: float):
        used = gpu_memory_used()
        mem = ""
        if used and expected_gib:
            pct = min(100, 100 * (min(used) / 2 ** 30) / expected_gib)
            mem = f"  GPU memory {min(used) / 2 ** 30:.0f}/{expected_gib:.0f} GiB weights ({pct:.0f}%)"
        print(f"\r\033[K  loading {elapsed:5.0f}s{mem}  {last_log_line(log)[:70]}", end="", flush=True)
    return show


def print_selftest(rep: selftest.Report, verbose: bool = False) -> None:
    say(f"  {'✓' if rep.passed else '✗'} self-test: {rep.summary()}")
    for c in rep.cases:
        if verbose or not c.ok:
            mark = "✓" if c.ok else ("✗" if c.critical else "!")
            say(f"      {mark} {c.name}: {c.detail}")


class RunFiles:
    """<data>/run/: PIDs and per-boot secrets, removed again at shutdown."""

    def __init__(self, paths: Paths):
        self.paths, self.written = paths, []

    def pid(self, name: str, pid: int) -> None:
        p = self.paths.run / f"{name}.pid"
        p.write_text(f"{pid}\n")
        self.written.append(p)

    def secret(self, name: str, text: str) -> Path:
        p = self.paths.write_secret(name, text)
        self.written.append(p)
        return p

    def clear(self) -> None:
        for p in self.written:
            try:
                p.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python perseus.py")
    p.add_argument("--config", type=Path, default=None, help="perseus.toml to use (default: ./perseus.toml)")
    p.add_argument("--data", type=Path, default=None, help="data directory (default: the one preflight recorded)")
    p.add_argument("--ask", metavar="QUESTION", help="answer one question through the engine and gateway, then exit")
    p.add_argument("--selftest", action="store_true", help="run the gateway refusal checks and exit")
    p.add_argument("--replay", metavar="EXPORT.json", help="re-run a saved session offline (no GPU, no network)")
    p.add_argument("--fast-start", action="store_true", help="skip CUDA graph capture (faster load)")
    p.add_argument("--console", action="store_true", help="terminal console instead of the dashboard")
    p.add_argument("--timings", action="store_true", help="print the latest boot timings and exit")
    p.add_argument("--print-cmd", action="store_true", help="print the vLLM command and exit")
    args = p.parse_args(argv)

    if args.replay:
        return run_replay(Path(args.replay))
    config = (args.config or default_config_path()).expanduser().absolute()
    try:
        paths = find_paths(config, args.data).ensure()
        r = load_resolved(paths)
    except ConfigError as e:
        die("; ".join(e.problems))
    timing.configure(paths)
    if args.timings:
        return say(timing.report())
    vllm_port = free_port()
    if args.print_cmd:
        return say(" ".join(build_vllm_cmd(r, vllm_port, args.fast_start)))

    key = secrets.token_urlsafe(32)  # per boot: this process, vLLM, and run/vllm.key (600) for scripts
    search_port = int(r["search"]["port"])
    limits = r.get("limits", {})
    logs = paths.logs
    run = RunFiles(paths)
    run.pid("perseus", os.getpid())
    run.secret("vllm.key", key)

    # 1. the walls first ------------------------------------------------------
    gateway = Gateway(timeout_s=limits.get("fetch_timeout_s", 15), max_response_kb=limits.get("max_response_kb", 2048),
                      exact_secrets=(key,), log_path=logs / "gateway.jsonl")
    refused: list[str] = []
    socketguard.install({vllm_port, search_port}, on_refuse=refused.append)
    loader = default_loader()
    for change in loader.refresh():
        if not change.startswith("loaded "):
            say(f"  tools: {change}")
    registry = load_builtin()
    ports = {"vLLM": vllm_port, "SearXNG": search_port, "Perseus": int(r["server"]["port"])}
    rep = selftest.run(gateway, ports, registry, reach_url="https://example.com/", tool_dirs=[TOOLS_DIR])
    refused.clear()  # the self-test's own probe isn't a real refusal
    if args.selftest:
        print_selftest(rep, verbose=True)
        run.clear()
        sys.exit(0 if rep.passed else 1)

    model = r["model"]["id"]
    say(f"\033[1mPerseus\033[0m · {model} on {r['hardware']['summary']} · TP={r['vllm']['tensor_parallel_size']}, "
        f"{r['vllm']['weights']}, context {r['vllm']['max_model_len']:,}")
    say(f"  data directory: {paths.data}")
    print_selftest(rep)
    if not rep.passed:
        run.clear()
        die("the web gateway failed its refusal checks; Perseus won't start (fails closed)",
            *[f"{c.name}: {c.detail}" for c in rep.failures])
    if r["profile"]["small_gpu"]:
        say(f"  Small-GPU profile: on — {r['profile']['small_gpu_reason']}")
    for w in r.get("notes", {}).get("warnings", []):
        say(f"  ! {w}")

    # 2. weights --------------------------------------------------------------
    t_wait = time.monotonic()
    try:
        wait_for_download(r, paths)
    except SystemExit:
        run.clear()
        raise
    if time.monotonic() - t_wait > 5:
        timing.record("wait_for_download", time.monotonic() - t_wait)

    # 3. processes ------------------------------------------------------------
    last_state: dict[str, str] = {}

    def on_state(name, m):
        try:
            _report_state(name, m)
        finally:
            last_state[name] = m.state

    def _report_state(name, m):
        if m.proc is not None and m.state in ("starting", "restarting"):
            run.pid(name.lower(), m.proc.pid)
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
    search = SearchClient(search_port, engines=list(r["search"]["engines"]))
    sx_cmd, sx_env = searxng_command(paths)
    have_searxng = Path(sx_cmd[0]).exists()
    if have_searxng:
        sup.add(ProcessSpec("SearXNG", sx_cmd, {**os.environ, **sx_env}, logs / "searxng.log", search.healthy,
                            cwd=paths.searxng, ready_timeout_s=90, max_restarts=max_restarts))
    sup.add(ProcessSpec("vLLM", build_vllm_cmd(r, vllm_port, args.fast_start), vllm_env(key, paths.run),
                        logs / "vllm.log", lambda: vllm_healthy(vllm_port, key), cwd=paths.run,
                        max_restarts=max_restarts, explain=lambda log: explain_crash(log, r)))
    try:
        if have_searxng:
            sup.start("SearXNG")
        sup.start("vLLM")
        say(f"  starting vLLM (log: {logs / 'vllm.log'})")
        t_load = time.monotonic()
        m = sup.wait_ready("vLLM", load_progress(r["vllm"].get("weight_gib_per_gpu", 0), logs / "vllm.log"))
        say("")
        if m.state != "up":
            die(f"vLLM didn't start: {m.reason}", *m.hints[1:], code=1)
        load_s = time.monotonic() - t_load
        timing.record("model_load", load_s)
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
        timing.record("first_token", ttft)
        age = timing.instance_age_seconds()
        if age is not None:
            timing.record("boot_to_first_token", age)
        say(f"  ✓ first token in {ttft:.2f}s" + (f" · {age / 60:.1f} min since the instance started" if age else ""))
        ok, detail = tool_call_check(llm, r["vllm"]["tool_call_parser"])
        if not ok:
            die(f"tool-call test: {detail}")
        say(f"  ✓ tool-call test: {detail}")
        say("\n" + timing.report())

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
            from perseus.console import Console
            console = Console(engine, depths, limits.get("research_depth", "standard"), logs, gateway,
                              loader.refresh, recorder, model_dir=Path(r["model"]["path"]))
            if args.ask:
                print("model › ", end="", flush=True)
                console.ask(args.ask)
                say(f"  saved {console.export()}")
            else:
                console.loop()
                if console.history:
                    say(f"  saved {console.export()}")
        else:
            serve_dashboard(r, paths, run, sup, llm, registry, engine.ctx, gateway, depths, loader, rep,
                            args.fast_start, vllm_port)
        if refused:
            say(f"  ! socket guard refused {len(refused)} connection(s) outside the gateway: {', '.join(refused[:5])}")
    except KeyboardInterrupt:
        pass
    finally:
        say("  stopping…")
        sup.stop_all()
        run.clear()


def serve_dashboard(r: dict, paths: Paths, run: RunFiles, sup, llm, registry, ctx, gateway, depths, loader,
                    selftest_report, fast_start: bool, vllm_port: int) -> None:
    """Serve the dashboard until Ctrl-C (or until vLLM fails for good)."""
    from perseus.web.dashboard import Dashboard
    from perseus.web.server import Server

    v, prof = r["vllm"], r["profile"]
    model_dir = Path(r["model"]["path"])
    info = {"model": r["model"]["id"], "hardware": r["hardware"]["summary"], "precision": precision_label(v),
            "context": int(v["max_model_len"]), "capacity": int(v.get("context_capacity") or v["max_model_len"]),
            "max_positions": int(r["model"].get("max_positions") or v["max_model_len"]),
            "thinks": bool(r["model"].get("thinks")), "reasoning_effort": v.get("reasoning_parser") == "openai_gptoss",
            "small_gpu": bool(prof.get("small_gpu")), "tensor_parallel": int(v["tensor_parallel_size"])}

    def restart_model(tokens: int) -> None:
        r["vllm"]["max_model_len"] = tokens
        sup.restart("vLLM", build_vllm_cmd(r, vllm_port, fast_start))
        m = sup.wait_ready("vLLM")
        if m.state != "up":
            raise RuntimeError(m.reason or "vLLM didn't come back")

    def raw_prompt(messages, tools):
        from perseus.model.prompt_view import render_prompt
        return render_prompt(model_dir, messages, tools)

    def status():
        return {name: {"state": st["state"], "restarts": st["restarts"], **({"reason": st["reason"]} if st.get("reason") else {})}
                for name, st in sup.status().items()}

    engine_settings = {"tool_result_tokens": prof.get("tool_result_tokens") or None,
                       "max_context": int(v["max_model_len"]), "small_gpu": bool(prof.get("small_gpu"))}
    dash = Dashboard(info=info, llm=llm, registry=registry, ctx=ctx, depths=depths, engine_settings=engine_settings,
                     engine_list=list(r["search"]["engines"]), status=status, restart_model=restart_model,
                     raw_prompt=raw_prompt, refresh_tools=loader.refresh, gateway=gateway)
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
    url = server.url(_public_ip()) if srv.get("public") else server.url()
    link_file = run.secret("dashboard.url", url)
    say("")
    if srv.get("public"):
        say(f"  ✓ dashboard (public): {url}")
        say("  ! the dashboard is reachable from the internet; anyone with this link can use the model")
    else:
        say(f"  ✓ dashboard: {url}")
        say(f"    from your computer: ssh -N -L {port}:127.0.0.1:{port} <user>@<this box>, then open the link")
    say(f"    the link is also in {link_file} · Ctrl-C stops Perseus.")
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
    from perseus.engine.session import replay

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
