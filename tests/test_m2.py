"""M2: calls and sets, executors, undo, jobs, policy enforcement."""
import importlib.util
import os
import sys
import time

import pytest

from conftest import needs_314
from exposure import exprs, protocol
from exposure.client import Client, RemoteError


# -- expression parsing (no target needed) ---------------------------------------------

def test_parse_call():
    t, a, kw = exprs.parse_call('self._mirror_param(2, "gain", 0.1, cap=@4.max)')
    assert t == "self._mirror_param"
    assert a == [{"lit": "2"}, {"lit": "'gain'"}, {"lit": "0.1"}]
    assert kw == {"cap": {"path": "@4.max"}}
    t, a, kw = exprs.parse_call('@3.items[0].f("@3 stays a string", [1, (2, 3)], -4)')
    assert t == "@3.items[0].f"
    assert a[0] == {"lit": "'@3 stays a string'"} and a[2] == {"lit": "-4"}
    assert exprs.parse_assignment("player.pos[0] = @7") == ("player.pos[0]", {"path": "@7"})
    assert exprs.parse_value("None") == {"lit": "None"}
    assert exprs.parse_target('d["k"]') == "d['k']"


@pytest.mark.parametrize("bad", ["1 + 2", "f(g(x))", "f(*a)", "f(**k)", "x.y", "f(lambda: 1)",
                                 "(lambda: 1)()", "f(x[y])"])
def test_parse_call_rejects(bad):
    with pytest.raises(exprs.ExprError):
        exprs.parse_call(bad)


# -- against a game-style loop -------------------------------------------------------------

