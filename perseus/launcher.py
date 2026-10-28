"""perseus.py, Phase 0: wait for the download, start vLLM, prove it answers.

Reads only perseus.resolved.toml (no secrets). vLLM runs on 127.0.0.1 with a
random per-boot API key passed through its environment, offline (no Hub
access, no token), in its own process group so Ctrl-C stops everything.

Later phases add the supervisor, the Perseus server and the dashboard; this
stub ends in a terminal chat so the Phase 0 gate (serve a model on a fresh
box, measure boot-to-first-token) can be checked by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from . import LOG_DIR, RESOLVED_PATH, ROOT, STATE_DIR
from .download import MARKER
from .timing import instance_age_seconds, record, report

STATUS_PATH = STATE_DIR / "download-status.json"
VLLM_LOG = LOG_DIR / "vllm.log"
SECRET_ENV = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_API_TOKEN")

TOOL_TEST = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current UTC time.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def say(msg: str) -> None:
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


# --------------------------------------------------------------------------- vLLM command

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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- waiting

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


def tail(path: Path, n: int = 40) -> str:
    try:
        with path.open(errors="replace") as f:
            return "".join(deque(f, n))
    except OSError:
        return "(no log)"


OOM_MARKERS = ("CUDA out of memory", "OutOfMemoryError", "No available memory for the cache blocks",
               "insufficient memory", "larger than the available KV cache")


def explain_crash(log_text: str, r: dict) -> list[str]:
    if any(m in log_text for m in OOM_MARKERS):
        return [f"vLLM ran out of GPU memory at context {r['vllm']['max_model_len']:,}",
                "set [model] max_context lower in perseus.toml, or smaller weights (quantization), "
                "then re-run bash preflight.sh --check"]
    if "parser" in log_text.lower() and "invalid choice" in log_text.lower():
        return ["vLLM rejected the tool or reasoning parser name",
                "set [model] tool_parser / reasoning_parser (see `vllm serve --help`)"]
    return ["see the log above and logs/vllm.log"]


def http(port: int, key: str, path: str, body: dict | None = None, timeout: float = 120):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def wait_healthy(proc: subprocess.Popen, port: int, key: str, r: dict, expected_gib: float) -> float:
    t0 = time.monotonic()
    while True:
        if proc.poll() is not None:
            log = tail(VLLM_LOG)
            print("\n" + log, file=sys.stderr)
            die(f"vLLM exited with code {proc.returncode} while loading", *explain_crash(log, r))
        try:
            with http(port, key, "/health", timeout=3) as resp:
                if resp.status == 200:
                    say("")
                    return time.monotonic() - t0
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass
        used = gpu_memory_used()
        mem = ""
        if used and expected_gib:
            pct = min(100, 100 * (min(used) / 2 ** 30) / expected_gib)
            mem = f"  GPU memory {min(used) / 2 ** 30:.0f}/{expected_gib:.0f} GiB weights ({pct:.0f}%)"
        line = last_log_line(VLLM_LOG)
        print(f"\r\033[K  loading {time.monotonic() - t0:5.0f}s{mem}  {line[:70]}", end="", flush=True)
        time.sleep(2)


# --------------------------------------------------------------------------- checks

def stream_chat(port: int, key: str, model: str, messages: list, max_tokens: int = 1024, on_text=None):
    """Stream a chat completion. Returns (text, seconds to first token)."""
    t0 = time.monotonic()
    first = None
    out = []
    body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens}
    with http(port, key, "/v1/chat/completions", body, timeout=600) as resp:
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            delta = (json.loads(data).get("choices") or [{}])[0].get("delta", {})
            piece = delta.get("content") or ""
            thought = delta.get("reasoning_content") or delta.get("reasoning") or ""
            if (piece or thought) and first is None:
                first = time.monotonic() - t0
            if on_text:
                if thought:
                    on_text(thought, True)
                if piece:
                    on_text(piece, False)
            out.append(piece)
    return "".join(out), first if first is not None else time.monotonic() - t0


def tool_call_check(port: int, key: str, model: str, parser: str) -> None:
    """Fail closed: a model whose tool calls come back as plain text can't use any tool."""
    body = {"model": model, "max_tokens": 512, "tools": [TOOL_TEST], "tool_choice": "auto",
            "messages": [{"role": "user", "content": "What time is it in UTC? Use the get_time tool."}]}
    try:
        with http(port, key, "/v1/chat/completions", body, timeout=300) as resp:
            msg = json.loads(resp.read())["choices"][0]["message"]
    except (urllib.error.URLError, KeyError, ValueError) as e:
        die(f"tool-call test request failed: {e}")
    calls = msg.get("tool_calls") or []
    if not calls or calls[0].get("function", {}).get("name") != "get_time":
        snippet = (msg.get("content") or "").strip().replace("\n", " ")[:160]
        die(f"tool-call test: the model's call wasn't parsed by the {parser!r} parser",
            f"model replied: {snippet or '(empty)'}",
            "set [model] tool_parser in perseus.toml to the right parser for this family")
    say(f"  ✓ tool-call test: parsed {calls[0]['function']['name']}() with the {parser} parser")


