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

from . import procs, protocol

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOOTSTRAP = '''\
# Exposure bootstrap, run by sys.remote_exec. Generated; safe to delete.
def _exposure_boot():
    import sys
    agent = getattr(sys, "_exposure_agent", None)
    if agent is not None:
        try:
            agent.ensure_alive()
        except BaseException:
            pass
        return
    import json
    root = {root!r}
    config = json.loads({config!r})
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
        sock.settimeout(60)
        protocol.send_msg(sock, {"token": info["token"]})
        hello = protocol.recv_msg(sock)
    except (OSError, ValueError, ConnectionError):
        sock.close()
        return None, info
    if not hello.get("ok"):
        sock.close()
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
               duty=None, wait=10.0, follow_launcher=True, reuse=True) -> "Client":
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
        if reuse:
            sock, hello = _connect(pid)
            if sock is not None:
                info["reused"] = True
                return cls(pid, sock, hello, info)
        # stale or absent session: inject
        for p in (protocol.session_path(pid), protocol.error_path(pid)):
            try:
                os.remove(p)
            except OSError:
                pass
        protocol.ensure_session_dir()
        config = {"error_file": protocol.error_path(pid)}
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
        boot = protocol.boot_path(pid)
        protocol.write_private(boot, BOOTSTRAP.format(root=PACKAGE_ROOT,
                                                      config=json.dumps(config)))
        t0 = time.perf_counter()
        try:
            sys.remote_exec(pid, boot)
        except PermissionError as exc:
            raise AttachError(f"not permitted to attach to {pid}: {exc} "
                              "(same user? elevated target? on Linux check ptrace_scope)") from None
        except (OSError, RuntimeError) as exc:
            raise AttachError(f"sys.remote_exec failed for {pid}: {exc} (remote debugging "
                              "may be disabled in the target, or it isn't Python 3.14+)") from None
        deadline = t0 + wait
        try:
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
        finally:
            try:
                os.remove(boot)
            except OSError:
                pass
        raise AttachError(
            f"no answer from {pid} after {wait:g}s. The target only runs the bootstrap "
            "when it next executes Python bytecode; a process blocked in a long C call "
            "(sleep, socket read, an idle GUI event loop) won't until that returns.")

    # -- requests ---------------------------------------------------------------------
    def request(self, cmd: str, **args):
        rid = self._next_id
        self._next_id += 1
        protocol.send_msg(self.sock, {"id": rid, "cmd": cmd,
                                      "args": {k: v for k, v in args.items() if v is not None}})
        resp = protocol.recv_msg(self.sock)
        if not resp.get("ok"):
            raise RemoteError(resp.get("error", "unknown error"), resp.get("traceback"))
        return resp.get("result")

    def ping(self):
        return self.request("ping")

    def threads(self):
        return self.request("threads")

    def stack(self, thread=None, limit=None):
        return self.request("stack", thread=thread, limit=limit)

    def ls(self, target=None, limit=None, offset=None):
        return self.request("ls", target=target, limit=limit, offset=offset)

    def show(self, target):
        return self.request("show", target=target)

    def scope(self):
        return self.request("scope")

    def release(self, handles=None):
        return self.request("release", handles=handles)

    def detach(self):
        try:
            return self.request("detach")
        finally:
            self.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
