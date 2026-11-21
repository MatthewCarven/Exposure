"""M3: pause/step/resume, watch sampling, nudge, find, repeated calls, presets,
and a frame-time benchmark."""
import importlib.util
import os
import sys
import threading
import time

import pytest

from conftest import needs_314
from exposure import policy
from exposure.client import Client, RemoteError, parse_interval

pytestmark = needs_314


def frame_of(c):
    return int(c.sample(["frame"])["values"][0]["preview"])


def test_parse_interval():
    assert parse_interval("250ms") == 0.25 and parse_interval("2s") == 2
    assert parse_interval("0.5") == 0.5 and parse_interval(3) == 3.0


def test_presets_parse():
    p = policy.parse({"presets": {"hud": {"watch": ["player.hp"], "tweak": ["player.speed"],
                                          "rate": 5, "step": {"player.speed": 0.1}}}})
    assert p.presets["hud"] == {"watch": ["player.hp"], "tweak": ["player.speed"],
                                "rate": 5.0, "step": {"player.speed": 0.1}}
    with pytest.raises(policy.PolicyError):
        policy.parse({"presets": {"x": {"watch": "player.hp"}}})


def test_pause_step_resume(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        r = c.pause()
        assert r["paused"] and "Not Responding" in r["warning"]
        f0 = frame_of(c)
        time.sleep(0.2)
        assert frame_of(c) == f0                       # frozen between frames
        assert c.call("player.heal(1)")["state"] == "done"   # calls still run
        assert c.nudge("player.speed", 0.5)["new"] == "2.0"
        r = c.step(3)
        assert r["paused"] and r["advanced"] == 3
        assert frame_of(c) == f0 + 3
        c.step()
        assert frame_of(c) == f0 + 4
        r = c.resume()
        assert not r["paused"]
        time.sleep(0.2)
        assert frame_of(c) > f0 + 10
        with pytest.raises(RemoteError, match="not paused"):
            c.step()


def test_pause_auto_resumes(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        r = c.pause(seconds=0.4)
        assert r["paused"] and r["auto_resume_in_s"] <= 0.4
        f0 = frame_of(c)
        time.sleep(0.8)
        assert not c.paused()["paused"] and frame_of(c) > f0


def test_detach_resumes(make_target):
    t = make_target(script="game_loop.py")
    c = Client.attach(t.pid)
    c.pause()
    f0 = frame_of(c)
    c.detach()
    time.sleep(1.0)
    with Client.attach(t.pid) as c2:
        assert c2.attach_info["reused"] is False
        assert frame_of(c2) > f0 + 10


def test_idle_timeout_resumes(make_target):
    t = make_target(script="game_loop.py")
    c = Client.attach(t.pid, idle_timeout=1)
    c.pause()
    f0 = frame_of(c)
    c.close()
    time.sleep(3.5)
    with Client.attach(t.pid) as c2:
        assert frame_of(c2) > f0 + 10


def test_read_only_refuses_pause(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid, read_only=True) as c:
        with pytest.raises(RemoteError, match="read-only"):
            c.pause()


def test_sample_and_nudge(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        v = {x["path"]: x for x in c.sample(["player.hp", "len(player.pos)", "nope",
                                              "len(player)", "player.name"])["values"]}
        assert v["player.hp"]["preview"] == "100"
        assert v["len(player.pos)"]["preview"] == "2"
        assert "not found" in v["nope"]["error"]
        assert "built-in containers" in v["len(player)"]["error"]
        assert v["player.name"]["type"] == "property"
        assert c.nudge("player.hp", -10)["new"] == "90"
        assert c.nudge("player.speed", 1)["new"] == "2.5"
        r = c.nudge("player.pos[1]", 0.5)
        assert r["new"] == "0.5"
        r = c.nudge('settings["difficulty"]', 1)
        assert r["state"] == "error" and "only nudge numbers" in r["error"]
        assert [h["target"] for h in c.history()] == ["player.hp", "player.speed",
                                                      "player.pos[1]"]
        c.undo(3)
        v = {x["path"]: x for x in c.sample(["player.hp", "player.speed"])["values"]}
        assert v["player.hp"]["preview"] == "100" and v["player.speed"]["preview"] == "1.5"


def test_find(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        r = c.find("type:Player")
        assert [e["path"] for e in r["results"]] == ["player"]
        assert r["results"][0]["handle"].startswith("@")
        r = c.find("name:*speed*")
        assert "player.speed" in [e.get("path") for e in r["results"]]
        r = c.find("type:__main__.Player", via="gc")
        assert len(r["results"]) == 1 and r["scanned"] > 100
        with pytest.raises(RemoteError, match="type:ClassName"):
            c.find("Player")
    t2 = make_target("--expose")
    with Client.attach(t2.pid, scope="explicit") as c:
        with pytest.raises(RemoteError, match="explicit scope"):
            c.find("type:Config", via="gc")
        r = c.find("type:Config")                       # graph walk stays in scope
        assert [e["path"] for e in r["results"]] == ["config"]


def test_every_and_stop(make_target):
    t = make_target(script="game_loop.py")
    with Client.attach(t.pid) as c:
        r = c.every("50ms", "bump()", count=4)
        rid = r["id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            reps = {x["id"]: x for x in c.repeats()}
            if not reps[rid]["running"]:
                break
            time.sleep(0.05)
        assert reps[rid]["runs"] == 4 and reps[rid]["errors"] == 0
        assert c.call("bump()")["result"]["preview"] == "5"
        r2 = c.every("20ms", "bump()")
        time.sleep(0.3)
        assert c.stop(r2["id"])["stopped"] == [r2["id"]]
        time.sleep(0.1)
        runs = {x["id"]: x for x in c.repeats()}[r2["id"]]["runs"]
        time.sleep(0.2)
        assert {x["id"]: x for x in c.repeats()}[r2["id"]]["runs"] == runs


def test_presets_and_panel_cli(make_target, tmp_path, capsys):
    from exposure.cli import main
    (tmp_path / "exposure.toml").write_text(
        '[presets.hud]\nwatch = ["frame", "player.hp"]\ntweak = ["player.speed"]\nrate = 20\n'
        'step = { "player.speed" = 0.5 }\n', encoding="utf-8")
    t = make_target(script="game_loop.py", cwd=str(tmp_path))
    with Client.attach(t.pid) as c:
        assert c.presets()["hud"]["tweak"] == ["player.speed"]
    assert main(["preset", str(t.pid), "hud", "--count", "2", "--plain"]) == 0
    out = capsys.readouterr().out
    assert out.count("player.speed  (step 0.5)") == 2 and "player.hp" in out
    assert main(["watch", str(t.pid), "frame", "len(player.pos)", "--count", "1",
                 "--plain"]) == 0
    assert "len(player.pos)" in capsys.readouterr().out
    assert main(["preset", str(t.pid), "nope", "--count", "1"]) == 2


def _has_display():
    return sys.platform in ("win32", "darwin") or bool(os.environ.get("DISPLAY")
                                                       or os.environ.get("WAYLAND_DISPLAY"))


@pytest.mark.skipif(importlib.util.find_spec("tkinter") is None or not _has_display(),
                    reason="needs tkinter and a display")
def test_tk_pause(make_target):
    t = make_target(script="tk_app.py")
    with Client.attach(t.pid) as c:
        def ticks():
            return c.sample(["ticks[0]"])["values"][0]["preview"]
        assert c.pause()["paused"]
        a = ticks()
        time.sleep(0.4)
        assert ticks() == a                             # Tk's loop is held
        assert c.call("where()")["result"]["preview"] == "'MainThread'"
        c.resume()
        time.sleep(0.4)
        assert ticks() != a


# -- benchmark --------------------------------------------------------------------------------

def test_frame_time_benchmark(make_target):
    """Frame time while idle, while a 10-value watch samples at 50 Hz, and while
    making main-loop calls. Loose bounds (shared CI machines are noisy); the
    numbers are printed for the record."""
    t = make_target(script="game_loop.py")
    paths = ["frame", "player.hp", "player.speed", "player.pos", "len(player.pos)",
             "settings", "settings['volume']", "mode", "turbo_frames", "bump.count"]
    with Client.attach(t.pid) as c:
        idle = c.frames(200)
        stop = threading.Event()

        def load(kind):
            with Client.attach(t.pid) as c2:
                while not stop.is_set():
                    if kind == "watch":
                        c2.sample(paths)
                        time.sleep(0.02)
                    else:
                        c2.call("player.heal(0)")

        results = {"idle": idle}
        for kind in ("watch", "calls"):
            stop.clear()
            th = threading.Thread(target=load, args=(kind,))
            th.start()
            time.sleep(0.1)
            results[kind] = c.frames(200)
            stop.set()
            th.join(10)
    print("\nframe-time benchmark (ms):",
          {k: (v["mean_ms"], v["p99_ms"]) for k, v in results.items()})
    for kind in ("watch", "calls"):
        r = results[kind]
        assert r["completed"]
        assert r["mean_ms"] < idle["mean_ms"] * 1.5 + 2
        assert r["p99_ms"] < idle["p99_ms"] * 2 + 10
