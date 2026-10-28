"""Built-in tools. Offline: calculator, current_time. Web: web_search, fetch_url.

No network imports here (enforced by a test): web tools use the gateway and
search client the engine injects through `ctx`.
"""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime, timezone as _tz
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ToolFailure, tool

# --------------------------------------------------------------------------- calculator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "log2": math.log2, "exp": math.exp,
          "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
          "atan": math.atan, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
          "min": min, "max": max, "factorial": math.factorial}
_CONST = {"pi": math.pi, "e": math.e, "tau": math.tau}
MAX_MAGNITUDE = 1e300


def safe_eval(expr: str):
    """Arithmetic only: numbers, + - * / // % **, parentheses, a few math functions."""
    try:
        tree = ast.parse(expr.replace("^", "**").replace("×", "*").replace("÷", "/"), mode="eval")
    except SyntaxError:
        raise ToolFailure("not a valid arithmetic expression")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.Name) and n.id in _CONST:
            return _CONST[n.id]
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
            return _UNARY[type(n.op)](ev(n.operand))
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 1000 or (abs(a) > 1e6 and abs(b) > 50)):
                raise ToolFailure("exponent too large")
            r = _BIN[type(n.op)](a, b)
            if isinstance(r, (int, float)) and abs(r) > MAX_MAGNITUDE:
                raise ToolFailure("result too large")
            return r
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS
                and not n.keywords and len(n.args) <= 4):
            args = [ev(a) for a in n.args]
            if n.func.id == "factorial" and (not args or args[0] > 1000):
                raise ToolFailure("factorial argument too large")
            r = _FUNCS[n.func.id](*args)
            if isinstance(r, (int, float)) and abs(r) > MAX_MAGNITUDE:
                raise ToolFailure("result too large")
            return r
        raise ToolFailure(f"unsupported element in expression: {type(n).__name__}")

    try:
        return ev(tree)
    except ToolFailure:
        raise
    except ZeroDivisionError:
        raise ToolFailure("division by zero")
    except (ValueError, TypeError, OverflowError) as e:
        raise ToolFailure(f"math error: {e}")


@tool(description="Evaluate an arithmetic expression exactly, e.g. '(3.5 * 2**10) / 7' or 'sqrt(2) * pi'. "
                  "Supports + - * / // % **, parentheses, sqrt, log, exp, trig, abs, round, floor, ceil, min, max.",
      parameters={"type": "object", "properties": {
          "expression": {"type": "string", "minLength": 1, "maxLength": 300,
                         "description": "The arithmetic expression."}},
          "required": ["expression"]},
      timeout_s=5)
def calculator(ctx, expression: str):
    result = safe_eval(expression)
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
        result = int(result)
    return {"expression": expression, "result": result}


@tool(description="Get the current date and time, in UTC or in an IANA time zone such as 'America/New_York'.",
      parameters={"type": "object", "properties": {
          "timezone": {"type": "string", "maxLength": 64, "default": "UTC",
                       "description": "IANA time zone name; default UTC."}}},
      timeout_s=5)
def current_time(ctx, timezone: str = "UTC"):
    try:
        tz = _tz.utc if timezone.upper() == "UTC" else ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolFailure(f"unknown time zone {timezone!r}; use an IANA name like 'Europe/Paris'")
    now = datetime.now(tz)
    return {"timezone": timezone, "iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A")}


# --------------------------------------------------------------------------- web


@tool(description="Search the web. Returns titles, URLs and snippets from several search engines. "
                  "Read a result with fetch_url.",
      parameters={"type": "object", "properties": {
          "query": {"type": "string", "minLength": 1, "maxLength": 400, "description": "Search terms."},
          "max_results": {"type": "integer", "minimum": 1, "maximum": 15, "default": 8}},
          "required": ["query"]},
      lookup=True, timeout_s=20)
def web_search(ctx, query: str, max_results: int = 8):
    if ctx.search is None:
        raise ToolFailure("search unavailable")
    return ctx.search.search(query, max_results)


@tool(description="Read a public web page (HTTP GET, read-only) and return its text, title and links. "
                  "Only public http/https sites; internal addresses and URLs carrying keys are refused.",
      parameters={"type": "object", "properties": {
          "url": {"type": "string", "minLength": 8, "maxLength": 2048, "pattern": "^https?://",
                  "description": "Full http(s) URL."}},
          "required": ["url"]},
      lookup=True, timeout_s=45)
def fetch_url(ctx, url: str):
    if ctx.gateway is None:
        raise ToolFailure("web access unavailable")
    r = ctx.gateway.fetch(url)  # Blocked / GatewayError propagate to the engine, which labels them
    out = {"url": r.url, "status": r.status, "content_type": r.content_type, "title": r.title}
    if r.binary:
        out["note"] = f"binary content ({r.content_type}, {r.bytes:,} bytes) not shown"
    else:
        out["text"] = r.text
        if r.links:
            out["links"] = [{"text": t, "url": u} for t, u in r.links[:15]]
    if r.truncated:
        out["note"] = "page was larger than the response limit and was cut off"
    if r.redirects:
        out["redirected_from"] = url
    return out
