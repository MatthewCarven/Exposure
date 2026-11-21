"""Hot reload: pick up edited source in a running program, in place.

Standard library only. ``importlib.reload`` alone isn't enough for a live
program: it makes *new* function and class objects, while the running code,
other modules' ``from x import f``, bound methods and existing instances all
keep the old ones. So instead:

1. The module's current source is compiled and executed into a fresh
   namespace (the module's own dict is not touched while it runs). As with
   ``importlib.reload``, its top-level statements run again. For ``__main__``
   the namespace's ``__name__`` isn't ``"__main__"``, so an
   ``if __name__ == "__main__":`` block doesn't start the program twice.
2. Existing functions get the new ``__code__`` (and defaults) swapped in, so
   every reference to them, including bound methods and instances, uses the
   new code from its next call. Classes defined in the module get the same
   treatment for their methods (including static/class methods and property
   accessors); new methods are added. A function whose closure variables
   changed can't be rebound in place and is reported.
3. Names that are new in the source are added to the module (functions and
   classes rebuilt so their globals are the module's own dict). Other
   module-level values are kept, so counters and state survive, unless
   ``values=True``.
4. Calls already running carry on with the old code until they return.

Everything changed is recorded so ``undo`` can put the old code back.
"""
from __future__ import annotations

import linecache
import sys
import types

from . import inspector
from .paths import is_dunder


def _is_func(x):
    return isinstance(x, types.FunctionType)


def _same_code(a, b):
    """Equal source-level code, ignoring sys.monitoring instrumentation (a live
    code object that a debugger or Exposure's own hooks have instrumented
    doesn't compare equal to a fresh compile of the same source)."""
    def key(c):
        consts = tuple(key(x) if isinstance(x, types.CodeType) else (type(x), x)
                       for x in c.co_consts)
        return (c.co_code, consts, c.co_names, c.co_varnames, c.co_freevars,
                c.co_firstlineno, c.co_linetable, c.co_flags, c.co_argcount,
                c.co_kwonlyargcount)
    try:
        return key(a) == key(b)
    except Exception:
        return False


def _rebuild(func, globs):
    """A copy of ``func`` whose globals are ``globs`` (the real module dict)."""
    new = types.FunctionType(func.__code__, globs, func.__name__, func.__defaults__,
                             func.__closure__)
    new.__kwdefaults__ = func.__kwdefaults__
    new.__qualname__ = func.__qualname__
    new.__doc__ = func.__doc__
    new.__dict__.update(func.__dict__)
    new.__module__ = globs.get("__name__")
    return new


