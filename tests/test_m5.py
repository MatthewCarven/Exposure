"""M5: strict mode (exposure.serve), audit, robustness, and stress tests.

Stress tests are marked ``stress`` and skipped by default:
    python -m pytest -m stress              # all of them
    EXPOSURE_STRESS_N=1000 python -m pytest -m stress -k attach_detach
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from conftest import ROOT, needs_314, target_python
from exposure import protocol
from exposure.client import Client, ConnectionLost, RemoteError

pytestmark = needs_314


def frame_of(c):
    return int(c.sample(["frame"])["values"][0]["preview"])


def handle_count(pid):
    """Open handles (Windows) or file descriptors (Linux) of a process."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)
        try:
            n = wintypes.DWORD()
            k32.GetProcessHandleCount(h, ctypes.byref(n))
            return n.value
        finally:
            k32.CloseHandle(h)
    if sys.platform.startswith("linux"):
        return len(os.listdir(f"/proc/{pid}/fd"))
    pytest.skip("handle counting needs Windows or Linux")


# -- strict mode ---------------------------------------------------------------------------

def _served(make_target, tmp_path, audit=False):
    pol = tmp_path / "strict.toml"
    pol.write_text('[agent]\nscope = "explicit"\n[expose]\n'
                   '"__main__:Config" = { read = true }\n', encoding="utf-8")
    args = [str(pol)]
    if audit:
        args.append(str(tmp_path / "audit.jsonl"))
    return make_target(*args, script="served_app.py",
                       env={"PYTHON_DISABLE_REMOTE_DEBUG": "1"})


def test_served_strict_mode(make_target, tmp_path):
    t = _served(make_target, tmp_path)
    with pytest.raises(Exception):                 # nothing can be injected into it
        sys.remote_exec(t.pid, os.path.join(ROOT, "tests", "targets", "churn.py"))
    with Client.attach(t.pid) as c:
        assert c.attach_info["reused"] is True     # found via its session file
        p = c.ping()
        assert p["strict"] and p["served"] and p["scope"] == "explicit"
        roots = {e["name"] for e in c.ls()["entries"]}
        assert roots == {"config", "restart", "Config"}
        with pytest.raises(RemoteError, match="explicit scope"):
            c.ls("secret")
        with pytest.raises(RemoteError, match="strict mode"):
            c.eval("1 + 1", confirmed=True)
        assert c.set("config.debug", "True")["new"] == "True"
        with pytest.raises(RemoteError, match="not allowlisted"):
            c.set("config.retries", "9")
        assert c.call("restart(2)")["result"]["preview"] == "'restarted 2'"
        r = c.detach()
        assert r["served"] and not r["detaching"]
    with Client.attach(t.pid, scope="reachable") as c:   # it keeps serving; options ignored
        assert c.attach_info["reused"] is True
        assert c.ping()["scope"] == "explicit"
        assert "warnings" in c.attach_info


