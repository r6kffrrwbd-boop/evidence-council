"""Safe evaluation of reference arithmetic for ``numeric_reference`` checks.

Only literals, + - * / // % **, unary +/-, parentheses and a few pure functions
(abs, round, min, max, sum, len, count, sqrt, floor, ceil) are allowed. No names,
attributes, subscripts, comprehensions or arbitrary calls.
"""

from __future__ import annotations

import ast
import math
import operator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": lambda *a: sum(a),
    "len": lambda s: len(s), "count": lambda s, sub: str(s).count(str(sub)),
    "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil,
}


class UnsafeExpression(ValueError):
    pass


def safe_eval(expr: str) -> float | int:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise UnsafeExpression(f"not a valid expression: {e.msg}") from None
    return _ev(tree.body)


def _ev(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)) and not isinstance(node.value, bool):
        if isinstance(node.value, str) and len(node.value) > 10000:
            raise UnsafeExpression("string literal too long")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _ev(node.left), _ev(node.right)
        if not all(isinstance(v, (int, float)) for v in (left, right)):
            raise UnsafeExpression("arithmetic is only allowed on numbers")
        if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 1e12):
            raise UnsafeExpression("exponent too large")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
        v = _ev(node.operand)
        if not isinstance(v, (int, float)):
            raise UnsafeExpression("unary operator on non-number")
        return _UN[type(node.op)](v)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_ev(a) for a in node.args])
    raise UnsafeExpression(f"disallowed syntax: {type(node).__name__}")