class Reload:
    def __init__(self, module):
        self.module = module
        self.rebound = []      # (description, function, old_code, old_defaults, old_kwdefaults)
        self.added = []        # (container, name)
        self.values = []       # (name, old value)
        self.report = {"module": module.__name__, "rebound": [], "added": [], "skipped": [],
                       "removed_in_source": [], "kept_values": [], "updated_values": [],
                       "running": []}

    # -- rebinding -------------------------------------------------------------------------
    def _swap(self, desc, old, new):
        if old.__code__.co_freevars != new.__code__.co_freevars:
            self.report["skipped"].append(f"{desc}: closure variables changed "
                                          f"{old.__code__.co_freevars} -> "
                                          f"{new.__code__.co_freevars}")
            return
        if _same_code(old.__code__, new.__code__) and old.__defaults__ == new.__defaults__:
            return                                   # unchanged
        self.rebound.append((desc, old, old.__code__, old.__defaults__, old.__kwdefaults__))
        old.__code__ = new.__code__
        old.__defaults__ = new.__defaults__
        old.__kwdefaults__ = new.__kwdefaults__
        old.__doc__ = new.__doc__
        self.report["rebound"].append(desc)

    @staticmethod
    def _unwrap(x):
        if isinstance(x, (staticmethod, classmethod)):
            return x.__func__
        return x

    def _class(self, name, old_cls, new_cls, globs):
        old_d = type.__dict__["__dict__"].__get__(old_cls)
        new_d = type.__dict__["__dict__"].__get__(new_cls)
        for attr, new_v in new_d.items():
            if is_dunder(attr) and attr not in ("__init__", "__call__",
                                                                "__repr__", "__str__"):
                continue
            desc = f"{name}.{attr}"
            old_v = old_d.get(attr)
            nf, of = self._unwrap(new_v), self._unwrap(old_v)
            if _is_func(nf):
                if _is_func(of) and type(new_v) is type(old_v):
                    self._swap(desc, of, nf)
                elif old_v is None:
                    rebuilt = _rebuild(nf, globs)
                    wrapped = type(new_v)(rebuilt) if isinstance(new_v, (staticmethod,
                                                                         classmethod)) \
                        else rebuilt
                    setattr(old_cls, attr, wrapped)
                    self.added.append((old_cls, attr))
                    self.report["added"].append(desc)
                else:
                    self.report["skipped"].append(f"{desc}: changed kind "
                                                  f"({type(old_v).__name__} -> "
                                                  f"{type(new_v).__name__})")
            elif isinstance(new_v, property) and isinstance(old_v, property):
                for part in ("fget", "fset", "fdel"):
                    o, n = getattr(old_v, part), getattr(new_v, part)
                    if _is_func(o) and _is_func(n):
                        self._swap(f"{desc}.{part}", o, n)
        for attr, old_v in old_d.items():
            if attr not in new_d and _is_func(self._unwrap(old_v)):
                self.report["removed_in_source"].append(f"{name}.{attr} (left in place)")

    def apply(self, values=False):
        mod = self.module
        globs = mod.__dict__
        path = getattr(mod, "__file__", None)
        if not path:
            raise LookupError(f"module {mod.__name__} has no source file")
        linecache.checkcache(path)
        with open(path, "rb") as f:
            source = f.read()
        # dont_inherit: this module's own `from __future__ import annotations` must not
        # leak into the target's code
        code = compile(source, path, "exec", dont_inherit=True)
        ns = {"__name__": mod.__name__ if mod.__name__ != "__main__" else "__exposure_reload__",
              "__file__": path, "__package__": getattr(mod, "__package__", None),
              "__spec__": getattr(mod, "__spec__", None), "__builtins__": __builtins__}
        exec(code, ns)
        for name, new in ns.items():
            if is_dunder(name):
                continue
            old = globs.get(name, _MISSING)
            if old is _MISSING:
                if _is_func(new):
                    globs[name] = _rebuild(new, globs)
                elif isinstance(new, type) and new.__module__ in (ns["__name__"],
                                                                   mod.__name__):
                    for attr, v in list(type.__dict__["__dict__"].__get__(new).items()):
                        f = self._unwrap(v)
                        if _is_func(f):
                            r = _rebuild(f, globs)
                            setattr(new, attr, type(v)(r) if isinstance(
                                v, (staticmethod, classmethod)) else r)
                    new.__module__ = mod.__name__
                    globs[name] = new
                else:
                    globs[name] = new
                self.added.append((globs, name))
                self.report["added"].append(name)
            elif _is_func(old) and _is_func(new):
                self._swap(name, old, new)
            elif isinstance(old, type) and isinstance(new, type) and \
                    getattr(old, "__module__", None) == mod.__name__:
                self._class(name, old, new, globs)
            elif isinstance(new, (types.ModuleType, type)) or _is_func(new):
                continue          # an import or another module's object: leave it
            elif values:
                self.values.append((name, old))
                globs[name] = new
                self.report["updated_values"].append(name)
            else:
                try:
                    differs = old != new
                except Exception:
                    differs = True
                if differs and type(old) in inspector.SCALARS + inspector.TEXT:
                    self.report["kept_values"].append(name)
        # which rebound functions are running right now (they finish on old code)
        codes = {id(old_code): desc for desc, _f, old_code, _d, _k in self.rebound}
        for frame in sys._current_frames().values():
            f = frame
            while f is not None:
                if id(f.f_code) in codes:
                    self.report["running"].append(codes[id(f.f_code)])
                f = f.f_back
        return self.report

    def undo(self):
        for desc, func, old_code, old_defaults, old_kw in reversed(self.rebound):
            func.__code__ = old_code
            func.__defaults__ = old_defaults
            func.__kwdefaults__ = old_kw
        for container, name in reversed(self.added):
            try:
                if isinstance(container, dict):
                    container.pop(name, None)
                else:
                    delattr(container, name)
            except Exception:
                pass
        for name, old in reversed(self.values):
            self.module.__dict__[name] = old
        return {"module": self.module.__name__,
                "restored": [d for d, *_ in self.rebound],
                "removed": [n for _c, n in self.added]}


_MISSING = object()