def test_audit_log(make_target, tmp_path):
    t = _served(make_target, tmp_path, audit=True)
    with Client.attach(t.pid) as c:
        c.set("config.debug", "True")
        c.call("restart()")
        with pytest.raises(RemoteError):
            c.set("config.retries", "1")
        c.detach()
    lines = [json.loads(x) for x in
             (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    cmds = [(x["cmd"], x["ok"]) for x in lines]
    assert cmds[0] == ("attach", True)
    assert ("set", True) in cmds and ("call", True) in cmds and ("set", False) in cmds
    assert cmds[-1] == ("detach", True)
    ok_set = next(x for x in lines if x["cmd"] == "set" and x["ok"])
    assert ok_set["what"] == "config.debug" and ok_set["new"] == "True"


def test_audit_hook_can_block(make_target):
    t = make_target("--audit", script="game_loop.py")
    with Client.attach(t.pid) as c:
        with pytest.raises(RemoteError, match="audit hook says no"):
            c.call("blocked_by_audit()")
        assert "blocked_by_audit ran" not in str(c.ls("calls")["entries"])
        f0 = frame_of(c)
        time.sleep(0.2)
        assert frame_of(c) > f0                    # the target carried on


# -- robustness ------------------------------------------------------------------------------

def test_target_exits_mid_call(make_target):
    t = make_target("--seconds", "2", script="game_loop.py")
    with Client.attach(t.pid) as c:
        with pytest.raises(ConnectionLost, match="has exited"):
            c.call("slow(10)", timeout=20, on="agent")   # the main loop ends meanwhile


def test_client_killed_mid_request(make_target):
    t = make_target(script="game_loop.py")
    Client.attach(t.pid, idle_timeout=1).close()
    code = (f"import sys; sys.path.insert(0, {ROOT!r}); "
            "from exposure.client import Client; "
            f"c = Client.attach({t.pid}); c.call('slow(3)', timeout=30)")
    p = subprocess.Popen([target_python(), "-c", code])
    time.sleep(1.0)
    p.kill()
    p.wait(10)
    deadline = time.time() + 15                     # job ends, then the idle timeout
    while os.path.exists(protocol.session_path(t.pid)) and time.time() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(protocol.session_path(t.pid))
    with Client.attach(t.pid) as c:                 # and a fresh session works
        assert c.call("where()")["result"]["preview"] == "'MainThread'"


def test_several_clients_at_once(make_target):
    t = make_target(script="game_loop.py")
    Client.attach(t.pid).close()
    errors, counts = [], {}

    def worker(kind):
        n = 0
        try:
            with Client.attach(t.pid) as c:
                end = time.time() + 3
                while time.time() < end:
                    if kind == "call":
                        c.call("player.heal(0)")
                    elif kind == "read":
                        c.ls("player")
                        c.sample(["frame", "player.hp", "len(player.pos)"])
                    else:
                        c.threads()
                        c.stack()
                    n += 1
        except Exception as exc:
            errors.append(f"{kind}: {exc!r}")
        counts[kind] = n

    ths = [threading.Thread(target=worker, args=(k,)) for k in ("call", "read", "meta",
                                                                   "call", "read")]
    for th in ths:
        th.start()
    for th in ths:
        th.join(30)
    assert not errors, errors
    assert all(v > 5 for v in counts.values()), counts


# -- stress (off by default: -m stress) ---------------------------------------------------------

@pytest.mark.stress
def test_stress_attach_detach(make_target):
    n = int(os.environ.get("EXPOSURE_STRESS_N", "1000"))
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:                 # warm-up, so imports are counted
        c.call("where()")
        c.detach()
    before = handle_count(t.pid)
    t0 = time.perf_counter()
    for i in range(n):
        c = Client.attach(t.pid)
        if i % 10 == 0:
            c.call("player.heal(0)")                # uses the main executor's hook
        c.detach()
    elapsed = time.perf_counter() - t0
    after = handle_count(t.pid)
    print(f"\nSTRESS {n} attach/detach cycles in {elapsed:.1f} s "
          f"({elapsed / n * 1000:.1f} ms each); handles {before} -> {after}")
    with Client.attach(t.pid) as c:
        names = [x["name"] for x in c.threads()]
        assert names.count("exposure-agent") == 1
        assert not [x for x in names if x.startswith(("exposure-conn", "exposure-call"))
                    and x != "exposure-conn-1"]
        r = c.eval("[sys.monitoring.get_tool(i) for i in range(6)], "
                   "type(sys.stdout).__name__, type(sys.stderr).__name__",
                   on="agent", confirmed=True, capture=False)
        assert r["result"]["preview"] == (
            "tuple[3] (<list>, 'TextIOWrapper', 'TextIOWrapper')")
        tools = c.eval("[sys.monitoring.get_tool(i) for i in range(6)]",
                       on="agent", confirmed=True, capture=False)["result"]["preview"]
        assert tools == "list[6] [None, None, None, None, None, …]"
        c.detach()
    files = [f for f in os.listdir(protocol.session_dir()) if f.startswith(f"{t.pid}.")]
    assert files == []
    assert after - before < 50


@pytest.mark.stress
def test_stress_thread_churn(make_target):
    t = make_target(script="churn.py")
    t0 = time.perf_counter()
    cycles = 0
    while time.perf_counter() - t0 < 20:
        with Client.attach(t.pid) as c:
            for _ in range(10):
                c.threads()
                c.stack()
                c.ls()
                c.find("type:Thread")
            c.detach()
        cycles += 1
    assert t.proc.poll() is None
    print(f"\nSTRESS {cycles} attach/browse/detach cycles under thread churn in 20 s")
