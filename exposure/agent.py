"""The agent that runs inside the target process.

Started by the injected bootstrap (see ``exposure.client``) via ``start()``.
Standard library only. Rules it keeps:

* Nothing raises out of the agent: every request, the server loop and the
  bootstrap catch everything (targets such as PySynthRack hook
  ``threading.excepthook`` and would report an escape as a crash).
* It never takes the target's locks, and never runs the target's own code
  for reads (see ``exposure.inspector``).
* It stays mostly idle: work is cut into short slices with GIL-releasing
  sleeps between them, a duty-cycle cap spaces requests out, and an optional
  pressure counter (for example an audio xrun count) makes it back off
  further when the target is struggling. M0 showed a tight read loop alone
  is enough to starve an audio callback of the GIL.
* M1 is read-only: there are no commands that write, call or evaluate.
"""
from __future__ import annotations

import atexit
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import types

from . import inspector, paths, policy as policy_mod, protocol

AGENT_VERSION = "0.1.0"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


# -- rate limiting ------------------------------------------------------------------

class Budget:
    """Keeps the agent's share of the GIL small.

    ``tick()`` is called inside every loop; once the current slice has run for
    ``slice_s`` it sleeps ``yield_s`` (time.sleep releases the GIL). Between
    requests, ``pace()`` sleeps so the agent's busy time stays under ``duty``
    of wall time. A pressure probe (a callable returning a counter) is read at
    most every 50 ms during work; when it rises, the yield sleep doubles and
    the duty halves, decaying back after a few quiet seconds.
    """

    def __init__(self, duty=0.25, slice_s=0.002, yield_s=0.001):
        self.base_duty = duty
        self.base_yield = yield_s
        self.slice_s = slice_s
        self.level = 0              # back-off level, 0..4
        self.level_until = 0.0
        self.probe = None
        self._probe_last = None
        self._probe_checked = 0.0
        self._slice_start = time.perf_counter()
        self.busy_s = 0.0
        self.slept_s = 0.0
        self.backoffs = 0
        self.pressure_events = 0

    @property
    def duty(self):
        return max(0.03, self.base_duty / (2 ** self.level))

    @property
    def yield_s(self):
        return min(0.016, self.base_yield * (2 ** self.level))

    def begin(self):
        self._slice_start = time.perf_counter()
        self._req_start = self._slice_start
        self._req_slept = 0.0
        self.check_pressure(force=True)

    def tick(self):
        now = time.perf_counter()
        if now - self._slice_start >= self.slice_s:
            self.check_pressure(now)
            time.sleep(self.yield_s)
            after = time.perf_counter()
            self._req_slept += after - now
            self._slice_start = after

    def end(self):
        """Account for the request and return how long to rest before the next."""
        total = time.perf_counter() - self._req_start
        busy = max(0.0, total - self._req_slept)
        self.busy_s += busy
        self.slept_s += self._req_slept
        rest = busy * (1.0 / self.duty - 1.0)
        return min(rest, 0.5)

    def check_pressure(self, now=None, force=False):
        now = now or time.perf_counter()
        if self.level and now > self.level_until:
            self.level -= 1
            self.level_until = now + 2.0
        if self.probe is None or (not force and now - self._probe_checked < 0.05):
            return
        self._probe_checked = now
        try:
            value = self.probe()
        except Exception:
            return
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        if self._probe_last is not None and value > self._probe_last:
            self.pressure_events += 1
            if self.level < 4:
                self.level += 1
                self.backoffs += 1
            self.level_until = now + 3.0
        self._probe_last = value

    def stats(self):
        return {"duty": round(self.duty, 3), "yield_ms": round(self.yield_s * 1000, 2),
                "backoff_level": self.level, "backoffs": self.backoffs,
                "pressure_events": self.pressure_events,
                "busy_s": round(self.busy_s, 4), "slept_s": round(self.slept_s, 4),
                "probe": self.probe is not None}


# -- handles -------------------------------------------------------------------------