@needs_314
def test_calls_run_between_frames(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        ex = c.executors()["main"]
        assert ex["mode"] == "loop" and ex["loop"]["function"] == "main"
        r = c.call("where()")
        assert r["state"] == "done" and r["result"]["preview"] == "'MainThread'"
        assert r["blocked_ms"] < 50
        r = c.call("player.heal(5, cap=200)")
        assert r["result"]["preview"] == "105"
        r = c.call("where()", on="agent")
        assert r["result"]["preview"].startswith("'exposure-call-")
        r = c.call("player.heal(1)", on="thread:MainThread")
        assert r["thread"] == "MainThread"
        r = c.call("calls.copy()")
        assert r["result"]["handle"].startswith("@")
        f = c.frames(50)
        assert f["completed"] and f["frames"] == 50


@needs_314
def test_two_clients_at_once(make_target):
    import threading
    t = make_target(script="game_loop.py")
    Client.attach(t.pid).close()
    out = {}

    def sample():
        with Client.attach(t.pid) as c:
            out["frames"] = c.frames(150)

    th = threading.Thread(target=sample)
    th.start()
    time.sleep(0.1)
    with Client.attach(t.pid) as c:
        for i in range(30):
            assert c.call(f"player.heal({i % 3})")["state"] == "done"
    th.join(30)
    assert out["frames"]["completed"] and out["frames"]["frames"] == 150


@needs_314
def test_sets_and_undo(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        assert c.set("player.hp", "42")["new"] == "42"              # slot
        assert c.assign('player.name = "bob"')["old"] == "'p1'"      # property setter runs
        assert c.set("mode", '"turbo"')["new"] == "'turbo'"          # local, PEP 667
        assert c.set("bump.count", "10")["old"] == "0"              # closure cell
        assert c.set('settings["volume"]', "0.9")["old"] == "0.5"   # dict item
        assert c.set("player.pos[1]", "7.5")["new"] == "7.5"        # list item
        time.sleep(0.2)
        n = {e["name"]: e for e in c.ls()["entries"]}
        assert int(n["turbo_frames"]["preview"]) > 0                  # the loop saw it
        assert c.call("bump()")["result"]["preview"] == "11"
        p = {e["name"]: e for e in c.ls("player")["entries"]}
        assert p["_name"]["preview"] == "'BOB'"
        hist = c.history()
        assert [h["target"] for h in hist] == ["player.hp", "player.name", "mode", "bump.count",
                                               "settings['volume']", "player.pos[1]"]
        res = c.undo(6)["undone"]
        assert all(u["state"] == "done" for u in res), res
        p = {e["name"]: e for e in c.ls("player")["entries"]}
        assert p["hp"]["preview"] == "100"
        assert {e["name"]: e for e in c.ls()["entries"]}["mode"]["preview"] == "'normal'"
        assert c.call('settings.get("volume")')["result"]["preview"] == "0.5"
        assert c.call("bump()")["result"]["preview"] == "1"           # cell back to 0
        assert all(h["undone"] for h in c.history())


@needs_314
def test_errors_and_timeouts(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        r = c.call("boom()")
        assert r["state"] == "error" and r["error"] == "ValueError: kaboom"
        assert "kaboom" in r["traceback"] and "executor.py" not in r["traceback"]
        assert r["exception"]["handle"].startswith("@")
        r = c.call("slow(1.0)", timeout=0.2)
        assert r["state"] == "running" and "result" in r["note"]
        jid = r["job"]
        assert any(j["job"] == jid and j["state"] == "running" for j in c.jobs())
        r = c.result(jid, timeout=5)
        assert r["state"] == "done" and r["result"]["preview"] == "'slept'"
        assert r["blocked_ms"] >= 900
        with pytest.raises(RemoteError, match="literal|not found"):
            c.call("nothing_here()")


@needs_314
def test_read_only(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid, read_only=True) as c:
        with pytest.raises(RemoteError, match="read-only"):
            c.call("where()")
        with pytest.raises(RemoteError, match="read-only"):
            c.set("player.hp", "1")
        assert c.scope()["read_only"] is True
        assert "player" in {e["name"] for e in c.ls()["entries"]}


@needs_314
def test_audit_events(make_target):
    t = make_target("--audit", script="game_loop.py")
    with Client.attach(t.pid) as c:
        c.call("where()")
        c.set("player.hp", "50")
        log = c.call("audit_log.copy()")["result"]
        h = log["handle"]
        items = [e["preview"] for e in c.ls(h)["entries"]]
        assert items[:2] == ["tuple[2] ('exposure.call', 'where')",
                             "tuple[2] ('exposure.set', 'player.hp')"]


@needs_314
def test_explicit_scope_enforces_write_and_call(make_target):
    t = make_target("--expose")       # app_main: config writable ["debug"], items read-only
    with Client.attach(t.pid, scope="explicit") as c:
        assert c.set("config.debug", "True")["new"] == "True"
        with pytest.raises(RemoteError, match="not allowlisted"):
            c.set("config.retries", "5")
        with pytest.raises(RemoteError, match="not allowlisted"):
            c.call("items.append(1)")
        with pytest.raises(RemoteError, match="explicit scope"):
            c.set("secret", '"x"')
        assert c.scope()["enforced"] == ["read", "write", "call"]


@needs_314
def test_policy_actions_and_both_scope(make_target, tmp_path):
    (tmp_path / "exposure.toml").write_text(
        '[agent]\nscope = "both"\n\n'
        '[expose]\n"<entry>:player" = { read = true, write = ["hp"], call = ["heal"] }\n'
        '"<entry>:mode" = { read = true, write = true }\n\n'
        '[actions]\nheal = "player.heal"\nwhere = { call = "where", on = "agent" }\n',
        encoding="utf-8")
    t = make_target(script="game_loop.py", cwd=str(tmp_path))
    with Client.attach(t.pid) as c:
        assert c.scope()["actions"] == ["heal", "where"]
        assert c.do("heal", "-30")["result"]["preview"] == "70"
        assert c.set("player.hp", "99")["new"] == "99"
        assert c.set("mode", '"turbo"')["new"] == "'turbo'"           # rule on a local
        with pytest.raises(RemoteError, match="not allowlisted"):
            c.set("player.speed", "9")
        with pytest.raises(RemoteError, match="not allowlisted"):
            c.call("where()")                                         # both: browse only
        with pytest.raises(RemoteError, match="not allowlisted"):
            c.do("where")
        assert "frame" in {e["name"] for e in c.ls()["entries"]}     # browsing still works
        c.ls("player")


@needs_314
def test_handle_cap(make_target):
    t = make_target()
    with Client.attach(t.pid, max_handles=100) as c:
        for _ in range(3):
            for target in (None, "Config", "Counter", "threading", "os", "sys"):
                c.ls(target, limit=200)
        p = c.ping()
        assert p["handles"] <= 100 and p["handles_evicted"] > 0


@needs_314
def test_idle_timeout(make_target):
    t = make_target()
    Client.attach(t.pid, idle_timeout=1).close()
    assert os.path.exists(protocol.session_path(t.pid))
    deadline = time.time() + 8
    while os.path.exists(protocol.session_path(t.pid)) and time.time() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(protocol.session_path(t.pid))
    with Client.attach(t.pid) as c:
        assert c.attach_info["reused"] is False


# -- GUI toolkits ---------------------------------------------------------------------------

def _has_display():
    return sys.platform in ("win32", "darwin") or bool(os.environ.get("DISPLAY")
                                                       or os.environ.get("WAYLAND_DISPLAY"))


@needs_314
@pytest.mark.skipif(importlib.util.find_spec("tkinter") is None or not _has_display(),
                    reason="needs tkinter and a display")
def test_tk_executor(make_target):
    t = make_target(script="tk_app.py")
    with Client.attach(t.pid) as c:
        assert c.executors()["main"]["mode"] == "tk"
        assert c.call("where()")["result"]["preview"] == "'MainThread'"
        c.call('label.config(text="changed")')
        assert c.call('label.cget("text")')["result"]["preview"] == "'changed'"


def _qt_usable():
    """PySide6 importable *and* its Qt libraries loadable (e.g. libEGL on Linux)."""
    if importlib.util.find_spec("PySide6") is None:
        return False
    import site
    import subprocess
    from conftest import target_python
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.pathsep.join(site.getsitepackages()))
    try:
        r = subprocess.run([target_python(), "-c", "from PySide6.QtWidgets import QApplication"],
                           env=env, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


@needs_314
@pytest.mark.skipif(not _qt_usable(),
                    reason="needs a working PySide6 (pip install PySide6-Essentials, plus "
                           "Qt's system libraries)")
def test_qt_executor(make_target):
    t = make_target(script="qt_app.py")
    with Client.attach(t.pid) as c:
        assert c.executors()["main"]["mode"] == "qt"
        assert c.call("where()")["result"]["preview"] == "'MainThread'"
        c.call('label.setText("changed")')
        assert c.call("label.text()")["result"]["preview"] == "'changed'"
        c.detach()
