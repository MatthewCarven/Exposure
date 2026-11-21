"""Client library: attach to a process and talk to its agent.

    from exposure.client import Client
    with Client.attach(pid, entry="App.run") as c:
        print(c.ls())
        print(c.ls("self.patch.modules"))

Attaching injects a small bootstrap with ``sys.remote_exec`` (PEP 768, Python
3.14+). The bootstrap puts Exposure's install directory on the target's
``sys.path`` just long enough to import ``exposure.agent`` and start it, so
the target doesn't need Exposure installed.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

from . import exprs, procs, protocol

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOOTSTRAP = '''\
# Exposure bootstrap, run by sys.remote_exec. Generated; it deletes itself
# when it runs. If the client gave up waiting (the target was blocked in C),
# a late run finds the attempt expired and exits quietly.
def _exposure_boot():
    import json
    import os
    import sys
    import time
    config = json.loads({config!r})
    try:
        os.remove(config["boot_file"])
    except OSError:
        pass
    if time.time() > config["expires"]:
        return
    agent = getattr(sys, "_exposure_agent", None)
    if agent is not None:
        try:
            if agent.ensure_alive():
                return
        except BaseException:
            return
    root = {root!r}
    # A previous session's agent modules may still be cached in sys.modules;
    # drop them so this attach runs the Exposure code on disk now. Keep the
    # package and its registry: the program may hold exposure.expose() items.
    for name in [m for m in list(sys.modules) if m.startswith("exposure.")]:
        if name == "exposure.registry":
            continue
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None) or ""
        if f.startswith(root):
            del sys.modules[name]
            pkg = sys.modules.get("exposure")
            # `from exposure import agent` would take the package attribute
            # without importing, so drop that as well
            try:
                delattr(pkg, name.split(".", 1)[1])
            except (AttributeError, TypeError):
                pass
    added = root not in sys.path
    try:
        if added:
            sys.path.insert(0, root)
        from exposure import agent as _agent
        _agent.start(config)
    except BaseException:
        try:
            import traceback
            with open(config["error_file"], "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except BaseException:
            pass
    finally:
        if added:
            try:
                sys.path.remove(root)
            except ValueError:
                pass


try:
    _exposure_boot()
except BaseException:
    pass
finally:
    try:
        del _exposure_boot
    except BaseException:
        pass
'''


class AttachError(RuntimeError):
    pass


class RemoteError(RuntimeError):
    def __init__(self, message, traceback_text=None):
        super().__init__(message)
        self.traceback = traceback_text


class ConnectionLost(RemoteError):
    """The agent's connection dropped mid-request (target exited or detached)."""


def _connect(pid: int, timeout=2.0):
    """Connect using an existing session file; None if there's no live agent."""
    try:
        with open(protocol.session_path(pid), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None, None
    try:
        sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout)
    except (OSError, KeyError, ValueError):
        return None, info
    try:
        sock.settimeout(300)
        protocol.send_msg(sock, {"token": info["token"]})
        hello = protocol.recv_msg(sock)
    except (OSError, ValueError, ConnectionError):
        sock.close()
        return None, info
    if not hello.get("ok"):
        sock.close()
        if hello.get("error") == "detaching":   # a dying agent: inject a fresh one
            return None, info
        raise AttachError(f"agent refused the session token: {hello.get('error')}")
    return sock, hello


def clean_stale() -> list[int]:
    """Remove session files whose process is gone. Returns their pids."""
    removed = []
    d = protocol.session_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return removed
    for name in names:
        stem = name.split(".", 1)[0]
        if not stem.isdigit():
            continue
        pid = int(stem)
        if not procs.is_alive(pid):
            try:
                os.remove(os.path.join(d, name))
                if pid not in removed:
                    removed.append(pid)
            except OSError:
                pass
    return removed


def list_sessions() -> list[dict]:
    out = []
    d = protocol.session_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        pid = int(name[:-5])
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = {}
        out.append({"pid": pid, "alive": procs.is_alive(pid), "port": info.get("port"),
                    "agent": info.get("agent"), "started": info.get("started")})
    return out


