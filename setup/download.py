"""Download exactly the planned files with the Xet parallel downloader.

    python3 -m setup.download [--config Config]     (preflight.sh runs this as its last step)

Reads the plan `check` wrote to <data>/run/download-plan.json and fetches into
<data>/weights/<org>/<model>/. Progress (bytes, MB/s, ETA) goes to the terminal
and to <data>/run/download-status.json, which perseus.py reads while it waits.
A marker file is written only after every file is present at its expected size;
then the HF token is removed from Hugging Face's caches.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

GB = 1000 ** 3
from perseus.config import WEIGHTS_MARKER as MARKER  # noqa: E402


class DownloadError(Exception):
    pass


def dir_bytes(path: Path) -> int:
    """Bytes on disk under path, including in-progress chunks in .cache/."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def is_complete(dest: Path, sha: str, files: dict[str, int]) -> bool:
    try:
        marker = json.loads((dest / MARKER).read_text())
    except (OSError, ValueError):
        return False
    return marker.get("sha") == sha and all(
        (dest / f).is_file() and (dest / f).stat().st_size == size for f, size in files.items())


def verify(dest: Path, files: dict[str, int]) -> list[str]:
    bad = []
    for f, size in files.items():
        p = dest / f
        if not p.is_file():
            bad.append(f"{f}: missing")
        elif p.stat().st_size != size:
            bad.append(f"{f}: {p.stat().st_size} bytes, expected {size}")
    return bad


def write_status(path: Path, **fields) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**fields, "updated": time.time()}))
    tmp.replace(path)


class Paused(Exception):
    """Ctrl-C during the download: finished files are kept, the next run resumes."""


def _partial_name(rel: str, names: list[str]) -> str | None:
    """The planned file an in-progress download belongs to (the longest planned name it starts with).

    huggingface_hub 2.3 writes <file>.<etag>.<random8>.incomplete, older versions
    <file>.<etag>.incomplete, so match on the name prefix rather than a fixed suffix.
    """
    best = None
    for n in names:
        if rel.startswith(n + ".") and (best is None or len(n) > len(best)):
            best = n
    return best


def file_progress(dest: Path, files: dict[str, int]) -> list[dict]:
    """Per-file state: done (final file at full size), active (bytes in flight) or waiting.

    huggingface_hub downloads into <dest>/.cache/huggingface/download/<file>.….incomplete
    and moves the file into place when it finishes.
    """
    partial: dict[str, int] = {}
    names = list(files)
    dl_root = dest / ".cache" / "huggingface" / "download"
    if dl_root.is_dir():
        for root, _, found in os.walk(dl_root):
            for n in found:
                if n.endswith(".incomplete"):
                    full = Path(root) / n
                    name = _partial_name(str(full.relative_to(dl_root)), names)
                    if name is None:
                        continue
                    try:
                        partial[name] = max(partial.get(name, 0), full.stat().st_size)
                    except OSError:
                        pass
    out = []
    for name, size in sorted(files.items()):
        p = dest / name
        try:
            on_disk = p.stat().st_size if p.is_file() else 0
        except OSError:
            on_disk = 0
        if on_disk == size:
            out.append({"name": name, "size": size, "have": size, "state": "done"})
        else:
            have = min(partial.get(name, 0), size)
            out.append({"name": name, "size": size, "have": have, "state": "active" if have else "waiting"})
    return out


