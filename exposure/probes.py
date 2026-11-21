"""Logpoints, function traces and line breakpoints: watching code run without
stopping it.

Standard library only. Everything uses one ``sys.monitoring`` tool id with
*local* events on the specific code objects involved, so the rest of the
program runs uninstrumented:

* **logpoint** (``log FILE:LINE EXPR``): a LINE event on the code objects
  covering that line; each hit evaluates EXPR in the running frame.
* **trace** (``trace FUNC``): PY_START / PY_RETURN / PY_UNWIND on one
  function's code: arguments, return value or exception, duration, thread.
* **line break** (``break FILE:LINE``): a LINE event that asks the main-loop
  executor to pause at the next frame boundary (pausing mid-frame isn't safe).

Each probe has a token-bucket rate limit (default 20 events/s); hits over the
limit are counted, not evaluated. Errors in an expression are reported once
and the probe disables itself. Nothing raises into the target. Events go to
a ring buffer that clients poll (``probe_events``).
"""
from __future__ import annotations

import collections
import itertools
import os
import sys
import threading
import time
import types

from . import inspector

MAX_EVENTS = 5000


class Bucket:
    def __init__(self, rate, burst=None):
        self.rate = float(rate)
        self.capacity = float(burst or max(1.0, rate))
        self.tokens = self.capacity
        self.stamp = time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Probe:
    _ids = itertools.count(1)

    def __init__(self, kind, spec, codes, *, line=None, expr=None, when=None, every=1,
                 rate=20.0, until=None, source=""):
        self.id = next(Probe._ids)
        self.kind = kind            # "log" | "trace" | "break"
        self.spec = spec
        self.codes = codes
        self.line = line
        self.expr = expr            # compiled code (log)
        self.when = when            # compiled code or None
        self.every = max(1, int(every))
        self.bucket = Bucket(rate)
        self.until = until          # monotonic deadline or None
        self.source = source
        self.hits = 0
        self.emitted = 0
        self.suppressed = 0
        self.errors = 0
        self.enabled = True
        self.disabled_reason = None
        self.starts = {}            # trace: thread ident -> [perf_counter stamps]

    def info(self):
        return {"id": self.id, "kind": self.kind, "spec": self.spec, "line": self.line,
                "hits": self.hits, "emitted": self.emitted, "suppressed": self.suppressed,
                "errors": self.errors, "enabled": self.enabled,
                "disabled_reason": self.disabled_reason, "codes": len(self.codes),
                "source": self.source}


