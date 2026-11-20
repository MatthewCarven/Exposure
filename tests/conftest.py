import os
import queue
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = os.path.join(ROOT, "tests", "targets")
sys.path.insert(0, ROOT)

needs_314 = pytest.mark.skipif(not hasattr(sys, "remote_exec"),
                               reason="sys.remote_exec needs Python 3.14+")


def target_python():
    # A Windows venv's python.exe is a launcher that re-spawns the base
    # interpreter; start the base one so the pid we hold is the real one.
    return getattr(sys, "_base_executable", None) or sys.executable


@pytest.fixture(scope="session", autouse=True)
def private_session_dir(tmp_path_factory):
    """Keep session files for test runs out of the user's real folder."""
    d = str(tmp_path_factory.mktemp("sessions-home"))
    old = {k: os.environ.get(k) for k in ("LOCALAPPDATA", "XDG_RUNTIME_DIR")}
    os.environ["LOCALAPPDATA"] = d
    os.environ["XDG_RUNTIME_DIR"] = d
    yield d
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class Target:
    def __init__(self, *args, cwd=None):
        self.proc = subprocess.Popen(
            [target_python(), os.path.join(TARGETS, "app_main.py"), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.output = []
        while True:
            try:
                line = self.lines.get(timeout=20)
            except queue.Empty:
                self.stop()
                raise RuntimeError("target never printed 'ready'")
            if line is None:
                raise RuntimeError("target exited early: " + "".join(self.output))
            self.output.append(line)
            if line.strip() == "ready":
                break
        self.pid = self.proc.pid

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)


@pytest.fixture
def make_target():
    made = []

    def make(*args, **kw):
        t = Target(*args, **kw)
        made.append(t)
        return t

    yield make
    for t in made:
        t.stop()
