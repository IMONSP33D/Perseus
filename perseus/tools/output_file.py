"""output_file: hand the user a file (Markdown, code, data, or a binary like an .exe) as a download in the chat.

The tool writes nothing to disk. It returns the file under a display-only "file"
key; the engine moves that onto the tool event, the dashboard shows it as a file
card under the answer (Preview and Copy for text, Download for everything), and
the model is told only that the file was delivered, its size and its SHA-256.

Three ways in, exactly one per call:
  content         text the model writes itself (UTF-8)
  content_base64  a small binary the model generated in code (at most ~370 KB)
  source          a ref to a file a tool brought back this chat, e.g. "windows:publish/app.exe"
                  from run_windows_code's `collect`. The engine kept its exact bytes
                  (perseus.engine.outputs), so nothing passes through the model.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import posixpath
import re

from perseus.tools import ToolFailure, tool

MAX_CHARS = 500_000
MAX_B64_CHARS = 500_000
MAX_BYTES = 64 * 1024 * 1024
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\- ]{0,119}")

# extension -> (MIME type, language label for the preview); anything else is plain text
TYPES = {
    "md": ("text/markdown", "markdown"), "markdown": ("text/markdown", "markdown"),
    "txt": ("text/plain", "text"), "log": ("text/plain", "text"),
    "py": ("text/x-python", "python"), "js": ("text/javascript", "javascript"),
    "mjs": ("text/javascript", "javascript"), "ts": ("text/plain", "typescript"),
    "tsx": ("text/plain", "tsx"), "jsx": ("text/plain", "jsx"),
    "json": ("application/json", "json"), "yaml": ("text/yaml", "yaml"), "yml": ("text/yaml", "yaml"),
    "toml": ("text/plain", "toml"), "ini": ("text/plain", "ini"), "cfg": ("text/plain", "ini"),
    "csv": ("text/csv", "csv"), "tsv": ("text/tab-separated-values", "tsv"),
    "html": ("text/html", "html"), "htm": ("text/html", "html"), "css": ("text/css", "css"),
    "xml": ("application/xml", "xml"), "svg": ("image/svg+xml", "svg"),
    "sh": ("text/x-shellscript", "bash"), "bash": ("text/x-shellscript", "bash"),
    "sql": ("text/plain", "sql"), "c": ("text/x-c", "c"), "h": ("text/x-c", "c"),
    "cpp": ("text/x-c++", "cpp"), "hpp": ("text/x-c++", "cpp"), "cc": ("text/x-c++", "cpp"),
    "java": ("text/x-java", "java"), "go": ("text/plain", "go"), "rs": ("text/plain", "rust"),
    "rb": ("text/plain", "ruby"), "php": ("text/plain", "php"), "swift": ("text/plain", "swift"),
    "kt": ("text/plain", "kotlin"), "cs": ("text/plain", "csharp"), "lua": ("text/plain", "lua"),
    "r": ("text/plain", "r"), "tex": ("text/x-tex", "latex"), "dockerfile": ("text/plain", "dockerfile"),
    "ps1": ("text/plain", "powershell"), "psm1": ("text/plain", "powershell"), "bat": ("text/plain", "bat"),
    "cmd": ("text/plain", "bat"), "csproj": ("application/xml", "xml"), "sln": ("text/plain", "text"),
}
# extension -> MIME type for files served as bytes; anything else is application/octet-stream
BINARY_TYPES = {
    "exe": "application/vnd.microsoft.portable-executable", "dll": "application/vnd.microsoft.portable-executable",
    "sys": "application/vnd.microsoft.portable-executable", "scr": "application/vnd.microsoft.portable-executable",
    "msi": "application/x-msi", "msix": "application/octet-stream", "appx": "application/octet-stream",
    "zip": "application/zip", "7z": "application/x-7z-compressed", "gz": "application/gzip",
    "tgz": "application/gzip", "tar": "application/x-tar", "rar": "application/vnd.rar",
    "jar": "application/java-archive", "pdf": "application/pdf",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "bmp": "image/bmp", "ico": "image/x-icon", "mp3": "audio/mpeg", "wav": "audio/wav", "mp4": "video/mp4",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "sqlite": "application/vnd.sqlite3", "db": "application/octet-stream", "bin": "application/octet-stream",
    "pdb": "application/octet-stream", "wasm": "application/wasm",
}
# files that run code when opened: the dashboard card marks them
EXECUTABLE = {"exe", "dll", "sys", "scr", "com", "msi", "msix", "appx", "ps1", "psm1", "bat", "cmd", "vbs",
              "jar", "hta", "lnk", "reg", "sh"}


def clean_name(filename: str) -> str:
    """A safe, flat file name: no folders, no hidden files, no odd characters."""
    name = posixpath.basename(filename.replace("\\", "/")).strip()
    if not NAME_RE.fullmatch(name) or name.strip(".") == "":
        raise ToolFailure("filename must be a plain file name like 'notes.md' or 'main.py' "
                          "(letters, digits, '.', '_', '-' and spaces; no folders)")
    return name


def _ext(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    return (ext if dot else name).lower()


def file_type(name: str) -> tuple[str, str]:
    return TYPES.get(_ext(name), ("text/plain", "text"))


def binary_type(name: str) -> str:
    return BINARY_TYPES.get(_ext(name), "application/octet-stream")


def _lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


def _as_text(name: str, data: bytes) -> str | None:
    """The bytes as text when they are a text file (exact round-trip, BOM and CRLF kept), else None."""
    if b"\x00" in data or _ext(name) in BINARY_TYPES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _text_file(name: str, content: str, description: str, source: str = "") -> dict:
    mime, language = file_type(name)
    raw = content.encode("utf-8")
    size, lines = len(raw), _lines(content)
    sha = hashlib.sha256(raw).hexdigest()
    plural = "" if lines == 1 else "s"
    sections = [{"title": name, "text": content}]
    if source:
        sections.insert(0, {"title": "Source", "text": source})
    return {"delivered": name, "kind": "text", "bytes": size, "lines": lines, "sha256": sha,
            "note": "The user can now preview and download this file from the chat.",
            "file": {"name": name, "mime": mime, "language": language, "content": content, "bytes": size,
                     "lines": lines, "description": description.strip(), "source": source,
                     "executable": _ext(name) in EXECUTABLE},
            "feed": {"pill": "File ready", "summary": f"{name} · {lines} line{plural}",
                     "detail": f"{size:,} bytes", "sections": sections}}


def _binary_file(name: str, data: bytes, description: str, source: str = "") -> dict:
    sha = hashlib.sha256(data).hexdigest()
    lines = [f"SHA-256 {sha}", f"{len(data):,} bytes"]
    if source:
        lines.insert(0, f"from {source}")
    return {"delivered": name, "kind": "binary", "bytes": len(data), "sha256": sha,
            "note": "The user can now download this file from the chat.",
            "file": {"name": name, "mime": binary_type(name), "data": data, "description": description.strip(),
                     "source": source, "executable": _ext(name) in EXECUTABLE},
            "feed": {"pill": "File ready", "summary": f"{name} · {_size(len(data))}",
                     "detail": f"sha256 {sha[:12]}", "sections": [{"title": "Binary file", "text": "\n".join(lines)}]}}


def _deliver(name: str, data: bytes, description: str, source: str = "") -> dict:
    if len(data) > MAX_BYTES:
        raise ToolFailure(f"the file is {_size(len(data))}; output_file delivers at most {_size(MAX_BYTES)}")
    text = _as_text(name, data)
    if text is not None and len(text) <= MAX_CHARS:
        return _text_file(name, text, description, source)
    return _binary_file(name, data, description, source)


def _from_source(ctx, ref: str) -> tuple[bytes, str]:
    store = getattr(ctx, "outputs", None)
    found = store.get(ref) if store is not None else None
    if found is not None:
        return found.data, found.ref
    refs = store.refs()[:10] if store is not None else []
    if refs:
        raise ToolFailure(f"no collected file {ref!r} in this chat; collected files: {', '.join(refs)}")
    raise ToolFailure(f"no collected file {ref!r}: no tool has brought a file back in this chat yet. "
                      "Build or write the file in a run and list its path in that run's `collect`, then "
                      "pass the ref the run reports (e.g. \"windows:publish/app.exe\") as source.")


@tool(description="Give the user a file they can download from the chat (text files can also be previewed and "
                  "copied). Pass exactly one of: `content` for text you write yourself (Markdown, source code, "
                  "CSV); `source` for a file a tool brought back, such as an .exe, .dll, .zip or image a program "
                  "built and listed in run_windows_code's `collect` (pass the ref that run reported, e.g. "
                  "\"windows:publish/app.exe\"); or `content_base64` for a small binary you generated yourself. "
                  "Never paste binary data into `content`. The file is not shown in your answer, so mention it "
                  "in a sentence; don't repeat its content. One file per call; call it again for each further file.",
      parameters={"type": "object", "properties": {
          "filename": {"type": "string", "minLength": 1, "maxLength": 120,
                       "description": "File name the user gets, with extension, e.g. 'README.md' or 'app.exe'."},
          "content": {"type": "string", "maxLength": MAX_CHARS,
                      "description": "Text only: the complete content of the file, exactly as it should be saved."},
          "source": {"type": "string", "minLength": 3, "maxLength": 620,
                     "description": "Ref of a file a tool brought back this chat, e.g. \"windows:publish/app.exe\". "
                                    "Use this for anything a program built (executables, archives, images)."},
          "content_base64": {"type": "string", "maxLength": MAX_B64_CHARS,
                             "description": "A small binary you generated, base64-encoded."},
          "description": {"type": "string", "maxLength": 200, "default": "",
                          "description": "Optional one-line note shown on the file card."}},
          "required": ["filename"]},
      timeout_s=30)
def output_file(ctx, filename: str, content: str | None = None, description: str = "", source: str | None = None,
                content_base64: str | None = None):
    given = [k for k, v in (("content", content), ("source", source), ("content_base64", content_base64))
             if v is not None]
    if len(given) != 1:
        raise ToolFailure("pass exactly one of content (text you wrote), source (a collected file's ref) or "
                          f"content_base64 (a small binary); got {', '.join(given) or 'none'}")
    name = clean_name(filename)
    if content is not None:
        if "\x00" in content:
            raise ToolFailure("content holds NUL bytes, so it's binary data, which text can't carry intact. Write "
                              "the file in a run, list it in that run's `collect`, and pass its ref as source")
        return _text_file(name, content, description)
    if content_base64 is not None:
        try:
            data = base64.b64decode("".join(content_base64.split()), validate=True)
        except (binascii.Error, ValueError):
            raise ToolFailure("content_base64 isn't valid base64")
        return _deliver(name, data, description)
    data, ref = _from_source(ctx, source)
    return _deliver(name, data, description, ref)
