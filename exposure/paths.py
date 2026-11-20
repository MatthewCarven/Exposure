"""Path expressions: ``name``, ``@3``, ``@3.items[0]``, ``config["db"].host``.

Grammar (no evaluation, ever):

    path     := root segment*
    root     := '@' digits | identifier
    segment  := '.' identifier | '[' literal ']'
    literal  := an int, float, str, bytes, bool or None literal (ast.literal_eval)

Standard library only.
"""
from __future__ import annotations

import ast
import re

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HANDLE = re.compile(r"@(\d+)")


class PathError(ValueError):
    pass


def _scan_bracket(text: str, i: int) -> int:
    """Index just past the ']' matching the '[' at text[i], honouring quotes."""
    depth = 0
    quote = None
    j = i
    while j < len(text):
        c = text[j]
        if quote:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise PathError(f"unclosed '[' in {text!r}")


def parse(text: str):
    """Return ``(root, segments)``.

    ``root`` is ``("handle", int)`` or ``("name", str)``; each segment is
    ``("attr", str)`` or ``("item", value)``.
    """
    text = text.strip()
    if not text:
        raise PathError("empty path")
    m = _HANDLE.match(text)
    if m:
        root = ("handle", int(m.group(1)))
    else:
        m = _IDENT.match(text)
        if not m:
            raise PathError(f"path must start with a name or @id: {text!r}")
        root = ("name", m.group(0))
    i = m.end()
    segs = []
    while i < len(text):
        c = text[i]
        if c == ".":
            m = _IDENT.match(text, i + 1)
            if not m:
                raise PathError(f"expected a name after '.' at {i} in {text!r}")
            segs.append(("attr", m.group(0)))
            i = m.end()
        elif c == "[":
            end = _scan_bracket(text, i)
            inner = text[i + 1:end - 1].strip()
            try:
                value = ast.literal_eval(inner)
            except (ValueError, SyntaxError):
                raise PathError(f"index must be a literal: [{inner}]") from None
            if not isinstance(value, (int, float, str, bytes, bool, type(None), tuple)):
                raise PathError(f"unsupported index type: [{inner}]")
            segs.append(("item", value))
            i = end
        else:
            raise PathError(f"unexpected {c!r} at {i} in {text!r}")
    return root, segs


def format_segments(segs) -> str:
    out = []
    for kind, v in segs:
        out.append(f".{v}" if kind == "attr" else f"[{v!r}]")
    return "".join(out)


def is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")
