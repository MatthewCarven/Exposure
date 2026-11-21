"""Finding Python processes: ``exposure ps``, liveness, venv launchers.

Client side only. Standard library (ctypes on Windows, /proc on Linux).
Never use ``os.kill(pid, 0)`` for liveness: on Windows it terminates the
process.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

# the optional "t" marks a free-threaded build: python314t.dll -> "3.14t"
_PYDLL = re.compile(r"^python3(\d+)(t?)\.dll$", re.I)
_PYLIB = re.compile(r"libpython3\.(\d+)(t?)\.so")
_PYEXE = re.compile(r"python3\.(\d+)(t?)$")


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    name: str
    exe: str | None = None
    version: str | None = None          # "3.14" when known
    cmdline: str | None = None
    launcher_for: int | None = None     # venv launcher: the real interpreter's pid
    remote_debug: str = "unknown"       # "enabled" | "disabled" | "unknown"
    notes: list = field(default_factory=list)

    @property
    def attachable(self) -> str:
        if self.launcher_for:
            return f"use {self.launcher_for}"
        if self.remote_debug == "disabled":
            return "no (remote debug disabled)"
        if self.version is None:
            return "unknown"
        major, minor = (int(x) for x in self.version.rstrip("t").split("."))
        if (major, minor) < (3, 14):
            return "no (needs 3.14+)"
        return "yes"


# -- Windows -----------------------------------------------------------------------------

def _win_api():
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                    ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                    ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                    ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    return ctypes, wintypes, k32, PROCESSENTRY32W, MODULEENTRY32W


INVALID_HANDLE = -1 & 0xFFFFFFFFFFFFFFFF


def _win_processes():
    ctypes, _wt, k32, PE, _ME = _win_api()
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == INVALID_HANDLE:
        return []
    out = []
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def _win_modules(pid):
    ctypes, _wt, k32, _PE, ME = _win_api()
    snap = k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
    if not snap or snap == INVALID_HANDLE:
        return None
    mods = []
    try:
        e = ME()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Module32FirstW(snap, ctypes.byref(e))
        while ok:
            mods.append(e.szModule)
            ok = k32.Module32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return mods


def _win_exe(pid):
    ctypes, wintypes, k32, _PE, _ME = _win_api()
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(h)


def _win_alive(pid) -> bool:
    ctypes, wintypes, k32, _PE, _ME = _win_api()
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ctypes.get_last_error() == 5  # access denied: exists, not ours
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# -- Linux ----------------------------------------------------------------------------------

def _linux_processes():
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            continue
        out.append((pid, ppid, comm))
    return out


def _linux_info(info: ProcInfo):
    pid = info.pid
    try:
        info.exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            info.cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _PYLIB.search(line)
                if m:
                    info.version = f"3.{m.group(1)}{m.group(2)}"
                    break
    except OSError:
        info.notes.append("maps not readable")
    if info.version is None and info.exe:
        m = _PYEXE.search(os.path.basename(info.exe))
        if m:
            info.version = f"3.{m.group(1)}{m.group(2)}"
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
        disabled = any(e.startswith(b"PYTHON_DISABLE_REMOTE_DEBUG=") and e.split(b"=", 1)[1]
                       for e in env)
        if info.cmdline and "disable-remote-debug" in info.cmdline:
            disabled = True
        info.remote_debug = "disabled" if disabled else "enabled"
    except OSError:
        pass


# -- public API -------------------------------------------------------------------------------

def is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _win_alive(pid)
    try:
        os.kill(pid, 0)   # POSIX only: signal 0 just checks
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        # a process that has exited but not been reaped yet (a zombie) still
        # answers signal 0; it isn't running Python any more
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            if stat[stat.rindex(")") + 2:].split()[0] in ("Z", "X"):
                return False
        except (OSError, ValueError, IndexError):
            pass
    return True


def python_children(pid: int) -> list[int]:
    if sys.platform == "win32":
        procs = _win_processes()
    elif sys.platform.startswith("linux"):
        procs = _linux_processes()
    else:
        return []
    return [p for p, pp, name in procs if pp == pid and name.lower().startswith("python")]


def _win_dll_version(pid):
    """'3.14' if the process has python314.dll loaded, None if none/unreadable."""
    if pid == 0:   # a module snapshot of pid 0 means "this process"
        return None
    mods = _win_modules(pid)
    for m in mods or ():
        mm = _PYDLL.match(m)
        if mm:
            return f"3.{mm.group(1)}{mm.group(2).lower()}"   # python314t.dll -> 3.14t
    return None


def python_version(pid: int) -> str | None:
    """The target's Python version ("3.12") when it can be read, else None."""
    if sys.platform == "win32":
        return _win_dll_version(pid)
    if sys.platform.startswith("linux"):
        info = ProcInfo(pid, 0, "")
        _linux_info(info)
        return info.version
    return None