def repl(port: int, key: str, model: str) -> None:
    say("\nTerminal chat (Phase 0 test harness). /exit or Ctrl-D to stop Perseus, /reset to clear.\n")
    history: list[dict] = []
    state = {"thinking": False}

    def show(text: str, is_thought: bool):
        if is_thought and not state["thinking"]:
            state["thinking"] = True
            print("\033[2m", end="")
        if not is_thought and state["thinking"]:
            state["thinking"] = False
            print("\033[0m\n", end="")
        print(text, end="", flush=True)

    while True:
        try:
            q = input("you › ").strip()
        except EOFError:
            say("")
            return
        if q in ("/exit", "/quit"):
            return
        if q == "/reset":
            history.clear()
            say("  (cleared)")
            continue
        if not q:
            continue
        history.append({"role": "user", "content": q})
        print("model › ", end="", flush=True)
        try:
            text, ttft = stream_chat(port, key, model, history, max_tokens=4096, on_text=show)
        except (urllib.error.URLError, OSError) as e:
            say(f"\n  ✗ request failed: {e}")
            history.pop()
            continue
        if state["thinking"]:
            state["thinking"] = False
            print("\033[0m", end="")
        say(f"\n\033[2m  (first token {ttft:.2f}s)\033[0m")
        history.append({"role": "assistant", "content": text})


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python perseus.py")
    p.add_argument("--fast-start", action="store_true", help="skip CUDA graph capture (faster load)")
    p.add_argument("--no-repl", action="store_true", help="serve until Ctrl-C without the terminal chat")
    p.add_argument("--timings", action="store_true", help="print the latest boot timings and exit")
    p.add_argument("--print-cmd", action="store_true", help="print the vLLM command and exit")
    args = p.parse_args(argv)

    if args.timings:
        say(report())
        return
    r = load_resolved()
    port = free_port()
    if args.print_cmd:
        say(" ".join(build_vllm_cmd(r, port, args.fast_start)))
        return

    model = r["model"]["id"]
    say(f"\033[1mPerseus\033[0m · {model} on {r['hardware']['summary']} · TP={r['vllm']['tensor_parallel_size']}, "
        f"{r['vllm']['weights']}, context {r['vllm']['max_model_len']:,}")
    if r["profile"]["small_gpu"]:
        say(f"  Small-GPU profile: on — {r['profile']['small_gpu_reason']}")
    for w in r.get("notes", {}).get("warnings", []):
        say(f"  ! {w}")

    t_wait = time.monotonic()
    wait_for_download(r)
    waited = time.monotonic() - t_wait
    if waited > 5:
        record("wait_for_download", waited)

    key = secrets.token_urlsafe(32)  # per boot, held only by this process and vLLM
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = build_vllm_cmd(r, port, args.fast_start)
    with VLLM_LOG.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=vllm_env(key),
                                cwd=ROOT, start_new_session=True)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # unwinds into the finally below
    try:
        say(f"  starting vLLM (log: {VLLM_LOG.relative_to(ROOT)})")
        load_s = wait_healthy(proc, port, key, r, r["vllm"].get("weight_gib_per_gpu", 0))
        record("model_load", load_s)
        say(f"  ✓ vLLM ready in {load_s:.0f}s")

        _, ttft = stream_chat(port, key, model, [{"role": "user", "content": "Reply with one word: ready"}],
                              max_tokens=256)
        record("first_token", ttft)
        age = instance_age_seconds()
        if age is not None:
            record("boot_to_first_token", age)
        say(f"  ✓ first token in {ttft:.2f}s"
            + (f" · {age / 60:.1f} min since the instance started" if age is not None else ""))

        tool_call_check(port, key, model, r["vllm"]["tool_call_parser"])
        say("\n" + report())
        if args.no_repl:
            say("\nServing. Ctrl-C to stop.")
            proc.wait()
            die(f"vLLM exited with code {proc.returncode}", *explain_crash(tail(VLLM_LOG), r))
        repl(port, key, model)
    except KeyboardInterrupt:
        pass
    finally:
        stop_vllm(proc)


def stop_vllm(proc: subprocess.Popen) -> None:
    """Stop vLLM and its workers (one process group) on every exit path."""
    if proc.poll() is not None:
        return
    say("\n  stopping vLLM…")
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
