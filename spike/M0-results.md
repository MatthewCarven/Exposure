# Exposure M0 spike — results

2026-11-21, laptop (Windows 11), Python 3.14.4 (uv), target PySynthRack (PSR) from
`.venv314`, run with `PYTHONPATH=src`. Spike code: `spike/bootstrap.py` (the agent),
`spike/client.py` (injector and one-shot command client), `spike/targets/` (test targets).

**Verdict: the approach works.** `sys.remote_exec` attaches in about 50–80 ms. The agent
reads PSR's threads, patch and backend counters from its own thread. It runs code on PSR's
main thread between frames via a `sys.monitoring` LINE hook, and it detaches cleanly. Over
the whole session PSR's error hooks never fired (its stderr stayed empty). One real hazard
showed up: a busy agent thread starves the audio callback of the GIL (see Failures and
surprises, item 1).

## Setup

- `.venv314` created with `uv venv --python 3.14` (uv picked 3.14.4). Installed: numpy 2.5.5,
  scipy 1.18.1, sounddevice 0.5.6, dearpygui 2.3.1, mido 1.3.3, **python-rtmidi 1.5.8 (built
  from source without trouble)**. requirements.txt/pyproject list nothing else at runtime.
- `python -m pysynthrack` from `.venv314\Scripts\python.exe` with `PYTHONPATH=src`: the GUI
  opened and hello_sine auto-loaded. The backend picked was numpy (pyo isn't installed),
  sample rate 44100, block 512, host API MME.
- The PSR tree is unchanged (`git status` shows only what was already there). The 3.14 `.pyc`
  files under `src/**/__pycache__` date from August; none are newer than `.venv314`.

## Numbers

### Inject latency (from `remote_exec` until the agent connects)

| Target | remote_exec returns | session file seen | connected |
|---|---|---|---|
| PSR, first inject | 1.9 ms | 71 ms | **77 ms** |
| PSR, re-injects after detach | 1.0–1.1 ms | 43–55 ms | **47–59 ms** |
| `for _ in range(60): time.sleep(1)` | 0.3 ms | 1006 ms | **1011 ms** (next bytecode after the current 1 s sleep) |
| idle tkinter window (only an `after(60 s, destroy)`) | 0.5 ms | 57.8 s | **57.8 s**: fired only when that `after` callback ran Python |
| `python -c "import time; time.sleep(60)"` | 0.3 ms | never | **never**: no bytecode ran until the sleep ended, then the process exited |

`remote_exec` only queues the script and returns at once with no acknowledgement. The script
runs at the next eval-breaker check, so a target that runs no Python bytecode (blocked in C:
`sleep`, Tcl's event wait) won't run it.

### Frame time at the top of PSR's render loop (300 frames)

Measured as `perf_counter` stamps at the `while dpg.is_dearpygui_running()` line, taken by the
same LINE hook. xrun = `NumpyBackend._xruns` (output underflows counted in
`_note_stream_status`), overloads = `_dsp_overloads` (blocks rendered slower than real time).

| Scenario | mean | median | p95 | p99 | max | fps | xruns Δ | overloads Δ |
|---|---|---|---|---|---|---|---|---|
| Audio off, idle | 43.2 | 60.3 | 76.7 | 415.7 | 451.5 | 23.2 | 0 | 0 |
| Audio on, idle | 40.9 | 60.5 | 76.9 | 77.5 | 421.5 | 24.5 | 0 | 0 |
| Audio on, agent lists modules **in a tight loop** (2.39 M listings, 8 µs each) | **66.1** | 63.0 | **139.8** | **280.7** | 346.8 | **15.1** | **+162** | **+286** |
| Audio on, agent lists modules **every 5 ms** (2207 listings, 24 µs each) | 39.2 | 60.6 | 77.3 | 78.6 | 436.0 | 25.5 | 0 | 0 |
| Audio on, `_set_module_param` on the main thread (301 calls, gain ramp 0.03→0.01) | 42.5 | 60.3 | 76.7 | 404.9 | 433.0 | 23.5 | 0 | 0 |

All times are in ms. The cadence is odd but the same in every scenario. Frames alternate
between ~0.3 ms and ~60–77 ms (histogram: 150 frames under 10 ms, 145 at 60–80 ms), plus 4–5
stalls of ~400 ms per 300 frames. That is DearPyGui's own present pacing on this machine at
night, with the window probably not in front. It is not the hook: the hook fires on one line
only and costs microseconds. So read the scenario differences, not the absolute fps. Baseline
xruns were 0 in every scenario except the tight loop.

### Main-thread executor

- Queue → runs at the next loop top: **wait 29–66 ms** (about one frame on this cadence;
  4 ms when the hook was already on).
- `_set_module_param` itself: **0.015–0.02 ms** on the main thread.
- `_on_toggle_audio` (signature `() -> None`), start path: **440 ms** on the main thread
  (`set_block_size` + `compile` + MME stream open). That freezes one frame, the same as
  pressing the button.
- The hook hit 302 times for a 300-frame run. Each other line of `App.run` fired once and
  returned `DISABLE`, so only line 497 stays instrumented. When the queue is empty and no
  timing is running, the local events are switched off. It used tool id 0, the first free one.

## What worked

- **Bootstrap**: guarded by `sys._exposure_agent`. Starts a daemon thread named
  `exposure-agent` serving TCP on 127.0.0.1 (ephemeral port) and writes
  `%LOCALAPPDATA%\exposure\sessions\<pid>.json` (`pid, port, token, started`) atomically.
  The protocol is length-prefixed JSON, with the token check first (`compare_digest`).
  Every exception is caught, in the server, in each command and in the monitoring callback.
- **threads**: from `sys._current_frames()` plus `threading.enumerate()`. PSR at rest has
  MainThread (top frame `dearpygui.py:9484 render_dearpygui_frame`) and the agent.
- **modules**: located the `App.run` frame by `co_qualname == "App.run"` and
  `f_globals["__name__"] == "pysynthrack.ui.app"`, then took `self` from `f_locals`. Listed
  `patch.modules` as id, TYPE, name and params, using `reprlib` with a retry on
  "dict changed size". Cost: 8–24 µs.
- **backend**: plain attribute reads, no lock: `is_running`, `_xruns`, `_input_xruns`,
  `_host_api`, `_dsp_load`, `_dsp_load_peak`, `_dsp_overloads`, `block_size`. I never touched
  `backend._lock`.
- **Remote audio start/stop** via the executor running `App._on_toggle_audio`. The button
  label and status bar update because it runs the real handler.
- **Param edit through `App._set_module_param`**: the model and the backend both took it. Read
  back: model 0.01, backend patch 0.01, and `backend._patch is app.patch` is **True**, so the
  renderer reads the same module objects. A set made **before** Start (gain 0.03) was kept
  and used at compile time, which confirms the model-first path. **The widget did not follow**:
  the gain slider still showed 0.8, because `_set_module_param` doesn't touch widgets.
  `App._mirror_param(module_id, name, value)` does both. I did not verify the change by ear or
  by measurement: hello_sine has no Meter module, so there's no output level to read without
  a structural change, and it was the middle of the night.
- **Detach**: switches the events off, unregisters the callback, frees the tool id, closes
  the socket, deletes the session file and `del sys._exposure_agent`. Checked by
  re-injecting: the new agent saw exactly one `exposure-agent` thread (itself),
  `sys.monitoring.get_tool(0)` was None, and the session folder was empty after the final
  detach. Re-inject after detach works every time. PSR then closed normally (WM_CLOSE), with
  empty stdout/stderr.

## Failures and surprises

1. **A busy agent thread causes xruns.** A tight Python loop on the agent thread (reads only,
   no locks) meant 162 output underflows and 286 DSP overloads in 20 s, and the frame rate
   fell by about 40%. The PortAudio callback needs the GIL to render. The block budget is
   11.6 ms (512 @ 44.1 kHz), and the switch interval is 5 ms. With a 5 ms sleep between
   listings the same work cost nothing. "Never take `backend._lock`" isn't enough on its own:
   **the agent must also stay idle most of the time.** A param ramp on the main thread caused
   no xruns.
2. **DearPyGui runs callbacks on its own native thread, not MainThread.** A
   `dpg.set_frame_callback` probe ran on a non-Python-created thread (shown as `Dummy-1`,
   ident 5344), while the render loop is MainThread (19328). So PSR's widget callbacks run
   **concurrently** with the render loop's `_update_*` calls. The design's "GUI thread" is in
   fact two threads. The thread is invisible to `threading.enumerate()` and
   `_current_frames()` until it has run Python. (An item-visible handler bound to a temporary
   text item never fired in 2 s; I didn't chase why.)
3. **Windows venv launcher.** `.venv\Scripts\python.exe` is a launcher that re-spawns the
   base interpreter as a child, so the PID you get from `Start-Process` is the wrong one to
   inject. client.py now follows a single `python*` child (Toolhelp snapshot, ctypes).
4. **Agent frames show `<string>`** as their filename (`<string>:387 ...cmd_threads`).
   `remote_exec` compiles the file as a string, so agent tracebacks don't map to bootstrap.py.
5. **Stale session files.** When a target exits with the agent still attached, the daemon
   thread dies before cleaning up, and `<pid>.json` stays behind (two were left; I deleted
   them).
6. **Idle targets never run the script** (see the latency table). The `time.sleep(60)` target
   never connected. The tk target connected only when its own `after()` callback ran.
7. `remote_exec` gives no acknowledgement or error if the script never runs. The client only
   learns it from the timeout.

## Recommendations for M1

1. **Throttle the agent by design.** Rate-limit polling (≤ ~100–200 Hz), split large walks
   with `time.sleep(0)` / short sleeps, and put a self-limiter in the agent's read loop
   (a duty-cycle cap). Add an xrun/overload watch to the agent so it backs off on its own
   if `_xruns` climbs while it's busy.
2. **Keep the `sys.monitoring` loop-top executor** as the generic "run on main thread"
   mechanism: about one frame of latency, sub-ms cost, removable. Make the target code
   object and line configurable for apps other than PSR. For DearPyGui apps, consider
   `dpg.set_frame_callback` as a second executor that runs on the **same thread as UI
   callbacks**. That gives true serialisation with UI edits, but it is DPG-only and replaces
   any frame callback set for that frame.
3. **Param edits**: offer both `_set_module_param` (model + backend) and `_mirror_param`
   (also moves the widget). Default to mirror, so the GUI never shows stale values.
4. **Bootstrap as a stub** that loads the real agent with
   `exec(compile(src, real_path, "exec"))` or by importing it from a path. That gives real
   filenames in tracebacks and lets the agent be a proper module.
5. **Client robustness**: keep the venv-launcher following. Treat a session file as stale if
   the PID is dead or the connect fails, and add `atexit` cleanup in the agent. Use a clear
   timeout message that explains "the target is not running Python bytecode".
6. **Document the thread model** (MainThread render loop, DPG callback thread, PortAudio
   callback thread, agent) in the design doc. Treat model writes from the agent as racing
   UI callbacks, exactly as PSR's own callbacks already race the render loop.
7. A **persistent connection** (REPL mode) instead of one connection per command. The
   per-command connect is cheap, but a session will want state (cached app, hook).
8. For audio verification in later tests, add a Meter module to a test patch (structural:
   `backend.compile` on the main thread via the executor) so level changes can be measured
   rather than assumed.

## How to rerun

```powershell
# launch the target (from its folder); with a venv the real interpreter is the launcher's child
$env:PYTHONPATH="src"; .venv314\Scripts\python.exe -m pysynthrack
# from Exposure\spike, with any Python 3.14+:
& "<path to a Python 3.14>\python.exe" client.py <pid> threads
```

Commands: `ping`, `threads`, `modules`, `backend`, `hook_info`, `eval expr=...` (agent thread),
`eval_main expr=...` (main thread), `toggle_audio`, `set_param module_id=2 name=gain value=0.03`,
`frametime n=300 [during=list interval=0.005 | during=param module_id=2 name=gain start=0.03 end=0.01]`,
`callback_thread`, `detach`. Set `EXPOSURE_WAIT` (seconds) to wait longer for slow targets.
