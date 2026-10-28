"""Download exactly the planned files with the Xet parallel downloader.

Progress (bytes, MB/s, ETA) goes to the terminal and to a status file that
perseus.py reads while it waits. A marker file is written only after every
file is present at its expected size.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

GB = 1000 ** 3
MARKER = ".perseus-complete.json"


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


def download(repo: str, sha: str, files: dict[str, int], dest: Path, token: str | None,
             status_path: Path, warn_minutes: float, log=print) -> float:
    """Fetch `files` into `dest`. Returns seconds taken."""
    from huggingface_hub import snapshot_download  # HF_XET_HIGH_PERFORMANCE set by preflight.sh

    dest.mkdir(parents=True, exist_ok=True)
    total = sum(files.values())
    start_bytes = sum((dest / f).stat().st_size for f in files if (dest / f).is_file()
                      and (dest / f).stat().st_size == files[f])
    state = {"error": None, "done": False}

    def run():
        try:
            snapshot_download(repo_id=repo, revision=sha, allow_patterns=sorted(files), local_dir=str(dest),
                              token=token, max_workers=min(32, (os.cpu_count() or 8) * 2))
        except Exception as e:  # reported after the monitor loop
            state["error"] = e
        finally:
            state["done"] = True

    t0 = time.monotonic()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    warned = False
    last_print = 0.0
    while not state["done"]:
        time.sleep(1)
        elapsed = time.monotonic() - t0
        have = min(dir_bytes(dest), total)
        rate = max(have - start_bytes, 0) / elapsed if elapsed > 0 else 0
        eta = (total - have) / rate if rate > 0 else None
        write_status(status_path, state="downloading", bytes=have, total=total, rate=rate, eta=eta)
        if elapsed - last_print >= 5:
            last_print = elapsed
            log(f"  {have / GB:7.1f} / {total / GB:.1f} GB   {rate / 1e6:7.0f} MB/s   ETA {_eta(eta)}")
        if not warned and elapsed >= 30 and eta is not None and (elapsed + eta) / 60 > warn_minutes:
            warned = True
            log(f"  ! projected download time {(elapsed + eta) / 60:.0f} min is over warn_download_minutes "
                f"({warn_minutes:g}); consider an FP8 repo or a faster region/provider")
    worker.join()
    seconds = time.monotonic() - t0
    if state["error"] is not None:
        write_status(status_path, state="failed", error=type(state["error"]).__name__)
        raise DownloadError(f"download failed: {type(state['error']).__name__}: {state['error']}")

    write_status(status_path, state="verifying", bytes=total, total=total)
    bad = verify(dest, files)
    if bad:
        write_status(status_path, state="failed", error="verify")
        raise DownloadError("downloaded files don't match the Hub: " + "; ".join(bad[:5]))
    shutil.rmtree(dest / ".cache", ignore_errors=True)  # local_dir metadata; not needed to serve
    (dest / MARKER).write_text(json.dumps({"repo": repo, "sha": sha, "bytes": total,
                                           "seconds": round(seconds, 1), "files": len(files)}))
    write_status(status_path, state="done", bytes=total, total=total)
    return seconds


def _eta(s: float | None) -> str:
    if s is None:
        return "--"
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


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
