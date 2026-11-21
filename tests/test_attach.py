"""End-to-end: inject into a real 3.14 process and browse it."""
import os
import sys
import time

import pytest

from conftest import ROOT, TARGETS, needs_314
from exposure import procs, protocol
from exposure.client import AttachError, Client, RemoteError, clean_stale, list_sessions

pytestmark = needs_314


def names(res):
    return {e["name"]: e for e in res["entries"]}


def test_reachable_browse(make_target):
    t = make_target()
    with Client.attach(t.pid) as c:
        assert c.attach_info["reused"] is False
        roots = c.ls()
        assert roots["entry"]["function"] == "main"
        n = names(roots)
        assert {"config", "items", "table", "counter", "tick", "secret"} <= set(n)
        assert n["Config"]["kind"] == "global used"
        cfg = names(c.ls("config"))
        assert cfg["retries"]["preview"] == "3"
        assert names(c.ls("table"))["(1, 2)"]["path"] == "table[(1, 2)]"
        assert c.ls("table[(1, 2)]")["preview"] == "'tuple key'"
        assert c.ls("table[3]")["preview"] == "list[2] [7, 8]"
        # handles work in later commands
        h = n["items"]["handle"]
        assert c.ls(f"{h}[2]")["preview"] == "dict[1] {'k': 3}"
        # properties, __repr__ and overridden dict.items never ran
        assert names(c.ls("counter"))["value"]["kind"] == "property"
        c.ls("loud")
        c.show("counter")
        assert names(c.ls("Counter"))["reads"]["preview"] == "0"
        with pytest.raises(RemoteError, match="dunder"):
            c.ls("config.__class__")
        with pytest.raises(RemoteError, match="not found"):
            c.ls("nope")


def test_threads_stack_and_handles_persist(make_target):
    t = make_target()
    with Client.attach(t.pid) as c:
        threads = {x["name"]: x for x in c.threads()}
        assert {"MainThread", "worker-1", "exposure-agent"} <= set(threads)
        st = c.stack()
        assert [f["function"] for f in st["frames"]][-1] == "main"
        assert st["frames"][-1]["entry"] is True
        frame_handle = st["frames"][-1]["handle"]
        worker = c.stack("worker-1")
        assert worker["frames"][-1]["function"] == "worker"
    with Client.attach(t.pid) as c:           # new connection, same agent
        assert c.attach_info["reused"] is True
        assert "config" in names(c.ls(frame_handle))
        assert c.show(frame_handle)["alive"] is True
        assert c.release()["released"] > 0
        with pytest.raises(RemoteError, match="no handle"):
            c.ls(frame_handle)


def test_explicit_scope_from_code(make_target):
    t = make_target("--expose")
    with Client.attach(t.pid, scope="explicit") as c:
        roots = names(c.ls())
        assert set(roots) == {"config", "items"}
        assert roots["config"]["perm"] == {"read": True, "write": ["debug"], "call": False}
        assert names(c.ls("config"))["debug"]["perm"]["read"] is True
        for hidden in ("secret", "table", "counter"):
            with pytest.raises(RemoteError, match="explicit scope"):
                c.ls(hidden)
        sc = c.scope()
        assert sc["scope"] == "explicit" and sc["enforced"] == ["read", "write", "call"]
        assert all("handle" not in f for f in c.stack()["frames"])


def test_both_scope_marks_exposed_items(make_target):
    t = make_target("--expose")
    with Client.attach(t.pid) as c:           # registry present -> default "both"
        assert c.scope()["scope"] == "both"
        n = names(c.ls())
        assert "secret" in n                   # reachable browsing still works
        assert n["config"]["perm"]["write"] == ["debug"]


