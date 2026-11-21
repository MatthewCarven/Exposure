"""Read-only object inspection that never runs the target's own code.

* Previews are built by us for built-in types; user objects are shown as
  ``<module.Class>`` without calling ``__repr__``.
* Attribute reads go through ``inspect.getattr_static``. Properties and other
  Python-level descriptors are reported as "not evaluated", never called.
  C-level slot and getset descriptors are read (that is C code, not the
  target's).
* Containers are read with the built-in types' own unbound methods
  (``dict.items(d)``, ``list.__iter__``), so a subclass's overrides don't run.
* Snapshots are retried when another thread mutates a container mid-copy.

Standard library only.
"""
from __future__ import annotations

import collections
import inspect
import types

from .paths import is_dunder

SCALARS = (type(None), bool, int, float, complex)
TEXT = (str, bytes, bytearray)
MAX_STR = 80
INLINE_ITEMS = 5


class AccessDenied(Exception):
    pass


class NotEvaluated:
    """Stands in for a property/descriptor we refused to call."""

    __slots__ = ("descriptor", "name")

    def __init__(self, descriptor, name):
        self.descriptor = descriptor
        self.name = name


def _noop():
    pass


def type_name(obj) -> str:
    t = type(obj)
    mod = t.__module__
    qual = t.__qualname__
    return qual if mod in ("builtins", None) else f"{mod}.{qual}"


def needs_handle(obj) -> bool:
    return not isinstance(obj, SCALARS + TEXT) and not isinstance(obj, NotEvaluated)


def _short_text(obj) -> str:
    if isinstance(obj, str):
        s = obj if len(obj) <= MAX_STR else obj[:MAX_STR]
        r = repr(s)
        return r if len(obj) <= MAX_STR else r[:-1] + "…" + r[-1]
    data = bytes(obj[:MAX_STR])
    r = repr(data)
    if isinstance(obj, bytearray):
        r = f"bytearray({r})"
    return r if len(obj) <= MAX_STR else r + "…"


def _scalar(obj) -> str:
    if type(obj) is int and obj.bit_length() > 256:
        return f"<int, {obj.bit_length()} bits>"
    if isinstance(obj, (bool, int, float, complex, type(None))) and type(obj) in SCALARS:
        return repr(obj)
    return f"<{type_name(obj)}>"  # subclass of a scalar: its repr is user code


def container_items(obj, tick=_noop):
    """Snapshot a built-in container's contents without running overrides.

    Returns ``(kind, items)``: kind is "dict" (items are (key, value)) or
    "seq" (items are values), or ``(None, None)`` for other objects.
    """
    for _attempt in range(5):
        try:
            if isinstance(obj, dict):
                return "dict", list(dict.items(obj))
            if isinstance(obj, (types.MappingProxyType,)) and type(obj) is types.MappingProxyType:
                return "dict", list(obj.items())
            if isinstance(obj, list):
                return "seq", list(list.__iter__(obj))
            if isinstance(obj, tuple):
                return "seq", list(tuple.__iter__(obj))
            if isinstance(obj, (set, frozenset)):
                base = set if isinstance(obj, set) else frozenset
                return "seq", list(base.__iter__(obj))
            if isinstance(obj, collections.deque):
                return "seq", list(collections.deque.__iter__(obj))
            return None, None
        except RuntimeError:  # changed size during iteration
            tick()
            continue
    raise RuntimeError("container kept changing while being read")


