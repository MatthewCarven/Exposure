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

import ast
import collections
import contextvars
import fnmatch
import inspect as _inspect
import io
import itertools
import linecache

from . import executor as executor_mod
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

    def __init__(self, max_handles=10000):
        self._lock = threading.Lock()
        self._next = 1
        self._by_n: dict[int, tuple] = {}     # n -> (obj, perm), oldest first
        self._by_id: dict[int, int] = {}      # id(obj) -> n
        self.max = max(100, int(max_handles))
        self.evicted = 0

    def add(self, obj, perm=None) -> int:
        with self._lock:
            n = self._by_id.get(id(obj))
            if n is not None and self._by_n[n][0] is obj:
                entry = self._by_n.pop(n)            # re-insert: most recently used
                self._by_n[n] = entry if perm is None or entry[1] is not None else (obj, perm)
                return n
            if len(self._by_n) >= self.max:          # evict the least recently used 10%
                for old in list(self._by_n)[:max(1, self.max // 10)]:
                    o, _p = self._by_n.pop(old)
                    if self._by_id.get(id(o)) == old:
                        del self._by_id[id(o)]
                    self.evicted += 1
            n = self._next
            self._next += 1
            self._by_n[n] = (obj, perm)
            self._by_id[id(obj)] = n
            return n

    def get(self, n: int):
        with self._lock:
            try:
                entry = self._by_n.pop(n)
            except KeyError:
                raise LookupError(f"no handle @{n} (released, evicted, or from an earlier "
                                  "session)") from None
            self._by_n[n] = entry
            return entry

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
        self.handles = Handles(config.get("max_handles", 10000))
        self.budget = Budget(duty=float(config.get("duty", 0.25)))
        self.requests = 0
        self.errors = 0
        self.read_only = bool(config.get("read_only"))
        # served: started by the program itself with exposure.serve(); a client's
        # detach ends its session but the agent keeps serving. strict: eval/exec
        # off, scope from the program's policy only (see design section 3.1.1).
        self.served = bool(config.get("served"))
        self.strict = bool(config.get("strict"))
        default_idle = 0 if self.served else 1800
        self.idle_timeout = float(config.get("idle_timeout", default_idle))  # 0 = never
        self.audit_path = config.get("audit_log")
        self.frame_budget_s = float(config.get("frame_budget_ms", 2.0)) / 1000.0
        self.last_activity = time.time()
        self.executors = {}           # key -> ThreadExecutor
        self.agent_exec = executor_mod.AgentExecutor()
        self.jobs = executor_mod.Jobs()
        self.changes = []             # undo log / history, oldest first
        self._change_ids = 0
        self.idle_detached = False
        # Free-threaded 3.14t has no GIL to make `x += 1` or check-then-act on
        # shared lists atomic; connection threads, job threads and the target's
        # own threads all touch the agent, so these go under one small lock.
        self._state_lock = threading.RLock()
        self._conn_seq = 0
        self._open_conns = 0
        self._conns = set()
        self.pauser = None
        self.repeats = {}
        self._repeat_ids = 0
        self._captures = {}           # job id -> _Capture, read by _job_report
        self._captures_order = []
        self._capture_seq = itertools.count(1)
        self._routers = []            # (stream name, _Router) we installed
        self.scratch = {"__builtins__": __builtins__, "__name__": "__exposure__"}
        self._eval_seq = 0
        self.policy = None
        self.policy_error = None
        self._load_policy()
        safety = (self.policy.safety if self.policy else {}) or {}
        self.deny = list(safety.get("deny", [])) + list(config.get("deny") or [])
        self.policy_allows_eval = safety.get("eval") is True
        self.scope = (config.get("scope") or (self.policy.scope if self.policy else None)
                      or ("explicit" if self.strict else None)
                      or ("both" if self._has_explicit() else "reachable"))
        if self.strict and self.scope == "reachable":
            self.scope = "both"     # strict never allows changes outside the policy
        if not self.audit_path and self.policy:
            self.audit_path = self.policy.agent.get("audit_log")
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
        sys.audit("exposure.attach", self.pid, "served" if self.served else "injected")
        self.write_session()
        atexit.register(self._remove_files)
        self.thread.start()
        self._audit("attach", {"target": "served" if self.served else "injected"}, True, None)

    def ensure_alive(self):
        """Refresh the session file of a running agent. False if this agent is
        detaching (or gone), so the caller should wait for it and start anew."""
        if self.thread.is_alive() and not self.stop.is_set():
            self.write_session()
            return True
        return False

    def _begin_stop(self):
        """Stop taking clients at once: drop the session file and close the
        listening socket, so nobody connects to an agent that's shutting down.
        The accept loop then wakes with OSError and runs _cleanup."""
        self.stop.set()
        self._remove_files()
        try:
            self.sock.close()
        except BaseException:
            pass

    def write_session(self):
        protocol.ensure_session_dir()
        protocol.write_private(protocol.session_path(self.pid), json.dumps({
            "pid": self.pid, "port": self.port, "token": self.token,
            "started": self.started, "agent": AGENT_VERSION,
            "protocol": protocol.PROTOCOL_VERSION}))

    def _remove_files(self):
        """Remove our session file, only if it's still ours (a fresh agent may
        already have replaced it). The boot file is left alone: the stub deletes
        it, and removing it here could drop a new attach's pending boot file."""
        path = protocol.session_path(self.pid)
        try:
            with open(path, encoding="utf-8") as f:
                ours = json.load(f).get("token") == self.token
        except (OSError, ValueError, AttributeError):
            ours = False
        if ours:
            try:
                os.remove(path)
            except OSError:
                pass

    def _cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        try:
            sys.audit("exposure.detach", self.pid)
            self._audit("detach", {}, True, None)
        except BaseException:
            pass
        for name, router in self._routers:      # put sys.stdout/stderr back
            try:
                if getattr(sys, name) is router:
                    setattr(sys, name, router.orig)
            except BaseException:
                pass
        self._routers = []
        for r in list(self.repeats.values()):   # stop repeated calls
            r["stop"].set()
        if self.pauser is not None:              # never leave the program frozen
            try:
                self.pauser.resume(timeout=2.0)
            except BaseException:
                pass
        for ex in list(self.executors.values()):
            try:
                ex.close()
            except BaseException:
                pass
        self.executors.clear()
        try:
            self.handles.release()
        except BaseException:
            pass
        self.changes = []
        try:
            self.sock.close()
        except BaseException:
            pass
        with self._state_lock:
            conns = list(self._conns)
        for conn in conns:               # other clients: drop them too
            try:
                conn.close()
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
                    if (self.idle_timeout and time.time() - self.last_activity > self.idle_timeout
                            and not self.jobs.pending() and not self._open_conns):
                        self.idle_detached = True
                        break   # abandoned session: tidy up as if detached
                    continue
                except OSError:
                    break
                # one thread per connection, so a long request (frames, a slow
                # call) doesn't hold up another client
                with self._state_lock:
                    self._conn_seq += 1
                threading.Thread(target=self._conn_thread, args=(conn,), daemon=True,
                                 name=f"exposure-conn-{self._conn_seq}").start()
        except BaseException:
            pass
        finally:
            self._cleanup()

    def _conn_thread(self, conn):
        with self._state_lock:
            self._open_conns += 1
            self._conns.add(conn)
        try:
            self._handle_conn(conn)
        except BaseException:
            pass
        finally:
            with self._state_lock:
                self._open_conns -= 1
                self._conns.discard(conn)
            self.last_activity = time.time()
            try:
                conn.close()
            except BaseException:
                pass

    def _handle_conn(self, conn):
        conn.settimeout(min(300.0, self.idle_timeout) if self.idle_timeout else 300.0)
        hello = protocol.recv_msg(conn)
        token = hello.get("token") if isinstance(hello, dict) else None
        if not isinstance(token, str) or not hmac.compare_digest(
                token.encode("utf-8"), self.token.encode("utf-8")):
            protocol.send_msg(conn, {"ok": False, "error": "bad token"})
            return
        if self.stop.is_set():
            protocol.send_msg(conn, {"ok": False, "error": "detaching"})
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
            detaching = (req.get("cmd") == "detach" and resp.get("ok")) if isinstance(
                req, dict) else False
            if detaching and not self.served:
                # stop advertising *before* answering: once the client reads
                # "detaching", the session file is already gone and new hellos
                # are refused. This connection takes itself off the list the
                # cleanup closes (setting stop lets the accept loop exit and
                # clean up while we're still replying); it closes itself.
                with self._state_lock:
                    self._conns.discard(conn)
                self.stop.set()
                self._remove_files()
            try:
                protocol.send_msg(conn, resp, default=inspector.preview)
            except (TypeError, ValueError) as exc:
                protocol.send_msg(conn, {"id": resp.get("id"), "ok": False,
                                         "error": f"unserialisable result: {exc}"})
            rest = self.budget.end()
            if detaching:
                if not self.served:
                    self._begin_stop()
                return              # (a served agent keeps serving other sessions)

    def _dispatch(self, req):
        rid = req.get("id") if isinstance(req, dict) else None
        with self._state_lock:
            self.requests += 1
        self.last_activity = time.time()
        self.budget.begin()
        try:
            cmd = req.get("cmd")
            args = req.get("args") or {}
            fn = getattr(self, "cmd_" + str(cmd), None) if isinstance(cmd, str) else None
            if fn is None:
                return {"id": rid, "ok": False, "error": f"unknown command {cmd!r}"}
            result = fn(**args)
            if cmd in _AUDITED:
                self._audit(cmd, args, True, result)
            return {"id": rid, "ok": True, "result": result}
        except BaseException as exc:
            with self._state_lock:
                self.errors += 1
            out = {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if isinstance(req, dict) and req.get("cmd") in _AUDITED:
                self._audit(req.get("cmd"), req.get("args") or {}, False, out["error"])
            if not isinstance(exc, (LookupError, inspector.AccessDenied, paths.PathError,
                                    PermissionError, TypeError, ValueError)):
                out["traceback"] = traceback.format_exc(limit=12)
            return out

    def _audit(self, cmd, args, ok, result):
        """Append one JSON line per change-making command to the audit log, if set."""
        if not self.audit_path:
            return
        try:
            what = args.get("target") or args.get("source") or args.get("action") or ""
            rec = {"t": round(time.time(), 3), "pid": self.pid, "cmd": cmd,
                   "what": str(what)[:500], "ok": ok}
            if isinstance(result, dict):
                for k in ("state", "error", "on", "old", "new", "job"):
                    if k in result:
                        rec[k] = result[k] if k != "error" else str(result[k])[:500]
            elif not ok:
                rec["error"] = str(result)[:500]
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass

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
        ours = {t.ident for t in threading.enumerate()
                if t.name == "exposure-agent" or t.name.startswith("exposure-conn-")}
        ours.add(threading.get_ident())
        for tid, top in sys._current_frames().items():
            if tid in ours:
                continue  # the agent's own threads
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
                "strict": self.strict, "served": self.served,
                "requests": self.requests, "errors": self.errors,
                "handles": len(self.handles), "budget": self.budget.stats(),
                "config": {"entry": self.entry_spec, "scope": self.scope,
                           "pressure": self.pressure_spec, "duty": self.budget.base_duty,
                           "policy": self.policy.path if self.policy else None,
                           "read_only": self.read_only, "idle_timeout": self.idle_timeout,
                           "frame_budget_ms": self.frame_budget_s * 1000,
                           "max_handles": self.handles.max},
                "handles_evicted": self.handles.evicted,
                "jobs_pending": len(self.jobs.pending()), "changes": len(self.changes),
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
        info = {"scope": self.scope, "strict": self.strict}
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
                "read_only": self.read_only,
                "enforced": (["read"] if self.read_only else ["read", "write", "call"])
                if self.scope != "reachable" else [],
                "actions": sorted(self.policy.actions) if self.policy else []}

    def cmd_release(self, handles=None):
        if handles is None:
            return {"released": self.handles.release()}
        ns = [int(str(h).lstrip("@")) for h in handles]
        return {"released": self.handles.release(ns)}

    def cmd_detach(self):
        if self.served:
            # the program started this agent itself: end the session, keep serving
            for r in list(self.repeats.values()):
                r["stop"].set()
            if self.pauser is not None:
                self.pauser.resume(timeout=2.0)
            released = self.handles.release()
            return {"detaching": False, "served": True, "handles_released": released,
                    "note": "this agent was started by the program (exposure.serve); "
                            "the session ended but the agent keeps serving"}
        return {"detaching": True, "handles_released": len(self.handles),
                "jobs_still_running": len(self.jobs.pending())}

    # =====================================================================================
    # M2: calls and sets
    # =====================================================================================

    def _check_writable(self):
        if self.read_only:
            raise PermissionError("attached with --read-only: calls and sets are refused")

    # -- executors -------------------------------------------------------------------------
    def _main_executor(self):
        with self._state_lock:      # one executor per thread, even when
            return self._main_executor_locked()   # two clients ask at once

    def _main_executor_locked(self):
        tid = threading.main_thread().ident
        ex = self.executors.get(("main", tid))
        if ex is None:
            prefer = None
            try:
                frame = self._entry_frame()
                prefer = frame.f_code
            except LookupError:
                pass
            ex = executor_mod.ThreadExecutor(tid, budget_s=self.frame_budget_s,
                                             prefer_code=prefer, is_main=True)
            self.executors[("main", tid)] = ex
        return ex

    def _line_executor(self, tid):
        with self._state_lock:      # one executor per thread, even when
            return self._line_executor_locked(tid)   # two clients ask at once

    def _line_executor_locked(self, tid):
        ex = self.executors.get(("line", tid))
        if ex is None:
            ex = executor_mod.ThreadExecutor(tid, budget_s=self.frame_budget_s)
            ex.mode = "line"
            self.executors[("line", tid)] = ex
        return ex

    def _executor(self, on):
        if on in (None, "", "main"):
            return self._main_executor()
        if on == "agent":
            return self.agent_exec
        if isinstance(on, str) and on.startswith("thread:"):
            tid, _top = self._find_thread(on.split(":", 1)[1])
            if tid == threading.main_thread().ident:
                return self._main_executor()
            return self._thread_executor(tid)
        if on == "loop":
            for tid, frames in self._all_stacks():
                if frames and executor_mod.running_asyncio_loop(frames[-1]) is not None:
                    if tid == threading.main_thread().ident:
                        return self._main_executor()
                    return self._thread_executor(tid)
            raise LookupError("no running asyncio event loop found on any thread")
        raise ValueError(f"--on must be main, agent, loop or thread:NAME, not {on!r}")

    def _thread_executor(self, tid):
        with self._state_lock:      # one executor per thread, even when
            return self._thread_executor_locked(tid)   # two clients ask at once

    def _thread_executor_locked(self, tid):
        """A named thread: its asyncio loop if it runs one, else its next line."""
        ex = self.executors.get(("thread", tid))
        if ex is None:
            ex = executor_mod.ThreadExecutor(tid, budget_s=self.frame_budget_s)
            self.executors[("thread", tid)] = ex
        return ex

    def _frame_executor(self, frame):
        """Executor that runs on the thread owning ``frame``, while it is alive."""
        for tid, frames in self._all_stacks():
            if frame in frames:
                if tid == threading.main_thread().ident:
                    ex = self._main_executor()
                    with ex.lock:
                        if ex.mode is None:
                            ex.mode = ex._pick_mode()
                    if ex.mode == "loop":
                        loop_code = ex.edge[0]
                        idx = frames.index(frame)
                        # the loop frame and its callers are alive between iterations
                        if any(f.f_code is loop_code for f in frames[idx:]):
                            return ex
                    elif ex.mode in ("tk", "qt"):
                        return ex
                return self._line_executor(tid)
        raise LookupError("frame has exited")

    # -- arguments ------------------------------------------------------------------------
    def _value(self, spec):
        if not isinstance(spec, dict) or len(spec) != 1:
            raise TypeError(f"bad argument spec {spec!r}")
        if "lit" in spec:
            try:
                return ast.literal_eval(spec["lit"])
            except (ValueError, SyntaxError):
                raise TypeError(f"not a literal: {spec['lit']!r}") from None
        if "path" in spec:
            obj, _perm = self._resolve(spec["path"])
            if isinstance(obj, inspector.NotEvaluated):
                raise TypeError(f"{spec['path']} is a property; M2 doesn't evaluate properties "
                                "to pass them as arguments")
            return obj
        raise TypeError(f"bad argument spec {spec!r}")

    # -- permissions ----------------------------------------------------------------------
    def _check_change(self, kind, parent, parent_perm, name, member=None):
        """Raise PermissionError unless the scope lets us call/write ``parent.name``."""
        if self.scope == "reachable":
            return
        key = "call" if kind == "call" else "write"
        if parent_perm is not None:
            v = parent_perm.get(key)
            if kind == "write" and v is True:
                return
            if isinstance(v, list) and name in v:
                return
        if kind == "call" and member is not None:
            perm = self._perm_for(member)
            if perm is None and isinstance(member, types.MethodType):
                perm = self._perm_for(member.__func__)
            if perm is not None and perm.get("call") is True:
                return
        what = "calls" if kind == "call" else "changes"
        raise PermissionError(f"{self.scope} scope: {name!r} is not allowlisted for {what} "
                              "(see exposure.toml [expose] or exposure.expose())")

    # -- jobs -------------------------------------------------------------------------------
    def _run(self, fn, label, ex, timeout):
        job = executor_mod.Job(fn, label, getattr(ex, "mode", None) or "agent")
        job.awaiter = self._await_anywhere     # an asyncio executor swaps in its own
        cap = getattr(fn, "_capture", None)
        if cap is not None:
            with self._state_lock:
                self._captures[job.id] = cap[1]
                self._captures_order.append(job.id)
                for old in self._captures_order[:-500]:
                    self._captures.pop(old, None)
                del self._captures_order[:-500]
        self.jobs.add(job)
        ex.submit(job)
        job.done.wait(float(timeout) if timeout is not None else 10.0)
        return job

    def _job_report(self, job, perm_for_result=None):
        out = {"job": job.id, "label": job.label, "state": job.state, "on": job.where}
        if not job.done.is_set():
            out["note"] = ("still running (or waiting for the target thread to run Python). "
                           "Python can't stop a thread, so it will finish on its own; "
                           f"check with `jobs` or `result {job.id}`")
            return out
        out["thread"] = job.thread
        out["blocked_ms"] = job.blocked_ms
        if job.awaited:
            out["awaited"] = True
        with self._state_lock:
            cap = self._captures.pop(job.id, None) if job.done.is_set() else None
        if cap is not None:
            text, truncated = cap.text()
            if text:
                out["output"] = text
                if truncated:
                    out["output_truncated"] = True
        if job.ok:
            value = job.result
            if isinstance(value, _SetResult):
                out["old"] = inspector.preview(value.old) if value.existed else "<unset>"
                out["new"] = inspector.preview(value.new)
                out["change"] = value.change_id
            else:
                out["result"] = self._entry("result", None, "result", value, perm_for_result)
        else:
            exc = job.exc
            out["error"] = f"{type(exc).__name__}: {exc}"
            out["traceback"] = job.tb
            out["exception"] = self._entry("exception", None, "exception", exc,
                                           perm_for_result)
        return out

    # -- call -------------------------------------------------------------------------------
    def _resolve_callee(self, target):
        """``(parent, perm, segment, member)``; parent None for a bare root."""
        (rkind, rv), segs = paths.parse(target)
        if not segs:
            obj, perm = self._resolve(target)
            return None, perm, None, obj
        parent_text = (f"@{rv}" if rkind == "handle" else rv) + paths.format_segments(segs[:-1])
        parent, perm = self._resolve(parent_text)
        last = segs[-1]
        if last[0] == "attr" and paths.is_dunder(last[1]):
            raise inspector.AccessDenied(f"dunder attribute {last[1]!r} is not exposed")
        try:
            member = self._step_raw(parent, last)
        except LookupError:
            member = None
        return parent, perm, last, member

    def cmd_call(self, target, args=None, kwargs=None, on=None, timeout=10.0, capture=True):
        fn = self._prepare_call(target, args, kwargs)
        job = self._run(self._capturing(fn, capture), f"call {target}", self._executor(on),
                        timeout)
        result_perm = dict(READ_ONLY) if self.scope == "explicit" else None
        return self._job_report(job, result_perm)

    def _prepare_call(self, target, args=None, kwargs=None):
        """Check permissions, resolve arguments, return a no-arg callable."""
        self._check_writable()
        parent, perm, seg, member = self._resolve_callee(target)
        if parent is None:
            if self.scope != "reachable" and not (perm and perm.get("call") is True):
                raise PermissionError(f"{self.scope} scope: {target!r} is not allowlisted "
                                      "for calls")
        else:
            self._check_change("call", parent, perm, str(seg[1]), member)
        self._check_deny(target, member)
        a = [self._value(x) for x in (args or [])]
        kw = {str(k): self._value(v) for k, v in (kwargs or {}).items()}
        sys.audit("exposure.call", target)

        if parent is None:
            def fn(obj=member):
                return obj(*a, **kw)
        elif seg[0] == "attr":
            def fn(parent=parent, name=seg[1]):
                if isinstance(parent, types.FunctionType):
                    cell = inspector.closure_cell(parent, name)
                    if cell is not None:
                        return cell.cell_contents(*a, **kw)
                return getattr(parent, name)(*a, **kw)
        else:
            def fn(parent=parent, key=seg[1]):
                return parent[key](*a, **kw)
        return fn

    def cmd_do(self, action, args=None, kwargs=None, on=None, timeout=10.0):
        """Run a named action from the policy's [actions] table."""
        if not self.policy or action not in self.policy.actions:
            known = sorted(self.policy.actions) if self.policy else []
            raise LookupError(f"no action {action!r} (policy defines: {known})")
        spec = self.policy.actions[action]
        return self.cmd_call(spec["call"], args=args, kwargs=kwargs,
                             on=on or spec.get("on"), timeout=timeout)

    # -- set --------------------------------------------------------------------------------
    def _plan_set(self, target):
        """Work out what a set changes: ``(kind, container, key, perm, frame)``."""
        (rkind, rv), segs = paths.parse(target)
        if rkind == "handle" and not segs:
            raise TypeError("can't rebind a handle; set an attribute or item of it")
        if not segs:
            return self._plan_root_set(rv)
        last = segs[-1]
        if last[0] == "attr" and paths.is_dunder(last[1]):
            raise inspector.AccessDenied(f"dunder attribute {last[1]!r} is not exposed")
        parent_text = (f"@{rv}" if rkind == "handle" else rv) + paths.format_segments(segs[:-1])
        parent, perm = self._resolve(parent_text)
        name = last[1]
        self._check_change("write", parent, perm, str(name))
        if isinstance(parent, types.FrameType):
            if last[0] != "attr":
                raise TypeError("frame locals are set by name: @frame.name")
            return "local", parent, name, perm, parent
        if isinstance(parent, types.FunctionType) and last[0] == "attr":
            cell = inspector.closure_cell(parent, name)
            if cell is not None:
                return "cell", cell, name, perm, None
        return ("attr" if last[0] == "attr" else "item"), parent, name, perm, None

    def _plan_root_set(self, name):
        explicit = self._explicit_items()
        if self.scope != "reachable":
            item = explicit.get(name)
            label = item[0] if item else ""
            if not item or not item[2].get("write") is True or not label.startswith("policy:"):
                raise PermissionError(f"{self.scope} scope: rebinding {name!r} isn't "
                                      "allowlisted (needs a policy rule with write = true)")
            rule = next(r for r in self.policy.rules if f"policy:{r.key}" == label)
            if rule.segments:
                raise TypeError("set an attribute of an exposed object, not the object itself")
            if rule.module == policy_mod.ENTRY:
                frame = self._entry_frame()
                return "local", frame, rule.root, item[2], frame
            mod = sys.modules.get(rule.module or "__main__")
            return "attr", mod, rule.root, item[2], None
        frame = self._entry_frame()
        if name in inspector.frame_locals(frame):
            return "local", frame, name, None, frame
        if name in frame.f_globals and not paths.is_dunder(name):
            return "global", frame.f_globals, name, None, None
        raise LookupError(f"{name!r} is not a local or global of the entry frame")

    def _apply(self, kind, container, key, value, delete=False):
        """Do one change on the executing thread; return (old, existed)."""
        if kind == "attr":
            existed = True
            try:
                old = getattr(container, key)
            except AttributeError:
                old, existed = None, False
            if delete:
                delattr(container, key)
            else:
                setattr(container, key, value)
        elif kind == "item":
            try:
                old, existed = container[key], True
            except (KeyError, IndexError):
                old, existed = None, False
            if delete:
                del container[key]
            else:
                container[key] = value
        elif kind == "global":
            existed = key in container
            old = container.get(key)
            if delete:
                container.pop(key, None)
            else:
                container[key] = value
        elif kind == "local":
            loc = container.f_locals          # PEP 667 write-through proxy
            existed = key in loc
            old = loc[key] if existed else None
            if delete:
                del loc[key]
            else:
                loc[key] = value
        elif kind == "cell":
            try:
                old, existed = container.cell_contents, True
            except ValueError:
                old, existed = None, False
            if delete:
                del container.cell_contents
            else:
                container.cell_contents = value
        else:  # pragma: no cover
            raise ValueError(kind)
        return old, existed

    def cmd_set(self, target, value=None, on=None, timeout=10.0, delta=None):
        self._check_writable()
        kind, container, key, _perm, frame = self._plan_set(target)
        if delta is None:
            new = self._value(value)
        elif isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise TypeError("delta must be a number")
        sys.audit("exposure.set", target)
        ex = self._frame_executor(frame) if frame is not None else self._executor(on)
        with self._state_lock:
            self._change_ids += 1
            cid = self._change_ids

        def fn():
            value = new if delta is None else self._nudged(kind, container, key, delta)
            old, existed = self._apply(kind, container, key, value)
            return _SetResult(old, existed, value, cid)

        label = f"set {target}" if delta is None else f"nudge {target} {delta:+g}"
        job = self._run(fn, label, ex, timeout)
        if job.done.is_set() and job.ok:
            r = job.result
            with self._state_lock:
                self.changes.append({"id": cid, "target": target, "kind": kind,
                                     "container": container, "key": key, "old": r.old,
                                     "existed": r.existed, "new": r.new, "frame": frame,
                                     "on": on, "time": time.time(), "undone": False})
                del self.changes[:-500]
        return self._job_report(job)

    def cmd_nudge(self, target, delta, on=None, timeout=10.0):
        """Add ``delta`` to a number, read and written on the executing thread."""
        return self.cmd_set(target, on=on, timeout=timeout, delta=delta)

    @staticmethod
    def _nudged(kind, container, key, delta):
        if kind == "attr":
            cur = getattr(container, key)
        elif kind == "local":
            cur = container.f_locals[key]
        elif kind == "cell":
            cur = container.cell_contents
        else:
            cur = container[key]
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            raise TypeError(f"can only nudge numbers, not {type(cur).__name__}")
        if isinstance(cur, int) and isinstance(delta, float) and not delta.is_integer():
            return cur + delta
        return type(cur)(cur + delta)

    def cmd_undo(self, n=1, timeout=10.0):
        self._check_writable()
        with self._state_lock:
            todo = [c for c in self.changes if not c["undone"]][-int(n):]
        out = []
        for c in reversed(todo):
            frame = c["frame"]
            try:
                ex = self._frame_executor(frame) if frame is not None else self._executor(c["on"])
            except LookupError as exc:
                out.append({"change": c["id"], "target": c["target"], "error": str(exc)})
                continue

            def fn(c=c):
                self._apply(c["kind"], c["container"], c["key"], c["old"],
                            delete=not c["existed"])
                return None

            sys.audit("exposure.set", c["target"])
            job = self._run(fn, f"undo {c['target']}", ex, timeout)
            rep = {"change": c["id"], "target": c["target"], "state": job.state,
                   "restored": inspector.preview(c["old"]) if c["existed"] else "<unset>"}
            if job.done.is_set() and job.ok:
                c["undone"] = True
            elif job.done.is_set():
                rep["error"] = f"{type(job.exc).__name__}: {job.exc}"
            out.append(rep)
        return {"undone": out}

    def cmd_history(self, limit=50):
        return [{"change": c["id"], "target": c["target"], "kind": c["kind"],
                 "old": inspector.preview(c["old"]) if c["existed"] else "<unset>",
                 "new": inspector.preview(c["new"]), "undone": c["undone"],
                 "time": c["time"]} for c in self.changes[-int(limit):]]

    # -- jobs, executors, frames ---------------------------------------------------------
    def cmd_jobs(self):
        now = time.time()
        return [{"job": j.id, "label": j.label, "state": j.state, "on": j.where,
                 "blocked_ms": j.blocked_ms, "age_s": round(now - j.created, 2)}
                for j in self.jobs.all()[-50:]]

    def cmd_result(self, job, timeout=0.0):
        j = self.jobs.get(job)
        if timeout:
            j.done.wait(float(timeout))
        return self._job_report(j)

    def cmd_executors(self):
        ex = self._main_executor()
        with ex.lock:
            if ex.mode is None:
                try:
                    ex.mode = ex._pick_mode()
                except LookupError as exc:
                    return {"main": {"error": str(exc)}}
        out = {"main": ex.info()}
        others = [e.info() for k, e in self.executors.items() if k[0] == "line"]
        if others:
            out["threads"] = others
        return out

    def cmd_frames(self, n=300, timeout=30.0):
        ok, stamps = self._main_executor().sample(int(n), float(timeout))
        diffs = sorted(b - a for a, b in zip(stamps, stamps[1:]))
        if not diffs:
            return {"completed": ok, "frames": 0}

        def pct(p):
            return round(diffs[min(len(diffs) - 1, int(p / 100 * (len(diffs) - 1)))] * 1000, 3)

        mean = sum(diffs) / len(diffs)
        return {"completed": ok, "frames": len(diffs), "mean_ms": round(mean * 1000, 3),
                "median_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
                "max_ms": round(diffs[-1] * 1000, 3), "fps": round(1 / mean, 2) if mean else None}


    # =====================================================================================
    # M3: game tools
    # =====================================================================================

    PAUSE_WARNING = ("the main loop is frozen: a GUI stops repainting and Windows marks it "
                     "'Not Responding' after ~5 s; other threads (an audio callback, workers) "
                     "keep running. Calls and sets still work while paused.")

    def _pauser(self):
        with self._state_lock:      # one executor per thread, even when
            return self._pauser_locked()   # two clients ask at once

    def _pauser_locked(self):
        if self.pauser is None:
            self.pauser = executor_mod.Pauser(self._main_executor())
        return self.pauser

    def cmd_pause(self, seconds=None, timeout=10.0):
        self._check_writable()
        p = self._pauser()
        ok = p.pause(seconds, float(timeout))
        out = p.status()
        if not ok:
            out["note"] = ("pause queued but the main thread hasn't reached it yet "
                           "(it isn't running Python); it will pause when it does")
        out["warning"] = self.PAUSE_WARNING
        return out

    def cmd_resume(self):
        p = self._pauser()
        p.resume()
        return p.status()

    def cmd_step(self, n=1, timeout=10.0):
        self._check_writable()
        p = self._pauser()
        before = p.ex.iterations
        ok = p.step(int(n), float(timeout))
        out = p.status()
        out["advanced"] = p.ex.iterations - before
        if not ok:
            out["note"] = "the loop hasn't come round again yet"
        return out

    def cmd_paused(self):
        return self._pauser().status() if self.pauser else {"paused": False}

    # -- watch --------------------------------------------------------------------------
    def cmd_sample(self, paths_):
        """Read several paths at once (``watch``). ``len(path)`` gives a
        built-in container's length. Read-only, on the agent thread."""
        out = []
        for text in paths_:
            self.budget.tick()
            row = {"path": text}
            try:
                target = text.strip()
                want_len = target.startswith("len(") and target.endswith(")")
                if want_len:
                    target = target[4:-1]
                obj, _perm = self._resolve(target)
                if want_len:
                    kind, items = inspector.container_items(obj, self.budget.tick)
                    if kind is None:
                        raise TypeError(f"len() only for built-in containers, not "
                                        f"{inspector.type_name(obj)}")
                    row.update(type="int", preview=str(len(items)))
                else:
                    row.update(type=inspector.type_name(obj) if not isinstance(
                        obj, inspector.NotEvaluated) else "property",
                        preview=inspector.preview(obj))
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            out.append(row)
        return {"t": time.time(), "values": out}

    # -- find ------------------------------------------------------------------------------
    def cmd_find(self, query, via="graph", limit=50, depth=6, max_nodes=200000):
        """``type:Name`` (class name, qualname or module.qualname, matching
        subclasses too) or ``name:pattern`` (fnmatch on attribute/key names).

        via="graph" walks the reachable scope breadth-first in budgeted slices
        (safe beside an audio callback). via="gc" scans gc.get_objects(), which
        sees everything but builds its list in one long C call.
        """
        import fnmatch
        import gc
        kind, _, pat = str(query).partition(":")
        if kind not in ("type", "name") or not pat:
            raise ValueError("query must be type:ClassName or name:pattern")
        limit = max(1, min(int(limit), MAX_LIMIT))

        def type_matches(obj):
            for c in type(obj).__mro__:
                if pat in (c.__name__, c.__qualname__, f"{c.__module__}.{c.__qualname__}"):
                    return True
            return False

        results, seen_ids, scanned = [], set(), 0
        t0 = time.perf_counter()
        if via == "gc":
            if self.scope == "explicit":
                raise PermissionError("explicit scope: find --via gc would look outside it")
            if kind != "type":
                raise ValueError("--via gc finds types only; use the default graph walk for "
                                 "name: queries")
            objs = gc.get_objects()
            gc_ms = round((time.perf_counter() - t0) * 1000, 3)
            for obj in objs:
                scanned += 1
                if scanned % 256 == 0:
                    self.budget.tick()
                if type_matches(obj):
                    results.append(self._entry(inspector.type_name(obj), None, "instance", obj))
                    if len(results) >= limit:
                        break
            del objs
            return {"query": query, "via": "gc", "scanned": scanned, "results": results,
                    "truncated": len(results) >= limit, "gc_get_objects_ms": gc_ms,
                    "ms": round((time.perf_counter() - t0) * 1000, 3)}
        # breadth-first over the reachable (or explicit) scope
        explicit = self._explicit_items()
        queue = collections.deque()
        roots = self._ls_roots(explicit, MAX_LIMIT, 0)
        for e in roots["entries"]:
            if "path" in e and e.get("type") != "unresolved":
                try:
                    obj, _ = self._resolve(e["path"])
                except Exception:
                    continue
                queue.append((e["path"], obj, 0))
        while queue and len(results) < limit and scanned < max_nodes:
            path, obj, d = queue.popleft()
            if id(obj) in seen_ids:
                continue
            seen_ids.add(id(obj))
            scanned += 1
            self.budget.tick()
            if kind == "type" and type_matches(obj):
                results.append(self._entry(path.rsplit(".", 1)[-1], path, "instance", obj))
            if d >= depth or isinstance(obj, (types.ModuleType, type, types.FunctionType,
                                               types.FrameType)) and d > 0:
                continue   # don't wander into libraries, classes or other frames
            try:
                kids = inspector.children(obj, self.budget.tick)
            except Exception:
                continue
            for label, seg, ckind, value in kids:
                if isinstance(value, inspector.NotEvaluated):
                    continue
                cpath = path + paths.format_segments([seg]) if seg is not None else None
                if kind == "name" and fnmatch.fnmatch(str(label).strip("'\""), pat):
                    results.append(self._entry(label, cpath, ckind, value))
                    if len(results) >= limit:
                        break
                if cpath is not None and not isinstance(value, inspector.SCALARS + inspector.TEXT):
                    queue.append((cpath, value, d + 1))
        return {"query": query, "via": "graph", "scanned": scanned, "results": results,
                "truncated": len(results) >= limit or scanned >= max_nodes,
                "ms": round((time.perf_counter() - t0) * 1000, 3)}

    # -- repeated calls ---------------------------------------------------------------------
    def cmd_every(self, target, interval, args=None, kwargs=None, count=None, on=None):
        fn = self._prepare_call(target, args, kwargs)
        interval = max(0.01, float(interval))
        ex = self._executor(on)
        with self._state_lock:
            self._repeat_ids += 1
            rid = self._repeat_ids
        stop = threading.Event()
        rec = {"id": rid, "target": target, "interval": interval, "on": on or "main",
               "count": count, "runs": 0, "errors": 0, "last": None, "stop": stop,
               "started": time.time()}

        def loop():
            try:
                while not stop.is_set() and not self.stop.is_set():
                    job = executor_mod.Job(fn, f"every {target}", getattr(ex, "mode", "agent"))
                    self.jobs.add(job)
                    ex.submit(job)
                    job.done.wait(max(10.0, interval * 4))
                    rec["runs"] += 1
                    if job.done.is_set() and not job.ok:
                        rec["errors"] += 1
                    rec["last"] = job.id
                    if count and rec["runs"] >= int(count):
                        break
                    stop.wait(interval)
            except BaseException:
                pass
            finally:
                rec["finished"] = time.time()

        rec["thread"] = threading.Thread(target=loop, name=f"exposure-every-{rid}", daemon=True)
        self.repeats[rid] = rec
        rec["thread"].start()
        return self._repeat_info(rec)

    @staticmethod
    def _repeat_info(rec):
        return {k: rec.get(k) for k in ("id", "target", "interval", "on", "count", "runs",
                                        "errors", "last")} | {
            "running": "finished" not in rec}

    def cmd_repeats(self):
        return [self._repeat_info(r) for r in self.repeats.values()]

    def cmd_stop(self, repeat=None):
        recs = list(self.repeats.values()) if repeat in (None, "all") else [
            self.repeats.get(int(repeat))]
        stopped = []
        for r in recs:
            if r is None:
                raise LookupError(f"no repeat {repeat}")
            r["stop"].set()
            stopped.append(r["id"])
        return {"stopped": stopped}

    # -- presets ------------------------------------------------------------------------------
    def cmd_presets(self):
        return self.policy.presets if self.policy else {}


    # =====================================================================================
    # M4: eval / exec, output capture, asyncio awaiting, deny list, completion
    # =====================================================================================

    MAX_OUTPUT = 64 * 1024

    def _install_routers(self):
        with self._state_lock:
            self._install_routers_locked()

    def _install_routers_locked(self):
        if self._routers:
            return
        for name in ("stdout", "stderr"):
            current = getattr(sys, name)
            router = _Router(current)
            setattr(sys, name, router)
            self._routers.append((name, router))

    def _capturing(self, fn, capture):
        """Wrap ``fn`` so stdout/stderr it writes on the executing thread (or in
        tasks it starts) are captured; other threads' output passes through."""
        if not capture:
            return fn
        self._install_routers()
        cap = _Capture(self.MAX_OUTPUT)
        key = next(self._capture_seq)

        def run():
            token = _CAPTURE.set(cap)
            try:
                return fn()
            finally:
                _CAPTURE.reset(token)

        run._capture = (key, cap)
        return run

    def _await_anywhere(self, job, awaitable):
        """A call returned a coroutine: run it on a running asyncio loop, if any."""
        import asyncio
        for _tid, frames in self._all_stacks():
            if not frames:
                continue
            loop = executor_mod.running_asyncio_loop(frames[-1])
            if loop is not None:
                fut = asyncio.run_coroutine_threadsafe(awaitable, loop)
                fut.add_done_callback(job.finish_future)
                return True
        return False   # no loop: the coroutine object itself is the result

    def _check_deny(self, target, member=None):
        if not self.deny:
            return
        names = {str(target)}
        obj = member
        if isinstance(obj, types.MethodType):
            obj = obj.__func__
        mod = getattr(obj, "__module__", None)
        qual = getattr(obj, "__qualname__", None)
        if mod and qual:
            names.add(f"{mod}.{qual}")
        for pat in self.deny:
            for n in names:
                if fnmatch.fnmatchcase(n, pat):
                    raise PermissionError(f"{target!r} matches the deny list ({pat!r}); "
                                          "see [safety] deny in exposure.toml")

    def _eval_warnings(self, source):
        hits = []
        for pat in self.deny:
            word = pat.rstrip("*").rstrip(".").rsplit(".", 1)[-1]
            if word and word in source:
                hits.append(pat)
        return [f"source mentions {p!r} from the deny list; eval can't be sandboxed, so "
                "this is a warning only" for p in hits]

    def cmd_eval(self, source, mode="auto", frame=None, on=None, timeout=10.0,
                 confirmed=False, capture=True):
        """Run Python in the target. ``mode``: eval, exec or auto (eval if it
        parses as an expression). Namespace: a frame's (``frame`` = a path to a
        frame, e.g. the entry function's name or @N), else the session's
        scratch namespace, which can also read the entry frame's locals and
        globals. ``_`` is the last eval result."""
        self._check_writable()
        if not confirmed:
            raise PermissionError("eval/exec must be confirmed (the client asks first; "
                                  "`--yes` or `confirm off` in the shell skips the question)")
        if self.strict:
            raise PermissionError("strict mode (exposure.serve): eval/exec are disabled")
        if self.scope != "reachable" and not self.policy_allows_eval:
            raise PermissionError(f"{self.scope} scope: eval/exec is off unless the policy "
                                  "sets [safety] eval = true")
        if mode == "auto":
            try:
                compile(source, "<probe>", "eval")
                mode = "eval"
            except SyntaxError:
                mode = "exec"
        if mode not in ("eval", "exec"):
            raise ValueError("mode must be eval, exec or auto")
        with self._state_lock:
            self._eval_seq += 1
            fname = f"<exposure-eval-{self._eval_seq}>"
        code = compile(source, fname, mode)
        linecache.cache[fname] = (len(source), None, source.splitlines(True) or ["\n"], fname)
        extras = {"__exposure_h__": lambda n: self.handles.get(int(n))[0]}
        frame_obj = None
        if frame:
            frame_obj = None
            if not frame.startswith("@"):     # a running function's name: App.run, main
                for _tid, frames in self._all_stacks():
                    for f in reversed(frames):
                        if self._matches(f, frame):
                            frame_obj = f
                            break
                    if frame_obj is not None:
                        break
            if frame_obj is None:
                frame_obj, _perm = self._resolve(frame)
            if not isinstance(frame_obj, types.FrameType):
                raise TypeError(f"{frame} is a {inspector.type_name(frame_obj)}, not a frame")
            if not self._frame_alive(frame_obj):
                raise LookupError("frame has exited")
            g = frame_obj.f_globals
            ns = collections.ChainMap(frame_obj.f_locals, extras)
        else:
            g = self.scratch
            maps = [self.scratch]
            try:
                entry = self._entry_frame()
                maps += [entry.f_locals, entry.f_globals]
            except LookupError:
                pass
            ns = collections.ChainMap(*maps, extras)
        sys.audit("exposure.eval", source)

        def fn():
            if mode == "eval":
                value = eval(code, g, ns)
                self.scratch["_"] = value
                return value
            exec(code, g, ns)
            return None

        if frame_obj is not None and on is None:
            ex = self._frame_executor(frame_obj)
        else:
            ex = self._executor(on)
        wrapped = self._capturing(fn, capture)
        job = self._run(wrapped, f"{mode} {fname}", ex, timeout)
        rep = self._job_report(job)
        rep["mode"] = mode
        rep["namespace"] = f"frame {frame}" if frame else "scratch"
        warnings = self._eval_warnings(source)
        if warnings:
            rep["warnings"] = warnings
        return rep

    def cmd_scratch(self, clear=False):
        if clear:
            self.scratch.clear()
            self.scratch.update({"__builtins__": __builtins__, "__name__": "__exposure__"})
        return sorted(k for k in self.scratch if not k.startswith("__"))

    def cmd_complete(self, text, limit=200):
        """Completions for a partial path (read-only, for the shell's Tab)."""
        text = text or ""
        base, dot, prefix = text.rpartition(".")
        names = []
        if not dot:
            explicit = self._explicit_items()
            if self.scope != "explicit":
                try:
                    frame = self._entry_frame()
                    names += list(inspector.frame_locals(frame))
                    names += [n for n in frame.f_globals if not paths.is_dunder(n)]
                except LookupError:
                    pass
            names += list(explicit)
            names += [f"@{n}" for n in list(self.handles._by_n)[-200:]]
            names += [k for k in self.scratch if not k.startswith("__")]
            prefix = text
            out = sorted({n for n in names if n.startswith(prefix)})
        else:
            try:
                obj, _perm = self._resolve(base)
            except Exception:
                return []
            for label, seg, _kind, _value in inspector.children(obj, self.budget.tick):
                if seg is not None and seg[0] == "attr" and str(seg[1]).startswith(prefix):
                    names.append(f"{base}.{seg[1]}")
            if not isinstance(obj, (type, types.ModuleType, types.FrameType)):
                for klass in type(obj).__mro__:          # methods and class attributes
                    if klass is object:
                        continue
                    for k in type.__dict__["__dict__"].__get__(klass):
                        if not paths.is_dunder(k) and k.startswith(prefix):
                            names.append(f"{base}.{k}")
            out = sorted(set(names))
        return out[:int(limit)]


_CAPTURE = contextvars.ContextVar("exposure_capture", default=None)


class _Capture:
    def __init__(self, cap):
        self.cap = cap
        self.parts = []
        self.size = 0
        self.truncated = False

    def write(self, s):
        if self.size < self.cap:
            room = self.cap - self.size
            self.parts.append(s[:room])
            self.size += min(len(s), room)
            if len(s) > room:
                self.truncated = True
        else:
            self.truncated = True

    def text(self):
        return "".join(self.parts), self.truncated


class _Router(io.TextIOBase):
    """Stands in for sys.stdout/stderr while an Exposure session is attached:
    writes go to the capture buffer of the current context (the thread or
    task running an Exposure job), everything else to the original stream."""

    def __init__(self, orig):
        super().__init__()
        self.orig = orig

    def write(self, s):
        cap = _CAPTURE.get()
        if cap is not None:
            cap.write(str(s))
            return len(s)
        if self.orig is None:
            return len(s)
        return self.orig.write(s)

    def flush(self):
        if _CAPTURE.get() is None and self.orig is not None:
            try:
                self.orig.flush()
            except Exception:
                pass

    def writable(self):
        return True

    def isatty(self):
        try:
            return self.orig.isatty()
        except Exception:
            return False

    def fileno(self):
        return self.orig.fileno()

    @property
    def encoding(self):
        return getattr(self.orig, "encoding", "utf-8")

    @property
    def buffer(self):
        return getattr(self.orig, "buffer", None)

    def __getattr__(self, name):
        return getattr(self.orig, name)


class _SetResult:
    __slots__ = ("old", "existed", "new", "change_id")

    def __init__(self, old, existed, new, change_id):
        self.old, self.existed, self.new, self.change_id = old, existed, new, change_id


# -- entry point used by the bootstrap -------------------------------------------------

_AUDITED = {"call", "set", "nudge", "undo", "do", "eval", "pause", "step", "resume",
            "every", "stop", "detach"}

_start_lock = threading.Lock()


def start(config: dict):
    """Start the agent once per process; later calls refresh the session file."""
    with _start_lock:
        existing = getattr(sys, "_exposure_agent", None)
        if existing is not None:
            if existing.ensure_alive():
                return existing
            existing.thread.join(2.0)    # detaching: let its cleanup finish
            if getattr(sys, "_exposure_agent", None) is existing:
                try:
                    del sys._exposure_agent
                except BaseException:
                    pass
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
