"""M4 check against a running PySynthRack: shell, completion, eval, capture.

    python m4_psr_check.py <pid>

Reads and prints only: no param or patch changes. Audio is left as it is.
Prints JSON plus the scripted shell transcript.
"""
import io
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exposure.client import Client  # noqa: E402
from exposure.repl import Shell  # noqa: E402

pid = int(sys.argv[1])
OPTS = dict(entry="App.run", pressure="<entry>:self.backend._xruns")
c = Client.attach(pid, **OPTS)
out = {"attach": c.attach_info}


def val(path):
    v = c.sample([path])["values"][0]
    return v.get("preview", v.get("error"))


def xruns():
    return int(val("self.backend._xruns")), int(val("self.backend._dsp_overloads"))


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


out["audio_running"] = val("self.backend._running")
out["completion"] = {"self.patch.m": c.complete("self.patch.m"),
                     "self.backend._x": c.complete("self.backend._x"),
                     "self._set_m": c.complete("self._set_m")}
script = "\n".join([
    "ls self.patch.modules",
    "!self.backend.sample_rate",
    "!print('hello from', __import__('threading').current_thread().name)",
    "find type:Oscillator",
    "frame App.run",
    "!type(self).__name__, self.backend.name",
    "frame",
    "quit", ""])
buf = io.StringIO()
Shell(c, stdin=io.StringIO(script), stdout=buf, confirm=False, page=False).cmdloop()
out["shell_transcript"] = buf.getvalue().splitlines()
out["idle"] = with_frames(None)


def eval_load():
    blocked, outputs = [], 0
    with Client.attach(pid, **OPTS) as ce:
        for i in range(200):
            r = ce.eval(f"print({i}, self.backend._xruns); self.backend._dsp_load",
                        confirmed=True)
            assert r["state"] == "done", r
            blocked.append(r["blocked_ms"])
            outputs += bool(r.get("output"))
    blocked.sort()
    return {"evals": 200, "captured_outputs": outputs,
            "job_blocked_ms": {"mean": round(sum(blocked) / len(blocked), 4),
                               "p99": blocked[int(len(blocked) * 0.99)], "max": blocked[-1]}}


out["evals_on_main_200"] = with_frames(eval_load)
out["ping"] = {k: c.ping()[k] for k in ("budget", "handles", "errors")}
c.detach()
print(json.dumps(out, indent=1))
