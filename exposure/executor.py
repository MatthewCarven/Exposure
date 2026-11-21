"""Where calls and sets run: between frames on the main loop, on a named
thread at its next line, or on a worker thread of the agent's own.

Standard library only (Qt support uses the target's own Qt if it is loaded).

Main-thread strategies, chosen when work is first queued:

* **qt**: a ``QApplication`` exists. A helper QObject living in the GUI
  thread is signalled from the agent thread (Qt's own thread-safe route).
* **tk**: ``tkinter`` has a default root. ``root.after(0, ...)`` is called
  from the agent thread; threaded Tcl marshals it onto the interpreter thread.
* **loop**: the main thread is inside a Python loop (a ``while running:`` game
  loop, PySynthRack's render loop). A ``sys.monitoring`` JUMP event local to
  that loop's code object fires on the loop's back-edge, so queued work runs
  *between iterations*. Other jumps return DISABLE.
* **line**: no loop found. LINE events on every code object on the thread's
  stack; work runs at the thread's next line (a one-shot breakpoint).

Hooks are switched on only while there is work (or a frame sample) pending.
Work queued on the main thread gets a per-iteration budget (default 2 ms):
what doesn't fit waits for the next iteration. Each job records how long it
blocked the thread.
"""
from __future__ import annotations

import collections
import dis
import itertools
import os
import sys
import threading
import time
import traceback

_job_ids = itertools.count(1)
_PKG_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
MAX_JOBS = 200


class Job:
    __slots__ = ("id", "fn", "label", "where", "created", "started", "finished",
                 "ok", "result", "exc", "tb", "blocked_ms", "thread", "done")

    def __init__(self, fn, label, where):
        self.id = next(_job_ids)
        self.fn = fn
        self.label = label
        self.where = where
        self.created = time.time()
        self.started = None
        self.finished = None
        self.ok = None
        self.result = None
        self.exc = None
        self.tb = None
        self.blocked_ms = None
        self.thread = None
        self.done = threading.Event()

    def run(self):
        self.started = time.time()
        self.thread = threading.current_thread().name
        t0 = time.perf_counter()
        try:
            self.result = self.fn()
            self.ok = True
        except BaseException as exc:  # never escape into the target's thread
            self.ok = False
            self.exc = exc
            try:
                te = traceback.TracebackException.from_exception(exc)
                te.stack = traceback.StackSummary.from_list(
                    [f for f in te.stack if not f.filename.startswith(_PKG_DIR)][-20:])
                self.tb = "".join(te.format())
            except BaseException:
                self.tb = None
        self.blocked_ms = round((time.perf_counter() - t0) * 1000, 3)
        self.finished = time.time()
        self.fn = None
        self.done.set()

    @property
    def state(self):
        if self.done.is_set():
            return "done" if self.ok else "error"
        return "running" if self.started else "queued"


def _backward_jumps(code):
    """``[(src_offset, dst_offset)]`` for the code object's backward jumps."""
    out = []
    for ins in dis.get_instructions(code):
        if ins.opname.startswith("JUMP_BACKWARD") and ins.jump_target is not None:
            out.append((ins.offset, ins.jump_target))
    return out


def find_loop_frame(top, prefer_code=None):
    """The frame on this stack that is running a loop, and its back-edge.

    Returns ``(frame, (src, dst))`` or ``(None, None)``. Picks ``prefer_code``'s
    frame if it is in a loop, else the outermost frame currently inside a loop.
    """
    frames = []
    f = top
    while f is not None:
        frames.append(f)
        f = f.f_back
    frames.reverse()  # outermost first
    found = []
    for f in frames:
        lasti = f.f_lasti
        best = None
        for src, dst in _backward_jumps(f.f_code):
            if dst <= lasti <= src and (best is None or src - dst < best[0] - best[1]):
                best = (src, dst)  # innermost loop enclosing the current position
        if best is not None:
            found.append((f, best))
    if not found:
        return None, None
    if prefer_code is not None:
        for f, edge in found:
            if f.f_code is prefer_code:
                return f, edge
    return found[0]


