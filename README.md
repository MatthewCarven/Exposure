# Exposure

Everything you wish you had in a remote debugger, for a Python program that is
already running.

Exposure attaches to a live Python 3.14+ process by its PID. It needs no import,
no flag and no code change in the target: it uses `sys.remote_exec`
([PEP 768](https://peps.python.org/pep-0768/)) to start a small agent inside it.
From the command line you can then:

- list the functions, objects and variables reachable from an entry point such
  as `main()` (or a running `App.run()`), including what each thread is doing;
- read values safely while the program keeps running;
- call functions and edit variables live, with calls that touch GUI or model
  state run on the program's own main loop between frames.

## Status

Early: reading, calling and setting work; the game tools and the REPL come
next.

- **M0 spike, done.** It proved the approach on PySynthRack, a modular
  synthesiser built on DearPyGui and sounddevice: attach in about 50-80 ms, a
  `sys.monitoring` main-loop executor, and a clean detach. It also showed that
  an agent which never yields starves an audio callback of the GIL. Details:
  [`spike/M0-results.md`](spike/M0-results.md).
- **M1 read-only attach, done.** The `exposure` package and CLI are below.
  Browsing PySynthRack with audio playing, even with commands sent back to back
  (35,000 in 11 s), caused 0 audio dropouts and left the frame time unchanged.
  Details: [`docs/M1-results.md`](docs/M1-results.md). The design is in
  [`docs/design.md`](docs/design.md).
- **M2 calls and sets, done.** Calls and sets run on the program's own main
  loop between frames. Exposure finds Python game loops, Tkinter and Qt by
  itself. M2 also adds undo, background jobs and allowlist enforcement. On
  PySynthRack, 300 parameter changes made through `call` caused 0 dropouts,
  and a level meter confirmed the change in the audio itself. Details:
  [`docs/M2-results.md`](docs/M2-results.md).

## Requirements

- Python 3.14 or newer, for both the client and the target.
- Tested on Windows 11 and Linux. `ps` doesn't support macOS yet.
- The client must run as the same user as the target, with the usual
  permission to debug that process (on Linux, check `ptrace_scope`).
- No dependencies. The agent uses only the standard library, so nothing is
  added to the target's environment.

## Using it

Run it from a checkout with `python -m exposure`, or `pip install .` to get
the `exposure` command. Any command that needs the agent injects it first.

```
exposure ps                                   # Python processes, attachable?
exposure ls 41872                             # entry frame (main) and what it holds
exposure ls 41872 config                      # drill in by name...
exposure ls 41872 @3.items[0]                 # ...or by handle
exposure show 41872 table["db"]
exposure threads 41872
exposure stack 41872 worker-1
exposure detach 41872
```

### Changing things (M2)

```
exposure call 41872 'player.heal(5, cap=200)'     # runs on the main loop, between frames
exposure call 41872 'save_game()' --on agent      # or on a worker thread
exposure set 41872 player.speed 2.5
exposure set 41872 'settings["volume"] = 0.8'
exposure set 41872 mode '"turbo"'                 # a local in the running main()
exposure undo 41872                               # put the last change back
exposure history 41872
exposure jobs 41872                               # calls that outlived --timeout
exposure frames 41872 300                         # main-loop frame-time stats
```

Arguments and values can be literals, names, handles (`@3`), and `.attr` /
`[key]` steps on them. Strings need their quotes, which in a shell means
wrapping them: `'"turbo"'`. Arbitrary expressions arrive with `eval` in M4.

Calls and sets go to the main thread by default, the place GUI and game code
expects to be touched. Exposure works out how to get there:

- a Python game loop: between iterations, through a `sys.monitoring` hook on
  the loop's back-edge;
- Tkinter: `after`;
- Qt: a queued signal.

Each job reports how long it blocked the frame, and queued work gets 2 ms per
frame (`--frame-budget`). A call that runs past `--timeout` keeps going in the
background, because Python can't stop a thread: check it with `jobs` and
`result`.

`--read-only` refuses every call and set. Each call and set also raises a
`sys.audit` event (`exposure.call`, `exposure.set`) that the program's own
audit hooks can see or block.

The entry frame is `main` by default. Pass `--entry App.run` (or
`module:qualname`) for programs whose state lives elsewhere. Every non-trivial
value shown gets a handle (`@3`) that later commands can use until you
`release` it or detach.

Inspection never runs the target's own code. That means no `__repr__`, no
properties, and no subclass overrides of container methods. Properties show
as "not evaluated".

### Rate limiting

The agent works in 2 ms slices with GIL-releasing pauses between them, and
caps itself at 25% of wall time (`--duty`). For real-time programs, point it
at a counter that rises when the program is struggling, and it backs off
further:

```
exposure ls 41872 --entry App.run --pressure "<entry>:self.backend._xruns"
```

### Explicit scope

What may be looked at can be listed in an `exposure.toml`, found next to the
target's main script or in its working directory, or passed with `--policy`:

```toml
[agent]
scope = "explicit"            # reachable | explicit | both
entry = "App.run"

[expose]
"app.main:config"     = { read = true, write = ["debug"] }
"<entry>:self.patch"  = { read = true }
```

Or it can be declared in code, if the program imports `exposure`:

```python
import exposure
exposure.expose(config, name="config", writable=["debug"])
```

`reachable` shows everything reachable from the entry frame and allows any
call or set. `explicit` shows only the listed items and what they hold, and
`both` browses everything. In those two, calls and sets are allowed only where
the policy grants `write` (true, or a list of attribute names) or `call` (true,
or a list of methods).

The policy can also name actions, so a common change becomes one short
command. [`examples/pysynthrack.toml`](examples/pysynthrack.toml) is a full
example:

```toml
[actions]
param = "self._mirror_param"       # exposure do PID param 1 '"amp"' 0.1
audio = "self._on_toggle_audio"    # exposure do PID audio
```

## The M0 spike

The spike in [`spike/`](spike/) is the M0 prototype, kept for reference along
with the scripts that drove the M1 and M2 PySynthRack checks. See
[`spike/M0-results.md`](spike/M0-results.md).

On Windows, a venv's `Scripts\python.exe` is a launcher that re-spawns the real
interpreter as a child process, and Exposure follows it to the child. A target
that isn't running Python bytecode (blocked in a long `time.sleep`, or an idle
Tk window with no callbacks) won't run the agent until it next does. The
client says so instead of hanging.

## Roadmap

- **M1**: read-only attach with an allowlist scope. Done.
- **M2**: function calls and variable sets on the main loop. Done.
- **M3**: tools for games and GUIs: watch, tweak, pause and step.
- **M4**: interactive REPL.
- **M5**: hardening.
- **Later**: remote attach over TLS.

## Security

Attaching is arbitrary code execution in the target process. Only attach to
processes you own and trust.

- The agent listens on `127.0.0.1` only, on an ephemeral port.
- Every connection must present a random per-session token. The token is
  written to a session file in the user's local app-data folder.
- The explicit scope is a guard rail against mistakes, not a lock. Anyone who
  can run `sys.remote_exec` against your process can inject any code they like.
- A stricter mode is planned for a later milestone: the target turns remote
  debugging off and opts in with `exposure.serve(policy=...)`.

## Licence

MIT. See [LICENSE](LICENSE).
