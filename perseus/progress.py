"""Live download panel for the terminal, shared by preflight (setup/download.py) and perseus.py.

The download writes a status snapshot to <data>/run/download-status.json once a
second; render() turns a snapshot into lines, and LivePanel redraws them in
place on a terminal. Without a terminal (logs, pipes, CI) the panel prints one
summary line every 15 s instead.

Colours follow NO_COLOR (https://no-color.org).
"""

from __future__ import annotations

import os
import sys
import time
from typing import TextIO

GB = 1000 ** 3
PARTIAL = " ▏▎▍▌▋▊▉█"
SPARK = "▁▂▃▄▅▆▇█"


def _c(code: str, color: bool):
    return (lambda s: f"\033[{code}m{s}\033[0m") if color else (lambda s: s)


def fmt_gb(n: float) -> str:
    return f"{n / GB:.1f}"


def fmt_rate(r: float | None) -> str:
    if not r:
        return "-- MB/s"
    return f"{r / GB:.2f} GB/s" if r >= GB else f"{r / 1e6:.0f} MB/s"


def fmt_time(s: float | None) -> str:
    if s is None or s < 0:
        return "--"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def bar(frac: float, width: int) -> tuple[str, str]:
    """(filled, empty) parts of a smooth bar, eighth-block resolution."""
    frac = max(0.0, min(1.0, frac))
    cells = frac * width
    full = int(cells)
    part = int((cells - full) * 8)
    filled = "█" * full + (PARTIAL[part] if part and full < width else "")
    return filled, "░" * (width - len(filled))


def spark(values: list[float], width: int) -> str:
    vals = values[-width:]
    if not vals:
        return ""
    top = max(vals) or 1
    return "".join(SPARK[min(7, int(v / top * 7.999))] for v in vals)


def _short(name: str, width: int) -> str:
    """Shorten from the middle, dropping the extension first: shard numbers must stay visible."""
    if len(name) <= width:
        return name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem if len(stem) <= width else "…" + stem[-(width - 1):]   # "…00002-of-00007"