class ThreadExecutor:
    """Runs jobs on one target thread. See the module docstring."""

    def __init__(self, tid, *, budget_s=0.002, prefer_code=None, is_main=False):
        self.tid = tid
        self.is_main = is_main
        self.budget_s = budget_s
        self.prefer_code = prefer_code
        self.queue = collections.deque()
        self.lock = threading.Lock()
        self.tool = None
        self.mode = None
        self.codes = []          # code objects with local events enabled
        self.edge = None         # (code, src, dst) for loop mode
        self.enabled = False
        self.sampling = None     # list of perf_counter stamps
        self.sample_n = 0
        self.sample_done = threading.Event()
        self.last_samples = None
        self.iterations = 0
        self.errors = []
        self._tk_root = None
        self._qt = None

    # -- strategy -----------------------------------------------------------------
    def _top_frame(self):
        frame = sys._current_frames().get(self.tid)
        if frame is None:
            raise LookupError("the target thread has no Python frames (exited, or in C)")
        return frame

    def _pick_mode(self):
        if self.is_main:
            qt = self._qt_app()
            if qt is not None:
                return "qt"
            root = self._tk_default_root()
            if root is not None:
                self._tk_root = root
                return "tk"
            frame, edge = find_loop_frame(self._top_frame(), self.prefer_code)
            if frame is not None:
                self.edge = (frame.f_code, edge[0], edge[1])
                return "loop"
        return "line"

    @staticmethod
    def _tk_default_root():
        tk = sys.modules.get("tkinter")
        root = getattr(tk, "_default_root", None) if tk else None
        return root

    @staticmethod
    def _qt_app():
        for name in ("PySide6.QtWidgets", "PyQt6.QtWidgets", "PySide6.QtCore", "PyQt6.QtCore"):
            mod = sys.modules.get(name)
            if mod is None:
                continue
            for cls_name in ("QApplication", "QCoreApplication"):
                cls = getattr(mod, cls_name, None)
                if cls is not None:
                    try:
                        app = cls.instance()
                    except Exception:
                        app = None
                    if app is not None:
                        return app
        return None

    def _ensure_tool(self):
        if self.tool is not None:
            return
        mon = sys.monitoring
        for tid in range(6):
            if mon.get_tool(tid) is None:
                mon.use_tool_id(tid, "exposure")
                self.tool = tid
                break
        if self.tool is None:
            raise RuntimeError("no free sys.monitoring tool id (another debugger/profiler "
                               "is using all six)")
        mon.register_callback(self.tool, mon.events.JUMP, self._on_jump)
        mon.register_callback(self.tool, mon.events.LINE, self._on_line)

    # -- enable / disable (lock held) ------------------------------------------------
    def _want(self):
        return bool(self.queue) or self.sampling is not None

    def _sync(self):
        if self.mode in ("tk", "qt"):
            return
        want = self._want()
        if want == self.enabled:
            return
        mon = sys.monitoring
        if want:
            self._ensure_tool()
            if self.mode == "loop":
                self.codes = [self.edge[0]]
                mon.set_local_events(self.tool, self.edge[0], mon.events.JUMP)
            else:
                codes = []
                f = self._top_frame()
                while f is not None:
                    if f.f_code not in codes:
                        codes.append(f.f_code)
                    f = f.f_back
                self.codes = codes
                for co in codes:
                    mon.set_local_events(self.tool, co, mon.events.LINE)
        else:
            for co in self.codes:
                try:
                    mon.set_local_events(self.tool, co, 0)
                except Exception:
                    pass
            self.codes = []
        self.enabled = want

    # -- callbacks (target thread) ----------------------------------------------------
    def _on_jump(self, code, src, dst):
        try:
            edge = self.edge
            if edge is None or code is not edge[0] or src != edge[1] or dst != edge[2]:
                return sys.monitoring.DISABLE
            if threading.get_ident() != self.tid:
                return None
            self._iteration()
        except BaseException as exc:
            self._note_error(exc)
        return None

    def _on_line(self, code, line):
        try:
            if self.mode != "line" or threading.get_ident() != self.tid:
                return None
            self._iteration()
        except BaseException as exc:
            self._note_error(exc)
        return None

    def _note_error(self, exc):
        try:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            del self.errors[:-20]
        except BaseException:
            pass

    def _iteration(self):
        self.iterations += 1
        now = time.perf_counter()
        if self.sampling is not None:
            self.sampling.append(now)
            if len(self.sampling) > self.sample_n:
                with self.lock:
                    self.last_samples = self.sampling
                    self.sampling = None
                self.sample_done.set()
        self._drain()
        with self.lock:
            self._sync()

    def _drain(self):
        start = time.perf_counter()
        while True:
            with self.lock:
                if not self.queue:
                    return
                job = self.queue.popleft()
            job.run()
            if time.perf_counter() - start >= self.budget_s:
                return  # the rest waits for the next iteration

    # -- tk / qt -------------------------------------------------------------------------
    def _tk_kick(self):
        root = self._tk_root
        root.after(0, self._tk_run)

    def _tk_run(self):
        try:
            self._drain()
            with self.lock:
                more = bool(self.queue)
            if more:
                self._tk_root.after(1, self._tk_run)
        except BaseException as exc:
            self._note_error(exc)

    def _qt_kick(self):
        if self._qt is None:
            self._qt = _make_qt_bridge(self._qt_app(), self._qt_run)
        self._qt.kick()

    def _qt_run(self):
        try:
            self._drain()
            with self.lock:
                more = bool(self.queue)
            if more:
                self._qt.kick()
        except BaseException as exc:
            self._note_error(exc)

    # -- agent-thread API --------------------------------------------------------------
    def submit(self, job: Job):
        with self.lock:
            if self.mode is None:
                self.mode = self._pick_mode()
            self.queue.append(job)
            self._sync()
        if self.mode == "tk":
            self._tk_kick()
        elif self.mode == "qt":
            self._qt_kick()
        return job

    def sample(self, n, timeout):
        """Timestamps of ``n`` consecutive loop iterations (loop mode only)."""
        with self.lock:
            if self.mode is None:
                self.mode = self._pick_mode()
            if self.mode != "loop":
                raise LookupError(f"frame sampling needs a Python main loop "
                                  f"(this thread's executor is in {self.mode!r} mode)")
            self.sample_done.clear()
            self.last_samples = None
            self.sample_n = int(n)
            self.sampling = []
            self._sync()
        ok = self.sample_done.wait(timeout)
        with self.lock:
            stamps = self.last_samples if ok else self.sampling
            self.sampling = None
            self._sync()
        return ok, stamps or []

    def info(self):
        out = {"thread": self.tid, "mode": self.mode, "queued": len(self.queue),
               "enabled": self.enabled, "iterations": self.iterations,
               "budget_ms": self.budget_s * 1000, "tool": self.tool,
               "errors": self.errors[-5:]}
        if self.edge is not None:
            co = self.edge[0]
            out["loop"] = {"function": co.co_qualname, "file": co.co_filename,
                           "back_edge": [self.edge[1], self.edge[2]]}
        return out

    def close(self):
        with self.lock:
            self.queue.clear()
            self.sampling = None
            self._sync()
        if self._qt is not None:
            try:
                self._qt.bridge.go.disconnect()
                self._qt.bridge.deleteLater()
            except Exception:
                pass
            self._qt = None
        self._tk_root = None
        if self.tool is not None:
            mon = sys.monitoring
            for co in self.codes:
                try:
                    mon.set_local_events(self.tool, co, 0)
                except Exception:
                    pass
            for ev in (mon.events.JUMP, mon.events.LINE):
                try:
                    mon.register_callback(self.tool, ev, None)
                except Exception:
                    pass
            try:
                mon.free_tool_id(self.tool)
            except Exception:
                pass
            self.tool = None


