"""M6: polish (doc/src, ls filters, sparklines, dropped clients), and more."""
import io
import os
import subprocess
import time

import pytest

from conftest import ROOT, needs_314, target_python
from exposure import cli, protocol, tui
from exposure.client import Client


def test_sparkline_and_numbers():
    assert tui.as_number("1.5") == 1.5 and tui.as_number("True") is None
    assert tui.as_number("'x'") is None
    assert tui.sparkline([1, 1, 1], chars="ab") == "aaa"
    assert tui.sparkline([0, 5, 10], chars=tui.ASCII_SPARKS) == "_=@"
    assert len(tui.sparkline(list(range(24)))) == 24


def test_split_ls_args():
    assert cli.split_ls_args("player", "*p*") == ("player", "*p*")
    assert cli.split_ls_args("*speed*", None) == (None, "*speed*")
    assert cli.split_ls_args("player", None) == ("player", None)


@needs_314
def test_doc_src_and_ls_filter(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        d = c.doc("player.heal")
        assert d["signature"] == "Player.heal(self, amount, *, cap=100)"
        assert d["defined"].endswith("game_loop.py:" + d["defined"].rsplit(":", 1)[1])
        d = c.doc("player")                     # an instance: about its class
        assert d["about"].startswith("its class") and d["signature"].startswith("Player(")
        d = c.doc("time.sleep")
        assert "sleep" in d.get("doc", "") or "sleep" in d.get("signature", "")
        s = c.src("player.heal")
        assert s["lines"][0].strip().startswith("def heal") and s["start"] > 1
        assert any("min(cap" in ln for ln in s["lines"])
        names = [e["name"] for e in c.ls("player", pattern="*p*")["entries"]]
        assert set(names) == {"hp", "speed", "pos"}
        roots = c.ls(pattern="*frame*")
        assert [e["name"] for e in roots["entries"]] == ["turbo_frames", "frame"]
        assert roots["total"] == 2


@needs_314
def test_shell_polish(make_target):
    from exposure.repl import Shell
    t = make_target(script="game_loop.py")
    script = "\n".join(["?player.heal", "help player", "src player.heal", "!x = 41",
                        "!x + 1", "ls player *p*", "quit", ""])
    out = io.StringIO()
    with Client.attach(t.pid) as c:
        Shell(c, stdin=io.StringIO(script), stdout=out, confirm=False, page=False).cmdloop()
    text = out.getvalue()
    assert "Player.heal(self, amount, *, cap=100)" in text
    assert "its class" in text and "def heal" in text
    assert "-> None" not in text and "-> 42" in text
    assert "speed" in text and "turbo_frames" not in text


@needs_314
def test_dropped_client_is_tidied_quickly(make_target):
    t = make_target(script="game_loop.py")
    # a client that is killed (no bye) -> the agent tidies up after the grace
    code = (f"import sys, time; sys.path.insert(0, {ROOT!r}); "
            "from exposure.client import Client; "
            f"c = Client.attach({t.pid}, drop_grace=1); c.ping(); "
            "print('up', flush=True); time.sleep(60)")
    p = subprocess.Popen([target_python(), "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "up"
    p.kill()
    p.wait(10)
    deadline = time.time() + 8
    while os.path.exists(protocol.session_path(t.pid)) and time.time() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(protocol.session_path(t.pid))


@needs_314
def test_clean_close_keeps_the_session(make_target):
    t = make_target(script="game_loop.py")
    c = Client.attach(t.pid, drop_grace=1)
    c.pause()                                  # survives a clean disconnect
    c.close()                                  # says bye
    time.sleep(2.5)
    assert os.path.exists(protocol.session_path(t.pid))
    with Client.attach(t.pid) as c2:
        assert c2.attach_info["reused"] and c2.paused()["paused"]
        c2.resume()


# -- part 2: logpoints, traces, breakpoints ---------------------------------------------------

def _line_of(text, script="game_loop.py"):
    path = os.path.join(ROOT, "tests", "targets", script)
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if text in line:
                return f"{script}:{i}"
    raise AssertionError(text)


HOT = "player.pos[0] += player.speed"


def _events(c, seconds):
    out = []
    c.stream_events(out.append, since=0, seconds=seconds)
    return out


@needs_314
def test_logpoint(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        p = c.log(_line_of(HOT), "frame", rate=50, confirmed=True)
        assert p["kind"] == "log" and p["codes"] == 1
        evs = [e for e in _events(c, 0.8) if e.get("probe") == p["id"]]
        assert 5 < len(evs) <= 50 * 0.8 + 50 + 5          # rate limited (bucket burst 50)
        assert all(e["thread"] == "MainThread" for e in evs)
        info = {x["id"]: x for x in c.probes()}[p["id"]]
        assert info["suppressed"] > 0 and info["hits"] > info["emitted"]
        c.unprobe(p["id"])
        p = c.log(_line_of(HOT), "frame", when="frame % 2 == 0", rate=1000, confirmed=True)
        evs = [e for e in _events(c, 0.3) if e.get("probe") == p["id"]]
        vals = [int(e["text"].rsplit("= ", 1)[1]) for e in evs]
        assert vals and all(v % 2 == 0 for v in vals)
        bad = c.log(_line_of(HOT), "1 / 0", confirmed=True)
        evs = _events(c, 0.3)
        assert any("disabled" in e["text"] and "ZeroDivisionError" in e["text"] for e in evs)
        assert {x["id"]: x for x in c.probes()}[bad["id"]]["enabled"] is False
        c.unprobe()
        assert c.probes() == []


@needs_314
def test_probe_gating(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        with pytest.raises(Exception, match="confirmed"):
            c.log(_line_of(HOT), "frame")
        with pytest.raises(Exception, match="FILE:LINE"):
            c.log("nowhere", "frame", confirmed=True)
        with pytest.raises(Exception, match="no function"):
            c.log("game_loop.py:99999", "frame", confirmed=True)
    t2 = make_target(script="game_loop.py")
    with Client.attach(t2.pid, read_only=True) as c:
        with pytest.raises(Exception, match="read-only"):
            c.log(_line_of(HOT), "frame", confirmed=True)


@needs_314
def test_trace(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        c.trace("player.heal")
        c.trace("boom")
        # (calls made by the main-loop executor run inside its monitoring callback,
        # where CPython doesn't report events; the program's own calls are traced)
        c.call("player.heal(3)", on="agent")
        c.call("boom()", on="agent")
        texts = [e["text"] for e in _events(c, 0.3)]
        assert any(x.startswith("-> Player.heal(self=<__main__.Player>, amount=3, cap=100)")
                   for x in texts)
        assert any(x.startswith("<- Player.heal = 100") for x in texts)
        assert any("<- boom raised ValueError: kaboom" in x for x in texts)
        with pytest.raises(Exception, match="trace needs a Python function"):
            c.trace("player")


@needs_314
def test_break_when_and_line_break(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        b = c.break_(when="frame % 50 == 0", confirmed=True)
        assert b["kind"] == "break when"
        deadline = time.time() + 5
        while not c.paused()["paused"] and time.time() < deadline:
            time.sleep(0.05)
        assert c.paused()["paused"]
        f = int(c.sample(["frame"])["values"][0]["preview"])
        assert f % 50 == 0                                   # paused right at that frame
        assert any("is true at iteration" in e["text"] for e in _events(c, 0.2))
        assert c.probes() == []                              # one-shot: it disarmed
        c.resume()
        lb = c.break_(_line_of(HOT), when="player.hp < 50", confirmed=True)
        time.sleep(0.2)
        assert not c.paused()["paused"]
        c.set("player.hp", "40")
        deadline = time.time() + 5
        while not c.paused()["paused"] and time.time() < deadline:
            time.sleep(0.05)
        assert c.paused()["paused"]
        c.unprobe(lb["id"])
        c.resume()


@needs_314
def test_logpoint_on_hot_loop_keeps_frame_budget(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        base = c.frames(200)
        c.log(_line_of(HOT), "frame, player.pos[0]", confirmed=True)   # every frame hit
        c.trace("player.heal")
        hot = c.frames(200)
        print(f"\nframes without/with a logpoint on the hot line: "
              f"{base['mean_ms']}/{hot['mean_ms']} ms mean, "
              f"{base['p99_ms']}/{hot['p99_ms']} ms p99")
        assert hot["mean_ms"] < base["mean_ms"] * 1.5 + 1
        c.detach()
    with Client.attach(t.pid) as c:                          # probes' tool id was freed
        r = c.eval("[sys.monitoring.get_tool(i) for i in range(6)]", on="agent",
                   confirmed=True, capture=False)
        assert "exposure-probes" not in r["result"]["preview"]


@needs_314
def test_shell_probes(make_target):
    from exposure.repl import Shell
    t = make_target(script="game_loop.py")
    script = "\n".join([f"log {_line_of(HOT)} frame --when frame % 3 == 0", "probes",
                        "unlog all", "break when frame > 0", "paused", "resume", "quit", ""])
    out = io.StringIO()
    with Client.attach(t.pid) as c:
        sh = Shell(c, stdin=io.StringIO(script), stdout=out, confirm=False, page=False)
        sh.stream_seconds = 0.4
        sh.cmdloop()
    text = out.getvalue()
    assert "log 1 added" in text and ": frame = " in text
    assert "break when" in text and "PAUSED" in text
