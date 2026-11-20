"""Exposure M0 spike client.

    python client.py <pid> <command> [key=value ...]

Injects bootstrap.py with sys.remote_exec (PEP 768) unless an agent is already
answering for that pid, waits for its session file, connects, authenticates
with the token and runs one command. Values in key=value are parsed as JSON
when they parse, else kept as strings. Needs Python 3.14+.

If <pid> is a Windows venv launcher (Scripts\\python.exe re-spawns the base
interpreter as a child), the single python child is targeted instead.
"""
import json
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BOOTSTRAP = os.path.join(HERE, "bootstrap.py")


def session_file(pid):
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "exposure", "sessions", f"{pid}.json")


def python_children(pid):
    """Child processes of pid whose exe name starts with python (Windows)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ParentProcessID == pid and e.szExeFile.lower().startswith("python"):
                out.append(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("agent closed the connection")
        buf += chunk
    return buf


def send(s, obj):
    data = json.dumps(obj).encode("utf-8")
    s.sendall(struct.pack(">I", len(data)) + data)


def recv(s):
    (n,) = struct.unpack(">I", _recv_exact(s, 4))
    return json.loads(_recv_exact(s, n).decode("utf-8"))


def try_connect(pid, timeout=2.0):
    try:
        with open(session_file(pid), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        s = socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout)
    except OSError:
        return None
    s.settimeout(120)
    send(s, {"token": info["token"]})
    hello = recv(s)
    if not hello.get("ok"):
        s.close()
        raise SystemExit(f"agent refused: {hello}")
    return s


def attach(pid, wait=10.0):
    """Return (socket, info dict with timings)."""
    s = try_connect(pid)
    if s is not None:
        return s, {"reused": True}
    try:
        os.remove(session_file(pid))
    except OSError:
        pass
    t0 = time.perf_counter()
    sys.remote_exec(pid, BOOTSTRAP)
    t_exec = time.perf_counter()
    t_file = None
    while time.perf_counter() - t0 < wait:
        if t_file is None and os.path.exists(session_file(pid)):
            t_file = time.perf_counter()
        if t_file is not None:
            s = try_connect(pid)
            if s is not None:
                t_conn = time.perf_counter()
                return s, {"reused": False,
                           "remote_exec_call_ms": round((t_exec - t0) * 1000, 1),
                           "session_file_ms": round((t_file - t0) * 1000, 1),
                           "connected_ms": round((t_conn - t0) * 1000, 1)}
        time.sleep(0.005)
    raise SystemExit(f"no agent answered within {wait}s (remote_exec returned after "
                     f"{(t_exec - t0) * 1000:.1f} ms)")


def parse_args(items):
    args = {}
    for item in items:
        k, _, v = item.partition("=")
        try:
            args[k] = json.loads(v)
        except ValueError:
            args[k] = v
    return args


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    pid = int(argv[1])
    kids = python_children(pid)
    if len(kids) == 1:
        print(f"[client] pid {pid} is a launcher; targeting its python child {kids[0]}",
              file=sys.stderr)
        pid = kids[0]
    cmd = argv[2]
    s, info = attach(pid, wait=float(os.environ.get("EXPOSURE_WAIT", "10")))
    print(f"[client] attach: {json.dumps(info)}", file=sys.stderr)
    with s:
        send(s, {"cmd": cmd, "args": parse_args(argv[3:])})
        resp = recv(s)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