def _make_qt_bridge(app, run):
    """A QObject in the GUI thread whose signal runs ``run`` there."""
    mod = type(app).__module__.split(".")[0]
    if mod == "PySide6":
        from PySide6.QtCore import QObject, Qt, Signal  # type: ignore
    else:
        from PyQt6.QtCore import QObject, Qt, pyqtSignal as Signal  # type: ignore

    class Bridge(QObject):
        go = Signal()

    bridge = Bridge()
    bridge.moveToThread(app.thread())
    bridge.go.connect(run, Qt.ConnectionType.QueuedConnection)

    class Kicker:
        def __init__(self):
            self.bridge = bridge

        def kick(self):
            self.bridge.go.emit()

    return Kicker()


class AgentExecutor:
    """Runs each job on a fresh daemon worker thread, so the agent can stop
    waiting on a timeout without blocking its own request loop."""

    mode = "agent"

    def submit(self, job: Job):
        threading.Thread(target=job.run, name=f"exposure-call-{job.id}", daemon=True).start()
        return job

    def info(self):
        return {"mode": "agent"}

    def close(self):
        pass


class Jobs:
    def __init__(self):
        self._jobs = collections.OrderedDict()
        self._lock = threading.Lock()

    def add(self, job):
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > MAX_JOBS:
                self._jobs.popitem(last=False)
        return job

    def get(self, jid):
        with self._lock:
            try:
                return self._jobs[int(jid)]
            except KeyError:
                raise LookupError(f"no job {jid} (finished jobs are kept for the last "
                                  f"{MAX_JOBS})") from None

    def all(self):
        with self._lock:
            return list(self._jobs.values())

    def pending(self):
        return [j for j in self.all() if not j.done.is_set()]