def preview(obj) -> str:
    if isinstance(obj, NotEvaluated):
        return f"<{type(obj.descriptor).__name__}, not evaluated>"
    if type(obj) in SCALARS:
        return _scalar(obj)
    if type(obj) in TEXT:
        return _short_text(obj)
    if isinstance(obj, types.ModuleType):
        return f"<module {obj.__name__}>"
    if isinstance(obj, type):
        return f"<class {obj.__module__}.{obj.__qualname__}>"
    if isinstance(obj, (types.FunctionType, types.BuiltinFunctionType)):
        return f"<function {getattr(obj, '__module__', None)}.{obj.__qualname__}>"
    if isinstance(obj, types.MethodType):
        return f"<bound method {obj.__func__.__qualname__}>"
    if isinstance(obj, types.FrameType):
        co = obj.f_code
        return f"<frame {co.co_qualname} at {co.co_filename}:{obj.f_lineno}>"
    if isinstance(obj, types.CodeType):
        return f"<code {obj.co_qualname}>"
    if isinstance(obj, types.CellType):
        return "<cell>"
    kind, items = (None, None)
    if type(obj) in (dict, list, tuple, set, frozenset, collections.deque):
        kind, items = container_items(obj)
    if kind is not None:
        name = type(obj).__name__
        shown = []
        for item in items[:INLINE_ITEMS]:
            if kind == "dict":
                k, v = item
                shown.append(f"{_inline(k)}: {_inline(v)}")
            else:
                shown.append(_inline(item))
        more = ", …" if len(items) > INLINE_ITEMS else ""
        open_, close = {"dict": "{}", "list": "[]", "tuple": "()"}.get(name, "{}")
        if name == "deque":
            open_, close = "deque([", "])"
        return f"{name}[{len(items)}] {open_}{', '.join(shown)}{more}{close}"
    return f"<{type_name(obj)}>"


def _inline(obj) -> str:
    if type(obj) in SCALARS:
        return _scalar(obj)
    if type(obj) in TEXT:
        s = _short_text(obj)
        return s if len(s) <= 40 else s[:39] + "…"
    return f"<{type_name(obj)}>"


# -- attribute access -----------------------------------------------------------

def instance_dict(obj):
    try:
        d = object.__getattribute__(obj, "__dict__")
    except Exception:
        return None
    return d if type(d) is dict else None


def _class_dict(cls) -> dict:
    return dict(type.__dict__["__dict__"].__get__(cls))


_C_DESCRIPTORS = (types.MemberDescriptorType, types.GetSetDescriptorType)


def get_attr(obj, name: str):
    """Read ``obj.name`` without running the target's code."""
    if is_dunder(name):
        raise AccessDenied(f"dunder attribute {name!r} is not exposed")
    if isinstance(obj, types.FrameType):
        loc = frame_locals(obj)
        if name in loc:
            return loc[name]
        raise LookupError(f"frame has no local {name!r}")
    if isinstance(obj, types.FunctionType):
        cell = closure_cell(obj, name)
        if cell is not None:
            try:
                return cell.cell_contents
            except ValueError:
                raise LookupError(f"closure variable {name!r} is empty") from None
    if not isinstance(obj, type):
        d = instance_dict(obj)
        if d is not None and name in d:
            # an instance attribute, unless a data descriptor on the class wins
            try:
                cls_attr = inspect.getattr_static(type(obj), name)
            except AttributeError:
                cls_attr = None
            if cls_attr is None or not _is_data_descriptor(cls_attr):
                return d[name]
    try:
        attr = inspect.getattr_static(obj, name)
    except AttributeError:
        raise LookupError(f"{type_name(obj)} has no attribute {name!r}") from None
    if isinstance(obj, type):
        return attr  # raw class attribute (function, property, ...)
    if isinstance(attr, _C_DESCRIPTORS):
        try:
            return attr.__get__(obj, type(obj))
        except AttributeError:
            raise LookupError(f"{name!r} is not set") from None
    if isinstance(attr, (staticmethod, classmethod)):
        return attr.__func__
    if hasattr(type(attr), "__get__") and not isinstance(
            attr, (types.FunctionType, types.BuiltinFunctionType, type)):
        return NotEvaluated(attr, name)
    return attr


def closure_cell(func, name):
    """The closure cell for free variable ``name`` of ``func``, or None."""
    code = func.__code__
    cells = func.__closure__ or ()
    if name in code.co_freevars:
        i = code.co_freevars.index(name)
        if i < len(cells):
            return cells[i]
    return None


def _is_data_descriptor(attr) -> bool:
    t = type(attr)
    return hasattr(t, "__get__") and (hasattr(t, "__set__") or hasattr(t, "__delete__"))


