"""M4: eval/exec, output capture, asyncio, deny list, completion, shell, config."""
import io
import os
import queue
import sys
import time

import pytest

from conftest import needs_314
from exposure import config, exprs, policy, protocol
from exposure.client import AttachError, Client, RemoteError

pytestmark = needs_314


def drain(t):
    """Lines the target printed since the last call."""
    out = []
    while True:
        try:
            line = t.lines.get_nowait()
        except queue.Empty:
            return out
        if line is not None:
            out.append(line)


def test_eval_source_handles():
    assert exprs.eval_source('@3.hp + len("@4")') == '__exposure_h__(3).hp + len("@4")'


def test_safety_parse():
    p = policy.parse({"safety": {"deny": ["os.system"], "eval": True}})
    assert p.safety == {"deny": ["os.system"], "eval": True}
    with pytest.raises(policy.PolicyError):
        policy.parse({"safety": {"deny": "os.system"}})


def test_config_file(tmp_path, monkeypatch):
    f = tmp_path / "config.toml"
    f.write_text('[client]\nconfirm = false\nrate = 4\n[safety]\ndeny = ["os.*"]\n',
                 encoding="utf-8")
    monkeypatch.setenv("EXPOSURE_CONFIG", str(f))
    cfg = config.load()
    assert cfg["client"]["confirm"] is False and cfg["client"]["rate"] == 4
    assert cfg["client"]["history"] == 1000 and cfg["safety"]["deny"] == ["os.*"]


