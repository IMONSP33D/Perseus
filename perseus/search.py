"""SearXNG: the local search engine, and the one reviewed exception to "web only via the gateway".

`web_search` never takes a URL from the model: it sends a query string to one
hard-coded loopback address. `fetch_url`, the only tool that takes a URL, goes
through the gateway, which refuses every loopback address including this one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path



class SearchUnavailable(Exception):
    pass


class SearchClient:
    def __init__(self, port: int, timeout_s: float = 12.0, engines: list[str] | None = None):
        self.base = f"http://127.0.0.1:{int(port)}"   # fixed; nothing from the model reaches this string
        self.timeout_s = timeout_s
        # Named on every query: SearXNG otherwise asks only its "general" engines, which leaves out
        # GitHub, arXiv and Stack Overflow even when they're configured.
        self.engines = list(engines or [])
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy

    def search(self, query: str, max_results: int = 8, engines: list[str] | None = None) -> dict:
        params = {"q": query, "format": "json", "safesearch": 0}
        engines = engines or self.engines
        if engines:
            params["engines"] = ",".join(e for e in engines if e.replace("_", "").isalnum())
        q = urllib.parse.urlencode(params)
        try:
            with self._opener.open(f"{self.base}/search?{q}", timeout=self.timeout_s) as resp:
                data = json.loads(resp.read(4 * 1024 * 1024))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SearchUnavailable(f"search unavailable ({type(e).__name__})")
        results = []
        for r in data.get("results", []):
            if len(results) >= max_results:
                break
            if not str(r.get("url", "")).startswith(("http://", "https://")):
                continue
            results.append({"title": r.get("title", "")[:200], "url": r["url"],
                            "snippet": " ".join(str(r.get("content", "")).split())[:400],
                            "engines": r.get("engines") or ([r["engine"]] if r.get("engine") else [])})
        answered = sorted({e for r in results for e in r["engines"]})
        down = sorted({str(u[0]) for u in data.get("unresponsive_engines", []) if u})
        out = {"query": query, "results": results, "engines_answered": answered}
        if down:
            out["engines_unresponsive"] = down
        if not results:
            out["note"] = "no results; try different terms"
        return out

    def healthy(self) -> bool:
        try:
            with self._opener.open(f"{self.base}/healthz", timeout=3) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False


def command(paths) -> tuple[list[str], dict[str, str]]:
    """How to start the SearXNG preflight installed in <data>/searxng. VERIFY entry point on the pinned commit."""
    sx = paths.searxng
    py = sx / "venv" / "bin" / "python"
    env = {"SEARXNG_SETTINGS_PATH": str(sx / "settings.yml"), "PYTHONDONTWRITEBYTECODE": "1"}
    return [str(py), "-m", "searx.webapp"], env