def get_item(obj, key):
    if isinstance(obj, dict):
        try:
            return dict.__getitem__(obj, key)
        except KeyError:
            raise LookupError(f"key {key!r} not found") from None
    if isinstance(obj, (list, tuple)) and type(key) is int:
        base = list if isinstance(obj, list) else tuple
        try:
            return base.__getitem__(obj, key)
        except IndexError:
            raise LookupError(f"index {key} out of range") from None
    if isinstance(obj, collections.deque) and type(key) is int:
        items = list(collections.deque.__iter__(obj))
        try:
            return items[key]
        except IndexError:
            raise LookupError(f"index {key} out of range") from None
    raise LookupError(f"{type_name(obj)} can't be indexed safely")


def frame_locals(frame) -> dict:
    for _attempt in range(5):
        try:
            return dict(frame.f_locals.items())
        except RuntimeError:
            continue
    raise RuntimeError("frame locals kept changing")


# -- children -------------------------------------------------------------------

def _literal_key(k) -> bool:
    if type(k) is tuple:
        return all(_literal_key(x) for x in k)
    return type(k) in (int, str, bytes, bool, float, type(None))


def children(obj, tick=_noop):
    """List what can be drilled into from ``obj``.

    Returns a list of ``(label, segment_or_None, kind, value)``. The segment
    is a paths segment (("attr", name) or ("item", key)) when the child can be
    reached by a path, else None (reach it by its handle).
    """
    out = []
    if isinstance(obj, types.FrameType):
        for k, v in frame_locals(obj).items():
            tick()
            out.append((k, ("attr", k), "local", v))
        return out
    if isinstance(obj, types.ModuleType):
        d = instance_dict(obj) or {}
        for k, v in list(d.items()):
            tick()
            if not is_dunder(k):
                out.append((k, ("attr", k), "global", v))
        return out
    if isinstance(obj, type):
        for k, v in _class_dict(obj).items():
            tick()
            if is_dunder(k):
                continue
            if isinstance(v, (staticmethod, classmethod)):
                out.append((k, ("attr", k), type(v).__name__, v.__func__))
            elif isinstance(v, types.FunctionType):
                out.append((k, ("attr", k), "method", v))
            elif isinstance(v, property):
                out.append((k, ("attr", k), "property", NotEvaluated(v, k)))
            else:
                out.append((k, ("attr", k), "class attr", v))
        return out
    if isinstance(obj, types.FunctionType):
        code = obj.__code__
        cells = obj.__closure__ or ()
        for name, cell in zip(code.co_freevars, cells):
            tick()
            try:
                out.append((name, ("attr", name), "closure", cell.cell_contents))
            except ValueError:
                out.append((name, None, "closure", NotEvaluated(cell, name)))
        return out
    if isinstance(obj, types.MethodType):
        out.append(("__self__", None, "bound to", obj.__self__))
        out.append(("__func__", None, "function", obj.__func__))
        return out
    if isinstance(obj, types.CellType):
        try:
            out.append(("cell_contents", None, "contents", obj.cell_contents))
        except ValueError:
            pass
        return out
    kind, items = container_items(obj, tick)
    if kind == "dict":
        for k, v in items:
            tick()
            seg = ("item", k) if _literal_key(k) else None
            label = repr(k)[:60] if type(k) is tuple and seg else _inline(k)
            out.append((label, seg, "item", v))
    elif kind == "seq":
        indexable = isinstance(obj, (list, tuple, collections.deque))
        for i, v in enumerate(items):
            tick()
            out.append((f"[{i}]", ("item", i) if indexable else None, "item", v))
    # attributes of the instance itself
    d = instance_dict(obj)
    if d is not None:
        for k, v in list(d.items()):
            tick()
            if not is_dunder(k):
                out.append((k, ("attr", k), "attr", v))
    cls = type(obj)
    seen = set(d or ())
    for base in cls.__mro__:
        if base is object:
            continue
        for k, v in _class_dict(base).items():
            tick()
            if k in seen or is_dunder(k):
                continue
            if isinstance(v, types.MemberDescriptorType):
                seen.add(k)
                try:
                    out.append((k, ("attr", k), "slot", v.__get__(obj, cls)))
                except AttributeError:
                    out.append((k, ("attr", k), "slot", NotEvaluated(v, k)))
            elif isinstance(v, property):
                seen.add(k)
                out.append((k, ("attr", k), "property", NotEvaluated(v, k)))
    return out
