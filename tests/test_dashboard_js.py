"""The dashboard script calls only functions it defines (or the browser provides).

A static check, no browser needed: a call to a helper that was renamed or deleted
otherwise only shows up when that part of the page renders, as a ReferenceError
that stops the answer from appearing.
"""

import re
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "perseus" / "web" / "static" / "app.js"

BROWSER = {"AbortController", "Date", "Error", "Set", "Map", "String", "Number", "Boolean", "Array", "Object",
           "JSON", "Promise", "TextDecoder", "TextEncoder", "URL", "clearTimeout", "setTimeout", "setInterval",
           "clearInterval", "encodeURIComponent", "decodeURIComponent", "fetch", "matchMedia", "parseInt",
           "parseFloat", "isNaN", "requestAnimationFrame", "cancelAnimationFrame", "structuredClone", "alert", "RegExp"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "typeof", "await", "async", "new", "function"}
# `get(...)` / `set(...)` are the shorthand methods of the `store` object; the rest are words that sit
# before a parenthesis in UI text or comments
NOT_CALLS = {"get", "set", "Clear", "content", "node", "scrolls"}


def undefined_calls(source: str) -> set[str]:
    code = re.sub(r"(?m)^\s*//.*$", "", source)
    calls = set(re.findall(r"(?<![.\w$'\"`])([A-Za-z_$][\w$]*)\s*\(", code))
    defined = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", source))
    defined |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", source))
    for group in re.findall(r"\b(?:const|let|var)\s*\{([^}]*)\}", source):
        defined |= set(re.findall(r"[A-Za-z_$][\w$]*", group))
    return calls - defined - BROWSER - KEYWORDS - NOT_CALLS


class DashboardScript(unittest.TestCase):
    def test_every_called_function_exists(self):
        missing = undefined_calls(APP_JS.read_text())
        self.assertEqual(missing, set(), "app.js calls functions it never defines")

    def test_the_check_catches_a_missing_helper(self):
        src = "function render(m) { return fileList(m); }\nconst h = (x) => x;\nh(render(1));\n"
        self.assertEqual(undefined_calls(src), {"fileList"})


if __name__ == "__main__":
    unittest.main()
