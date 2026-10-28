"""Preflight stages, called by preflight.sh in this order:

  check     config, driver, hardware, Hub metadata, sizing, disk -> perseus.resolved.toml
            (seconds; nothing installed or downloaded yet)
  smoke     after vLLM installs: torch sees every GPU and can allocate on each
  searxng   write SearXNG's settings.yml for this box
  download  fetch the planned files, verify sizes, remove the HF token from caches

Only this module and preflight.sh ever read [secrets].
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import CONFIG_PATH, LOG_DIR, RESOLVED_PATH, ROOT, STATE_DIR, tomlw
from .config import Config, ConfigError, load, split_overrides
from .hardware import GIB, HardwareError, detect, driver_ok, pick_model_dir
from .hub import HubClient, HubError, inspect
from .parsers import ParserError, pick
from .sizing import ModelMeta, SizingError, resolve

PLAN_PATH = STATE_DIR / "download-plan.json"
STATUS_PATH = STATE_DIR / "download-status.json"
DRIVER_MIN = ROOT / "requirements" / "driver.min"

EXIT = {"config": 2, "hardware": 3, "hub": 4, "sizing": 5, "disk": 6, "gpu": 7, "download": 8}


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    print(f"  ! {msg}")


def fail(kind: str, msg: str, hints: list[str] | tuple = ()) -> "NoReturn":  # noqa: F821
    print(f"  ✗ {msg}", file=sys.stderr)
    for h in hints:
        if h:
            print(f"      → {h}", file=sys.stderr)
    sys.exit(EXIT[kind])


def section(title: str) -> None:
    print(f"\n{title}")


def read_driver_min() -> str:
    for line in DRIVER_MIN.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    return "0"


# --------------------------------------------------------------------------- check

def cmd_check(args, overrides) -> None:
    t0 = time.monotonic()
    section("Config")
    try:
        cfg = load(CONFIG_PATH, overrides)
    except ConfigError as e:
        for p in e.problems:
            print(f"  ✗ {p}", file=sys.stderr)
        sys.exit(EXIT["config"])
    m = cfg["model"]
    ok(f"perseus.toml valid: {m['id']}@{m['revision']}")
    for w in cfg.warnings:
        warn(w)
    if CONFIG_PATH.exists() and CONFIG_PATH.stat().st_mode & 0o077:
        os.chmod(CONFIG_PATH, 0o600)
        warn("perseus.toml was readable by other users; set to 600")

    section("Hardware")
    try:
        hw = detect(ROOT)
    except HardwareError as e:
        fail("hardware", str(e))
    if not hw.gpus:
        fail("hardware", "no NVIDIA GPU visible", ["one GPU is the floor; check the instance type"])
    minimum = read_driver_min()
    if not driver_ok(hw.driver_version, minimum):
        fail("hardware", f"NVIDIA driver {hw.driver_version} is older than {minimum}, the minimum for the "
                         "pinned vLLM", ["pick a provider image with a newer driver, or on a VM run "
                                         "preflight.sh --install-driver"])
    ok(f"{hw.summary()}, compute {hw.min_compute_cap[0]}.{hw.min_compute_cap[1]}, "
       f"driver {hw.driver_version} (CUDA ≤ {hw.cuda_version or '?'})")
    if len({g.vram_bytes for g in hw.gpus}) > 1:
        warn("GPUs have different VRAM; sizing uses the smallest")
    ok(f"{hw.cpu_cores} CPU cores, {hw.ram_bytes / GIB:.0f} GiB RAM, interconnect: {hw.interconnect}")
    if hw.interconnect == "unknown" and len(hw.gpus) > 1:
        warn("couldn't read GPU topology (nvidia-smi topo -m)")

    section("Model (Hub metadata only, no weights)")
    token = cfg["secrets"]["hf_token"] or None
    if args.meta_file:
        insp = _inspection_from_file(Path(args.meta_file))
        ok(f"metadata from {args.meta_file} (offline dry run)")
    else:
        client = HubClient(token)
        try:
            if token:
                ok(f"HF token valid (user {client.whoami()})")
            insp = inspect(client, m["id"], m["revision"], m["trust_remote_code"])
        except HubError as e:
            fail("hub", str(e), [e.hint])
    meta = insp.meta
    total = sum(insp.download.values())
    ok(f"{meta.repo_id}@{meta.revision_sha[:12]}: {meta.model_type or '?'}, "
       f"{_params(meta)} params, {meta.native_dtype}{' (' + meta.quant_method + ')' if meta.quant_method else ''}, "
       f"{len(insp.download)} files, {total / 1e9:.1f} GB to download")
    ok(f"{meta.num_layers} layers, {meta.num_attention_heads} heads / {meta.num_kv_heads} KV heads, "
       f"context limit {meta.max_positions:,}" + ("; chat template thinks" if meta.thinks else ""))
    for w in insp.warnings:
        warn(w)

    section("Sizing")
    try:
        res = resolve(hw, meta, m)
    except SizingError as e:
        fail("sizing", str(e), e.suggestions)
    try:
        tool_parser, reasoning_parser = pick(meta, m["tool_parser"], m["reasoning_parser"])
    except ParserError as e:
        fail("sizing", str(e))
    ok(f"TP={res.tensor_parallel}, weights {res.weights} ({res.weight_gib_per_gpu} GiB/GPU), "
       f"KV cache {res.kv_gib_total} GiB {'fp8' if res.kv_cache_dtype == 'fp8' else 'bf16'}, "
       f"context {res.max_context:,} tokens")
    ok(f"Small-GPU profile: {'on' if res.small_gpu else 'off'} — {res.small_gpu_reason}")
    if res.small_gpu:
        lk = res.research_lookups
        ok(f"tool results capped at {res.tool_result_tokens:,} tokens; research depth on this GPU: "
           f"Quick {lk['quick']}, Standard {lk['standard']}, Deep {lk['deep']} lookups")
    ok(f"parsers: tool-call {tool_parser}" + (f", reasoning {reasoning_parser}" if reasoning_parser else ""))
    for w in res.warnings:
        warn(w)

    section("Disk")
    need = int(total * 1.05) + 5 * GIB
    try:
        if cfg["storage"]["model_dir"] != "auto":
            base = Path(cfg["storage"]["model_dir"])
            base.mkdir(parents=True, exist_ok=True)
            free = os.statvfs(base).f_bavail * os.statvfs(base).f_frsize
            if free < need:
                raise HardwareError(f"{base} has {free / GIB:.0f} GiB free; need {need / GIB:.0f} GiB")
            model_dir = base / _slug(meta.repo_id)
        else:
            base, disk = pick_model_dir(hw.disks, need, ROOT)
            model_dir = base / _slug(meta.repo_id)
            kind = "NVMe" if disk.nvme else ("disk" if disk.nvme is False else "volume")
            ok(f"{disk.mount}: {disk.free_bytes / GIB:.0f} GiB free ({kind}); need {need / GIB:.0f} GiB")
    except HardwareError as e:
        fail("disk", str(e))
    ok(f"model directory: {model_dir}")

    _write_resolved(cfg, hw, meta, res, tool_parser, reasoning_parser, model_dir)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps({"repo": meta.repo_id, "sha": meta.revision_sha, "dest": str(model_dir),
                                     "files": insp.download}, indent=1))
    print(f"\nChecks passed in {time.monotonic() - t0:.1f}s → {RESOLVED_PATH.name}")


def _params(meta: ModelMeta) -> str:
    p = meta.params_total
    if not p:
        return "?"
    return f"{p / 1e9:.0f}B" if p >= 1e9 else f"{p / 1e6:.0f}M"


def _slug(repo: str) -> str:
    return repo.replace("/", "--")


def _write_resolved(cfg: Config, hw, meta: ModelMeta, res, tool_parser, reasoning_parser, model_dir: Path) -> None:
    sections = {
        "model": {"id": meta.repo_id, "revision_sha": meta.revision_sha, "path": str(model_dir),
                  "model_type": meta.model_type, "max_positions": meta.max_positions, "thinks": meta.thinks},
        "vllm": {"tensor_parallel_size": res.tensor_parallel, "weights": res.weights,
                 "quantization": res.quantization or "", "dtype": res.dtype,
                 "kv_cache_dtype": res.kv_cache_dtype, "max_model_len": res.max_context,
                 "gpu_memory_utilization": res.gpu_memory_utilization, "cpu_offload_gb": res.cpu_offload_gb,
                 "tool_call_parser": tool_parser, "reasoning_parser": reasoning_parser or "",
                 "trust_remote_code": cfg["model"]["trust_remote_code"],
                 "weight_gib_per_gpu": res.weight_gib_per_gpu, "kv_gib_total": res.kv_gib_total},
        "profile": {"small_gpu": res.small_gpu, "small_gpu_reason": res.small_gpu_reason,
                    "tool_result_tokens": res.tool_result_tokens or 0,
                    **{f"lookups_{k}": v for k, v in res.research_lookups.items()}},
        "hardware": {"summary": hw.summary(), "gpus": len(hw.gpus), "vram_gib": round(hw.min_vram_bytes / GIB, 1),
                     "compute_cap": f"{hw.min_compute_cap[0]}.{hw.min_compute_cap[1]}",
                     "driver": hw.driver_version, "cuda": hw.cuda_version, "interconnect": hw.interconnect,
                     "cpu_cores": hw.cpu_cores, "ram_gib": round(hw.ram_bytes / GIB)},
        **{k: v for k, v in cfg.public().items() if k not in ("model", "storage")},
        "notes": {"warnings": res.warnings},
    }
    sections["model"].update({"requested_" + k: v for k, v in cfg["model"].items() if k != "id"})
    header = (f"Written by preflight at {datetime.now(timezone.utc).isoformat(timespec='seconds')}.\n"
              "Generated file: edit perseus.toml and re-run `bash preflight.sh`, not this file.\n"
              "Holds no secrets; perseus.py reads only this file.")
    RESOLVED_PATH.write_text(tomlw.dumps(sections, header))


def _inspection_from_file(path: Path):
    """Offline dry run: a JSON file with ModelMeta fields plus `download` sizes."""
    from .hub import Inspection
    d = json.loads(path.read_text())
    download = d.pop("download")
    return Inspection(ModelMeta(**d), download, [], "", [])


# --------------------------------------------------------------------------- smoke

def cmd_smoke(args, overrides) -> None:
    section("GPU smoke test")
    try:
        import torch
    except ImportError:
        fail("gpu", "torch isn't installed in .venv; the vLLM install step didn't complete")
    expected = len(detect(ROOT).gpus)
    if not torch.cuda.is_available():
        fail("gpu", f"torch {torch.__version__} can't use CUDA on this driver",
             ["the driver is likely older than the CUDA runtime in the torch wheel; raise requirements/driver.min"])
    seen = torch.cuda.device_count()
    if seen != expected:
        fail("gpu", f"torch sees {seen} GPU(s), nvidia-smi sees {expected}", ["check CUDA_VISIBLE_DEVICES"])
    for i in range(seen):
        x = torch.ones(64 * 1024 * 1024, device=f"cuda:{i}")  # 256 MB
        assert float(x.sum()) == x.numel()
        del x
    torch.cuda.empty_cache()
    ok(f"torch {torch.__version__} (CUDA {torch.version.cuda}): {seen} GPU(s), allocation OK on each")


# --------------------------------------------------------------------------- searxng

SEARXNG_DIR = STATE_DIR / "searxng"


def cmd_searxng(args, overrides) -> None:
    import tomllib
    resolved = tomllib.loads(RESOLVED_PATH.read_text())
    port, engines = resolved["search"]["port"], resolved["search"]["engines"]
    template = (ROOT / "searxng" / "settings.template.yml").read_text()
    out = (template.replace("@PORT@", str(port))
                   .replace("@SECRET@", secrets.token_hex(32))
                   .replace("@ENGINES@", "\n".join(f"      - {e}" for e in engines)))
    SEARXNG_DIR.mkdir(parents=True, exist_ok=True)
    path = SEARXNG_DIR / "settings.yml"
    path.write_text(out)
    os.chmod(path, 0o600)
    ok(f"SearXNG settings: 127.0.0.1:{port}, engines {', '.join(engines)}")


# --------------------------------------------------------------------------- download

def cmd_download(args, overrides) -> None:
    from .download import DownloadError, download, is_complete, scrub_token, write_status
    from .timing import record
    section("Model download")
    plan = json.loads(PLAN_PATH.read_text())
    dest, files = Path(plan["dest"]), {k: int(v) for k, v in plan["files"].items()}
    cfg = load(CONFIG_PATH, overrides)
    token = cfg["secrets"]["hf_token"] or None
    if is_complete(dest, plan["sha"], files):
        ok(f"already downloaded and verified: {dest}")
        write_status(STATUS_PATH, state="done", bytes=sum(files.values()), total=sum(files.values()))
    else:
        print(f"  {plan['repo']}@{plan['sha'][:12]} → {dest} ({sum(files.values()) / 1e9:.1f} GB, "
              f"{len(files)} files)")
        try:
            secs = download(plan["repo"], plan["sha"], files, dest, token, STATUS_PATH,
                            cfg["limits"]["warn_download_minutes"])
        except DownloadError as e:
            fail("download", str(e), ["re-run `bash preflight.sh`: finished files are kept and resumed"])
        record("download", secs, bytes=sum(files.values()))
        ok(f"downloaded and verified in {secs / 60:.1f} min "
           f"({sum(files.values()) / secs / 1e6:.0f} MB/s average)")
    removed = scrub_token(token or "", Path(os.environ.get("HF_HOME", STATE_DIR / "hf")))
    ok("HF token removed from Hugging Face caches" + (f" ({', '.join(removed)})" if removed else
                                                     " (none stored: Perseus never logs in)"))


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        rest, overrides = split_overrides(argv)
    except ConfigError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(EXIT["config"])
    p = argparse.ArgumentParser(prog="python -m perseus.preflight")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--meta-file", help="offline dry run: model metadata JSON instead of the Hub")
    sub.add_parser("smoke")
    sub.add_parser("searxng")
    sub.add_parser("download")
    args = p.parse_args(rest)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    {"check": cmd_check, "smoke": cmd_smoke, "searxng": cmd_searxng, "download": cmd_download}[args.cmd](args, overrides)


if __name__ == "__main__":
    main()