class HubCounters:
    """Live byte counts from huggingface_hub itself, independent of what is on disk yet.

    snapshot_download(tqdm_class=...) feeds every file into two shared bars:
    "Downloading bytes" (received from the network) and "Reconstructing…" (written
    to disk; Xet buffers chunks, so this lags), plus "Fetching N files" (files
    finished). `tqdm_class()` returns a silent stand-in that records them here.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.received = 0      # network bytes (Xet dedups/compresses, so can be < file bytes)
        self.written = 0       # file bytes reconstructed on disk this run
        self.files_done = 0

    def snapshot(self) -> tuple[int, int, int]:
        with self.lock:
            return self.received, self.written, self.files_done

    def tqdm_class(self):
        counters = self

        class Bar:
            def __init__(self, *args, desc: str = "", total=None, initial=0, **kwargs):
                d = (desc or "").removeprefix("[dry-run] ")
                self.kind = ("received" if d.startswith("Downloading bytes") else
                             "written" if d.startswith("Reconstructing") else
                             "files_done" if d.startswith("Fetching") else None)
                self.n, self.total, self.desc = initial or 0, total, desc
                self.format_dict = {"rate": None}
                if initial:
                    self.update(0)

            def update(self, n=1):
                n = int(n or 0)
                with counters.lock:
                    self.n += n
                    if self.kind:
                        setattr(counters, self.kind, self.n)
                return True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def refresh(self, *a, **k): pass
            def close(self): pass
            def set_description(self, *a, **k): pass
            def set_description_str(self, *a, **k): pass
            def set_postfix_str(self, *a, **k): pass
            def set_postfix(self, *a, **k): pass

        return Bar


def _rate(samples: list[tuple[float, int]]) -> float:
    if len(samples) > 1 and samples[-1][0] > samples[0][0]:
        return max(0.0, (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0]))
    return 0.0


def _disk(dest: Path) -> tuple[int | None, str]:
    try:
        st = os.statvfs(dest)
        free = st.f_bavail * st.f_frsize
    except OSError:
        return None, ""
    mount = dest.resolve()
    while not os.path.ismount(mount) and mount != mount.parent:
        mount = mount.parent
    return free, str(mount)


def download(repo: str, sha: str, files: dict[str, int], dest: Path, token: str | None,
             status_path: Path, warn_minutes: float, log=print, panel=None, tick: float = 0.25) -> float:
    """Fetch `files` into `dest`, drawing `panel` (perseus.progress.LivePanel). Returns seconds taken."""
    from huggingface_hub import snapshot_download  # HF_XET_HIGH_PERFORMANCE set by preflight.sh

    dest.mkdir(parents=True, exist_ok=True)
    total = sum(files.values())
    start_files = file_progress(dest, files)
    start_bytes = sum(f["have"] for f in start_files if f["state"] == "done")
    state = {"error": None, "done": False}
    hub = HubCounters()

    def run():
        try:
            snapshot_download(repo_id=repo, revision=sha, allow_patterns=sorted(files), local_dir=str(dest),
                              token=token, max_workers=min(32, (os.cpu_count() or 8) * 2),
                              tqdm_class=hub.tqdm_class())
        except Exception as e:  # reported after the monitor loop
            state["error"] = e
        finally:
            state["done"] = True

    base = {"repo": repo, "sha": sha, "dest": str(dest), "total": total}
    t0 = time.monotonic()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    samples: list[tuple[float, int]] = []   # (time, bytes): current speed over the last ~5 s
    net: list[tuple[float, int]] = []       # (time, network bytes): speed while writes lag behind
    history: list[float] = []               # one speed per second, for the sparkline
    last_hist = t0
    warning = None
    snap = {}
    last_write = 0.0
    try:
        while not state["done"]:
            time.sleep(tick)
            now = time.monotonic()
            elapsed = now - t0
            fp = file_progress(dest, files)
            received, written, files_done = hub.snapshot()
            # Whichever source is furthest along: files on disk, or huggingface_hub's own count.
            have = min(max(sum(f["have"] for f in fp), start_bytes + written), total)
            samples.append((now, have))
            samples = [x for x in samples if now - x[0] <= 5.0]
            net.append((now, received))
            net = [x for x in net if now - x[0] <= 5.0]
            rate = _rate(samples)
            if rate <= 0:                       # nothing written yet in this window: use network speed
                rate = _rate(net)
            done_bytes = max(have - start_bytes, received if have <= start_bytes else 0)
            rate_avg = done_bytes / elapsed if elapsed > 0 else 0.0
            eta = (total - have) / rate if rate > 0 else ((total - have) / rate_avg if rate_avg > 0 else None)
            if now - last_hist >= 1.0:
                last_hist = now
                history = (history + [rate])[-60:]
            if warning is None and elapsed >= 30 and eta is not None and (elapsed + eta) / 60 > warn_minutes:
                warning = (f"projected download time {(elapsed + eta) / 60:.0f} min is over warn_download_minutes "
                           f"({warn_minutes:g}); consider an FP8 repo or a faster region/provider")
                if not (panel and panel.live):
                    log(f"  ! {warning}")
            free, mount = _disk(dest)
            snap = {**base, "state": "downloading", "bytes": have, "rate": rate, "rate_avg": rate_avg, "eta": eta,
                    "elapsed": elapsed, "files": fp, "history": history, "disk_free": free, "disk_mount": mount,
                    "warning": warning, "received": received, "files_done": files_done}
            if panel:
                panel.update(snap)
            if now - last_write >= 1.0:   # perseus.py reads this while it waits
                last_write = now
                write_status(status_path, **snap)
    except KeyboardInterrupt:
        snap = {**(snap or base), "state": "paused", "warning": None}
        write_status(status_path, **snap)
        if panel:
            panel.close(snap)
        raise Paused(f"paused at {snap.get('bytes', 0) / GB:.1f} of {total / GB:.1f} GB")
    worker.join()
    seconds = time.monotonic() - t0
    if state["error"] is not None:
        snap = {**(snap or base), "state": "failed", "error": type(state["error"]).__name__}
        write_status(status_path, **snap)
        if panel:
            panel.close(snap)
        raise DownloadError(f"download failed: {type(state['error']).__name__}: {state['error']}")

    fp = file_progress(dest, files)
    verifying = {**(snap or base), "state": "verifying", "bytes": total, "files": fp}
    write_status(status_path, **verifying)
    if panel:
        panel.update(verifying)
    bad = verify(dest, files)
    if bad:
        write_status(status_path, **{**verifying, "state": "failed", "error": "verify"})
        if panel:
            panel.close({**verifying, "state": "failed", "error": "sizes don't match the Hub"})
        raise DownloadError("downloaded files don't match the Hub: " + "; ".join(bad[:5]))
    shutil.rmtree(dest / ".cache", ignore_errors=True)  # local_dir metadata; not needed to serve
    (dest / MARKER).write_text(json.dumps({"repo": repo, "sha": sha, "bytes": total,
                                           "seconds": round(seconds, 1), "files": len(files)}))
    done = {**verifying, "state": "done", "elapsed": seconds,
            "rate_avg": (total - start_bytes) / seconds if seconds > 0 else 0, "warning": None, "disk_free": None}
    write_status(status_path, **done)
    if panel:
        panel.close(done)
    return seconds


def scrub_token(token: str, hf_home: Path) -> list[str]:
    """Remove the token from Hugging Face's on-disk caches once the download is done.

    Perseus never logs in (the token is passed per call), so normally there's
    nothing to remove; this deletes any token file that holds this exact token.
    """
    import configparser

    removed: list[str] = []
    if not token:
        return removed
    for home in dict.fromkeys([hf_home, Path.home() / ".cache" / "huggingface"]):
        tok = home / "token"
        try:
            if tok.read_text().strip() == token:
                tok.unlink()
                removed.append(str(tok))
        except OSError:
            pass
        stored = home / "stored_tokens"  # INI: one [name] section per saved token
        try:
            ini = configparser.ConfigParser()
            ini.read_string(stored.read_text())
        except (OSError, configparser.Error):
            continue
        hits = [s for s in ini.sections() if ini[s].get("hf_token", "").strip() == token]
        if not hits:
            continue
        for s in hits:
            ini.remove_section(s)
        if ini.sections():
            with stored.open("w") as f:
                ini.write(f)
        else:
            stored.unlink()
        removed.append(f"{stored} ({', '.join(hits)})")
    return removed


def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    from perseus import timing
    from perseus.config import ConfigError, default_config_path, find_paths, load, split_overrides

    argv = list(sys.argv[1:] if argv is None else argv)
    rest, overrides = split_overrides(argv)
    ap = argparse.ArgumentParser(prog="python3 -m setup.download")
    ap.add_argument("--config", type=Path, default=default_config_path())
    args = ap.parse_args(rest)
    config = args.config.expanduser().absolute()

    def fail(msg, *hints):
        print(f"  ✗ {msg}", file=sys.stderr)
        for h in hints:
            print(f"      → {h}", file=sys.stderr)
        sys.exit(8)

    print("\nModel download", flush=True)
    try:
        paths = find_paths(config)
        cfg = load(config, overrides)
    except ConfigError as e:
        fail("; ".join(e.problems))
    timing.configure(paths)
    plan = json.loads((paths.run / "download-plan.json").read_text())
    status = paths.run / "download-status.json"
    dest, files = Path(plan["dest"]), {k: int(v) for k, v in plan["files"].items()}
    token = cfg["secrets"]["hf_token"] or None
    os.environ["HF_HOME"] = str(paths.run / "hf")
    total = sum(files.values())
    if is_complete(dest, plan["sha"], files):
        print(f"  ✓ already downloaded and verified: {dest}")
        write_status(status, state="done", bytes=total, total=total)
    else:
        from perseus.progress import LivePanel, open_terminal
        print(f"  {plan['repo']}@{plan['sha'][:12]} → {dest} ({total / 1e9:.1f} GB, {len(files)} files)", flush=True)
        panel = LivePanel(open_terminal())
        try:
            secs = download(plan["repo"], plan["sha"], files, dest, token, status,
                            cfg["limits"]["warn_download_minutes"], panel=panel)
        except Paused as e:
            print(f"\n  ■ download {e}; finished files are kept", flush=True)
            print("      → run bash preflight.sh again to resume", flush=True)
            sys.exit(130)
        except DownloadError as e:
            fail(str(e), "re-run `bash preflight.sh`: finished files are kept and resumed")
        finally:
            panel.close()
        timing.record("download", secs, bytes=total)
        print(f"  ✓ downloaded and verified in {secs / 60:.1f} min ({total / secs / 1e6:.0f} MB/s average)")
    removed = scrub_token(token or "", paths.run / "hf")
    print("  ✓ HF token removed from Hugging Face caches"
          + (f" ({', '.join(removed)})" if removed else " (none stored: Perseus never logs in)"))


if __name__ == "__main__":
    main()
