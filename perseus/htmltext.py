"""HTML to readable text with the stdlib parser: scripts, styles and markup removed."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "head", "form", "button",
        "select", "nav", "footer"}
BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "main", "aside",
         "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dd", "dt", "figcaption", "hr"}
VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _Extractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.parts: list[str] = []
        self.title: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in SKIP and tag not in VOID:
            self._skip += 1
        if self._skip:
            return
        if tag in BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("## " if tag != "h1" else "# ")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href, self._anchor = urljoin(self.base, href), []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in SKIP and tag not in VOID and self._skip:
            self._skip -= 1
        elif tag in BLOCK and not self._skip:
            self.parts.append("\n")
        if tag == "a" and self._href is not None:
            if urlsplit(self._href).scheme in ("http", "https"):
                self.links.append((" ".join("".join(self._anchor).split())[:80], self._href))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
            return
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)


def html_to_text(html: str, base_url: str = "", max_links: int = 30) -> tuple[str, str, list[tuple[str, str]]]:
    """Returns (title, text, links)."""
    p = _Extractor(base_url)
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    seen, links = set(), []
    for label, href in p.links:
        if href not in seen:
            seen.add(href)
            links.append((label, href))
        if len(links) >= max_links:
            break
    return " ".join("".join(p.title).split())[:200], text, links
