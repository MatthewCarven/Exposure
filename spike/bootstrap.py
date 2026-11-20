# Exposure M0 spike - agent bootstrap.
#
# This file is handed to sys.remote_exec(pid, path). The target interpreter
# runs it on whichever thread next reaches an eval-breaker check (usually the
# main thread), so it must be quick: it only starts a daemon thread and returns.
#
# Rules this agent keeps (see the Exposure design notes):
#   * stdlib only;
#   * no exception may escape anything that runs in the target (PSR hooks
#     threading.excepthook / sys.excepthook and would report it as a crash);
#   * reads happen on the agent thread and never take the app's locks
#     (especially NumpyBackend._lock, held by the PortAudio callback);
#   * anything that touches DearPyGui or the model runs on the GUI thread via
#     a sys.monitoring LINE hook placed at the top of App.run's render loop.

def _exposure_bootstrap():
    import sys

    existing = getattr(sys, "_exposure_agent", None)
    if existing is not None:
        try:
            existing.ensure_alive()
        except Exception:
            pass
        return

    import json
    import linecache
    import os
    import queue
    import reprlib
    import secrets
    import socket
    import struct
    import threading
    import time
    import traceback

    _repr = reprlib.Repr()
    _repr.maxstring = 120
    _repr.maxother = 120
    _repr.maxlist = 12
    _repr.maxdict = 24

    def safe_repr(obj):
        try:
            return _repr.repr(obj)
        except Exception as exc:  # repr itself can fail
            return f"<repr failed: {type(exc).__name__}>"

    def session_dir():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "exposure", "sessions")

    def stats(samples):
        if not samples:
            return {}
        s = sorted(samples)
        n = len(s)

        def pct(p):
            return s[min(n - 1, int(round(p / 100.0 * (n - 1))))]

        mean = sum(s) / n
        return {
            "n": n,
            "mean_ms": round(mean * 1000, 3),
            "median_ms": round(pct(50) * 1000, 3),
            "p95_ms": round(pct(95) * 1000, 3),
            "p99_ms": round(pct(99) * 1000, 3),
            "max_ms": round(s[-1] * 1000, 3),
            "min_ms": round(s[0] * 1000, 3),
            "fps": round(1.0 / mean, 2) if mean > 0 else None,
        }

    class MainThreadHook:
        """Runs queued callables (and records frame times) at the top of
        App.run's ``while dpg.is_dearpygui_running()`` loop, on the GUI thread.

        Uses a sys.monitoring LINE event, local to App.run's code object.
        Every other line in App.run returns DISABLE, so after one pass only the
        loop-top line keeps firing. When there is no work the local events are
        switched off entirely.
        """

        def __init__(self, code, target_line):
            self.code = code
            self.target_line = target_line
            self.tool = None
            self.work = queue.SimpleQueue()
            self.pending = 0
            self.lock = threading.Lock()   # guards pending/timing/enabled
            self.enabled = False
            self.timing = None             # list of perf_counter stamps
            self.timing_n = 0
            self.last_stamps = None
            self.timing_done = threading.Event()
            self.hits = 0
            self.main_ident = None
            self.errors = []
            mon = sys.monitoring
            for tid in range(6):  # 0..5 are free for any tool
                if mon.get_tool(tid) is None:
                    mon.use_tool_id(tid, "exposure")
                    self.tool = tid
                    break
            if self.tool is None:
                raise RuntimeError("no free sys.monitoring tool id")
            mon.register_callback(self.tool, mon.events.LINE, self._on_line)

        # -- enable/disable (call with self.lock held) -----------------------
        def _want_on(self):
            return self.pending > 0 or self.timing is not None

        def _sync_events(self):
            want = self._want_on()
            if want != self.enabled:
                sys.monitoring.set_local_events(
                    self.tool, self.code,
                    sys.monitoring.events.LINE if want else 0)
                self.enabled = want

        # -- the callback (GUI thread) ---------------------------------------
        def _on_line(self, code, line):
            try:
                if line != self.target_line:
                    return sys.monitoring.DISABLE
                self.hits += 1
                self.main_ident = threading.get_ident()
                if self.timing is not None:
                    self.timing.append(time.perf_counter())
                    if len(self.timing) > self.timing_n:
                        with self.lock:
                            self.last_stamps = self.timing
                            self.timing = None
                        self.timing_done.set()
                while True:
                    try:
                        item = self.work.get_nowait()
                    except queue.Empty:
                        break
                    fn, box, ev = item
                    t0 = time.perf_counter()
                    try:
                        box["result"] = fn()
                        box["ok"] = True
                    except BaseException as exc:  # never escape into App.run
                        box["ok"] = False
                        box["error"] = f"{type(exc).__name__}: {exc}"
                        box["tb"] = traceback.format_exc(limit=8)
                    box["main_ms"] = round((time.perf_counter() - t0) * 1000, 3)
                    box["thread"] = threading.current_thread().name
                    with self.lock:
                        self.pending -= 1
                    ev.set()
                with self.lock:
                    self._sync_events()
            except BaseException as exc:
                try:
                    self.errors.append(f"{type(exc).__name__}: {exc}")
                except BaseException:
                    pass
            return None

        # -- agent-thread API --------------------------------------------------
        def submit(self, fn, timeout=5.0):
            box = {}
            ev = threading.Event()
            t0 = time.perf_counter()
            with self.lock:
                self.pending += 1
                self.work.put((fn, box, ev))
                self._sync_events()
            if not ev.wait(timeout):
                box.setdefault("ok", False)
                box.setdefault("error", f"timed out after {timeout}s waiting for GUI loop")
            box["wait_ms"] = round((time.perf_counter() - t0) * 1000, 3)
            return box

        def start_timing(self, n):
            self.timing_done.clear()
            with self.lock:
                self.last_stamps = None
                self.timing_n = n
                self.timing = []
                self._sync_events()

        def close(self):
            mon = sys.monitoring
            try:
                mon.set_local_events(self.tool, self.code, 0)
            except Exception:
                pass
            try:
                mon.register_callback(self.tool, mon.events.LINE, None)
            except Exception:
                pass
            try:
                mon.free_tool_id(self.tool)
            except Exception:
                pass

    class Agent:
        def __init__(self):
            self.token = secrets.token_hex(16)
            self.stop = threading.Event()
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(4)
            self.sock.settimeout(0.5)
            self.port = self.sock.getsockname()[1]
            self.started = time.time()
            self.app = None
            self.app_code = None
            self.hook = None
            self.thread = threading.Thread(target=self._serve, name="exposure-agent",
                                           daemon=True)

        # -- session file ------------------------------------------------------
        def session_path(self):
            return os.path.join(session_dir(), f"{os.getpid()}.json")

        def write_session(self):
            d = session_dir()
            os.makedirs(d, exist_ok=True)
            tmp = self.session_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "port": self.port, "token": self.token,
                           "started": self.started}, f)
            os.replace(tmp, self.session_path())

        def ensure_alive(self):
            if self.thread.is_alive():
                self.write_session()

        # -- server ------------------------------------------------------------
        def _serve(self):
            try:
                self.write_session()
                while not self.stop.is_set():
                    try:
                        conn, _ = self.sock.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        self._handle(conn)
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

        @staticmethod
        def _recv_exact(conn, n):
            buf = b""
            while len(buf) < n:
                chunk = conn.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("closed")
                buf += chunk
            return buf

        def _recv(self, conn):
            (n,) = struct.unpack(">I", self._recv_exact(conn, 4))
            if n > 16 * 1024 * 1024:
                raise ValueError("message too large")
            return json.loads(self._recv_exact(conn, n).decode("utf-8"))

        @staticmethod
        def _send(conn, obj):
            data = json.dumps(obj, default=safe_repr).encode("utf-8")
            conn.sendall(struct.pack(">I", len(data)) + data)

        def _handle(self, conn):
            conn.settimeout(120)
            hello = self._recv(conn)
            if not isinstance(hello, dict) or not secrets.compare_digest(
                    str(hello.get("token", "")), self.token):
                self._send(conn, {"ok": False, "error": "bad token"})
                return
            self._send(conn, {"ok": True, "pid": os.getpid()})
            while not self.stop.is_set():
                try:
                    req = self._recv(conn)
                except (ConnectionError, OSError):
                    return
                cmd = req.get("cmd")
                args = req.get("args") or {}
                fn = getattr(self, "cmd_" + str(cmd), None)
                t0 = time.perf_counter()
                if fn is None:
                    resp = {"ok": False, "error": f"unknown command {cmd!r}"}
                else:
                    try:
                        resp = {"ok": True, "result": fn(**args)}
                    except BaseException as exc:
                        resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                "tb": traceback.format_exc(limit=10)}
                resp["agent_ms"] = round((time.perf_counter() - t0) * 1000, 3)
                self._send(conn, resp)
                if cmd == "detach":
                    self.stop.set()
                    return

        def _cleanup(self):
            try:
                if self.hook is not None:
                    self.hook.close()
            except BaseException:
                pass
            self.hook = None
            try:
                self.sock.close()
            except BaseException:
                pass
            try:
                os.remove(self.session_path())
            except BaseException:
                pass
            try:
                if getattr(sys, "_exposure_agent", None) is self:
                    del sys._exposure_agent
            except BaseException:
                pass

        # -- helpers -------------------------------------------------------------
        def _find_app(self):
            if self.app is not None:
                return self.app
            for tid, frame in sys._current_frames().items():
                f = frame
                while f is not None:
                    co = f.f_code
                    if (co.co_name == "run" and getattr(co, "co_qualname", "") == "App.run"
                            and f.f_globals.get("__name__") == "pysynthrack.ui.app"):
                        self.app = f.f_locals.get("self")
                        self.app_code = co
                        self.app_thread = tid
                        return self.app
                    f = f.f_back
            raise LookupError("no pysynthrack.ui.app App.run frame found")

        def _get_hook(self):
            if self.hook is None:
                self._find_app()
                code = self.app_code
                target = None
                for _, _, ln in code.co_lines():
                    if ln is None:
                        continue
                    if "is_dearpygui_running" in linecache.getline(code.co_filename, ln):
                        target = ln
                        break
                if target is None:
                    raise LookupError("loop-top line not found in App.run")
                self.hook = MainThreadHook(code, target)
            return self.hook

        def _thread_names(self):
            return {t.ident: t.name for t in threading.enumerate()}

        # -- commands ---------------------------------------------------------------
        def cmd_ping(self):
            return {"pid": os.getpid(), "python": sys.version, "agent_thread":
                    threading.current_thread().name, "uptime_s": round(time.time() - self.started, 2)}

        def cmd_threads(self):
            names = self._thread_names()
            out = []
            for tid, frame in sys._current_frames().items():
                depth = 0
                f = frame
                while f is not None:
                    depth += 1
                    f = f.f_back
                out.append({
                    "ident": tid,
                    "name": names.get(tid, "<no threading.Thread>"),
                    "top": f"{frame.f_code.co_filename}:{frame.f_lineno} "
                           f"{getattr(frame.f_code, 'co_qualname', frame.f_code.co_name)}",
                    "depth": depth,
                })
            enumerated = [{"name": t.name, "ident": t.ident, "daemon": t.daemon,
                           "native_id": t.native_id} for t in threading.enumerate()]
            return {"frames": out, "threading.enumerate": enumerated,
                    "main_thread": threading.main_thread().ident}

        def cmd_modules(self):
            app = self._find_app()
            for _attempt in range(5):
                try:
                    items = list(app.patch.modules.items())
                    break
                except RuntimeError:
                    continue
            else:
                raise RuntimeError("patch.modules kept changing")
            out = []
            for mid, m in items:
                params = None
                for _attempt in range(5):
                    try:
                        params = dict(m.params)
                        break
                    except RuntimeError:
                        continue
                out.append({"id": mid, "type": getattr(m, "TYPE", type(m).__name__),
                            "name": safe_repr(getattr(m, "name", None)),
                            "params": {k: safe_repr(v) for k, v in (params or {}).items()}})
            return out

        def cmd_backend(self):
            b = self._find_app().backend
            out = {"class": type(b).__name__}
            for attr in ("name", "is_running", "sample_rate", "_running", "_xruns",
                         "_input_xruns", "_host_api", "_dsp_load", "_dsp_load_peak",
                         "_dsp_overloads", "_block_size", "block_size"):
                try:
                    v = getattr(b, attr)
                except Exception:
                    continue
                out[attr] = v if isinstance(v, (int, float, str, bool, type(None))) else safe_repr(v)
            return out

        def cmd_hook_info(self):
            h = self._get_hook()
            return {"tool": h.tool, "target_line": h.target_line, "enabled": h.enabled,
                    "hits": h.hits, "errors": h.errors[-5:], "main_ident": h.main_ident}

        def cmd_eval(self, expr):
            """Evaluate on the AGENT thread (reads only, please)."""
            ns = {"app": self._find_app(), "sys": sys, "threading": threading}
            return safe_repr(eval(expr, ns))

        def cmd_eval_main(self, expr, timeout=5.0):
            """Evaluate on the GUI thread at the top of the render loop."""
            app = self._find_app()
            ns = {"app": app, "sys": sys, "threading": threading}
            box = self._get_hook().submit(lambda: safe_repr(eval(expr, ns)), timeout)
            box.pop("tb", None) if box.get("ok") else None
            return box

        def cmd_toggle_audio(self):
            app = self._find_app()
            box = self._get_hook().submit(app._on_toggle_audio, 10.0)
            box["backend"] = self.cmd_backend()
            return box

        def cmd_set_param(self, module_id, name, value):
            app = self._find_app()
            box = self._get_hook().submit(
                lambda: app._set_module_param(int(module_id), name, value))
            box["readback"] = self._readback(module_id, name)
            return box

        def _readback(self, module_id, name):
            app = self._find_app()
            out = {}
            m = app.patch.modules.get(int(module_id))
            out["model"] = safe_repr(m.params.get(name)) if m is not None else None
            bp = getattr(app.backend, "_patch", None)
            if bp is not None:
                bm = bp.modules.get(int(module_id))
                out["backend_patch"] = safe_repr(bm.params.get(name)) if bm is not None else None
                out["backend_patch_is_model"] = bp is app.patch
            def widget():
                import dearpygui.dearpygui as dpg
                wid = app._param_widget(int(module_id), name)
                return {"widget_id": wid,
                        "widget_value": safe_repr(dpg.get_value(wid)) if wid else None}
            out["widget"] = self._get_hook().submit(widget)
            return out

        def cmd_frametime(self, n=300, during="none", module_id=None, name=None,
                          start=None, end=None, timeout=30.0, interval=0.0):
            hook = self._get_hook()
            b = self._find_app().backend
            x0 = (getattr(b, "_xruns", None), getattr(b, "_dsp_overloads", None))
            hook.start_timing(int(n))
            t_start = time.perf_counter()
            extra = {"lists": 0, "param_sets": 0, "param_fail": 0, "list_ms": []}
            if during == "list":
                while not hook.timing_done.is_set() and time.perf_counter() - t_start < timeout:
                    t0 = time.perf_counter()
                    self.cmd_modules()
                    extra["list_ms"].append(time.perf_counter() - t0)
                    extra["lists"] += 1
                    if interval:
                        time.sleep(float(interval))
            elif during == "param":
                i = 0
                while not hook.timing_done.is_set() and time.perf_counter() - t_start < timeout:
                    frac = min(1.0, i / max(1, int(n) // 2))
                    v = float(start) + (float(end) - float(start)) * frac
                    app = self._find_app()
                    box = hook.submit(lambda v=v: app._set_module_param(int(module_id), name, v))
                    extra["param_sets"] += 1
                    if not box.get("ok"):
                        extra["param_fail"] += 1
                    i += 1
            ok = hook.timing_done.wait(timeout)
            with hook.lock:
                stamps = hook.last_stamps
            x1 = (getattr(b, "_xruns", None), getattr(b, "_dsp_overloads", None))
            res = {"completed": ok, "wall_s": round(time.perf_counter() - t_start, 3),
                   "xruns_delta": (x1[0] - x0[0]) if None not in (x0[0], x1[0]) else None,
                   "dsp_overloads_delta": (x1[1] - x0[1]) if None not in (x0[1], x1[1]) else None,
                   "xruns_now": x1[0]}
            if stamps:
                diffs = [b2 - a2 for a2, b2 in zip(stamps, stamps[1:])]
                res["frame"] = stats(diffs)
            if extra["list_ms"]:
                res["list_call"] = stats(extra["list_ms"])
                res["lists"] = extra["lists"]
            if during == "param":
                res["param_sets"] = extra["param_sets"]
                res["param_fail"] = extra["param_fail"]
                res["readback"] = self._readback(module_id, name)
            with hook.lock:
                hook.timing = None
                hook._sync_events()
            return res

        def cmd_callback_thread(self, seconds=1.0):
            """Find which thread DearPyGui runs Python callbacks on, by binding a
            temporary item-visible handler to a temporary blank text item."""
            app = self._find_app()
            seen = {}

            def cb(sender, app_data, user_data=None):
                t = threading.current_thread()
                seen[threading.get_ident()] = t.name

            ids = {}

            def install():
                import dearpygui.dearpygui as dpg
                from pysynthrack.ui.app import MAIN_WINDOW_TAG
                ids["text"] = dpg.add_text(".", parent=MAIN_WINDOW_TAG)
                with dpg.item_handler_registry() as reg:
                    dpg.add_item_visible_handler(callback=cb)
                ids["reg"] = reg
                dpg.bind_item_handler_registry(ids["text"], reg)
                ids["frame"] = dpg.get_frame_count() + 3

                def frame_cb(sender=None, app_data=None, user_data=None):
                    seen.setdefault("frame_callback", []).append(
                        (threading.get_ident(), threading.current_thread().name))
                dpg.set_frame_callback(ids["frame"], frame_cb)
                return dict(ids)

            def remove():
                import dearpygui.dearpygui as dpg
                dpg.delete_item(ids["text"])
                dpg.delete_item(ids["reg"])
                return True

            hook = self._get_hook()
            r1 = hook.submit(install)
            time.sleep(float(seconds))
            r2 = hook.submit(remove)
            return {"install": r1, "remove": r2, "callback_threads": seen,
                    "gui_thread_ident": hook.main_ident,
                    "main_thread_ident": threading.main_thread().ident,
                    "names": self._thread_names()}

        def cmd_detach(self):
            if self.hook is not None:
                self.hook.close()
                self.hook = None
            return {"detaching": True, "thread": self.thread.name}

    agent = Agent()
    sys._exposure_agent = agent
    agent.thread.start()


try:
    _exposure_bootstrap()
except BaseException:
    pass
finally:
    try:
        del _exposure_bootstrap
    except BaseException:
        pass
