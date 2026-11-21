# Exposure: design and plan

*Draft, 20 Nov 2026. Research only, no code yet.*

## 1. What we're building

Exposure is a command-line tool that attaches to a Python 3.14+ process that is **already running**, by PID, without the target having imported anything. Once attached you can:

- **List** what a function such as `main()` is holding: its live frame, locals, the objects those point at, module globals, threads and their stacks.
- **Inspect** any of those objects (attributes, items, type, size, source).
- **Call** functions and methods inside the target and get the result back.
- **Set** variables, attributes and items live.

```
$ exposure attach 41872
attached: python 3.14.1, pid 41872, 3 threads, entry main() found in thread MainThread

exposure> ls main
  frame main()  app.py:88  (MainThread, currently in poll_queue() app.py:41)
    config      Config(debug=False, retries=3)      @1
    queue       deque([...], maxlen=100)  len=12     @2
    workers     list[Worker]  len=4                  @3
exposure> show @3[0]
exposure> set main.config.debug = True
exposure> call @3[0].restart(timeout=5)
-> True   (12.4 ms, ran on exposure-worker thread)
```

## 2. Architecture

Four pieces. The first two are the hard part; the last two are ordinary engineering.

```
 ┌──────────── your terminal ────────────┐        ┌──────────── target process ──────────────┐
 │  CLI / REPL  ──►  Client library      │  TCP   │  Agent (injected)                        │
 │                     │                 │◄──────►│   ├─ RPC server thread                   │
 │                     ▼                 │ token  │   ├─ Handle table (id → object)          │
 │                  Injector ────────────┼────────┼─► │ sys.remote_exec(pid, bootstrap.py)   │
 └───────────────────────────────────────┘        │   └─ Executor (where calls/sets run)     │
                                                   └──────────────────────────────────────────┘
```

### 2.1 Injector (CLI side)

Uses **`sys.remote_exec(pid, script_path)`** from PEP 768, new in 3.14. It writes a request into the target's interpreter state; the target runs the script at its next safe evaluation point. This is the same mechanism `python -m pdb -p PID` uses in 3.14, so it is the supported route, with no ptrace hacks or C code.

The injected bootstrap is tiny. It:
1. Checks the agent isn't already running (an attribute on `sys`, e.g. `sys._exposure_agent`).
2. Adds Exposure's install path to `sys.path` and imports `exposure.agent`.
3. Starts the agent's server thread, writes the port and a fresh token to a per-user session file, and returns immediately.

The CLI then polls for the session file (a couple of seconds) and connects.

**Caveats to design around:**
- **The target must execute Python bytecode for injection to fire.** A process sitting inside a long C call (e.g. `time.sleep(3600)`, a blocking `socket.recv`, `input()`) won't pick up the script until that returns. The CLI should say "waiting for the target to reach a safe point…" with a timeout, rather than hang silently. Signals or I/O usually wake it; we document this.
- **Permissions.** Same OS rules as any debugger. Windows: same user is normally fine, elevated targets need an elevated Exposure. Linux: `ptrace_scope` may need to be 0, or `CAP_SYS_PTRACE`. macOS: root.
- **Opt-out exists.** Targets started with `PYTHON_DISABLE_REMOTE_DEBUG=1`, `-X disable-remote-debug`, or a build `--without-remote-debug` cannot be attached. Exposure detects and reports this clearly.
- **Version match.** The agent is pure Python and runs with the *target's* interpreter, so Exposure must be importable by that interpreter (same Python minor version, and either installed in that environment or loaded from a path we inject). Section 6 has the plan.

**Fallback for blocked or opted-out targets:** a one-line opt-in, `import exposure; exposure.serve()`, that starts the same agent. Same protocol, same CLI. Useful for production-ish processes where remote debug is disabled on purpose.

### 2.2 Agent (inside the target)

