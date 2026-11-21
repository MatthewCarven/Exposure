# M4 results: shell, eval/exec, asyncio, safety

21 Nov 2026. Windows 11, Python 3.14.4. All 85 tests pass on Windows. The
coordinator runs every push on Linux (Python 3.14.6, xvfb).

## What M4 adds

| Command | What it does |
|---|---|
| `shell PID` | Interactive session on one connection (below) |
| `eval PID 'EXPR'` / `exec PID 'CODE'` | Run any Python in the target. Asks first unless `--yes` (or `confirm = false` in the user config). `--frame NAME\|@N` runs in a live frame; otherwise the session's scratch namespace. `--no-capture`, `--on main\|agent\|loop\|thread:NAME`, `--timeout` |
| `call ... --no-capture` | Calls capture stdout/stderr too, by default |
| `--on loop` | Run on whichever thread is running an asyncio event loop |

### eval / exec

- **Confirmation** is asked by the client each time: `--yes` or `confirm off`
  in the shell skips it. The agent independently refuses an unconfirmed
  request.
- **Where it's allowed:** refused under `--read-only`, and outside the
  reachable scope unless the policy has `[safety] eval = true`.
- **Namespaces:**
  - The default **scratch** namespace persists for the session. It can also
    read the entry frame's locals and globals, and `_` holds the last result.
  - **`--frame main`** (or `App.run`, or a frame handle) reads and writes a
    live frame's locals through PEP 667 on that frame's own thread.
  - `@N` in the source stands for handle N.
- **Tracebacks** carry real line numbers from a `<exposure-eval-N>` filename
  that is registered with `linecache`, so the source lines show up.

### Output capture

- While attached, `sys.stdout`/`sys.stderr` are routed through a ContextVar.
  Only output written by the thread or asyncio task running an Exposure job is
  captured (64 KiB cap). The app's own output from other threads is
  untouched, and the test checks that.
- Detach puts the original streams back.

### asyncio

- A running event loop is detected from its `run_forever` frame. Jobs reach it
  with `call_soon_threadsafe` (executor mode `asyncio`).
- If a call or eval returns a coroutine, it is awaited: as a task when the job
  ran on the loop, or via `run_coroutine_threadsafe` from any other executor.
  The job finishes when it completes, with the usual timeout and job semantics.

### Safety

- `[safety] deny = ["os.system", "subprocess.*", ...]` (in `exposure.toml`,
  or `[safety]` in the user config) refuses matching `call`/`do` targets. The
  match is against both the path typed and the function's `module.qualname`.
- For eval/exec the deny list only produces a **warning**: Python can't be
  sandboxed, so this is a guard rail, not a lock.

### Shell (`exposure shell PID`)

- Every command without retyping the PID, plus Python:
  - `!expr` runs one line of Python;
  - `eval` / `exec` (`exec` alone takes several lines);
  - `py` toggles a mode where every line is Python;
  - `frame NAME` makes Python run in that frame;
  - `scratch [clear]` lists or empties the scratch namespace.
- `confirm on|off` and a short `help`.
- **Tab completion** over command names, paths (asked of the agent, read-only,
  including methods and class attributes), handles and scratch names.
- **Persistent history** per user.
- **Paging** of long output.
- **Panels** (`watch`, `tweak`, `preset`) open inside the shell, and Ctrl+C
  returns to the prompt.
- Completion and history need `readline`. That's there on Linux. On Windows
  install `pyreadline3` on the client side; without it the shell works but
  without Tab or history. The agent stays stdlib-only.

### User config

`%APPDATA%\exposure\config.toml` (or `~/.config/exposure/config.toml`, or
`$EXPOSURE_CONFIG`). Settings:

- `[client] confirm`, `rate`, `history`
- `[safety] deny`, which is sent to the agent

## Bugs found on the way (fixed)

- **Detach / re-attach race.** For up to 0.5 s after `detach`, a new attach
  could reach the dying agent and lose its connection. It showed up on
  PySynthRack and as a flaky Linux test (6 of 15 runs). The fix:
  - the agent now drops its session file and closes its socket the moment it
    answers `detach`;
  - a late hello gets "detaching", and the client injects a fresh agent;
  - a bootstrap that arrives during shutdown waits for it to finish;
  - an old agent never deletes a newer session's files.

  This is the coordinator's patch merged with mine. A regression test loops
  attach/detach.
- **Stale agent code.** A long-running program kept the first session's
  `exposure.agent` in `sys.modules`. After updating Exposure, PySynthRack
  answered "unknown command 'complete'".
  - The bootstrap now drops cached `exposure.*` modules (and the package's
    attributes for them) before importing.
  - It keeps `exposure` and `exposure.registry`, so a program's `expose()`
    registrations survive.
- **A failed `sys.remote_exec`** (for example on PySynthRack's 3.12 `.exe`)
  left its boot file behind. It's now removed, because nothing was queued in
  the target. Leftovers clear with `exposure sessions --clean`.
- **`--frame App.run`** found the function `App.run` rather than its running
  frame. A running function's name now wins.

## PySynthRack check (your running copy, pid 5764, audio on as you left it)

This check only read and printed: no param or patch changes, and audio was
left running as it was (amp 0.1, gain 0.8). I attached with `--entry App.run`
and the xrun pressure probe.

- **Completion** of `self.patch.m` gave `self.patch.modules`,
  `self.backend._x` gave `self.backend._xruns`, and `self._set_m` gave
  `self._set_module_param`.
- **Scripted shell session** (transcript in `spike/m4_psr_check.py`'s
  output). Each command ran on the main loop:
  - `ls self.patch.modules`
  - `!self.backend.sample_rate` → 44100
  - `!print('hello from', <thread name>)` → captured output "hello from
    MainThread"
  - `find type:Oscillator` → `self.patch.modules[1]` (783 objects, 7.5 ms)
  - `frame App.run`, then `!type(self).__name__, self.backend.name` →
    ('App', 'numpy', 512), checked again after the frame-name fix

**Frame time and xruns:**

| Scenario | Frame mean | median | p99 | xruns Δ | overloads Δ |
|---|---|---|---|---|---|
| Idle, audio on | 34.5 ms | 3.0 | 77.4 | 0 | 0 |
| 200 evals on the main loop, each printing (all 200 outputs captured) | 35.0 ms | 2.6 | 77.4 | **0** | 0 |

Each eval blocked the frame for 0.046 ms on average (p99 0.20 ms). The
cadence (~29 fps, alternating ~3 ms and ~77 ms frames) is DearPyGui's own
pacing with the window not in front, the same pattern M0 saw at night. It's
identical with and without the evals, which is the comparison that matters.

## Known gaps

- **Windows has no Tab completion or history** in the shell without
  `pyreadline3`. The shell doesn't try the private `_pyrepl` module.
- **The deny list can't constrain eval/exec**; it only warns. This is by
  design.
- **Output capture covers `sys.stdout`/`sys.stderr` writes in Python.** C-level
  writes to file descriptors 1/2 (from C extensions) aren't captured.
- **`find` and the panels still don't warn while audio is running** (carried
  over from M3).