class Probes:
    def __init__(self, on_break=None):
        self.lock = threading.RLock()
        self.tool = None
        self.probes = {}                         # id -> Probe
        self.by_code = collections.defaultdict(list)
        self.events = collections.deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.on_break = on_break                 # callable(probe) -> None
        self._in_callback = threading.local()

    # -- tool ------------------------------------------------------------------------------
    def _ensure_tool(self):
        if self.tool is not None:
            return
        mon = sys.monitoring
        for tid in range(6):
            if mon.get_tool(tid) is None:
                mon.use_tool_id(tid, "exposure-probes")
                self.tool = tid
                break
        if self.tool is None:
            raise RuntimeError("no free sys.monitoring tool id")
        E = mon.events
        mon.register_callback(self.tool, E.LINE, self._on_line)
        mon.register_callback(self.tool, E.PY_START, self._on_start)
        mon.register_callback(self.tool, E.PY_RETURN, self._on_return)
        mon.register_callback(self.tool, E.PY_UNWIND, self._on_unwind)

    def _events_for(self, code):
        E = sys.monitoring.events
        ev = 0
        for p in self.by_code.get(code, ()):
            if not p.enabled:
                continue
            ev |= E.LINE if p.kind in ("log", "break") else (E.PY_START | E.PY_RETURN)
        return ev

    def _sync_global(self):
        """PY_UNWIND (a frame left by an exception) can't be a local event, so it's
        switched on globally only while a trace is active; its callback ignores
        every code object that isn't traced."""
        E = sys.monitoring.events
        tracing = any(p.kind == "trace" and p.enabled for p in self.probes.values())
        sys.monitoring.set_events(self.tool, E.PY_UNWIND if tracing else 0)

    def _sync_code(self, code):
        sys.monitoring.set_local_events(self.tool, code, self._events_for(code))

    # -- events ---------------------------------------------------------------------------------
    def emit(self, probe, text, **extra):
        with self.lock:
            self.seq += 1
            rec = {"seq": self.seq, "t": round(time.time(), 4), "probe": probe.id if probe
                   else None, "kind": probe.kind if probe else "info",
                   "thread": threading.current_thread().name, "text": text}
            rec.update(extra)
            self.events.append(rec)
            if probe is not None:
                probe.emitted += 1

    def read(self, since=0, limit=500):
        with self.lock:
            out = [e for e in self.events if e["seq"] > since][:int(limit)]
            suppressed = {p.id: p.suppressed for p in self.probes.values() if p.suppressed}
            return {"events": out, "next": out[-1]["seq"] if out else max(since, 0),
                    "latest": self.seq, "suppressed": suppressed}

    # -- adding / removing --------------------------------------------------------------------
    def add(self, probe):
        with self.lock:
            self._ensure_tool()
            self.probes[probe.id] = probe
            for code in probe.codes:
                self.by_code[code].append(probe)
                self._sync_code(code)
            self._sync_global()
            if probe.kind in ("log", "break"):
                # lines elsewhere in these functions may have returned DISABLE for
                # an earlier probe; make them report again
                sys.monitoring.restart_events()
        return probe

    def remove(self, pid=None):
        with self.lock:
            ids = list(self.probes) if pid in (None, "all") else [int(pid)]
            removed = []
            for i in ids:
                p = self.probes.pop(i, None)
                if p is None:
                    continue
                removed.append(i)
                for code in p.codes:
                    lst = self.by_code.get(code, [])
                    if p in lst:
                        lst.remove(p)
                    if self.tool is not None:
                        try:
                            self._sync_code(code)
                        except Exception:
                            pass
                    if not lst:
                        self.by_code.pop(code, None)
            if self.tool is not None:
                try:
                    self._sync_global()
                except Exception:
                    pass
            return removed

    def _disable(self, p, reason):
        p.enabled = False
        p.disabled_reason = reason
        for code in p.codes:
            try:
                self._sync_code(code)
            except Exception:
                pass
        try:
            self._sync_global()
        except Exception:
            pass
        self.emit(p, f"probe {p.id} disabled: {reason}")

    def close(self):
        with self.lock:
            self.remove("all")
            if self.tool is not None:
                mon = sys.monitoring
                E = mon.events
                for ev in (E.LINE, E.PY_START, E.PY_RETURN, E.PY_UNWIND):
                    try:
                        mon.register_callback(self.tool, ev, None)
                    except Exception:
                        pass
                try:
                    mon.free_tool_id(self.tool)
                except Exception:
                    pass
                self.tool = None

    # -- callbacks (target threads) ---------------------------------------------------------
    def _guard(self):
        if getattr(self._in_callback, "busy", False):
            return False          # an expression we evaluate hit a probe itself
        self._in_callback.busy = True
        return True

    def _release(self):
        self._in_callback.busy = False

    def _admit(self, p):
        """Common gate: enabled, not expired, every-N, rate limit."""
        if not p.enabled:
            return False
        if p.until is not None and time.monotonic() > p.until:
            self._disable(p, "time limit reached")
            return False
        p.hits += 1
        if p.hits % p.every:
            return False
        if not p.bucket.take():
            p.suppressed += 1
            return False
        return True

    def _check_when(self, p, frame):
        if p.when is None:
            return True
        try:
            return bool(eval(p.when, frame.f_globals, frame.f_locals))
        except Exception as exc:
            p.errors += 1
            self._disable(p, f"--when raised {type(exc).__name__}: {exc}")
            return False

    def _on_line(self, code, line):
        try:
            probes = self.by_code.get(code)
            if not probes:
                return sys.monitoring.DISABLE
            mine = [p for p in probes if p.kind in ("log", "break") and p.line == line]
            if not mine:
                if any(p.kind in ("log", "break") and p.enabled for p in probes):
                    return sys.monitoring.DISABLE     # some other line of this function
                return None
            if not self._guard():
                return None
            try:
                frame = sys._getframe(1)
                for p in mine:
                    if not self._admit(p) or not self._check_when(p, frame):
                        continue
                    if p.kind == "break":
                        self.emit(p, f"break at {p.spec}: pausing at the next frame boundary")
                        if self.on_break:
                            self.on_break(p)
                        continue
                    try:
                        value = eval(p.expr, frame.f_globals, frame.f_locals)
                        self.emit(p, f"{p.spec}: {p.source} = {inspector.preview(value)}")
                    except Exception as exc:
                        p.errors += 1
                        self._disable(p, f"{p.source!r} raised {type(exc).__name__}: {exc}")
            finally:
                self._release()
        except BaseException:
            pass
        return None

    def _trace_probes(self, code):
        return [p for p in self.by_code.get(code, ()) if p.kind == "trace" and p.enabled]

    def _on_start(self, code, offset):
        try:
            probes = self._trace_probes(code)
            if not probes or not self._guard():
                return None
            try:
                frame = sys._getframe(1)
                tid = threading.get_ident()
                for p in probes:
                    p.starts.setdefault(tid, []).append(time.perf_counter())
                    if not self._admit(p):
                        p.starts[tid][-1] = None           # don't report its return
                        continue
                    argc = code.co_argcount + code.co_kwonlyargcount + bool(
                        code.co_flags & 0x04) + bool(code.co_flags & 0x08)
                    loc = frame.f_locals
                    args = ", ".join(f"{n}={inspector.preview(loc[n])}"
                                     for n in code.co_varnames[:argc] if n in loc)
                    self.emit(p, f"-> {code.co_qualname}({args})")
            finally:
                self._release()
        except BaseException:
            pass
        return None

    def _finish(self, code, how):
        try:
            probes = self._trace_probes(code)
            if not probes:
                return
            tid = threading.get_ident()
            for p in probes:
                stack = p.starts.get(tid)
                if not stack:
                    continue
                t0 = stack.pop()
                if t0 is None:
                    continue
                ms = (time.perf_counter() - t0) * 1000
                self.emit(p, f"<- {code.co_qualname} {how}  ({ms:.3f} ms)",
                          ms=round(ms, 4))
        except BaseException:
            pass

    def _on_return(self, code, offset, retval):
        if self._guard():
            try:
                self._finish(code, f"= {inspector.preview(retval)}")
            finally:
                self._release()
        return None

    def _on_unwind(self, code, offset, exc):
        if code not in self.by_code:
            return None              # global event: not one of ours
        if self._guard():
            try:
                self._finish(code, f"raised {type(exc).__name__}: {exc}")
            finally:
                self._release()
        return None


