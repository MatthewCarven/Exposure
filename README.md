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

Early. The **M0 spike** has proven the approach against a real application:
PySynthRack, a modular synthesiser built on DearPyGui and sounddevice. Headline
results (full numbers in [`spike/M0-results.md`](spike/M0-results.md)):

- **Attach in about 50-80 ms** from `sys.remote_exec` until the agent answers.
- **Main-loop executor**: a `sys.monitoring` LINE hook at the top of the
  target's render loop runs queued calls on the main thread, about one frame
  after they are queued, at sub-millisecond cost. It switches itself off when
  idle.
- **Clean detach**: hook removed, tool id freed, agent thread stopped, and
  re-attach works.
- **The agent must be rate-limited.** A tight read loop on the agent thread
  starved the audio callback of the GIL and caused dropouts (162 xruns in
  20 s). The same reads with a 5 ms pause between them caused none.

## Requirements

- Python 3.14 or newer, for both the client and the target.
- Tested on Windows 11. The mechanism is cross-platform, but other platforms are
  untested so far.
- The client must run as the same user as the target, with the usual
  permission to debug that process.

## Trying the spike

The spike lives in [`spike/`](spike/). It is stdlib-only and talks to a target
that is already running:

```
python spike/client.py <pid> <command> [key=value ...]
```

The first command injects `spike/bootstrap.py`. Later commands reuse the running
agent. Values in `key=value` are parsed as JSON where they can be. Some useful
commands:

| Command | What it does |
|---|---|
| `ping` | Interpreter version and agent uptime |
| `threads` | Every thread with its top frame |
| `eval expr="..."` | Evaluate on the agent thread (reads) |
| `eval_main expr="..."` | Evaluate on the target's main thread, between frames |
| `frametime n=300` | Frame-time stats for the main loop |
| `detach` | Remove all hooks and stop the agent |

Commands that find the app object automatically (`modules`, `backend`,
`toggle_audio`, `set_param`, `callback_thread`) are specific to PySynthRack.
They show what an app-aware layer looks like.

On Windows, a venv's `Scripts\python.exe` is a launcher that re-spawns the real
interpreter as a child process. The client detects this and targets the child.
A target that is not running Python bytecode (blocked in a long `time.sleep`,
or an idle Tk window with no callbacks) won't run the agent until it next does.
Set `EXPOSURE_WAIT` (seconds) to wait longer.

## Roadmap

- **M1**: read-only attach with an allowlist scope (what may be looked at).
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
- A stricter mode, where the target opts in and limits what may be touched, is
  planned for a later milestone.

## Licence

MIT. See [LICENSE](LICENSE).
