"""calculator: exact arithmetic, evaluated from a parsed expression tree (never eval)."""

from __future__ import annotations

import ast
import math
import operator

from perseus.tools import ToolFailure, tool

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
