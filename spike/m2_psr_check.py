"""M2 check against a running PySynthRack: calls on the main loop, audio level.

    python m2_psr_check.py <pid> <patch.json>

The patch should be Osc(1) -> Meter(3) -> Speaker(2). The script loads it,
starts audio, measures frame time and xruns while idle, while making 300
App._mirror_param calls on the main executor and while making 300 calls on
the agent executor, checks the Meter level follows the oscillator's amp, then
stops audio. Prints JSON.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exposure.client import Client  # noqa: E402

pid, patch = int(sys.argv[1]), sys.argv[2]
OPTS = dict(entry="App.run", pressure="<entry>:self.backend._xruns")
c = Client.attach(pid, **OPTS)
out = {"attach": c.attach_info}


def preview_of(listing, name):
    for e in listing["entries"]:
        if e["name"] == name:
            return e["preview"]
    return None


def xruns():
    b = c.ls("self.backend", limit=1000)
    return int(preview_of(b, "_xruns")), int(preview_of(b, "_dsp_overloads"))


def level():
    return float(c.ls("self.backend._audio_levels")["entries"][0]["preview"])


def with_frames(load):
    """Sample 300 frames on a second connection while ``load`` runs here."""
    res = {}

    def sample():
        with Client.attach(pid, **OPTS) as c2:
            res["frames"] = c2.frames(300)

    x0 = xruns()
    th = threading.Thread(target=sample)
    th.start()
    time.sleep(0.05)
    t0 = time.perf_counter()
    blocked = load() if load else None
    th.join(60)
    x1 = xruns()
    res["load_s"] = round(time.perf_counter() - t0, 3)
    res["xruns_delta"], res["overloads_delta"] = x1[0] - x0[0], x1[1] - x0[1]
    if blocked:
        blocked.sort()
        res["job_blocked_ms"] = {"mean": round(sum(blocked) / len(blocked), 3),
                                 "p99": blocked[int(len(blocked) * 0.99)], "max": blocked[-1]}
    return res


r = c.call(f"self._load_patch_from({patch!r})")
out["load_patch"] = {k: r.get(k) for k in ("state", "blocked_ms", "error")}
out["modules"] = [e["preview"] for e in c.ls("self.patch.modules")["entries"]]
r = c.call("self._on_toggle_audio()")
out["start_audio"] = {k: r.get(k) for k in ("state", "blocked_ms", "thread")}
time.sleep(1.0)
out["level_amp_0.3"] = level()
out["idle"] = with_frames(None)


def main_load():
    blocked = []
    for i in range(300):
        amp = 0.3 - 0.2 * (i / 299)
        r = c.call(f'self._mirror_param(1, "amp", {amp!r})')
        assert r["state"] == "done", r
        blocked.append(r["blocked_ms"])
    return blocked


out["main_calls"] = with_frames(main_load)
time.sleep(1.5)  # meter release 0.4 s
out["level_amp_0.1"] = level()
out["model_amp"] = preview_of(c.ls("self.patch.modules[1].params"), "amp")
wid = c.call('self._param_widget(1, "amp")')["result"]["preview"]
out["widget_amp"] = c.call(f"dpg.get_value({wid})")["result"]["preview"]


def agent_load():
    blocked = []
    for _ in range(300):
        r = c.call("self.backend.snapshot_audio_levels()", on="agent")
        assert r["state"] == "done", r
        blocked.append(r["blocked_ms"])
    return blocked


out["agent_calls"] = with_frames(agent_load)
r = c.call("self._on_toggle_audio()")
out["stop_audio"] = r["state"]
out["ping"] = {k: c.ping()[k] for k in ("budget", "handles", "jobs_pending", "errors")}
out["executors"] = c.executors()
print(json.dumps(out, indent=1))
c.close()
