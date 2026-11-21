"""M3 check against a running PySynthRack: watch, find, pause/step with audio on.

    python m3_psr_check.py <pid> <patch.json>

Patch: Osc(1) -> Meter(3) -> Speaker(2). Prints JSON.
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
WATCH = ["self.backend._xruns", "self.backend._dsp_load", "self.backend._dsp_load_peak",
         "self.backend._audio_levels", "self.patch.modules[1].params['amp']",
         "self.patch.modules[1].params['freq']", "self.backend.sample_rate",
         "len(self.patch.modules)", "self._buffer_size", "self.backend._running"]


def val(path):
    v = c.sample([path])["values"][0]
    return v.get("preview", v.get("error"))


def xruns():
    return int(val("self.backend._xruns")), int(val("self.backend._dsp_overloads"))


def level():
    return float(c.ls("self.backend._audio_levels")["entries"][0]["preview"])


def with_frames(load, n=300):
    res = {}

    def sample():
        with Client.attach(pid, **OPTS) as c2:
            res["frames"] = c2.frames(n)

    x0 = xruns()
    th = threading.Thread(target=sample)
    th.start()
    time.sleep(0.05)
    extra = load() if load else None
    th.join(60)
    x1 = xruns()
    res["xruns_delta"], res["overloads_delta"] = x1[0] - x0[0], x1[1] - x0[1]
    if extra:
        res.update(extra)
    return res


c.call(f"self._load_patch_from({patch!r})")
c.call("self._on_toggle_audio()")
time.sleep(1.0)
out["idle"] = with_frames(None)


def watch_load():
    stop = time.perf_counter() + 2.2
    n, lat = 0, []
    with Client.attach(pid, **OPTS) as cw:
        while time.perf_counter() < stop:
            t0 = time.perf_counter()
            vals = cw.sample(WATCH)["values"]
            lat.append(time.perf_counter() - t0)
            n += 1
            time.sleep(max(0.0, 0.1 - (time.perf_counter() - t0)))
    errors = [v for v in vals if "error" in v]
    return {"samples": n, "values": len(WATCH), "errors": errors,
            "sample_ms_mean": round(sum(lat) / len(lat) * 1000, 3),
            "sample_ms_max": round(max(lat) * 1000, 3)}


out["watch_10hz"] = with_frames(watch_load)


def find_load(via):
    def run():
        time.sleep(0.3)
        r = c.find("type:Meter", via=via)
        return {"found": [e.get("path") or e["name"] for e in r["results"]],
                "scanned": r["scanned"], "find_ms": r["ms"],
                "gc_get_objects_ms": r.get("gc_get_objects_ms")}
    return run


out["find_graph"] = with_frames(find_load("graph"))
out["find_gc"] = with_frames(find_load("gc"))

# pause: the main loop freezes, audio keeps going
x0 = xruns()
p = c.pause()
it0 = p["iteration"]
levels, loads = [], []
t_end = time.perf_counter() + 2.5
while time.perf_counter() < t_end:
    levels.append(level())
    loads.append(float(val("self.backend._dsp_load")))
    time.sleep(0.25)
s = c.step(10)
st = c.paused()
c.resume()
x1 = xruns()
out["pause"] = {"paused_iteration": it0, "after_step_iteration": st["iteration"],
                "step_advanced": s["advanced"],
                "meter_levels_while_paused": [round(v, 4) for v in levels],
                "dsp_load_samples_while_paused": [round(v, 4) for v in loads],
                "xruns_delta": x1[0] - x0[0], "overloads_delta": x1[1] - x0[1],
                "total_paused_s": c.paused()["total_paused_s"]}
out["after_resume"] = with_frames(None, 150)
c.call("self._on_toggle_audio()")
out["ping"] = {k: c.ping()[k] for k in ("budget", "handles", "errors")}
print(json.dumps(out, indent=1))
c.close()
