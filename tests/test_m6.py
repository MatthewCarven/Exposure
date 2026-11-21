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


# -- part 3: snapshots ---------------------------------------------------------------------------

@needs_314
def test_snap_and_diff(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        r = c.snap("before", "player")
        assert r["values"] >= 5 and not r["truncated"]
        c.nudge("player.hp", -25)
        c.set("player.speed", "3.0")
        d = c.diff("before")
        changed = {p: (o, n) for p, o, n in d["changed"]}
        assert changed["player.hp"] == ("100", "75")
        assert changed["player.speed"] == ("1.5", "3.0")
        assert "player.pos[0]" in changed                  # the loop moves it every frame
        assert d["b"] == "now" and d["counts"]["removed"] == 0
        c.snap("s1", "settings")
        c.set('settings["volume"]', "0.9")
        c.call('settings.update(extra=1)')
        c.snap("s2", "settings")
        d = c.diff("s1", "s2")
        assert sorted(map(tuple, d["changed"])) == [("settings", "dict[2]", "dict[3]"),
                                                    ("settings['volume']", "0.5", "0.9")]
        assert d["added"] == [["settings['extra']", "1"]]
        assert {x["name"] for x in c.snaps()} == {"before", "s1", "s2"}
        with pytest.raises(Exception, match="no snapshot"):
            c.diff("nope")
        assert c.unsnap()["removed"] == 3


def test_print_diff(capsys):
    cli.print_diff({"target": "player", "a": "x", "b": "now", "seconds_apart": 1.0,
                    "truncated": False, "changed": [["player.hp", "100", "75"]],
                    "added": [["player.new", "1"]], "removed": [],
                    "counts": {"changed": 1, "added": 1, "removed": 0, "same": 4}})
    out = capsys.readouterr().out
    assert "~ player.hp: 100 -> 75" in out and "+ player.new: 1" in out


# -- part 4: hot reload ----------------------------------------------------------------------------

MOD_V1 = '''counter = 0


def compute(x):
    return x + 1


class Game:
    def __init__(self):
        self.level = 1

    def speed(self):
        return 10

    @staticmethod
    def label():
        return "v1"
'''

MOD_V2 = '''counter = 0


def compute(x):
    return x + 100


def bonus():
    return 7


class Game:
    def __init__(self):
        self.level = 1

    def speed(self):
        return 99

    def boost(self):
        return bonus() * 2

    @staticmethod
    def label():
        return "v2"
'''

MAIN = '''import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reloadable  # noqa: E402
from reloadable import compute  # noqa: E402


def helper():
    return "{tag}"


def main():
    game = reloadable.Game()
    result = speed = tag = None
    print("ready", flush=True)
    end = time.monotonic() + 60
    while time.monotonic() < end:
        result = compute(1)
        speed = game.speed()
        tag = helper()
        reloadable.counter += 1
        time.sleep(0.005)


if __name__ == "__main__":
    main()
'''


@needs_314
def test_hot_reload(make_target, tmp_path):
    (tmp_path / "reloadable.py").write_text(MOD_V1, encoding="utf-8")
    main_py = tmp_path / "reload_main.py"
    main_py.write_text(MAIN.replace("{tag}", "old"), encoding="utf-8")
    t = make_target(script=str(main_py))

    def vals(c, *names):
        v = {x["path"]: x.get("preview") for x in c.sample(list(names))["values"]}
        return [v[n] for n in names]

    with Client.attach(t.pid) as c:
        assert vals(c, "result", "speed", "tag") == ["2", "10", "'old'"]
        (tmp_path / "reloadable.py").write_text(MOD_V2, encoding="utf-8")
        with pytest.raises(Exception, match="confirmed"):
            c.reload("reloadable")
        r = c.reload("reloadable", confirmed=True)
        rep = r["report"]
        # (__init__ only moved down the file; its line numbers changed, so it is rebound too)
        assert {"compute", "Game.speed", "Game.label"} <= set(rep["rebound"])
        assert set(rep["added"]) == {"bonus", "Game.boost"}
        assert rep["kept_values"] == ["counter"]
        time.sleep(0.1)
        assert vals(c, "result", "speed") == ["101", "99"]     # from-import and instance
        assert c.call("game.boost()")["result"]["preview"] == "14"
        assert c.call("reloadable.Game.label()")["result"]["preview"] == "'v2'"
        n = int(vals(c, "reloadable.counter")[0])
        assert n > 10                                           # state kept
        # __main__ too: main() keeps running its old code, helper() is rebound
        main_py.write_text(MAIN.replace("{tag}", "new"), encoding="utf-8")
        r = c.reload("__main__", confirmed=True)
        assert "helper" in r["report"]["rebound"] and "main" not in r["report"]["rebound"]
        time.sleep(0.1)
        assert vals(c, "tag") == ["'new'"]
        # undo, newest first
        c.reload("__main__", undo=True, confirmed=True)
        c.reload("reloadable", undo=True, confirmed=True)
        time.sleep(0.1)
        assert vals(c, "result", "speed", "tag") == ["2", "10", "'old'"]
        r = c.call("game.boost()")                              # the added method is gone
        assert r["state"] == "error" and "AttributeError" in r["error"]


@needs_314
def test_eval_does_not_inherit_agent_future_flags(make_target):
    """The agent's `from __future__ import annotations` must not leak into code it
    compiles for the target (eval, reload, logpoints)."""
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        c.eval("def annotated(x: int): pass", confirmed=True)
        r = c.eval("annotated.__annotations__['x'] is int", confirmed=True)
        assert r["result"]["preview"] == "True"


# -- part 5: browser panel ---------------------------------------------------------------------

PANEL_POLICY = '''
[actions]
heal = "player.heal"

[presets.hud]
watch = ["frame", "player.hp", "len(player.pos)"]
tweak = ["player.speed"]
range = { "player.speed" = [0, 10] }
sliders = [ { label = "heal by", read = "player.hp", action = "heal", args = [], range = [0, 50] } ]
'''


def _http(port, method, path, token=None, body=None, host=None, ctype="application/json",
          origin=None):
    import http.client
    import json as _json
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Exposure-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = _json.dumps(body).encode()
        headers["Content-Type"] = ctype
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    hdrs = dict(r.getheaders())
    conn.close()
    try:
        payload = _json.loads(raw)
    except ValueError:
        payload = raw
    return r.status, payload, hdrs


@needs_314
def test_panel_http_api(make_target, tmp_path):
    from exposure.webpanel import PanelServer
    (tmp_path / "exposure.toml").write_text(PANEL_POLICY, encoding="utf-8")
    t = make_target(script="game_loop.py", cwd=str(tmp_path))
    with Client.attach(t.pid) as c:
        panel = PanelServer(c, preset="hud").start()
        try:
            port, tok = panel.port, panel.token
            # security
            assert _http(port, "GET", "/")[0] == 403                          # no token
            assert _http(port, "GET", f"/?token={tok}x")[0] == 403
            st, body, hdrs = _http(port, "GET", f"/?token={tok}")
            assert st == 200 and b"<title>Exposure panel</title>" in body
            assert not any(k.lower().startswith("access-control") for k in hdrs)
            assert _http(port, "GET", "/api/state", tok, host="evil.example:80")[0] == 403
            assert _http(port, "GET", "/api/state", tok, host=f"localhost:{port}")[0] == 200
            assert _http(port, "POST", "/api/pause", tok, {}, ctype="text/plain")[0] == 415
            assert _http(port, "POST", "/api/pause", tok, {},
                         origin="http://evil.example")[0] == 403
            assert _http(port, "PUT", "/api/pause", tok)[0] in (403, 405)
            # config and state
            st, cfg, _ = _http(port, "GET", "/api/config", tok)
            assert cfg["watch"] == ["frame", "player.hp", "len(player.pos)"]
            assert [s["label"] for s in cfg["sliders"]] == ["heal by", "player.speed"]
            assert cfg["sliders"][1]["range"] == [0, 10] and cfg["actions"] == ["heal"]
            st, state, _ = _http(port, "GET", "/api/state", tok)
            vals = {v["path"]: v["preview"] for v in state["values"]}
            assert vals["player.hp"] == "100" and vals["len(player.pos)"] == "2"
            # changes
            r = _http(port, "POST", "/api/set", tok, {"target": "player.hp", "value": "40"})[1]
            assert r["new"] == "40"
            r = _http(port, "POST", "/api/slider", tok, {"index": 0, "value": "15"})[1]
            assert r["result"]["preview"] == "55"                             # heal(15)
            r = _http(port, "POST", "/api/slider", tok, {"index": 1, "value": 2.5})[1]
            assert r["new"] == "2.5"                                          # set speed
            r = _http(port, "POST", "/api/do", tok, {"action": "heal", "values": ["5"]})[1]
            assert r["result"]["preview"] == "60"
            assert _http(port, "POST", "/api/pause", tok, {})[1]["paused"] is True
            r = _http(port, "POST", "/api/step", tok, {"n": 2})[1]
            assert r["advanced"] == 2
            assert _http(port, "POST", "/api/resume", tok, {})[1]["paused"] is False
            st, ev, _ = _http(port, "GET", "/api/events?since=0", tok)
            assert st == 200 and "events" in ev
            assert _http(port, "POST", "/api/nope", tok, {})[0] == 400
        finally:
            panel.stop()


def test_slider_policy_parse():
    from exposure import policy
    p = policy.parse({"presets": {"x": {"sliders": [
        {"read": "a.b", "action": "param", "args": [1, "amp"], "range": [0, 1]},
        {"read": "c", "set": "c"}]}}})
    s0, s1 = p.presets["x"]["sliders"]
    assert s0["args"] == ["1", "'amp'"] and s0["action"] == "param"
    assert s1["set"] == "c"
    with pytest.raises(policy.PolicyError):
        policy.parse({"presets": {"x": {"sliders": [{"read": "a"}]}}})


def test_example_policy_parses():
    from exposure import policy
    p = policy.load(os.path.join(ROOT, "examples", "pysynthrack.toml"))
    assert p.entry == "pysynthrack.ui.app:App.run" and set(p.actions) == {"param", "audio"}
    s = p.presets["panel"]["sliders"]
    assert [x["args"] for x in s] == [["1", "'amp'"], ["2", "'gain'"]]