# -- finding code objects ---------------------------------------------------------------------

def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _walk_code(code):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _walk_code(const)


def _module_codes(mod):
    """Code objects reachable from a module's functions and classes."""
    seen = set()
    d = inspector.instance_dict(mod) or {}
    stack = list(d.values())
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, (staticmethod, classmethod)):
            obj = obj.__func__
        if isinstance(obj, types.FunctionType):
            yield from _walk_code(obj.__code__)
        elif isinstance(obj, type) and getattr(obj, "__module__", None) == mod.__name__:
            stack.extend(type.__dict__["__dict__"].__get__(obj).values())
        elif isinstance(obj, property):
            stack.extend(f for f in (obj.fget, obj.fset, obj.fdel) if f)


def codes_for_line(filename, line, extra_codes=()):
    """Code objects whose lines include ``line`` in a file matching ``filename``
    (full path, or a path suffix like ``ui/app.py``)."""
    want = filename.replace("\\", "/")
    matches = []
    candidates = list(extra_codes)
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        nf = f.replace("\\", "/")
        if nf == want or nf.endswith("/" + want.lstrip("/")) or _norm(f) == _norm(filename):
            candidates.extend(_module_codes(mod))
    seen = set()
    for code in candidates:
        if id(code) in seen:
            continue
        seen.add(id(code))
        nf = code.co_filename.replace("\\", "/")
        if not (nf == want or nf.endswith("/" + want.lstrip("/"))
                or _norm(code.co_filename) == _norm(filename)):
            continue
        if any(ln == line for _s, _e, ln in code.co_lines()):
            matches.append(code)
    # innermost first: a line in a nested function belongs to that function
    matches.sort(key=lambda c: -c.co_firstlineno)
    return matches[:1] if matches else []
