# M2 results: calls and sets on the main loop

21 Nov 2026. Windows 11, Python 3.14.4. All 55 tests pass on Windows. The
coordinator's Linux run (Python 3.14.6, under xvfb with PySide6) passed all 54
tests at commit 4192688; the concurrency test came later.

## What M2 adds

**Commands:**

| Command | What it does |
|---|---|
| `call PID 'path.to.fn(args, kw=...)'` | Call a function or method |
| `set PID TARGET VALUE`, or `set PID 'TARGET = VALUE'` | Set a variable, attribute or item |
| `do PID ACTION [VALUES...]` | Run a named action from `[actions]` in `exposure.toml` |
| `undo PID [N]` | Revert the last N sets |
| `history PID` | List the changes made this session |
| `jobs PID` | Recent calls and sets |
| `result PID JOB` | The result of a job |
| `executors PID` | How work reaches each thread |
| `frames PID [N]` | Main-loop iteration time stats |

**New attach options:** `--read-only`, `--idle-timeout`, `--frame-budget`,
`--max-handles`.

**New options on call, set and do:** `--on main|agent|thread:NAME`,
`--timeout`.

### Where work runs (`exposure/executor.py`)

On the main thread, the strategy is picked when work is first queued:

| Mode | When | How |
|---|---|---|
| `qt` | A `QApplication` exists | A helper QObject moved to the GUI thread is signalled from the agent thread with a queued connection. |
| `tk` | `tkinter` has a default root | `root.after(0, ...)` is called from the agent thread, and threaded Tcl marshals it onto Tk's thread. |
| `loop` | The main thread is in a Python loop | A `sys.monitoring` JUMP event local to the loop's code object fires on the loop's back-edge (found from `f_lasti` and the code's `JUMP_BACKWARD`s), so work runs between iterations. Every other jump returns DISABLE. |
| `line` | Anything else | LINE events on the thread's stack. Work runs at the next line. |

- Hooks are on only while work or a frame sample is pending.
- The main-loop budget is 2 ms per iteration: what doesn't fit waits for the
  next one. Every job reports how long it blocked the thread.
- `--on agent` runs the job on a fresh worker thread. `--on thread:NAME` runs
  it at that thread's next line.
- A local in a live frame is always set on the thread that owns it.

### Arguments and values

These are parsed on the client into literals (`ast.literal_eval` rules),
names, handles (`@3`), and `.attr` / `[key]` steps on them. Literals travel as
source text and the agent literal_evals them. Nothing is `eval`'ed, which is
left for M4.

### Sets

| Kind | How |
|---|---|
| Attributes | `setattr`, so property setters and `__slots__` behave normally |
| Items | `container[key] = value` |
| Globals | Written into the module's dict |
| Closure cells | Reached as `fn.varname` |
| Locals in a live frame | Written through PEP 667 `f_locals`, on the owning thread |

Every set records the old value. `undo` restores it, or deletes the name if
it didn't exist before. This is best effort: a setter that transforms its
value (such as `name` → uppercase) runs again on undo.

### Results and errors

- A result becomes a handle with a safe preview.
- An exception comes back as its type and message, a traceback trimmed of
  Exposure's own frames, and the exception object as a handle.
- A call that outlives `--timeout` (default 10 s) reports "still running" with
  a job id. Python can't stop a thread, so it finishes on its own; check it
  with `jobs` or `result`.

### Permissions

- In the `explicit` and `both` scopes, a set needs a `write` permission on the
  parent (true, or a list of attribute names), and a call needs `call` on the
  method or the parent's call list.
- `both` lets you browse everything but change only allowlisted items.
- `--read-only` refuses every call and set.
- Each call and set raises a `sys.audit` event (`exposure.call`,
  `exposure.set`), so a program's audit hook can see or block it.

### M1 leftovers, now done

- The handle table is LRU-capped (default 10,000) and evicts the least
  recently used 10% when full.
- The agent detaches itself after 30 minutes with no client (`--idle-timeout`,
  0 = never).
- The agent now serves each connection on its own thread, so a long `frames`
  sample or a slow call doesn't hold up another client.

### PySynthRack example policy

`examples/pysynthrack.toml` sets the entry `App.run`, `both` scope and the
xrun pressure probe. It exposes `_mirror_param` and `_on_toggle_audio` for
calls, with two actions:

- `param 1 '"amp"' 0.1`
- `audio`

## PySynthRack check

I loaded a temporary patch (in my scratchpad, not in the PySynthRack folder):
oscillator 1 → Meter 3 → speaker 2, speaker gain 0.05. Everything was driven
through `exposure` calls on the main executor:

- `self._load_patch_from(path)`
- `self._on_toggle_audio()` to start audio
- 300 × `self._mirror_param(1, "amp", …)`, ramping the amp from 0.3 to 0.1
- 300 × `self.backend.snapshot_audio_levels()` on the agent executor
- `self._on_toggle_audio()` to stop audio

The frame time came from M2's own `frames` command on a second connection
running at the same time. The main executor found PySynthRack's
`while dpg.is_dearpygui_running()` loop in `App.run` automatically.

| Scenario | Frame mean | p99 | max | fps | xruns Δ | DSP overloads Δ | Per-job main-thread block |
|---|---|---|---|---|---|---|---|
| Audio on, idle | 6.94 ms | 7.88 | 9.1 | 144.2 | 0 | 0 | – |
| 300 `_mirror_param` calls on the main executor (4.3 s) | 6.96 | 8.25 | 15.0 | 143.7 | **0** | 0 | mean 0.021 ms, p99 0.074, max 0.21 |
| 300 calls on the agent executor (2.0 s) | 6.94 | 7.62 | 7.8 | 144.2 | **0** | 0 | mean 0.004 ms |

- **The audio level follows the call.** The Meter read **0.300** at amp 0.3
  and **0.100** after the ramp to 0.1. The model held the new value too, so
  the model, the backend and the measured audio all agree. This closes the M0
  loose end. Audio played for about 10 s in total.
- **Attach** took 38 ms. `_load_patch_from` blocked the frame for 4.4 ms, and
  starting audio blocked it for 445 ms (compile plus opening the MME stream),
  the same as pressing the button.
- PySynthRack's stderr stayed empty. Detach released 354 handles and left no
  session files, and the app closed normally.

### A PySynthRack bug found on the way (not fixed: PySynthRack is read-only for this work)

The widget did **not** follow `_mirror_param` after a patch load. The cause:

- `App._clear_editor()` deletes the editor's DearPyGui items but doesn't clear
  `App._param_widgets`.
- After loading a second patch, the old patch's widget ids are still in the
  map.
- `_param_widget(1, "amp")` returns the first match, the dead id 211, so
  `_mirror_param`'s `dpg.set_value` goes to a deleted item. Its
  `try/except` swallows the error.
- The live slider (id 244) kept showing 0.3 while the model held 0.12.

The normal File > Open path (`_load_patch_from`, line ~6606) is affected
whenever the new patch reuses module ids, which is common (both patches here
have an oscillator with id 1). Stale ids are pruned only lazily, in the
mouse-wheel hover scan.

Likely fix: clear `_param_widgets` in `_clear_editor()`, or drop a module's
entries when its node is deleted.

### DearPyGui threads

As M0 found, DearPyGui runs widget callbacks on its own native thread (shown
as `Dummy-1`), not on MainThread. Exposure's main executor runs on MainThread
between frames, which serialises with PySynthRack's render loop but not with
those UI callbacks. PySynthRack's own callbacks already race its render loop
in exactly the same way, so this adds no new class of race.

## Known gaps, carried forward

- **No `eval`/`exec`** (M4), and no asyncio executor
  (`loop.call_soon_threadsafe`) yet.
- **No stdout/stderr capture** during calls.
- **Tk and Qt need some Python running** for injection to fire (a timer, or
  any callback). Once attached, the executors work while the app is idle.
- **Undo is best effort.** It can't reverse side effects, and a property
  setter runs again on undo.
- **I didn't try `examples/pysynthrack.toml` against a live PySynthRack.** The
  same mechanics (actions, call lists, both scope) are covered by
  `test_policy_actions_and_both_scope`.
- **Handles and history entries hold strong references** until release,
  eviction or detach.