def resolve_launcher(pid: int) -> int:
    """Follow a Windows launcher to the process that actually runs Python.

    Covers a venv's ``Scripts\\python.exe`` (it re-spawns the base interpreter)
    and a PyInstaller one-file exe (its bootloader runs the app in a child
    process of the same name). Returns ``pid`` unchanged otherwise.
    """
    if sys.platform != "win32":
        return pid
    if _win_dll_version(pid):
        return pid  # it runs Python itself
    kids = [p for p, pp, _name in _win_processes() if pp == pid and _win_dll_version(p)]
    if len(kids) == 1:
        return kids[0]
    kids = python_children(pid)
    return kids[0] if len(kids) == 1 else pid


def list_python() -> list[ProcInfo]:
    me = os.getpid()
    out = []
    if sys.platform == "win32":
        procs = _win_processes()
        for pid, ppid, name in procs:
            if pid in (0, me, os.getppid()):   # ourselves and our own venv launcher
                continue
            version = _win_dll_version(pid)
            named = name.lower().startswith("python")
            if not named and version is None:
                continue
            info = ProcInfo(pid, ppid, name, exe=_win_exe(pid), version=version)
            if not named:
                info.notes.append("embeds Python (a frozen or packaged app?)")
            out.append(info)
        by_pid = {i.pid: i for i in out}
        names = {pid: name for pid, _pp, name in procs}
        for i in list(out):
            parent = by_pid.get(i.ppid)
            if parent is not None and parent.version is None:
                parent.launcher_for = i.pid
            elif parent is None and names.get(i.ppid, "").lower() == i.name.lower():
                # a PyInstaller bootloader: same exe, no Python of its own
                out.append(ProcInfo(i.ppid, 0, i.name, exe=i.exe, launcher_for=i.pid))
        out.sort(key=lambda x: x.pid)
    elif sys.platform.startswith("linux"):
        for pid, ppid, name in _linux_processes():
            if pid == me or not name.lower().startswith("python"):
                continue
            info = ProcInfo(pid, ppid, name)
            _linux_info(info)
            out.append(info)
    return sorted(out, key=lambda i: i.pid)


def client_free_threaded() -> bool:
    import sysconfig
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def build_mismatch(version):
    """A message if sys.remote_exec can't cross between this client's build and
    the target's (free-threaded vs regular), else None."""
    if not version:
        return None
    target_ft = version.endswith("t")
    if target_ft == client_free_threaded():
        return None
    if target_ft:
        return (f"the target runs a free-threaded Python ({version}) and this client is a "
                "regular build; CPython only injects between matching builds. Run Exposure "
                "with a 3.14t interpreter (e.g. `uv tool install --python 3.14t "
                "git+https://github.com/MatthewCarven/Exposure`). Once a matching client "
                "has attached, any client can connect.")
    return (f"the target runs a regular Python ({version}) and this client is free-threaded; "
            "CPython only injects between matching builds. Run Exposure with a regular "
            "3.14 interpreter. Once attached, any client can connect.")
