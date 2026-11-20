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

Early, and read-only so far.

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

`reachable` shows everything reachable from the entry frame. `explicit` shows
only the listed items and what they hold. `both` browses everything but marks
the listed items with their permissions. M1 enforces `read`; `write` and
`call` take effect from M2.

## The M0 spike

The spike in [`spike/`](spike/) also has the commands that M2 will turn into
proper features: running code on the target's main loop (`eval_main`),
frame-time measurement, and PySynthRack-specific audio and param commands.
Run `python spike/client.py <pid> <command> [key=value ...]`; see
[`spike/M0-results.md`](spike/M0-results.md).

On Windows, a venv's `Scripts\python.exe` is a launcher that re-spawns the real
interpreter as a child process, and Exposure follows it to the child. A target
that isn't running Python bytecode (blocked in a long `time.sleep`, or an idle
Tk window with no callbacks) won't run the agent until it next does. The
client says so instead of hanging.

## Roadmap

- **M1**: read-only attach with an allowlist scope. Done.
- **M2**: function calls and variable sets on the main loop.
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
