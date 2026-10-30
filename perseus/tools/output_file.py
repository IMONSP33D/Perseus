"""output_file: hand the user a file (Markdown, code, data) as a download in the chat.

The tool writes nothing to disk. It returns the file under a display-only "file"
key; the engine moves that onto the tool event, the dashboard shows it as a file
card under the answer (Preview, Copy, Download), and the model is told only that
the file was delivered. Use it when the user asks for a file, or for content that
should stay raw instead of being rendered as Markdown in the chat.
"""

from __future__ import annotations

import posixpath
import re

from perseus.tools import ToolFailure, tool

MAX_CHARS = 500_000
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
}


def clean_name(filename: str) -> str:
    """A safe, flat file name: no folders, no hidden files, no odd characters."""
    name = posixpath.basename(filename.replace("\\", "/")).strip()
    if not NAME_RE.fullmatch(name) or name.strip(".") == "":
        raise ToolFailure("filename must be a plain file name like 'notes.md' or 'main.py' "
                          "(letters, digits, '.', '_', '-' and spaces; no folders)")
    return name


def file_type(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    key = (ext if dot else name).lower()
    return TYPES.get(key, ("text/plain", "text"))


def _lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0


@tool(description="Give the user a file they can preview, copy and download from the chat, e.g. a Markdown "
                  "document, a source file or a CSV. Use it when the user asks for a file or for raw content "
                  "(Markdown source, code) that shouldn't be rendered in the chat. Pass the complete file "
                  "content. The file is not shown in your answer, so mention it in a sentence; don't repeat "
                  "its content. One file per call; call it again for each further file.",
      parameters={"type": "object", "properties": {
          "filename": {"type": "string", "minLength": 1, "maxLength": 120,
                       "description": "File name with extension, e.g. 'README.md' or 'scrape.py'."},
          "content": {"type": "string", "maxLength": MAX_CHARS,
                      "description": "The complete content of the file, exactly as it should be saved."},
          "description": {"type": "string", "maxLength": 200, "default": "",
                          "description": "Optional one-line note shown on the file card."}},
          "required": ["filename", "content"]},
      timeout_s=5)
def output_file(ctx, filename: str, content: str, description: str = ""):
    name = clean_name(filename)
    mime, language = file_type(name)
    size = len(content.encode("utf-8"))
    lines = _lines(content)
    plural = "" if lines == 1 else "s"
    return {"delivered": name, "bytes": size, "lines": lines,
            "note": "The user can now preview and download this file from the chat.",
            "file": {"name": name, "mime": mime, "language": language, "content": content,
                     "bytes": size, "lines": lines, "description": description.strip()},
            "feed": {"pill": "File ready", "summary": f"{name} · {lines} line{plural}",
                     "detail": f"{size:,} bytes",
                     "sections": [{"title": name, "text": content}]}}
