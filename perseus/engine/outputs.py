"""Files a tool brought back this chat, kept as exact bytes so output_file can hand them over by ref.

A tool that returns files from somewhere else (run_windows_code's `collect`) puts them under
the reserved "outputs" key of its result: [{"ref": "windows:publish/app.exe", "data": b"..."}].
The engine pops that key, stores the bytes here (one store per chat), and the model is told
only each file's ref, name and size. output_file(source=ref) then delivers those exact
bytes, so a binary never passes through the model or a JSON string.

A ref names where the file came from and its path there: "<origin>:<relative/path>".
Collecting the same path again replaces the older copy. The store keeps at most
MAX_TOTAL_BYTES per chat and forgets the oldest files first.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
REF_RE = re.compile(r"[a-z][a-z0-9_]{0,19}:[A-Za-z0-9_][A-Za-z0-9_.\- /]{0,599}")


@dataclass
class Output:
    ref: str
    data: bytes
    tool: str = ""
    created: float = field(default_factory=time.time)

    @property
    def path(self) -> str:
        return self.ref.partition(":")[2]

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def make_ref(origin: str, path: str) -> str | None:
    """A ref for a path, with forward slashes; None when the pair can't make a safe one."""
    ref = f"{origin}:{str(path).replace(chr(92), '/').strip().lstrip('/')}"
    return ref if REF_RE.fullmatch(ref) and ".." not in ref.split(":", 1)[1].split("/") else None


class Outputs:
    def __init__(self, max_total: int = MAX_TOTAL_BYTES, max_file: int = MAX_FILE_BYTES):
        self.max_total, self.max_file = max_total, max_file
        self._items: OrderedDict[str, Output] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, ref: str, data: bytes, tool: str = "") -> Output | None:
        """Keep a file; None (nothing kept) when the ref is unsafe or the file is over the size cap."""
        if not isinstance(data, (bytes, bytearray)) or not isinstance(ref, str) or not REF_RE.fullmatch(ref):
            return None
        if len(data) > self.max_file:
            return None
        out = Output(ref, bytes(data), tool)
        with self._lock:
            self._items.pop(ref, None)
            self._items[ref] = out
            total = sum(len(o.data) for o in self._items.values())
            while total > self.max_total and len(self._items) > 1:
                _, old = self._items.popitem(last=False)
                total -= len(old.data)
        return out

    def get(self, ref: str) -> Output | None:
        """The file for a ref. A bare path or file name works too when exactly one stored file matches."""
        ref = (ref or "").replace("\\", "/").strip()
        with self._lock:
            if ref in self._items:
                return self._items[ref]
            tail = ref.partition(":")[2] if ":" in ref else ref
            hits = [o for o in self._items.values() if o.path == tail or o.path.endswith("/" + tail)]
            return hits[0] if len(hits) == 1 else None

    def refs(self) -> list[str]:
        with self._lock:
            return list(reversed(self._items))        # newest first

    def __len__(self) -> int:
        return len(self._items)