def _middle(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    keep = max(4, width - 1)
    return text[: keep // 3] + "…" + text[-(keep - keep // 3):]


ANSI = __import__("re").compile(r"\033\[[0-9;?]*[A-Za-z]")


def visible_len(line: str) -> int:
    return len(ANSI.sub("", line))


def fit(line: str, width: int) -> str:
    """Clip to `width` visible characters, keeping colour codes intact (a wrapped line breaks the redraw)."""
    if visible_len(line) <= width:
        return line
    out, seen, i = [], 0, 0
    while i < len(line) and seen < width - 1:
        m = ANSI.match(line, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(line[i])
        seen += 1
        i += 1
    return "".join(out) + "…\033[0m"


def render(st: dict, width: int = 80, color: bool = True, viewer: bool = False) -> list[str]:
    """Lines for one status snapshot (see setup/download.py for the fields)."""
    dim, bold, cyan, green, yellow, red = (_c(c, color) for c in ("2", "1", "36", "32", "33", "31"))
    width = max(56, min(width, 110))
    total = st.get("total") or 1
    have = min(st.get("bytes", 0), total)
    frac = have / total
    state = st.get("state", "downloading")
    lines = []

    head = f"{st.get('repo', 'model')}" + (f"@{st['sha'][:12]}" if st.get("sha") else "")
    room = width - 2 - len(head) - 4
    dest = f"  → {_middle(st['dest'], room)}" if st.get("dest") and room > 12 else ""
    lines.append(f"  {bold(head)}" + dim(dest))

    tail = f" {frac * 100:5.1f}%  {fmt_gb(have)} / {fmt_gb(total)} GB"
    filled, empty = bar(frac, max(10, width - 2 - len(tail)))
    tone = green if state == "done" else (red if state == "failed" else cyan)
    lines.append(f"  {tone(filled)}{dim(empty)}{bold(tail[:7])}{tail[7:]}")

    if state in ("downloading", "paused"):
        hist = st.get("history") or []
        sp = spark(hist, 24)
        speed = f"  {bold(fmt_rate(st.get('rate')))} now  {cyan(sp)}" if sp else f"  {bold(fmt_rate(st.get('rate')))} now"
        line = (f"{speed}  {dim('avg')} {fmt_rate(st.get('rate_avg'))}   "
                f"{dim('ETA')} {bold(fmt_time(st.get('eta')))}")
        if visible_len(line) + 16 <= width:
            line += f"   {dim('elapsed')} {fmt_time(st.get('elapsed'))}"
        lines.append(line)
    elif state == "verifying":
        lines.append(f"  {cyan('verifying file sizes…')}")
    elif state == "done":
        lines.append(f"  {green('✓ downloaded and verified')}  {dim('in')} {fmt_time(st.get('elapsed'))}  "
                     f"{dim('avg')} {fmt_rate(st.get('rate_avg'))}")
    elif state == "failed":
        lines.append(f"  {red('✗ download failed')}: {st.get('error', '?')}")

    files = st.get("files") or []
    if files:
        done = [f for f in files if f["state"] == "done"]
        active = [f for f in files if f["state"] == "active"]
        waiting = [f for f in files if f["state"] == "waiting"]
        lines.append(f"  {dim('files')} {len(done)} {dim('of')} {len(files)} {dim('done')}")
        name_w = max(16, min(48, width - 40))
        for f in sorted(active, key=lambda f: -(f["have"] / (f["size"] or 1)))[:4]:
            ff, fe = bar(f["have"] / (f["size"] or 1), 12)
            pct = f["have"] / (f["size"] or 1) * 100
            lines.append(f"    {cyan('↓')} {_short(f['name'], name_w):<{name_w}} {cyan(ff)}{dim(fe)} "
                         f"{pct:3.0f}%  {fmt_gb(f['have'])}/{fmt_gb(f['size'])} GB")
        if len(active) > 4:
            lines.append(f"    {cyan('↓')} {dim(f'{len(active) - 4} more downloading')}")
        if waiting:
            lines.append(f"    {dim(f'· {len(waiting)} waiting')}")

    notes = []
    if st.get("disk_free") is not None:
        notes.append(f"{st['disk_free'] / 1024 ** 3:.0f} GiB free on {st.get('disk_mount', 'disk')}")
    if state == "downloading":
        notes.append("Ctrl-C stops waiting; the download carries on in preflight" if viewer
                     else "Ctrl-C pauses; finished files are kept")
    if notes:
        lines.append("  " + dim(" · ".join(notes)))
    if st.get("warning"):
        lines.append(f"  {yellow('! ' + st['warning'])}")
    return [fit(l, width) for l in lines]


def summary_line(st: dict) -> str:
    """One plain line for logs and non-terminal output."""
    total = st.get("total") or 1
    have = min(st.get("bytes", 0), total)
    files = st.get("files") or []
    done = sum(f["state"] == "done" for f in files)
    return (f"  {fmt_gb(have)} / {fmt_gb(total)} GB ({have / total * 100:.0f}%) · {fmt_rate(st.get('rate'))} · "
            f"ETA {fmt_time(st.get('eta'))}" + (f" · {done}/{len(files)} files" if files else ""))


class LivePanel:
    """Redraws a block of lines in place on a terminal; plain periodic lines otherwise."""

    def __init__(self, stream: TextIO | None = None, plain: TextIO | None = None, plain_every: float = 15.0,
                 viewer: bool = False):
        self.viewer = viewer            # perseus.py watching preflight's download, not running it
        self.stream = stream            # a terminal, or None for plain mode
        self.plain = plain or sys.stdout
        self.plain_every = plain_every
        self.color = stream is not None and not os.environ.get("NO_COLOR")
        self._height = 0
        self._last_plain = float("-inf")   # the first update always prints
        if self.stream:
            if self.stream is not sys.stdout:
                # stdout goes through preflight's tee: let lines already printed reach the screen first
                sys.stdout.flush()
                time.sleep(0.2)
            self.stream.write("\033[?25l")   # hide the cursor while redrawing
            self.stream.flush()

    @property
    def live(self) -> bool:
        return self.stream is not None

    def width(self) -> int:
        try:
            cols = os.get_terminal_size(self.stream.fileno()).columns if self.stream else 80
            return cols if cols >= 20 else 80   # some ptys report 0 columns
        except (OSError, ValueError, AttributeError):
            return 80

    def update(self, st: dict) -> None:
        if self.stream:
            lines = render(st, self.width() - 1, self.color, self.viewer)
            # \r on every line: some terminals (and emulators) don't add it to a bare \n
            out = ((f"\033[{self._height}F" if self._height else "\r") + "\033[J"
                   + "".join(f"\r{l}\033[K\n\r" for l in lines))
            self.stream.write(out)
            self.stream.flush()
            self._height = len(lines)
        elif time.monotonic() - self._last_plain >= self.plain_every:
            self._last_plain = time.monotonic()
            print(summary_line(st), file=self.plain, flush=True)

    def close(self, st: dict | None = None) -> None:
        """Final frame (if given), cursor back on."""
        if self.stream:
            if st is not None:
                self.update(st)
            self.stream.write("\033[?25h")
            self.stream.flush()
        self._height = 0


def open_terminal(stdout: TextIO = sys.stdout) -> TextIO | None:
    """The stream to draw a live panel on, or None.

    preflight.sh pipes its output through `tee` into the log, so stdout isn't a
    terminal there; it sets PERSEUS_LIVE=1 when the real terminal is, and the
    panel is drawn straight on /dev/tty (the log gets the plain summary).
    """
    if os.environ.get("TERM") == "dumb":
        return None
    try:
        if stdout.isatty():
            return stdout
    except (AttributeError, ValueError):
        pass
    if os.environ.get("PERSEUS_LIVE") == "1":
        try:
            return open("/dev/tty", "w", buffering=1)
        except OSError:
            return None
    return None