class Handles:
    """``@n`` ids for objects shown to the client. Strong references while
    attached; cleared on detach or ``release``."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next = 1
        self._by_n: dict[int, tuple] = {}     # n -> (obj, perm)
        self._by_id: dict[int, int] = {}      # id(obj) -> n

    def add(self, obj, perm=None) -> int:
        with self._lock:
            n = self._by_id.get(id(obj))
            if n is not None and self._by_n[n][0] is obj:
                if perm is not None and self._by_n[n][1] is None:
                    self._by_n[n] = (obj, perm)
                return n
            n = self._next
            self._next += 1
            self._by_n[n] = (obj, perm)
            self._by_id[id(obj)] = n
            return n

    def get(self, n: int):
        with self._lock:
            try:
                return self._by_n[n]
            except KeyError:
                raise LookupError(f"no handle @{n} (released, or from an earlier session)") from None

    def release(self, ns=None) -> int:
        with self._lock:
            if ns is None:
                count = len(self._by_n)
                self._by_n.clear()
                self._by_id.clear()
                return count
            count = 0
            for n in ns:
                entry = self._by_n.pop(n, None)
                if entry is not None:
                    self._by_id.pop(id(entry[0]), None)
                    count += 1
            return count

    def __len__(self):
        return len(self._by_n)


# -- scope -----------------------------------------------------------------------------

READ_ONLY = {"read": True, "write": False, "call": False}


def _perm_dict(read, write, call):
    return {"read": bool(read), "write": write, "call": call}


class Agent:
    def __init__(self, config: dict):
        self.config = dict(config)
        self.pid = os.getpid()
        self.token = secrets.token_hex(32)
        self.started = time.time()
        self.stop = threading.Event()
        self.handles = Handles()
        self.budget = Budget(duty=float(config.get("duty", 0.25)))
        self.requests = 0
        self.errors = 0
        self.policy = None
        self.policy_error = None
        self._load_policy()
        self.scope = (config.get("scope") or (self.policy.scope if self.policy else None)
                      or ("both" if self._has_explicit() else "reachable"))
        if self.scope not in policy_mod.SCOPES:
            self.scope = "reachable"
        self.entry_spec = config.get("entry") or (self.policy.entry if self.policy else None)
        pressure = config.get("pressure") or (self.policy.pressure if self.policy else None)
        if pressure:
            self.budget.probe = lambda p=pressure: self._resolve_pressure(p)
        self.pressure_spec = pressure
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))   # loopback only, by design
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, name="exposure-agent", daemon=True)
        self._cleaned = False

    # -- lifecycle ---------------------------------------------------------------------
    def start(self):
        self.write_session()
        atexit.register(self._remove_files)
        self.thread.start()

    def ensure_alive(self):
        if self.thread.is_alive():
            self.write_session()

    def write_session(self):
        protocol.ensure_session_dir()
        protocol.write_private(protocol.session_path(self.pid), json.dumps({
            "pid": self.pid, "port": self.port, "token": self.token,
            "started": self.started, "agent": AGENT_VERSION,
            "protocol": protocol.PROTOCOL_VERSION}))

    def _remove_files(self):
        for p in (protocol.session_path(self.pid), protocol.boot_path(self.pid)):
            try:
                os.remove(p)
            except OSError:
                pass

    def _cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self.handles.release()
        except BaseException:
            pass
        try:
            self.sock.close()
        except BaseException:
            pass
        self._remove_files()
        try:
            atexit.unregister(self._remove_files)
        except BaseException:
            pass
        try:
            if getattr(sys, "_exposure_agent", None) is self:
                del sys._exposure_agent
        except BaseException:
            pass

    # -- server ------------------------------------------------------------------------
    def _serve(self):
        try:
            while not self.stop.is_set():
                try:
                    conn, _addr = self.sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    self._handle_conn(conn)
                except BaseException:
                    pass
                finally:
                    try:
                        conn.close()
                    except BaseException:
                        pass
        except BaseException:
            pass
        finally:
            self._cleanup()

    def _handle_conn(self, conn):
        conn.settimeout(300)
        hello = protocol.recv_msg(conn)
        token = hello.get("token") if isinstance(hello, dict) else None
        if not isinstance(token, str) or not hmac.compare_digest(
                token.encode("utf-8"), self.token.encode("utf-8")):
            protocol.send_msg(conn, {"ok": False, "error": "bad token"})
            return
        protocol.send_msg(conn, {"ok": True, "pid": self.pid, "agent": AGENT_VERSION,
                                 "protocol": protocol.PROTOCOL_VERSION})
        rest = 0.0
        while not self.stop.is_set():
            try:
                req = protocol.recv_msg(conn)
            except (ConnectionError, OSError, ValueError):
                return
            if rest:
                time.sleep(rest)
            resp = self._dispatch(req)
            try:
                protocol.send_msg(conn, resp, default=inspector.preview)
            except (TypeError, ValueError) as exc:
                protocol.send_msg(conn, {"id": resp.get("id"), "ok": False,
                                         "error": f"unserialisable result: {exc}"})
            rest = self.budget.end()
            if req.get("cmd") == "detach" and resp.get("ok"):
                self.stop.set()
                return

    def _dispatch(self, req):
        rid = req.get("id") if isinstance(req, dict) else None
        self.requests += 1
        self.budget.begin()
        try:
            cmd = req.get("cmd")
            args = req.get("args") or {}
            fn = getattr(self, "cmd_" + str(cmd), None) if isinstance(cmd, str) else None
            if fn is None:
                return {"id": rid, "ok": False, "error": f"unknown command {cmd!r}"}
            return {"id": rid, "ok": True, "result": fn(**args)}
        except BaseException as exc:
            self.errors += 1
            out = {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not isinstance(exc, (LookupError, inspector.AccessDenied, paths.PathError,
                                    PermissionError, TypeError)):
                out["traceback"] = traceback.format_exc(limit=12)
            return out

    # -- policy / explicit scope ---------------------------------------------------
    def _load_policy(self):
        path = self.config.get("policy")
        try:
            if not path:
                main = sys.modules.get("__main__")
                main_file = getattr(main, "__file__", None)
                dirs = [os.path.dirname(os.path.abspath(main_file)) if main_file else None,
                        os.getcwd()]
                path = policy_mod.find_default(dirs)
            if path:
                self.policy = policy_mod.load(path)
        except Exception as exc:
            self.policy_error = f"{type(exc).__name__}: {exc}"

    def _registry(self):
        reg = sys.modules.get("exposure.registry")
        if reg is None:
            return {}
        try:
            return reg.snapshot()
        except Exception:
            return {}

    def _has_explicit(self):
        return bool((self.policy and self.policy.rules) or self._registry())

    def _resolve_rule(self, rule):
        """Return the object a policy rule names, or raise LookupError."""
        if rule.module == policy_mod.ENTRY:
            frame = self._entry_frame()
            loc = inspector.frame_locals(frame)
            if rule.root not in loc:
                raise LookupError(f"entry frame has no local {rule.root!r}")
            obj, segs = loc[rule.root], rule.segments
        elif rule.module:
            mod = sys.modules.get(rule.module)
            if mod is None:
                raise LookupError(f"module {rule.module!r} is not imported")
            obj, segs = inspector.get_attr(mod, rule.root), rule.segments
        else:
            names = [rule.root]
            for kind, v in rule.segments:
                if kind != "attr":
                    break
                names.append(v)
            obj = None
            for i in range(len(names), 0, -1):
                mod = sys.modules.get(".".join(names[:i]))
                if mod is not None:
                    obj, segs = mod, rule.segments[i - 1:]
                    break
            if obj is None:
                main = sys.modules.get("__main__")
                obj, segs = inspector.get_attr(main, rule.root), rule.segments
        for seg in segs:
            obj = self._step_raw(obj, seg)
        return obj

    def _explicit_items(self):
        """``{root_name: (label, obj_or_exc, perm)}`` for policy rules and
        ``exposure.expose`` registrations."""
        out = {}

        def unique(base):
            name, i = base, 2
            while name in out:
                name, i = f"{base}_{i}", i + 1
            return name

        for name, item in self._registry().items():
            root = unique(name.rsplit(".", 1)[-1].replace("-", "_"))
            out[root] = (f"expose:{name}", item.obj,
                         _perm_dict(item.read, item.write, item.call))
        if self.policy:
            for rule in self.policy.rules:
                last = rule.root
                for kind, v in rule.segments:
                    if kind == "attr":
                        last = v
                root = unique(last)
                try:
                    obj = self._resolve_rule(rule)
                except Exception as exc:
                    obj = exc
                out[root] = (f"policy:{rule.key}", obj,
                             _perm_dict(rule.read, rule.write, rule.call))
        return out

    def _perm_for(self, obj, explicit=None):
        explicit = explicit if explicit is not None else self._explicit_items()
        for _label, eobj, perm in explicit.values():
            if eobj is obj:
                return perm
        return None

    # -- entry frame --------------------------------------------------------------------
    def _all_stacks(self):
        """``[(thread_ident, [frames outermost-first])]``, main thread first."""
        main = threading.main_thread().ident
        stacks = []
        for tid, top in sys._current_frames().items():
            if tid == threading.get_ident():
                continue  # our own thread
            frames = []
            f = top
            while f is not None:
                frames.append(f)
                f = f.f_back
            frames.reverse()
            stacks.append((tid, frames))
        stacks.sort(key=lambda s: s[0] != main)
        return stacks

    @staticmethod
    def _matches(frame, spec):
        co = frame.f_code
        if ":" in spec:
            mod, _, qual = spec.partition(":")
            return co.co_qualname == qual and frame.f_globals.get("__name__") == mod
        return co.co_qualname == spec or co.co_name == spec

    def _entry_matches(self):
        stacks = self._all_stacks()
        found = []
        if self.entry_spec:
            for tid, frames in stacks:
                for depth, f in enumerate(frames):
                    if self._matches(f, self.entry_spec):
                        found.append((tid, depth, f))
            return found
        for tid, frames in stacks:
            for depth, f in enumerate(frames):
                if f.f_code.co_name == "main":
                    found.append((tid, depth, f))
        if found:
            return found
        for tid, frames in stacks:
            for depth, f in enumerate(frames):
                if f.f_globals.get("__name__") == "__main__":
                    return [(tid, depth, f)]
        if stacks and stacks[0][1]:
            return [(stacks[0][0], 0, stacks[0][1][0])]
        return []

    def _entry_frame(self):
        found = self._entry_matches()
        if not found:
            spec = self.entry_spec or "main"
            raise LookupError(f"no frame for entry {spec!r} is on any thread's stack")
        return found[0][2]

    def _frame_alive(self, frame):
        for _tid, frames in self._all_stacks():
            for f in frames:
                if f is frame:
                    return True
        return False

    def _resolve_pressure(self, spec):
        if ":" in spec:
            mod, _, rest = spec.partition(":")
        else:
            mod, rest = None, spec
        (_kind, root), segs = paths.parse(rest)
        if mod == policy_mod.ENTRY:
            obj = inspector.frame_locals(self._entry_frame())[root]
        elif mod:
            obj = inspector.get_attr(sys.modules[mod], root)
        else:
            obj = inspector.get_attr(sys.modules["__main__"], root)
        for seg in segs:
            obj = self._step_raw(obj, seg)
        return obj

    # -- path resolution ----------------------------------------------------------------
    def _step_raw(self, obj, seg):
        kind, v = seg
        if kind == "attr":
            return inspector.get_attr(obj, v)
        return inspector.get_item(obj, v)

    def _check_explicit_child(self, obj):
        if isinstance(obj, (types.ModuleType, types.FrameType)):
            raise inspector.AccessDenied(
                "explicit scope: modules and frames below an exposed item are not shown")

    def _resolve(self, text):
        """Resolve a path to ``(obj, perm)``; perm is None outside explicit scope."""
        (rkind, rv), segs = paths.parse(text)
        explicit = self._explicit_items()
        if rkind == "handle":
            obj, perm = self.handles.get(rv)
            if self.scope == "explicit" and perm is None:
                raise inspector.AccessDenied(f"@{rv} is outside the explicit scope")
        else:
            obj, perm = self._resolve_root(rv, explicit)
        for seg in segs:
            if seg[0] == "attr" and paths.is_dunder(seg[1]):
                raise inspector.AccessDenied(f"dunder attribute {seg[1]!r} is not exposed")
            if perm is not None and not perm.get("read"):
                raise inspector.AccessDenied("not readable under the explicit scope")
            obj = self._step_raw(obj, seg)
            if perm is not None:
                if self.scope == "explicit":
                    self._check_explicit_child(obj)
                perm = dict(READ_ONLY)
            elif self.scope != "explicit":
                perm = self._perm_for(obj, explicit)
        if perm is None and self.scope != "explicit":
            perm = self._perm_for(obj, explicit)
        return obj, perm

    def _resolve_root(self, name, explicit):
        if self.scope != "explicit":
            try:
                frame = self._entry_frame()
            except LookupError:
                frame = None
            if frame is not None:
                if self._matches(frame, name) or name == "entry":
                    return frame, self._perm_for(frame, explicit)
                loc = inspector.frame_locals(frame)
                if name in loc:
                    return loc[name], self._perm_for(loc[name], explicit)
                g = frame.f_globals
                if name in g and not paths.is_dunder(name):
                    return g[name], self._perm_for(g[name], explicit)
            for tid, frames in self._all_stacks():
                for f in frames:
                    if self._matches(f, name):
                        return f, None
        if name in explicit:
            label, obj, perm = explicit[name]
            if isinstance(obj, Exception):
                raise LookupError(f"{label}: {obj}")
            if not perm.get("read"):
                raise inspector.AccessDenied(f"{label} is not readable")
            return obj, perm
        where = "the explicit scope" if self.scope == "explicit" else \
            "entry locals, entry globals, running frames or the explicit scope"
        raise LookupError(f"{name!r} not found in {where}")

    # -- rendering ------------------------------------------------------------------------
    def _entry(self, label, path, kind, value, perm=None, explicit=None):
        e = {"name": label, "kind": kind, "type": inspector.type_name(value)
             if not isinstance(value, inspector.NotEvaluated)
             else type(value.descriptor).__name__,
             "preview": inspector.preview(value)}
        if path:
            e["path"] = path
        if inspector.needs_handle(value):
            if perm is None and self.scope != "explicit":
                perm = self._perm_for(value, explicit)
            e["handle"] = f"@{self.handles.add(value, perm)}"
        if perm is not None:
            e["perm"] = perm
        return e

    # -- commands (all read-only) -----------------------------------------------------
    def cmd_ping(self):
        return {"pid": self.pid, "python": sys.version.split()[0],
                "implementation": sys.implementation.name,
                "agent": AGENT_VERSION, "protocol": protocol.PROTOCOL_VERSION,
                "uptime_s": round(time.time() - self.started, 2),
                "scope": self.scope, "entry": self.entry_spec or "(auto)",
                "requests": self.requests, "errors": self.errors,
                "handles": len(self.handles), "budget": self.budget.stats(),
                "config": {"entry": self.entry_spec, "scope": self.scope,
                           "pressure": self.pressure_spec, "duty": self.budget.base_duty,
                           "policy": self.policy.path if self.policy else None},
                "gil": getattr(sys, "_is_gil_enabled", lambda: True)()}

    def cmd_threads(self):
        frames = sys._current_frames()
        out = []
        for t in threading.enumerate():
            top = frames.get(t.ident)
            entry = {"name": t.name, "ident": t.ident, "native_id": t.native_id,
                     "daemon": t.daemon, "main": t is threading.main_thread()}
            if top is not None:
                co = top.f_code
                entry["top"] = f"{co.co_qualname} ({co.co_filename}:{top.f_lineno})"
            out.append(entry)
            self.budget.tick()
        known = {t.ident for t in threading.enumerate()}
        for tid, top in frames.items():
            if tid not in known:
                co = top.f_code
                out.append({"name": "<not a threading.Thread>", "ident": tid,
                            "top": f"{co.co_qualname} ({co.co_filename}:{top.f_lineno})"})
        return out

    def _find_thread(self, thread):
        frames = sys._current_frames()
        if thread is None:
            tid = threading.main_thread().ident
        elif isinstance(thread, int) or (isinstance(thread, str) and thread.isdigit()):
            tid = int(thread)
        else:
            tid = None
            for t in threading.enumerate():
                if t.name == thread:
                    tid = t.ident
                    break
            if tid is None:
                raise LookupError(f"no thread named {thread!r}")
        if tid not in frames:
            raise LookupError(f"thread {thread!r} has no Python frames")
        return tid, frames[tid]

    def cmd_stack(self, thread=None, limit=50):
        tid, top = self._find_thread(thread)
        frames = []
        f = top
        while f is not None:
            frames.append(f)
            f = f.f_back
        frames.reverse()
        entry = None
        try:
            entry = self._entry_frame()
        except LookupError:
            pass
        out = []
        for depth, f in enumerate(frames[-int(limit):] if limit else frames):
            co = f.f_code
            e = {"depth": depth, "function": co.co_qualname, "file": co.co_filename,
                 "line": f.f_lineno, "module": f.f_globals.get("__name__"),
                 "entry": f is entry}
            if self.scope != "explicit":
                e["handle"] = f"@{self.handles.add(f)}"
            out.append(e)
            self.budget.tick()
        return {"thread": tid, "frames": out}

    def cmd_ls(self, target=None, limit=DEFAULT_LIMIT, offset=0):
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        explicit = self._explicit_items()
        if target in (None, ""):
            return self._ls_roots(explicit, limit, offset)
        obj, perm = self._resolve(target)
        if self.scope == "explicit" and perm is None:
            raise inspector.AccessDenied(f"{target} is outside the explicit scope")
        if isinstance(obj, types.FrameType) and not self._frame_alive(obj):
            raise LookupError("frame has exited")
        kids = inspector.children(obj, self.budget.tick)
        total = len(kids)
        base = target
        entries = []
        child_perm = dict(READ_ONLY) if perm is not None and self.scope == "explicit" else None
        for label, seg, kind, value in kids[offset:offset + limit]:
            self.budget.tick()
            if child_perm is not None and isinstance(value, (types.ModuleType, types.FrameType)):
                continue
            path = base + paths.format_segments([seg]) if seg is not None else None
            entries.append(self._entry(label, path, kind, value, child_perm, explicit))
        return {"target": target, "type": inspector.type_name(obj),
                "preview": inspector.preview(obj), "total": total, "offset": offset,
                "entries": entries, "more": offset + limit < total}

    def _ls_roots(self, explicit, limit, offset):
        rows = []
        info = {"scope": self.scope}
        if self.scope != "explicit":
            try:
                found = self._entry_matches()
            except Exception as exc:
                found = []
                info["entry_error"] = str(exc)
            if found:
                tid, depth, frame = found[0]
                co = frame.f_code
                info["entry"] = {"function": co.co_qualname, "file": co.co_filename,
                                 "line": frame.f_lineno, "thread": tid, "depth": depth,
                                 "handle": f"@{self.handles.add(frame)}",
                                 "matches": len(found)}
                for k, v in inspector.frame_locals(frame).items():
                    self.budget.tick()
                    rows.append((k, k, "local", v, None))
                g = frame.f_globals
                if frame.f_code.co_name != "<module>":
                    for name in dict.fromkeys(co.co_names):
                        self.budget.tick()
                        if name in g and not paths.is_dunder(name):
                            rows.append((name, name, "global used", g[name], None))
            else:
                info["entry_missing"] = self.entry_spec or "main"
        for root, (label, obj, perm) in explicit.items():
            self.budget.tick()
            if isinstance(obj, Exception):
                rows.append((root, None, label, obj, perm))
            else:
                rows.append((root, root, label, obj, perm))
        entries = []
        for name, path, kind, value, perm in rows[offset:offset + limit]:
            if isinstance(value, Exception):
                entries.append({"name": name, "kind": kind, "type": "unresolved",
                                "preview": f"<{type(value).__name__}: {value}>", "perm": perm})
                continue
            entries.append(self._entry(name, path, kind, value, perm, explicit))
        info.update({"target": None, "total": len(rows), "offset": offset,
                     "entries": entries, "more": offset + limit < len(rows)})
        return info

    def cmd_show(self, target):
        obj, perm = self._resolve(target)
        if self.scope == "explicit" and perm is None:
            raise inspector.AccessDenied(f"{target} is outside the explicit scope")
        out = {"target": target, "type": inspector.type_name(obj)
               if not isinstance(obj, inspector.NotEvaluated)
               else type(obj.descriptor).__name__,
               "preview": inspector.preview(obj)}
        if inspector.needs_handle(obj):
            out["handle"] = f"@{self.handles.add(obj, perm)}"
        if perm is not None:
            out["perm"] = perm
        if isinstance(obj, types.FrameType):
            co = obj.f_code
            out.update({"function": co.co_qualname, "file": co.co_filename,
                        "line": obj.f_lineno, "alive": self._frame_alive(obj),
                        "locals": len(inspector.frame_locals(obj))})
        elif isinstance(obj, types.FunctionType):
            co = obj.__code__
            argc = co.co_argcount + co.co_kwonlyargcount
            out.update({"function": co.co_qualname, "module": obj.__module__,
                        "file": co.co_filename, "line": co.co_firstlineno,
                        "args": list(co.co_varnames[:argc]),
                        "closure": list(co.co_freevars)})
        elif isinstance(obj, type):
            out["mro"] = [f"{c.__module__}.{c.__qualname__}" for c in obj.__mro__]
        elif isinstance(obj, inspector.NotEvaluated):
            out["note"] = "a property or descriptor; M1 never calls it"
        else:
            kind, items = inspector.container_items(obj, self.budget.tick)
            if kind is not None:
                out["len"] = len(items)
            d = inspector.instance_dict(obj)
            if d is not None:
                out["attributes"] = len(d)
            if not isinstance(obj, (types.ModuleType,)) and not isinstance(
                    obj, inspector.SCALARS + inspector.TEXT):
                out["class_mro"] = [f"{c.__module__}.{c.__qualname__}"
                                    for c in type(obj).__mro__]
        return out

    def cmd_scope(self):
        explicit = self._explicit_items()
        try:
            found = self._entry_matches()
        except Exception:
            found = []
        return {"scope": self.scope, "entry_spec": self.entry_spec or "(auto)",
                "entry_matches": [{"thread": tid, "depth": d,
                                   "function": f.f_code.co_qualname,
                                   "file": f.f_code.co_filename, "line": f.f_lineno}
                                  for tid, d, f in found],
                "policy": self.policy.path if self.policy else None,
                "policy_error": self.policy_error,
                "pressure": self.pressure_spec,
                "explicit": [{"name": root, "source": label, "perm": perm,
                              "resolved": not isinstance(obj, Exception),
                              **({"error": str(obj)} if isinstance(obj, Exception) else {})}
                             for root, (label, obj, perm) in explicit.items()],
                "enforced": ["read"], "parsed_not_enforced": ["write", "call"]}

    def cmd_release(self, handles=None):
        if handles is None:
            return {"released": self.handles.release()}
        ns = [int(str(h).lstrip("@")) for h in handles]
        return {"released": self.handles.release(ns)}

    def cmd_detach(self):
        return {"detaching": True, "handles_released": len(self.handles)}


# -- entry point used by the bootstrap -------------------------------------------------

_start_lock = threading.Lock()


def start(config: dict):
    """Start the agent once per process; later calls refresh the session file."""
    with _start_lock:
        existing = getattr(sys, "_exposure_agent", None)
        if existing is not None:
            existing.ensure_alive()
            return existing
        agent = Agent(config)
        sys._exposure_agent = agent
        try:
            agent.start()
        except BaseException:
            try:
                del sys._exposure_agent
            except BaseException:
                pass
            raise
        return agent
