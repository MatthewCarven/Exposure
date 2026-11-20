"""Drive the M1 agent with browse traffic against PySynthRack for N seconds.

    python m1_browse_load.py <pid> <seconds> [hammer|paced] [pressure]

hammer: commands back to back (the M0 failure case, now paced by the agent's
budget). paced: one command every 100 ms, like a person browsing.
Prints JSON with command count, latency stats and the agent's budget stats.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exposure.client import Client  # noqa: E402

pid, seconds = int(sys.argv[1]), float(sys.argv[2])
mode = sys.argv[3] if len(sys.argv) > 3 else "hammer"
pressure = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None
duty = float(sys.argv[5]) if len(sys.argv) > 5 else None

c = Client.attach(pid, entry="App.run", pressure=pressure, duty=duty)
cmds = [("ls", {}), ("ls", {"target": "self.patch.modules"}),
        ("ls", {"target": "self.patch.modules[1]"}),
        ("ls", {"target": "self.patch.modules[1].params"}),
        ("ls", {"target": "self.backend"}), ("ls", {"target": "self"}),
        ("threads", {}), ("stack", {})]
lat = []
n = 0
end = time.perf_counter() + seconds
while time.perf_counter() < end:
    cmd, args = cmds[n % len(cmds)]
    t0 = time.perf_counter()
    c.request(cmd, **args)
    lat.append(time.perf_counter() - t0)
    n += 1
    if mode == "paced":
        time.sleep(0.1)
ping = c.ping()
lat.sort()
print(json.dumps({"mode": mode, "commands": n, "attach": c.attach_info,
                  "lat_ms": {"mean": round(sum(lat) / len(lat) * 1000, 2),
                             "p50": round(lat[len(lat) // 2] * 1000, 2),
                             "p99": round(lat[int(len(lat) * 0.99)] * 1000, 2),
                             "max": round(lat[-1] * 1000, 2)},
                  "budget": ping["budget"], "handles": ping["handles"]}, indent=1))
c.close()
