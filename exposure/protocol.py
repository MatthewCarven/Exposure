"""Wire protocol and session files, shared by the agent and the client.

Standard library only: this module is imported inside the target process.

Messages are JSON objects, each prefixed with a 4-byte big-endian length.
The first message on a connection is ``{"token": ...}``; the agent answers
``{"ok": true, ...}`` or drops the connection. After that, requests are
``{"id": n, "cmd": name, "args": {...}}`` and responses
``{"id": n, "ok": bool, "result": ... | "error": str}``.
Never pickle: unpickling is code execution on whichever side receives it.
"""
from __future__ import annotations

import json
import os
import struct
import sys

PROTOCOL_VERSION = 1
MAX_MESSAGE = 16 * 1024 * 1024
_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    pass


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def send_msg(sock, obj, default=None) -> None:
    data = json.dumps(obj, default=default, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ProtocolError(f"message too large ({len(data)} bytes)")
    sock.sendall(_HEADER.pack(len(data)) + data)


def recv_msg(sock):
    (n,) = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
    if n > MAX_MESSAGE:
        raise ProtocolError(f"message too large ({n} bytes)")
    return json.loads(_recv_exact(sock, n).decode("utf-8"))


# -- session files -------------------------------------------------------------

def session_dir() -> str:
    """Per-user directory holding ``<pid>.json`` session files."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "exposure", "sessions")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "exposure", "sessions")
    return os.path.join(os.path.expanduser("~"), ".cache", "exposure", "sessions")


def session_path(pid: int) -> str:
    return os.path.join(session_dir(), f"{pid}.json")


def error_path(pid: int) -> str:
    return os.path.join(session_dir(), f"{pid}.error.txt")


def boot_path(pid: int) -> str:
    return os.path.join(session_dir(), f"{pid}.boot.py")


def ensure_session_dir() -> str:
    d = session_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    if sys.platform != "win32":
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def write_private(path: str, text: str) -> None:
    """Write a file only the current user can read, atomically."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