class Client:
    def __init__(self, pid: int, sock, hello: dict, attach_info: dict):
        self.pid = pid
        self.sock = sock
        self.hello = hello
        self.attach_info = attach_info
        self._next_id = 1

    # -- attach ---------------------------------------------------------------------
    @classmethod
    def attach(cls, pid: int, *, entry=None, policy=None, scope=None, pressure=None,
               duty=None, read_only=None, idle_timeout=None, frame_budget_ms=None,
               max_handles=None, deny=None, drop_grace=None, wait=10.0,
               follow_launcher=True, reuse=True) -> "Client":
        if sys.version_info < (3, 14):
            raise AttachError("attaching needs Python 3.14+ on the client side")
        pid = int(pid)
        info = {"requested_pid": pid}
        if follow_launcher:
            real = procs.resolve_launcher(pid)
            if real != pid:
                info["launcher"] = pid
                pid = real
        info["pid"] = pid
        if not procs.is_alive(pid):
            raise AttachError(f"no process with pid {pid}")
        version = procs.python_version(pid)
        if version is not None:
            info["python"] = version
            major, minor = (int(x) for x in version.rstrip("t").split("."))
            if (major, minor) < (3, 14):
                raise AttachError(
                    f"pid {pid} runs Python {version}; attaching needs Python 3.14+ in the "
                    "target (sys.remote_exec, PEP 768). Run the program with a 3.14 "
                    "interpreter (a packaged .exe embeds the Python it was built with).")
        if reuse:
            sock, hello = _connect(pid)
            if sock is not None:
                try:
                    c = cls(pid, sock, hello, info)
                    c.ping()      # an agent mid-detach may accept, then drop us
                except (OSError, ConnectionError, ValueError, ConnectionLost):
                    sock.close()
                    sock = None
                    time.sleep(0.6)   # let it finish tidying up, then inject afresh
            if sock is not None:
                info["reused"] = True
                asked = {"entry": entry, "scope": scope, "pressure": pressure,
                         "duty": float(duty) if duty else None,
                         "policy": os.path.abspath(policy) if policy else None,
                         "read_only": True if read_only else None,
                         "idle_timeout": float(idle_timeout) if idle_timeout is not None
                         else None,
                         "frame_budget_ms": float(frame_budget_ms) if frame_budget_ms
                         else None,
                         "max_handles": int(max_handles) if max_handles else None}
                asked = {k: v for k, v in asked.items() if v is not None}
                if asked:
                    running = c.ping().get("config", {})
                    differ = [k for k, v in asked.items() if running.get(k) != v]
                    if differ:
                        info["warnings"] = [
                            f"agent already running with different {', '.join(differ)}; "
                            "options only apply when injecting (detach first to change them)"]
                return c
        # stale or absent session: inject (needs matching interpreter builds)
        mismatch = procs.build_mismatch(version)
        if mismatch:
            raise AttachError(f"can't inject into pid {pid}: {mismatch}")
        for p in (protocol.session_path(pid), protocol.error_path(pid)):
            try:
                os.remove(p)
            except OSError:
                pass
        protocol.ensure_session_dir()
        boot = protocol.boot_path(pid)
        config = {"error_file": protocol.error_path(pid), "boot_file": boot,
                  "expires": time.time() + wait + 1.0}
        if entry:
            config["entry"] = entry
        if policy:
            config["policy"] = os.path.abspath(policy)
        if scope:
            config["scope"] = scope
        if pressure:
            config["pressure"] = pressure
        if duty:
            config["duty"] = float(duty)
        if read_only:
            config["read_only"] = True
        if idle_timeout is not None:
            config["idle_timeout"] = float(idle_timeout)
        if frame_budget_ms:
            config["frame_budget_ms"] = float(frame_budget_ms)
        if max_handles:
            config["max_handles"] = int(max_handles)
        if deny:
            config["deny"] = list(deny)
        if drop_grace is not None:
            config["drop_grace"] = float(drop_grace)
        protocol.write_private(boot, BOOTSTRAP.format(root=PACKAGE_ROOT,
                                                      config=json.dumps(config)))
        t0 = time.perf_counter()
        try:
            sys.remote_exec(pid, boot)
        except (OSError, RuntimeError) as exc:
            try:   # nothing was queued in the target, so the boot file is ours to drop
                os.remove(boot)
            except OSError:
                pass
            if isinstance(exc, PermissionError):
                raise AttachError(f"not permitted to attach to {pid}: {exc} (same user? "
                                  "elevated target? on Linux check ptrace_scope)") from None
            raise AttachError(f"sys.remote_exec failed for {pid}: {exc} (remote debugging "
                              "may be disabled in the target, or it isn't Python 3.14+)") from None
        # The boot file is left in place on every path: the stub deletes it
        # when it runs, and a late run after we give up sees it has expired.
        deadline = t0 + wait
        while time.perf_counter() < deadline:
            if os.path.exists(protocol.error_path(pid)):
                with open(protocol.error_path(pid), encoding="utf-8") as f:
                    raise AttachError("the agent failed to start in the target:\n" + f.read())
            if os.path.exists(protocol.session_path(pid)):
                sock, hello = _connect(pid)
                if sock is not None:
                    info["reused"] = False
                    info["attach_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    return cls(pid, sock, hello, info)
            if not procs.is_alive(pid):
                raise AttachError(f"process {pid} exited while attaching")
            time.sleep(0.01)
        raise AttachError(
            f"no answer from {pid} after {wait:g}s. The target only runs the bootstrap "
            "when it next executes Python bytecode; a process blocked in a long C call "
            "(sleep, socket read, an idle GUI event loop) won't until that returns.")

    # -- requests ---------------------------------------------------------------------
    def request(self, cmd: str, **args):
        rid = self._next_id
        self._next_id += 1
        try:
            protocol.send_msg(self.sock, {"id": rid, "cmd": cmd,
                                          "args": {k: v for k, v in args.items()
                                                   if v is not None}})
            resp = protocol.recv_msg(self.sock)
        except (ConnectionError, OSError) as exc:
            alive = procs.is_alive(self.pid)
            why = ("the target is still running; the agent may have detached"
                   if alive else f"process {self.pid} has exited")
            raise ConnectionLost(f"lost the connection during `{cmd}`: {why} ({exc})") from None
        if not resp.get("ok"):
            raise RemoteError(resp.get("error", "unknown error"), resp.get("traceback"))
        return resp.get("result")

    def ping(self):
        return self.request("ping")

    def threads(self):
        return self.request("threads")

    def stack(self, thread=None, limit=None):
        return self.request("stack", thread=thread, limit=limit)

    def ls(self, target=None, limit=None, offset=None, pattern=None):
        return self.request("ls", target=target, limit=limit, offset=offset, pattern=pattern)

    def doc(self, target):
        return self.request("doc", target=target)

    def src(self, target):
        return self.request("src", target=target)

    def show(self, target):
        return self.request("show", target=target)

    def scope(self):
        return self.request("scope")

    def release(self, handles=None):
        return self.request("release", handles=handles)

    # -- M2: calls and sets -----------------------------------------------------------
    def call(self, expr: str, *, on=None, timeout=None, capture=None):
        """``c.call('self._mirror_param(2, "gain", 0.1)')``"""
        target, args, kwargs = exprs.parse_call(expr)
        return self.request("call", target=target, args=args, kwargs=kwargs, on=on,
                            timeout=timeout, capture=capture)

    # -- M4 ------------------------------------------------------------------------------
    def eval(self, source: str, *, mode="auto", frame=None, on=None, timeout=None,
             capture=None, confirmed=False):
        """Run Python in the target (``confirmed`` must be True: ask the user first)."""
        return self.request("eval", source=exprs.eval_source(source), mode=mode, frame=frame,
                            on=on, timeout=timeout, capture=capture, confirmed=confirmed)

    # -- M6: logpoints, traces, breakpoints ----------------------------------------------
    def log(self, location, expr, *, when=None, every=None, rate=None, seconds=None,
            confirmed=False):
        return self.request("log", location=location, expr=expr, when=when, every=every,
                            rate=rate, seconds=seconds, confirmed=confirmed)

    def trace(self, target, *, rate=None, seconds=None):
        return self.request("trace", target=exprs.parse_target(target), rate=rate,
                            seconds=seconds)

    def break_(self, location=None, *, when=None, keep=None, confirmed=False):
        return self.request("break", location=location, when=when, keep=keep,
                            confirmed=confirmed)

    def probes(self):
        return self.request("probes")

    def unprobe(self, probe="all"):
        return self.request("unprobe", probe=probe)

    def probe_events(self, since=0, limit=None):
        return self.request("probe_events", since=since, limit=limit)

    def stream_events(self, on_event, *, since=None, seconds=None, count=None, poll=0.1):
        """Call ``on_event(event)`` for new probe events until ``seconds`` pass or
        ``count`` events arrived (or forever; Ctrl+C stops). Returns the count."""
        if since is None:
            since = self.probe_events(since=0, limit=0)["latest"]
        n = 0
        end = time.monotonic() + seconds if seconds else None
        while (end is None or time.monotonic() < end) and (count is None or n < count):
            res = self.probe_events(since=since)
            for ev in res["events"]:
                on_event(ev)
                n += 1
                if count is not None and n >= count:
                    break
            since = res["next"]
            time.sleep(poll)
        return n

    def reload(self, module, *, undo=False, values=False, on=None, timeout=None,
               confirmed=False):
        return self.request("reload", module=module, undo=undo or None, values=values or None,
                            on=on, timeout=timeout, confirmed=confirmed)

    # -- M6: snapshots ---------------------------------------------------------------------
    def snap(self, name, target, *, depth=None):
        return self.request("snap", name=name, target=exprs.parse_target(target), depth=depth)

    def diff(self, a, b=None):
        return self.request("diff", a=a, b=b)

    def snaps(self):
        return self.request("snaps")

    def unsnap(self, name="all"):
        return self.request("unsnap", name=name)

    def scratch(self, clear=False):
        return self.request("scratch", clear=clear)

    def complete(self, text):
        return self.request("complete", text=text)

    def set(self, target: str, value: str, *, on=None, timeout=None):
        """``c.set("config.debug", "True")``: value is literal or path source."""
        return self.request("set", target=exprs.parse_target(target),
                            value=exprs.parse_value(value), on=on, timeout=timeout)

    def assign(self, statement: str, *, on=None, timeout=None):
        """``c.assign("config.retries = 5")``"""
        target, value = exprs.parse_assignment(statement)
        return self.request("set", target=target, value=value, on=on, timeout=timeout)

    def do(self, action: str, *values: str, on=None, timeout=None):
        args = [exprs.parse_value(v) for v in values]
        return self.request("do", action=action, args=args, on=on, timeout=timeout)

    def undo(self, n=1):
        return self.request("undo", n=n)

    def history(self, limit=None):
        return self.request("history", limit=limit)

    def jobs(self):
        return self.request("jobs")

    def result(self, job, timeout=None):
        return self.request("result", job=job, timeout=timeout)

    def executors(self):
        return self.request("executors")

    def frames(self, n=300, timeout=None):
        return self.request("frames", n=n, timeout=timeout)

    # -- M3: game tools -------------------------------------------------------------------
    def pause(self, seconds=None, timeout=None):
        return self.request("pause", seconds=seconds, timeout=timeout)

    def resume(self):
        return self.request("resume")

    def step(self, n=1, timeout=None):
        return self.request("step", n=n, timeout=timeout)

    def paused(self):
        return self.request("paused")

    def sample(self, paths):
        return self.request("sample", paths_=list(paths))

    def nudge(self, target, delta, *, on=None, timeout=None):
        return self.request("nudge", target=exprs.parse_target(target), delta=delta, on=on,
                            timeout=timeout)

    def find(self, query, *, via=None, limit=None, depth=None):
        return self.request("find", query=query, via=via, limit=limit, depth=depth)

    def every(self, interval, expr, *, count=None, on=None):
        target, args, kwargs = exprs.parse_call(expr)
        return self.request("every", target=target, interval=parse_interval(interval),
                            args=args, kwargs=kwargs, count=count, on=on)

    def repeats(self):
        return self.request("repeats")

    def stop(self, repeat=None):
        return self.request("stop", repeat=repeat)

    def presets(self):
        return self.request("presets")


    def detach(self):
        try:
            return self.request("detach")
        finally:
            self.close()

    def close(self):
        """Say bye (so the agent knows this was a clean exit, not a crash) and close."""
        try:
            protocol.send_msg(self.sock, {"id": self._next_id, "cmd": "bye"})
        except (OSError, ValueError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def parse_interval(text) -> float:
    """'1s', '250ms', '0.5', 2 -> seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).strip().lower()
    for suffix, scale in (("ms", 0.001), ("s", 1.0), ("m", 60.0)):
        if t.endswith(suffix):
            return float(t[:-len(suffix)]) * scale
    return float(t)