Pure Python, standard library only (it must not drag dependencies into someone else's process). Parts:

- **Server thread.** A daemon thread named `exposure-agent` that accepts connections and handles requests. Read-only requests run here directly.
- **Handle table.** Every object we show the client gets a short id (`@1`, `@2`, …). The table holds strong references while a session is attached, so ids stay valid, and drops them all on detach (plus an explicit `release`). Optionally weak references for objects that support them, so we don't keep things alive longer than the user expects; this is a config choice.
- **Executor.** Decides *where* a call or set actually runs (see §4).
- **Clean detach.** Stops the thread, clears the table, removes any `sys.monitoring` hooks, and deletes `sys._exposure_agent`. The target should be in the same state as before, minus anything you deliberately changed.

### 2.3 Transport

- **Now:** TCP on `127.0.0.1`, ephemeral port. A 32-byte random token is written to a session file only the current user can read (`%LOCALAPPDATA%\exposure\sessions\<pid>.json` on Windows, `$XDG_RUNTIME_DIR` or `~/.cache` elsewhere). The client must present the token as its first message or the connection is dropped. We use TCP rather than Unix sockets / named pipes so Windows and Linux share one code path.
- **Protocol:** length-prefixed JSON messages, request/response with ids, plus server-pushed events (watch updates, thread state changes). JSON keeps it debuggable and language-neutral; **never pickle**, since unpickling is code execution in whichever side receives it.
- **Designed for remote later:** the transport sits behind a small interface (`connect()`, `send()`, `recv()`), and auth is a separate step after connect. Adding remote means adding a TLS transport (or "run over SSH tunnel" as the zero-code option) and swapping the token for something stronger, such as mutual TLS or a signed short-lived token from a cloud broker. Nothing above the transport layer changes. The agent binds to localhost unless explicitly configured otherwise, and refuses a non-loopback bind without TLS.

### 2.4 Client library and CLI

- **Client library** (`exposure.client`): a Python API mirroring the protocol, so the CLI is a thin layer and others can script against it (tests, notebooks, a future TUI or VS Code panel).
- **CLI:** one-shot commands (`exposure ls 41872 main`) for scripting, and an interactive REPL (`exposure attach 41872`) with tab completion over names and handles, history, and pretty output. Likely built on `argparse` + the 3.14 stdlib REPL pieces or `prompt_toolkit`, and `rich` for tables. The CLI side can have dependencies; the agent can't.
- `exposure ps` lists Python processes on the machine with version and whether they're attachable.

## 3. What gets exposed: two scopes

You chose: **everything reachable from `main()`, and/or only what is explicitly specified**, so security can be kept tight. Exposure therefore has two scopes, which can be used alone or together:

| Scope | What you can see and touch | Needs code in the target? |
|---|---|---|
| **reachable** | Everything reachable from an entry frame such as `main()` (§3.2) | No |
| **explicit** | Only names and objects on an allowlist | No for a policy file, yes for decorators |
| **both** | Browse everything reachable, but calls and sets only on allowlisted items | Same as explicit |

### 3.1 Explicit scope: how things get specified

Three ways to specify, all compiled into one allowlist the agent enforces:

1. **In code (opt-in import):**
   ```python
   import exposure

   @exposure.expose                          # callable remotely
   def restart_worker(n: int) -> bool: ...

   exposure.expose(config, name="config", writable=["debug", "retries"])
   exposure.expose(queue, name="queue", read_only=True)
   ```
   Decorated functions can carry type hints, and the agent checks remote arguments against them before calling.
2. **Policy file, no import needed:** an `exposure.toml` next to the script (or passed with `--policy`) listing dotted paths and permissions:
   ```toml
   [expose]
   "app.main:config"      = { read = true, write = ["debug", "retries"] }
   "app.workers"          = { read = true, call = ["restart"] }
   "app.restart_worker"   = { call = true }
   ```
3. **At the prompt, for a session:** `pin app.main:config` narrows your own view. Convenient, but it's a view filter, not a security boundary (see below).

Permissions are per item: `read`, `write` (optionally a list of attributes), and `call` (optionally a list of methods). Anything not on the list is invisible in explicit scope: `ls` doesn't show it, and handles to it can't be created, even by drilling down from an allowed object. Return values from allowed calls get handles but inherit read-only unless the policy says otherwise.

### 3.1.1 How tight is "tight"? Being honest about the boundary

There's a catch worth understanding. If an attacker can run `sys.remote_exec` against your process, they can inject any code they like, not just Exposure, so an allowlist enforced by the agent can't stop them. The allowlist protects against **mistakes and against people who can only reach the Exposure port**, not against someone with debugger rights on the machine.

So for genuinely tight security there's a **strict mode**:
- The target starts with remote debugging turned off (`PYTHON_DISABLE_REMOTE_DEBUG=1` or `-X disable-remote-debug`), so nothing can be injected.
- The program calls `exposure.serve(policy="exposure.toml")` itself. The policy is loaded by the target, at startup, and the client cannot change it.
- `eval`/`exec` are disabled entirely; only allowlisted reads, writes and calls work.
- This is also the mode that becomes the basis for remote and cloud sessions later, combined with TLS.

In the normal attach-by-PID mode, the policy file is still enforced and is a good guard rail, but the doc and CLI say clearly that it's a guard rail, not a lock.

### 3.2 Reachable scope: enumerating "what main() is tracking"

**Everything reachable from `main()`'s live frame**, in layers the user can expand on demand.

1. **Find the frame.** `sys._current_frames()` gives the top frame of every thread; walk `f_back` to find frames whose `f_code` matches `main` (by name, or by `module:qualname` when ambiguous). Report every match (recursion, several threads). If `main` isn't on any stack (it already returned, or the program never had one), say so and offer module globals instead.
2. **Layer 0: locals.** `frame.f_locals`. In 3.13+ this is a write-through proxy (PEP 667), which matters for §4.
3. **Layer 1: callee frames.** Frames *below* `main` on the same stack (what it's currently calling), each with its locals. Shown as a stack.
4. **Layer 2: closures and globals.** The function's closure cells and its module's globals.
5. **Layer N: object graph.** Attributes, `__dict__`, `__slots__`, container items, expanded lazily, one level per request, with a depth and count cap. `gc.get_referents` as a fallback for opaque objects.
6. **Extras:** threads (`threading.enumerate()`), asyncio tasks and their coroutine stacks (3.14 ships `asyncio` task introspection we can reuse), loaded modules, and a "find" command that searches by name or type across the reachable set.

### Doing this safely across threads

The target keeps running while we look. Rules:

- **Snapshot, don't hold.** Each request grabs what it needs (`list(d.items())`, `list(frame.f_locals.items())`) and serialises from the copy. Containers mutated by another thread mid-copy raise `RuntimeError`; we retry a few times, then return a partial result marked as such.
- **Never call user code by accident.** `repr()` and property access run arbitrary user code that can block, deadlock, or have side effects. Default previews use a **safe repr**: built-in types formatted by us, user objects shown as `<ClassName @id>` plus their `__dict__` keys, without calling `__repr__`, `__getattr__` or properties. A `--deep`/`show -r` flag opts in to the real `repr()`, run with a timeout (see §4.3).
- **Bounded output.** Truncate long strings, big containers (first N items + length), and deep graphs. The client pages through the rest.
- **Free-threaded builds (3.14t).** Without the GIL, concurrent mutation is genuinely parallel. Snapshot-and-retry still works; we test on both builds in CI.
- **Frames are ephemeral.** A frame handle becomes stale once that call returns. We check `frame.f_back`/liveness and report "frame has exited" instead of showing stale data.

## 4. Remote call and set semantics

### 4.1 Where code runs

There are three places a call or set could execute. They trade safety against fidelity:

| Mode | Runs on | Good for | Risk |
|---|---|---|---|
| **agent** (default) | Exposure's own thread | Calling functions/methods, setting attributes and globals | Races with the program's threads, same as any multi-threaded code |
| **in-thread** | The target thread itself, at its next line | Code that must run on that thread (GUI loops, thread-locals, asyncio loop), or changing a local mid-function | Needs that thread to be executing Python |
| **paused** | Agent thread, while the target thread is parked | Consistent multi-step edits | The program stalls while paused |

**In-thread and pause use `sys.monitoring` (PEP 669).** The agent registers a `LINE` callback scoped to the target thread's current code object. When that thread hits the next line, the callback either runs the queued work there and returns, or blocks on a condition variable until the user resumes. This is low overhead and is removed as soon as it fires. For asyncio targets we offer a fourth path: `loop.call_soon_threadsafe` / `run_coroutine_threadsafe`, so `await`able functions can be called.

### 4.2 Arguments

Arguments are parsed on the client into a small, safe AST and sent as structured JSON, not as source code:
- literals (numbers, strings, lists, dicts, `None`, `True`) via `ast.literal_eval` rules
- handles: `@3`, `@3[0]`, `@3.name`
- names resolved in the target: `main.config`, `app.settings`

An explicit **`eval` / `exec` command** runs arbitrary Python in the target (optionally in a given frame's namespace) for when you really need it. It's the honest version: the tool can already run anything, so we expose it plainly, gated behind a confirmation in "safe mode" (§5).

### 4.3 Results, errors, timeouts

- The return value is stored in the handle table and returned as a preview plus handle, so you can keep drilling in.
- Exceptions come back as type, message and formatted traceback, and the exception object is also a handle.
- Every call has a timeout (default 10 s). Python can't kill a thread, so on timeout the CLI stops waiting and marks the call "still running", and you can check on it later. We say this plainly in the docs.
- `stdout`/`stderr` produced during the call can optionally be captured and returned (redirected only on the executing thread via a context var, so the program's own output isn't swallowed).

### 4.4 Setting variables

| Target | How | Notes |
|---|---|---|
| Global / module attr | `setattr(module, name, v)` | Straightforward |
| Object attribute / item | `setattr` / `__setitem__` | Respects properties and `__slots__` |
| Local in a live frame | `frame.f_locals[name] = v` | 3.13+ proxy writes through to the real variable (PEP 667). Safest done in-thread or paused so the function sees a consistent value |
| Closure variable | `cell.cell_contents = v` | Affects every closure sharing the cell |

Every set records the old value in an **undo log**, so `undo` puts it back (best effort: object identity is restored, side effects aren't). Values can be literals, handles, or `eval` results.

### 4.5 GUI apps and games (the main use case)

You'll mostly point Exposure at GUI apps and games, so it replaces hand-written debug screens. That changes three things.

**1. Calls and sets default to the main thread, not the agent thread.** Tkinter, Qt, pygame/SDL and most game engines expect their objects to be touched only from the thread running the loop. Calling `label.config(text=...)` or `sprite.kill()` from the agent thread can crash the app or corrupt state mid-frame. So the executor auto-detects the loop and posts work onto it:

| Loop | Detected by | How work gets onto the main thread |
|---|---|---|
| **pygame / arcade / custom `while running:` loop** | Main thread is executing Python in a loop | `sys.monitoring` LINE hook on the loop's code object: queued work runs at the top of the next iteration, i.e. *between frames* |
| **Tkinter** | `tkinter` imported, a `Tk` root exists | `root.after(0, fn)` scheduled from inside a Python callback, with the `sys.monitoring` hook as the way in |
| **Qt (PySide6 / PyQt6)** | `QApplication.instance()` exists | `QTimer.singleShot(0, fn)` / a helper `QObject` living in the GUI thread, signalled from the agent thread (Qt's own thread-safe route) |
| **asyncio-based** | Running loop found on a thread | `loop.call_soon_threadsafe` |

Reads still happen on the agent thread (they only copy), so browsing doesn't touch the frame loop at all. You can override per command, e.g. `call --on agent`.

**2. Frame-rate impact is a first-class budget.** With the GIL, the agent thread competes with the game for CPU, and a big `ls` could cause a visible hitch.
- **Chunked work:** the agent processes large listings in small slices and yields between them (`time.sleep(0)`), so the game thread gets the GIL back promptly.
- **Per-frame budget for main-thread work:** queued calls/sets run between frames with a default budget (e.g. 2 ms per frame); anything left waits for the next frame. A single slow call still takes as long as it takes, and Exposure reports how long it blocked the frame.
- **Hooks only while needed:** `sys.monitoring` events are enabled locally on one code object and disabled (`DISABLE`) as soon as the queue is empty, so an idle attached session costs close to nothing.
- **Measure it:** M0/M3 include a benchmark: a pygame loop at 60/144 fps, recording frame-time spikes while attached, idle, browsing and calling. Target: no extra dropped frames when idle or browsing.
- Free-threaded 3.14t removes most of the contention, and we'll support it, but most game libraries aren't free-threaded-ready yet.

**3. Game-friendly commands** (the "debug screen" replacements):
- `watch player.pos player.hp enemies.__len__` shows a live-updating panel in the terminal, sampled at a chosen rate (e.g. 10 Hz), read on the agent thread.
- `tweak player.speed` lets you nudge a number with arrow keys or type values; each change is applied between frames and logged for undo.
- `pause` / `step` / `resume` on the main loop: freeze the game between frames, inspect everything consistently, advance one frame at a time.
- `find type:Enemy` finds all live instances of a class (via `gc.get_objects()`, chunked so it doesn't stall the game).
- `call --every 1s spawn_enemy()` for repeated actions, with a stop command.
- Saved **presets**: a named group of watches and tweaks per project (`exposure.toml`), so your "debug screen" is a file rather than code inside the game.

**Injection caveat specific to GUIs.** Tkinter's `mainloop()` and Qt's `exec()` spend their idle time in C. `sys.remote_exec` only fires when Python bytecode runs, so a *completely idle* Tk or Qt app may not pick up the agent until an event triggers a Python callback (a timer, mouse movement over a bound widget, a redraw). Games with a Python `while` loop don't have this problem. M0 will test idle Tk and Qt apps on Windows specifically; if this bites, the fallbacks are "wiggle the mouse over the window" messaging, or the one-line `exposure.serve()` opt-in, which also enables strict mode.

### 4.6 First target: PySynthRack

I read the PySynthRack code (read-only) to fit the design to it. What matters:

**Blocker: its venv is Python 3.12.13.** `sys.remote_exec` only exists in 3.14+, so Exposure can't attach to it as it stands. The good news is the dependencies mostly allow 3.14: DearPyGui 2.3.1, numpy, scipy and sounddevice all publish 3.14 wheels on Windows (checked on PyPI today). The exception is **python-rtmidi 1.5.8, which has no 3.14 wheel**, so the `[midi]` extra would need a C build toolchain or would be missing on 3.14. (There are `cpython-314.pyc` files in its `__pycache__`, so it's been run under 3.14 at some point.) Suggested route: a second venv, `.venv314`, for Exposure work, leaving the 3.12 venv alone. Until then, the `exposure.serve()` opt-in can't help either, because the agent targets 3.14.

**Its threads:**

| Thread | What runs there | What Exposure must do |
|---|---|---|
| Main (GUI) | `App.run()`: a **Python `while dpg.is_dearpygui_running():` loop**, ~13 `_update_*` calls, then `render_dearpygui_frame()`, paced to vsync | Ideal: injection fires within one frame, and the top of this loop is where calls and sets run (§4.5) |
| PortAudio audio callback | `NumpyBackend._audio_callback` / `_duplex_callback` → `_fill_output`, holds `backend._lock` while reading the patch | **Never block it.** No taking `_lock` from Exposure, no long GIL holds |
| mido MIDI input | `MIDIInput._on_message` on mido's I/O thread | Read-only browsing is fine |
| Helpers | `FileDecode-*`, `SampleLoad`, `IRLoad`, disk writer | Browsable; leave alone |

**The audio thread is the strictest budget, stricter than frame rate.** The callback must produce a block every buffer period (a few ms at small buffer sizes). With the GIL, any long stretch of Exposure work on the agent thread can delay the callback and cause an audible dropout. So for this app:
- Agent reads are sliced much finer (well under 1 ms per slice) and yield between slices.
- `find type:X` (a full `gc.get_objects()` scan) is warned about while audio is running.
- The success metric for M0 is **no extra xruns**: PySynthRack already counts stream status flags via `_note_stream_status`, so Exposure can watch that while attached, along with frame time.

**Where things live, and the right way to change them:**
- `App()` is created inline (`App().run()`), so there's no global to find it by. The entry frame is **`App.run`**, and `self` in that frame is the app. So `exposure attach <pid> --entry pysynthrack.ui.app:App.run` gives you `self.patch` (the model), `self.backend` (the `NumpyBackend`), modules, widgets, everything.
- **Param edits must go through `App._set_module_param(module_id, name, value)`** on the GUI thread. It writes the model first, then tells the backend, as `docs/architecture.md` requires. Setting `module.params[...]` directly would skip the backend, and calling `backend.set_param` directly would skip the model. Exposure should offer this as a named action, e.g. `param 12 freq 220`, rather than a raw `set`.
- Structural changes (add/remove modules or cables) need `backend.compile(patch)`, also on the GUI thread, and may briefly restart the stream.
- PySynthRack's own `error_handler` hooks `threading.excepthook` and `sys.unraisablehook`. The agent must catch everything itself, or a hiccup in Exposure would show up as a PySynthRack crash report.
- To check in M0: whether DearPyGui runs widget callbacks on the main thread or its own worker thread. PySynthRack doesn't call `configure_app(manual_callback_management=...)`, so it uses DPG's default.

**A starter policy for it** (explicit scope, §3.1), giving a safe "debug screen" without writing UI code:

```toml
entry = "pysynthrack.ui.app:App.run"

[expose]
"self.patch.modules"      = { read = true }
"self.patch.cables"       = { read = true }
"self.backend"            = { read = true }
"self._set_module_param"  = { call = true }   # the only way params change
"self._on_toggle_audio"   = { call = true }

[presets.levels]
watch = ["self.backend.sample_rate", "self._dsp_load"]
```

(Names are illustrative; M1 will pin the real attribute names.)

## 5. Safety and security

The honest framing: **attaching Exposure gives you arbitrary code execution in the target**, exactly like a debugger. Security therefore means controlling *who* can attach, and *safety* means not breaking the target by accident.

**Security**
- OS permissions are the first gate: you can only `remote_exec` into processes your user can already debug.
- Agent binds `127.0.0.1` only; random port; per-session token in a user-only file; token compared in constant time; token is single-session and deleted on detach.
- No pickle, no `eval` of client-supplied strings except via the explicit `eval` command.
- An audit log in the target (optional, off by default) records every call and set, and a `sys.audit` event is raised for each, so programs with audit hooks can see or block Exposure.
- Remote/cloud later: TLS required, no plaintext non-loopback binds, stronger auth (§2.3).

**Safety**
- Read-only by default: `attach --read-only` refuses calls and sets entirely; useful when poking at something you care about.
- Safe repr by default (no user code runs just from listing things).
- Timeouts on everything; hard caps on output size and graph depth.
- Confirm before `eval`/`exec` and before calls on objects flagged as dangerous (configurable allow/deny list by module or name, e.g. deny `os.*`, `subprocess.*` in safe mode).
- Clean detach leaves no threads, hooks or monitoring callbacks behind. If the CLI dies, the agent notices the dropped connection and tidies up after a grace period.
- The agent catches *everything* in its own thread; a bug in Exposure must never raise into the target's threads.

## 6. Packaging

- One package, `exposure`, with `exposure.agent` (stdlib only), `exposure.client`, `exposure.cli`. Install with `pipx install exposure` for the CLI.
- The injected bootstrap prepends Exposure's own install directory to the target's `sys.path` before importing the agent, so the target doesn't need Exposure in its environment. This works because the agent is pure Python and version-checked; if the target's Python minor version differs from the CLI's, the bootstrap still works as long as the agent code supports that version (we test 3.14 and 3.15 when it lands).

## 7. Milestones

Each milestone ends in something usable.

1. **M0: spike (1-2 days), on PySynthRack.** Set up a 3.14 venv for PySynthRack (without the MIDI extra if rtmidi won't build). Prove `sys.remote_exec` on Windows: inject into the running synth, start the agent thread, open a socket, connect. Then, with audio playing, measure frame time and xruns (via `_note_stream_status`) while idle, while browsing `self.patch`, and while calling `_set_module_param` between frames. Check which thread DPG runs callbacks on. Also try a sleeping process and an idle Tkinter app to map the "blocked in C" case. This de-risks everything else.
2. **M1: read-only attach.** Includes the explicit scope (policy file + `exposure.expose`) from the start, so the allowlist shapes the agent's design rather than being bolted on. Injector, bootstrap, agent server, token auth, JSON protocol, client library. CLI commands: `ps`, `attach`, `threads`, `stack`, `ls <frame|func>`, `show @id`. Safe repr, handle table, clean detach.
3. **M2: call and set on the main loop.** (Moved up because your targets are GUIs and games.) `call`, `set` for globals/attributes/items/locals, executed between frames via the `sys.monitoring` executor with loop detection for pygame-style loops, Tkinter and Qt; per-frame budget; results as handles, exceptions with tracebacks, timeouts, undo log, `--read-only`.
4. **M3: game tools.** `watch` live panel, `tweak`, `pause`/`step`/`resume` the main loop, `find type:X`, repeated calls, presets in `exposure.toml`, frame-time benchmark in CI.
5. **M4: REPL polish.** Interactive shell with completion, history, output paging, `eval`/`exec` with confirmations, asyncio calls, config file with allow/deny lists.
6. **M5: hardening.** Free-threaded build testing, stress tests (attach/detach 1000×, attach under heavy thread churn), audit hooks, docs, the `exposure.serve()` opt-in fallback, PyPI release.
7. **Later: remote.** TLS transport, SSH-tunnel recipe, broker-based auth for cloud sessions, maybe a TUI or editor panel on top of the client library.

### M0 results (21 Nov 2026)

Done on PySynthRack, full write-up in `Exposure\spike\M0-results.md` on your computer. The approach works:
- Attach in 47-77 ms; reads of threads, patch modules and xrun counters from the agent thread; queued calls run on the main loop about one frame later via the `sys.monitoring` hook; audio started/stopped and gain changed remotely; clean detach and re-attach.

Design changes it forces:
- **The agent must be rate-limited by design.** A tight read loop (no locks) starved the audio callback of the GIL: 162 xruns in 20 s. With a 5 ms pause between reads: 0 xruns. M1 adds a built-in rate limit that backs off automatically when the target's xrun count climbs.
- **DearPyGui runs widget callbacks on its own native thread**, not MainThread, so the "GUI thread" is really two threads. Consider `dpg.set_frame_callback` for work that must run alongside UI callbacks.
- **Param edits should go through `App._mirror_param`**, which also moves the widget; `_set_module_param` alone leaves the slider where it was.
- **Windows venv launcher:** `.venv\Scripts\python.exe` spawns the real interpreter as a child, so the client follows to the child PID.
- **Blocked targets confirmed:** a process in `time.sleep(60)` never ran the script; an idle Tk window only ran it when its own `after()` fired.
- Small fixes: bootstrap as a stub that loads the agent from a real file (proper tracebacks), clean up stale session files.

### M1 results (21 Nov 2026)

Done. Full write-up in `docs/M1-results.md`.
- **`exposure` package and CLI**: `ps`, `attach`, `ping`, `threads`, `stack`,
  `ls`, `show`, `scope`, `release`, `detach`, `sessions`.
- **Core pieces**: handle table, safe inspection (no target code runs), and the
  reachable / explicit / both scopes from `exposure.toml` and
  `exposure.expose()`. Token auth and a bootstrap stub with an expiry.
- **Tests**: 34, passing on Windows and Linux.
- **PySynthRack with audio on**: browsing caused 0 xruns and left the frame
  time unchanged, even with commands sent back to back and the duty cap off.
  The 2 ms slices with GIL-releasing sleeps are what protect the audio. The
  xrun pressure back-off triggers when dropouts happen for other reasons.
- **Carried to M2**: handle-table growth (weak refs or a cap), and an idle
  timeout for abandoned sessions.

### M2 results (21 Nov 2026)

Done. Full write-up in `docs/M2-results.md`.
- **Commands**: `call`, `set`, `do` (policy `[actions]`), `undo`, `history`,
  `jobs`, `result`, `executors`, `frames`.
- **Main executor**: found PySynthRack's `App.run` loop by itself, using a
  `sys.monitoring` JUMP hook on the loop's back-edge rather than a fixed line.
  Tkinter (`after` from the agent thread) and Qt (a queued signal to a helper
  QObject) work on Windows and Linux. Anything else falls back to the next
  line.
- **Per job**: a 2 ms per-frame budget, the time it blocked the thread, and
  timeouts that leave the job running behind a job id. Results and exceptions
  become handles.
- **Permissions**: write/call enforced in the explicit and both scopes,
  `--read-only`, and `sys.audit` events. The handle table is LRU-capped, and
  an idle timeout tidies up abandoned sessions.
- **PySynthRack with audio on**: 300 `_mirror_param` calls on the main loop
  and 300 agent-executor calls caused 0 xruns. Frame p99 was 8.2 ms against
  7.9 ms idle. A Meter module confirmed the level change: 0.300 → 0.100.
- **Found in PySynthRack, not fixed**: `_clear_editor` leaves stale
  `_param_widgets` entries, so after loading a patch `_mirror_param` updates a
  deleted widget and the slider doesn't move.

### M3 results (21 Nov 2026)

Done. Full write-up in `docs/M3-results.md`.
- **Commands**: `pause`/`step`/`resume`/`paused`, `watch`, `tweak`,
  `preset`/`presets`, `nudge`, `find type:X|name:pattern [--via gc]`, and
  `every`/`repeats`/`stop`.
- **Pause** is a queued job that holds the main thread between iterations.
  Calls and sets still run while paused, and `step N` re-pauses after exactly
  N iterations. It ends on `--for`, detach or the idle timeout, but not on
  client disconnect, since one-shot CLI calls disconnect every time.
- **Panel**: one terminal panel for watch, tweak and presets, with changed
  values highlighted and arrow-key nudges that are undoable.
- **PySynthRack with audio on**: 0 xruns during a 10-value watch at 10 Hz, a
  graph and a gc `find`, and a 2.8 s pause with `step 10`. The audio callback
  kept rendering while the main loop was paused.
- **Tests**: 68, passing on Windows and Linux (xvfb).

### M4 results (21 Nov 2026)

Done. Full write-up in `docs/M4-results.md`.
- **`exposure shell`**: a cmd-based REPL with `!python`, `py` mode, frame
  selection, completion via the agent, history and paging.
- **`eval`/`exec`**: client confirmation, plus an agent-side `confirmed`
  flag. A scratch namespace or a live frame (PEP 667), `<exposure-eval-N>`
  tracebacks, `_`, and `@N` handles.
- **Output capture** through a ContextVar router on stdout/stderr, restored on
  detach.
- **asyncio**: an executor mode for a running loop, and awaiting coroutine
  results.
- **`[safety] deny`** for calls (a warning only for eval), and a user
  `config.toml`.
- **Two real bugs found live and fixed**: the detach/re-attach race (merged
  with the coordinator's patch), and stale agent code surviving a re-attach.
- **PySynthRack with audio on**: 200 captured evals on the main loop caused 0
  xruns and left the frame time unchanged.

## 8. Risks

- **Blocked targets** (stuck in C) won't accept injection until they wake. Mitigation: clear messaging; the opt-in `serve()` path; document that most event-loop programs wake often enough.
- **Accidental side effects from repr/properties.** Mitigation: safe repr default.
- **Timed-out calls can't be cancelled.** Mitigation: be explicit, track them, warn before calling known-blocking things.
- **Audio dropouts in real-time apps** like PySynthRack. Mitigation: very fine-grained agent work, no locks shared with the audio callback, xrun count as a test metric.
- **Idle GUI apps may not accept injection** until an event runs Python code (§4.5). Mitigation: tested in M0; `serve()` fallback.
- **Frame hitches while attached.** Mitigation: chunked reads, per-frame budget, hooks only while needed, benchmark in CI.
- **PEP 768 is new.** Edge cases on Windows and in virtualenvs may surface. M0 exists to find them early.

## 9. Open questions for you

All answered (Matthew accepted every recommendation, 21 Nov 2026).

1. ~~What does "tracked by main()" mean?~~ **Answered:** everything reachable from main, and/or an explicit allowlist for tight security. Now in §3. **Strict mode: answered.** A one-line `exposure.serve(policy=...)` opt-in in the target is fine.
2. ~~How much should Exposure be allowed to do by default?~~ **Answered:** full power by default (calls and sets, as M2 ships). Confirmation only on `eval`/`exec` when that lands in M4. `--read-only` is the opt-in for careful sessions.
3. ~~Which platforms matter first?~~ **Answered:** Windows first, Linux right behind (both tested from M1 on), macOS later.
4. ~~What kind of programs?~~ **Answered:** GUI apps and games. Now in §4.5, and main-loop execution moved up to M2. First target is PySynthRack (DearPyGui + sounddevice), now in §4.6. **Answered:** yes to a separate Python 3.14 venv (`.venv314`) for PySynthRack, MIDI input optional until python-rtmidi ships a 3.14 wheel.
5. ~~CLI style?~~ **Answered:** both one-shot commands (shipping now) and an interactive REPL (M4).
6. ~~Is the name final, and PyPI?~~ **Answered:** keep the name Exposure. `exposure` is already taken on PyPI, so if it's ever published, use a distribution name such as `exposure-debugger` (free as of 21 Nov 2026), with the import name still `exposure`. Decide on publishing at M5.
