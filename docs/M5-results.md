# M5 results: hardening

21 Nov 2026. Windows 11, Python 3.14.4 and free-threaded 3.14.4t. CI on GitHub
Actions runs windows-latest (3.14) and ubuntu-24.04 (3.14 and 3.14t) on every
push.

## What M5 adds

### Strict mode: `exposure.serve()`

```python
import exposure
exposure.expose(config, name="config", writable=["debug"])   # optional
exposure.serve(policy="exposure.toml")                        # strict by default
```

- **The agent runs in-process**, so it works when remote debugging is off
  (`PYTHON_DISABLE_REMOTE_DEBUG=1` or `-X disable-remote-debug`). The test
  checks that `sys.remote_exec` really is refused for that target, and that the
  CLI still connects through the session file.
- **The policy is loaded at startup.** A client's attach options are ignored:
  the client gets the usual "already running with different options" warning.
- **`eval`/`exec` are off.**
- **Scope** comes from the policy (default `explicit`). `reachable` is raised
  to `both`, so changes stay inside the allowlist.
- **It works with `exposure.expose()`**: registered items and policy items are
  both roots, with their write/call permissions enforced.
- **A client's `detach` ends its session** (releases handles, resumes a pause,
  stops repeats), but the agent keeps serving. There is no idle timeout.
- `ping` reports `strict`/`served`, and the `ls` header shows `STRICT`.

### Audit

- **New `sys.audit` events**: `exposure.attach` and `exposure.detach`, on top
  of the existing `exposure.call`, `exposure.set` and `exposure.eval`. An audit
  hook that raises blocks the action cleanly: the client sees the refusal, the
  function never runs, and the target carries on. This is tested.
- **Optional JSON-lines audit log** (`[agent] audit_log = "path"` or
  `serve(audit_log=...)`). It records one line per call, set, nudge, undo, do,
  eval, pause, step, resume, every, stop, attach and detach, with time, pid,
  target, outcome and old/new values where there are some.

### Free-threaded Python (3.14t)

- **The whole suite passes on 3.14t** with both client and targets
  free-threaded, the GIL really off: 95 passed, and the Qt test skips because
  PySide6 has no 3.14t wheels. It passes locally on Windows and in the
  coordinator's Linux runs.
- **Race audit:** an agent state lock now guards the things the GIL used to
  protect by accident:
  - the connection counter and set;
  - the request, error, change, repeat and eval counters;
  - the undo log;
  - the capture table;
  - installing the stdout/stderr routers;
  - creating executors and the pauser. Two clients could otherwise each
    create a main-loop hook.

  These were already safe:
  - the handle table and job list (their own locks);
  - each executor's queue (its own lock);
  - the Pauser's condition variable;
  - the `sys.monitoring` callbacks (they touch only the executor's locked
    queue and per-thread state).
- **The rate limiter still makes sense without a GIL.** The duty cap and slices
  limit the agent's share of CPU, and the pressure back-off still reacts to
  xruns. They just matter less, because the agent no longer competes for a
  global lock.
- **Mixed builds aren't supported by CPython.** `sys.remote_exec` from a
  regular build into a free-threaded process (or the reverse) fails with
  "Cannot send commands to a free-threaded Python process from a process
  running a non-free-threaded version".
  - Exposure now detects the target's build (`ps` shows `3.14t`) and refuses
    up front with that explanation.
  - **The way round it:** a 3.14t client injects, and from then on any client
    can connect over the session. I checked this: a regular 3.14 client
    called, evaluated and sampled frames on a 3.14t target after a 3.14t
    client had injected.

### Robustness tests (in the default suite)

- **The target exits mid-call:** the client gets a `ConnectionLost` error
  saying the process has exited (exit code 3 from the CLI), not a raw socket
  error.
- **A client killed mid-request:** the job finishes, the agent tidies up after
  the idle timeout (session file gone), and a fresh session works.
- **Several clients at once:** five connections running calls, reads,
  threads and stacks together for 3 s, with no errors.
- **Re-attach straight after detach** (from M4), in a loop.

### Stress tests (marker `stress`, off by default)

Run them with `python -m pytest -m stress`. Set `EXPOSURE_STRESS_N` to change
the cycle count (default 1000).

| Test | Result |
|---|---|
| **1000 attach/detach cycles** on one target, a main-loop call every 10th cycle | **50.6 s (50.6 ms per cycle)**. Afterwards: one agent thread, no stray connection or call threads, all six `sys.monitoring` tool ids free, `sys.stdout`/`stderr` the original `TextIOWrapper`s again, no leftover session or boot files. Target handle count went from 108 to 110. |
| **Thread churn**: a target starting and joining 8 threads at a time, non-stop | 33 attach / browse (threads, stack, ls, `find type:Thread`) / detach cycles in 20 s, with no errors and the target alive |

The first 1000-cycle runs found two real bugs, both fixed:

- **The session file outlived the detach reply.** It's now removed before the
  reply is sent.
- **Cleanup could close the replying connection.** Setting the stop flag let
  the accept loop clean up while the detach reply was still being sent. The
  replying connection now takes itself off the cleanup list first.

### Packaging

- **Distribution name `exposure-debugger`** (`exposure` is taken on PyPI);
  import name `exposure`, console script `exposure`.
- `requires-python >= 3.14`, version 0.1.0, MIT, with classifiers (including
  free threading).
- **Extras:** `shell` installs pyreadline3 on Windows, `dev` installs pytest.
- **`uv build`** makes an sdist and a wheel, and **`twine check`** passes on
  both.
- **Clean-install smoke test:** the wheel installed (with `[shell]`) into a
  fresh venv outside the repo. `exposure ps`, `ls`, `call` and `eval` against a
  test target all worked from the installed copy. The agent was imported from
  that venv's site-packages, which confirms the bootstrap's `sys.path`
  handling.
- **Not uploaded anywhere.** Matthew chose "GitHub install only for now".
  The README's install section uses `pipx`/`uv tool install` from git.

### CI

`.github/workflows/tests.yml` runs on every push and pull request:

- windows-latest with 3.14, with PySide6;
- ubuntu-24.04 with 3.14, under Xvfb with the Qt system libraries and PySide6;
- ubuntu-24.04 with 3.14t, with no PySide6 wheels, so Qt skips.

Ubuntu is pinned to 24.04 rather than `ubuntu-latest`.

The first run (6c5b7c8) was green on Windows. On Linux one test failed: a
target that had just exited was still a zombie, not yet reaped by its parent,
and `os.kill(pid, 0)` still succeeds on a zombie. `is_alive` now reads
`/proc/<pid>/stat` and treats state `Z`/`X` as gone.

## Bugs found and fixed in M5

1. **Detach reply ordering:** two races, found by the 1000-cycle stress run
   (above).
2. **Linux zombies counted as alive** (found by CI).
3. **Cross-build injection** gave an opaque error; it's now refused up front
   with an explanation.

## Known gaps

- **There's no grace period separate from the idle timeout.** An abandoned
  session is tidied up when the idle timeout passes (default 30 min; 1 s in
  the test).
- **macOS is untested**, and `ps` doesn't support it.
- **`find` and the panels still don't warn while audio is running.**
- **Not on PyPI.** That's deliberate for now.