def test_policy_file(make_target, tmp_path):
    (tmp_path / "exposure.toml").write_text(
        '[agent]\nscope = "explicit"\npressure = "<entry>:tick"\n\n'
        '[expose]\n"<entry>:config" = { read = true, write = ["debug"] }\n'
        '"<entry>:table" = { read = true }\n'
        '"<entry>:nothing_here" = { read = true }\n', encoding="utf-8")
    t = make_target(cwd=str(tmp_path))       # found via the target's cwd
    with Client.attach(t.pid) as c:
        sc = c.scope()
        assert sc["policy"].endswith("exposure.toml") and sc["scope"] == "explicit"
        unresolved = [e for e in sc["explicit"] if not e["resolved"]]
        assert [e["name"] for e in unresolved] == ["nothing_here"]
        assert set(names(c.ls())) == {"config", "table", "nothing_here"}
        assert c.ls("table[3]")["preview"] == "list[2] [7, 8]"
        with pytest.raises(RemoteError):
            c.ls("items")
        # tick rises on every loop, so the pressure probe must trip
        for _ in range(5):
            c.ls("config")
            time.sleep(0.06)
        assert c.ping()["budget"]["pressure_events"] > 0


def test_detach_and_reattach(make_target):
    t = make_target()
    c = Client.attach(t.pid)
    first_token = open(protocol.session_path(t.pid), encoding="utf-8").read()
    c.detach()
    deadline = time.time() + 5
    while os.path.exists(protocol.session_path(t.pid)) and time.time() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(protocol.session_path(t.pid))
    with Client.attach(t.pid) as c2:
        assert c2.attach_info["reused"] is False
        agents = [x for x in c2.threads() if x["name"] == "exposure-agent"]
        assert len(agents) == 1
        assert open(protocol.session_path(t.pid), encoding="utf-8").read() != first_token
        assert c2.ping()["handles"] == 0


def test_stale_session_cleanup(make_target):
    t = make_target()
    Client.attach(t.pid).close()
    t.stop()
    assert not procs.is_alive(t.pid)
    # the agent's atexit hook doesn't run on a hard kill: the file stays...
    if os.path.exists(protocol.session_path(t.pid)):
        assert any(s["pid"] == t.pid and not s["alive"] for s in list_sessions())
    assert t.pid in clean_stale() or not os.path.exists(protocol.session_path(t.pid))
    assert not os.path.exists(protocol.session_path(t.pid))
    with pytest.raises(AttachError, match="no process"):
        Client.attach(t.pid)


def test_boot_file_removes_itself(make_target):
    t = make_target()
    with Client.attach(t.pid):
        pass
    assert not os.path.exists(protocol.boot_path(t.pid))


def test_blocked_target_wakes_after_timeout(make_target):
    t = make_target("--block", "3")
    with pytest.raises(AttachError, match="bytecode"):
        Client.attach(t.pid, wait=0.5)
    assert os.path.exists(protocol.boot_path(t.pid))   # left for the late run
    line = t.lines.get(timeout=10)
    assert line.strip() == "woke"
    deadline = time.time() + 5
    while os.path.exists(protocol.boot_path(t.pid)) and time.time() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(protocol.boot_path(t.pid))  # the stub ran and expired
    time.sleep(0.3)
    assert not os.path.exists(protocol.session_path(t.pid))  # no agent started late
    assert t.proc.poll() is None
    assert not os.path.exists(protocol.error_path(t.pid))


def test_reuse_warns_about_new_options(make_target):
    t = make_target()
    Client.attach(t.pid).close()
    with Client.attach(t.pid, scope="explicit") as c:
        assert c.attach_info["reused"] is True
        assert "scope" in c.attach_info["warnings"][0]
    with Client.attach(t.pid, scope="reachable") as c:
        assert "warnings" not in c.attach_info


def test_ps_lists_target(make_target):
    if not (sys.platform == "win32" or sys.platform.startswith("linux")):
        pytest.skip("ps is implemented for Windows and Linux")
    t = make_target()
    found = {p.pid: p for p in procs.list_python()}
    assert t.pid in found
    p = found[t.pid]
    assert p.version == f"3.{sys.version_info.minor}"
    assert p.attachable == "yes"


def test_cli_smoke(make_target, capsys):
    from exposure.cli import main
    t = make_target()
    assert main(["ls", str(t.pid)]) == 0
    out = capsys.readouterr().out
    assert "entry main" in out and "config" in out
    assert main(["ls", str(t.pid), "config"]) == 0
    assert "retries" in capsys.readouterr().out
    assert main(["show", str(t.pid), "config.__class__"]) == 1
    assert main(["--json", "ping", str(t.pid)]) == 0
    assert '"agent"' in capsys.readouterr().out
    assert main(["detach", str(t.pid)]) == 0
