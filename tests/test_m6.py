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