def test_eval_and_exec(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        r = c.eval("player.hp * 2", confirmed=True)
        assert r["mode"] == "eval" and r["result"]["preview"] == "200"
        assert r["namespace"] == "scratch" and r["thread"] == "MainThread"
        assert c.eval("_ + 1", confirmed=True)["result"]["preview"] == "201"
        h = c.ls()["entries"][1]["handle"]                     # player
        assert c.eval(f"{h}.speed", confirmed=True)["result"]["preview"] == "1.5"
        c.eval("double = lambda x: 2 * x", confirmed=True)     # scratch persists
        assert c.eval("double(21)", confirmed=True)["result"]["preview"] == "42"
        assert "double" in c.scratch()
        assert c.eval("frame", frame="main", confirmed=True)["result"]["preview"].isdigit()
        r = c.eval('mode = "turbo"', frame="main", confirmed=True)
        assert r["mode"] == "exec" and r["namespace"] == "frame main"
        time.sleep(0.2)
        n = {e["name"]: e for e in c.ls()["entries"]}
        assert n["mode"]["preview"] == "'turbo'" and int(n["turbo_frames"]["preview"]) > 0
        r = c.eval("def f(n):\n    return 1 / n\nf(0)", confirmed=True)
        assert r["state"] == "error" and r["error"].startswith("ZeroDivisionError")
        assert "<exposure-eval-" in r["traceback"] and "return 1 / n" in r["traceback"]
        assert c.eval("where()", on="agent", confirmed=True)["result"]["preview"].startswith(
            "'exposure-call-")
        with pytest.raises(RemoteError, match="confirmed"):
            c.eval("1 + 1")


def test_capture(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        drain(t)
        r = c.eval('print("hello", where()); import sys; print("oops", file=sys.stderr)',
                   confirmed=True)
        assert r["output"] == "hello MainThread\noops\n"
        r = c.call('calls.append("x")')
        assert "output" not in r
        r = c.eval('print("x" * 100000)', confirmed=True)
        assert r["output_truncated"] and len(r["output"]) == 64 * 1024
        time.sleep(0.3)
        assert drain(t) == []                                  # the app's stdout saw none
        r = c.eval('print("to the app", flush=True)', confirmed=True, capture=False)
        assert "output" not in r
        deadline = time.time() + 3
        seen = []
        while time.time() < deadline and not any("to the app" in x for x in seen):
            seen += drain(t)
            time.sleep(0.05)
        assert any("to the app" in x for x in seen)
        c.detach()
    with Client.attach(t.pid) as c:                             # detach put stdout back
        r = c.eval("type(sys.stdout).__name__, type(sys.stderr).__name__", confirmed=True,
                   capture=False)
        assert r["result"]["preview"] == "tuple[2] ('TextIOWrapper', 'TextIOWrapper')"


def test_eval_refused_by_scope_and_read_only(make_target, tmp_path):
    t = make_target("--expose")
    with Client.attach(t.pid, scope="explicit") as c:
        with pytest.raises(RemoteError, match="eval/exec is off"):
            c.eval("1", confirmed=True)
    t2 = make_target(script="game_loop.py")
    with Client.attach(t2.pid, read_only=True) as c:
        with pytest.raises(RemoteError, match="read-only"):
            c.eval("1", confirmed=True)
    (tmp_path / "exposure.toml").write_text(
        '[agent]\nscope = "explicit"\n[expose]\n"<entry>:player" = { read = true }\n'
        '[safety]\neval = true\ndeny = ["time.*"]\n', encoding="utf-8")
    t3 = make_target(script="game_loop.py", cwd=str(tmp_path))
    with Client.attach(t3.pid) as c:
        r = c.eval("player.hp + 1", confirmed=True)
        assert r["result"]["preview"] == "101"
        r = c.eval("time.sleep(0)", confirmed=True)             # eval: warning only
        assert any("deny list" in w for w in r["warnings"])


def test_deny_list(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid, deny=["time.*", "*.heal"]) as c:
        with pytest.raises(RemoteError, match="deny list"):
            c.call("time.sleep(0)")
        with pytest.raises(RemoteError, match="deny list"):
            c.call("player.heal(1)")
        assert c.call("where()")["state"] == "done"


def test_asyncio(make_target):
    t = make_target(script="async_app.py")
    with Client.attach(t.pid) as c:
        assert c.executors()["main"]["mode"] == "asyncio"
        r = c.call("where()")
        assert r["on"] == "asyncio" and r["result"]["preview"] == "'MainThread'"
        r = c.call("fetch(21)")
        assert r["result"]["preview"] == "42" and r["awaited"]
        r = c.call("fetch(2)", on="agent")                        # awaited on the loop
        assert r["result"]["preview"] == "4" and r["awaited"]
        assert c.call("fetch(3)", on="loop")["result"]["preview"] == "6"
        r = c.call("failing()")
        assert r["state"] == "error" and "KeyError" in r["error"]
        r = c.eval("fetch(5)", confirmed=True)
        assert r["result"]["preview"] == "10"
        r = c.call("fetch(1)", timeout=0.01)
        assert r["state"] == "running"
        assert c.result(r["job"], timeout=2)["result"]["preview"] == "2"
        assert c.eval('state["fetched"]', confirmed=True)["result"]["preview"].startswith(
            "list[")


def test_complete(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        assert "player" in c.complete("pla")
        assert c.complete("player.h") == ["player.heal", "player.hp"]
        assert c.complete("settings.ke") == ["settings.keys"]          # methods too, not keys
        assert c.complete("nope.x") == []


def test_reattach_loads_current_agent_code(make_target):
    """A long-running target must not keep a previous session's agent code."""
    t = make_target("--expose")             # the program imported exposure itself
    with Client.attach(t.pid, scope="reachable") as c:
        c.eval("import sys; sys.modules['exposure.agent'].AGENT_VERSION = 'stale'",
               confirmed=True)
        assert c.ping()["agent"] == "stale"
        c.detach()
    with Client.attach(t.pid) as c:
        assert c.ping()["agent"] != "stale"
        assert {e["name"] for e in c.ls()["entries"]} >= {"config", "items"}  # registry kept
        assert c.scope()["scope"] == "both"


def test_failed_remote_exec_removes_boot_file(make_target, monkeypatch):
    t = make_target()

    def fail(pid, path):
        raise OSError("simulated")

    monkeypatch.setattr(sys, "remote_exec", fail)
    with pytest.raises(AttachError, match="simulated"):
        Client.attach(t.pid)
    assert not os.path.exists(protocol.boot_path(t.pid))


def test_shell_script(make_target):
    from exposure.repl import Shell
    t = make_target(script="game_loop.py")
    script = "\n".join([
        "help", "ls", "call where()", "!player.hp + 1", "set player.hp 50",
        "set player.speed = 2.0", "undo", "history", "py", "player.hp * 3", "py",
        "frame main", "!mode", "frame", "find type:Player", "nosuch", "ls nope",
        r".venv\Scripts\python -m exposure shell 1 --entry App.run", "quit", ""])
    out = io.StringIO()
    with Client.attach(t.pid) as c:
        sh = Shell(c, stdin=io.StringIO(script), stdout=out, confirm=False, page=False)
        sh.cmdloop()
    text = out.getvalue()
    assert "Browse:" in text and "entry main" in text
    assert "'MainThread'" in text and "-> 101" in text
    assert "100  ->  50" in text and "1.5  ->  2.0" in text
    assert "-> 150" in text                       # py mode: player.hp is 50 * 3
    assert "already in the exposure shell" in text   # a pasted PowerShell command
    assert "-> 'normal'" in text                  # frame main: the local `mode`
    assert "unknown command 'nosuch'" in text and "error:" in text


def test_reattach_right_after_detach(make_target):
    """A detaching agent must not take a new client: re-attaching straight after
    a detach gets a working session every time (it used to land on the dying
    agent about one time in three and lose the connection on the first request)."""
    t = make_target(script="game_loop.py")
    for _ in range(8):
        with Client.attach(t.pid) as c:
            assert c.ping()["pid"] == t.pid
            c.detach()
