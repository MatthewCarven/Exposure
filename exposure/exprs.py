"""Client-side parsing of call and set expressions into safe, structured specs.

    self._mirror_param(2, "gain", 0.1)       -> target + args
    player.speed                             -> a path
    @3.items[0]                              -> a path through a handle
    [1, 2, {"a": None}]                      -> a literal

Only literals (``ast.literal_eval`` rules), names, attribute/item steps and
handles are accepted. Nothing is evaluated here or in the target: literals
travel as source text and are literal_eval'ed by the agent; paths are
resolved by the agent's read-only resolver. Arbitrary expressions are M4's
``eval``.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize

from . import paths

_PLACEHOLDER = "_exposure_handle_"
_PH_RE = re.compile(_PLACEHOLDER + r"(\d+)")


class ExprError(ValueError):
    pass


def _substitute_handles(text: str) -> str:
    """Turn ``@3`` into a placeholder name, outside string literals only."""
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(pos):
        row, col = pos
        return starts[row - 1] + col

    string_spans = []
    try:
        depth_start = None
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.STRING:
                string_spans.append((offset(t.start), offset(t.end)))
            elif t.type == getattr(tokenize, "FSTRING_START", -1) and depth_start is None:
                depth_start = offset(t.start)
            elif t.type == getattr(tokenize, "FSTRING_END", -1) and depth_start is not None:
                string_spans.append((depth_start, offset(t.end)))
                depth_start = None
    except (tokenize.TokenError, SyntaxError) as exc:
        raise ExprError(f"can't parse {text!r}: {exc}") from None

    def outside_strings(m):
        i = m.start()
        if any(a <= i < b for a, b in string_spans):
            return m.group(0)
        return _PLACEHOLDER + m.group(1)

    return re.sub(r"@(\d+)", outside_strings, text)


def _parse(text: str, mode: str):
    src = _substitute_handles(text.strip())
    try:
        return ast.parse(src.strip(), mode="eval").body
    except SyntaxError as exc:
        raise ExprError(f"can't parse {text!r}: {exc.msg}") from None


def _path_of(node) -> str | None:
    """Path text for a Name/Attribute/Subscript chain, or None."""
    if isinstance(node, ast.Name):
        m = _PH_RE.fullmatch(node.id)
        return f"@{m.group(1)}" if m else node.id
    if isinstance(node, ast.Attribute):
        base = _path_of(node.value)
        return None if base is None else f"{base}.{node.attr}"
    if isinstance(node, ast.Subscript):
        base = _path_of(node.value)
        if base is None:
            return None
        try:
            key = ast.literal_eval(node.slice)
        except (ValueError, SyntaxError, TypeError):
            raise ExprError(f"index must be a literal: [{ast.unparse(node.slice)}]") from None
        return f"{base}[{key!r}]"
    return None


def value_spec(node) -> dict:
    try:
        ast.literal_eval(node)
        return {"lit": ast.unparse(node)}
    except (ValueError, SyntaxError, TypeError):
        pass
    p = _path_of(node)
    if p is not None:
        paths.parse(p)  # validate
        return {"path": p}
    raise ExprError(f"{ast.unparse(node)!r}: arguments and values may only be literals, "
                    "names, handles (@3) and .attr / [key] steps on them. "
                    "Arbitrary expressions come with `eval` in M4.")


def parse_value(text: str) -> dict:
    return value_spec(_parse(text, "eval"))


def parse_call(text: str):
    """``(target_path, [arg specs], {kw: spec})``."""
    node = _parse(text, "eval")
    if not isinstance(node, ast.Call):
        raise ExprError(f"expected a call like name(args): {text!r}")
    target = _path_of(node.func)
    if target is None:
        raise ExprError(f"can't call {ast.unparse(node.func)!r}: the callee must be a name, "
                        "handle or .attr / [key] path")
    args = []
    for a in node.args:
        if isinstance(a, ast.Starred):
            raise ExprError("*args aren't supported")
        args.append(value_spec(a))
    kwargs = {}
    for k in node.keywords:
        if k.arg is None:
            raise ExprError("**kwargs aren't supported")
        kwargs[k.arg] = value_spec(k.value)
    return target, args, kwargs


def parse_target(text: str) -> str:
    node = _parse(text, "eval")
    p = _path_of(node)
    if p is None:
        raise ExprError(f"{text!r} is not a path (name, handle, .attr, [key])")
    return p


def parse_assignment(text: str):
    """``"target = value"`` -> (target_path, value_spec)."""
    src = _substitute_handles(text.strip())
    try:
        mod = ast.parse(src.strip(), mode="exec")
    except SyntaxError as exc:
        raise ExprError(f"can't parse {text!r}: {exc.msg}") from None
    if len(mod.body) != 1 or not isinstance(mod.body[0], ast.Assign) \
            or len(mod.body[0].targets) != 1:
        raise ExprError(f"expected `target = value`: {text!r}")
    stmt = mod.body[0]
    target = _path_of(stmt.targets[0])
    if target is None:
        raise ExprError(f"can't assign to {ast.unparse(stmt.targets[0])!r}")
    return target, value_spec(stmt.value)
