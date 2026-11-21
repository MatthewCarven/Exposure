# M6 results: polish and bells

21 Nov 2026. Windows 11, Python 3.14.4: 115 tests pass. The coordinator runs
every push on Linux, with Python 3.14 and free-threaded 3.14t, and CI runs on
GitHub Actions.

## 1. Small polish (13b9e94)

| Command | What it does |
|---|---|
| `?thing`, `help thing`, `sig thing` (shell); `exposure doc` | Signature, first paragraph of the docstring, and where it's defined. An instance is described by its class. |
| `src thing` / `exposure src` | Numbered source, paged in the shell |
| `ls PATH GLOB` (e.g. `ls self *widget*`); a lone glob filters the roots | Filters names, then pages the result |

- **Watch and tweak panels:** a sparkline plus min/max for each numeric value.
  There's an ASCII fallback when the output can't encode block characters.
- **Shell:**
  - Tab completes `do` actions and `preset` names.
  - Python lines no longer print `-> None`, though captured output still
    shows.
  - `find --via gc` on a target with a pressure probe (real-time audio) gets
    a one-time warning and a prompt first.
- **Dropped clients:** a client now says "bye" when it closes.
  - **No bye** (a killed shell or crashed script): the agent waits a short
    grace period (default 60 s, `--drop-grace`). If nobody reconnects, it
    tidies up and detaches: it resumes a pause, stops repeats and releases
    handles.
  - **A clean disconnect** keeps everything (handles, undo history, a pause,
    repeats) until the 30-minute idle timeout, as before. That's what makes
    one-shot commands useful across a session.

## 2. Logpoints, traces and breakpoints (cdcd524)

All three use one `sys.monitoring` tool with **local** events on only the
code objects involved (`exposure/probes.py`), so the rest of the program runs
uninstrumented.

| Command | What it does |
|---|---|
| `log FILE:LINE EXPR [--when C] [--every N] [--rate HZ] [--for S]` | Evaluate EXPR in the running frame every time the line runs, without stopping anything |
| `trace FUNC` | Every call: arguments, return value or exception, duration, thread |
| `break FILE:LINE [--when C]` | Pause the main loop at the next frame boundary after the line runs (pausing mid-frame isn't safe) |
| `break --when C` (in the shell: `break when C`) | Check once per frame on the main loop and pause when it's true. One-shot unless `--keep` |
| `logs` | Stream the output (`--for`, `--count`, `--all`) |
| `probes`, `unlog` / `untrace` / `unbreak` | List or remove them |

In the shell, `log` and `trace` stream their output until Ctrl+C. The
browser panel has a log pane.

**Safety:**
- **Rate limit:** each probe has a token bucket, default 20 lines/s. Hits over
  the limit are counted, not evaluated.
- **Errors:** an expression that raises is reported once, and the probe
  disables itself. Nothing raises into the target.
- **Gating:** logpoints and conditions run code, so they get eval's gate:
  confirmation, and refusal under `--read-only`, in strict mode, and outside
  the reachable scope unless `[safety] eval = true`.
- **Cleanup:** detach removes everything and frees the tool id (tested).

**Cost:** a logpoint and a trace on the hot line of a 230 fps game loop left
the frame time unchanged (4.47 ms mean / 4.62 ms p99, with and without).

**A CPython limit:** a call Exposure itself runs on the main-loop executor
executes inside that executor's own monitoring callback, and CPython reports
no monitoring events inside a callback. So probes see the program's own
calls, not ones made with `exposure call` on the main loop (`--on agent`
calls are seen). `PY_UNWIND` can't be a local event, so it's switched on
globally only while a trace is active, and ignored for untraced code.

## 3. snap / diff (d751341)

- **`snap NAME PATH [--depth N]`** records a subtree with safe reads only:
  - scalars and text by their preview;
  - containers by type and size, with their items recorded as children;
  - other objects by type.

  Depth (default 4) and size (5000 values) are bounded. Shared objects are
  noted once, and modules, classes, functions and frames aren't entered.
- **`diff NAME [OTHER]`** compares against another snapshot or the same
  subtree now: changed (old → new), added, removed.
- **`snaps`** lists them.

## 4. Hot reload (e44fdfb)

**`reload MODULE`** (`exposure/reloader.py`) re-runs the module's current
source into a fresh namespace, then rebinds in place:

- Existing functions get the new `__code__` and defaults. So `from x import
  f`, bound methods and existing instances all use the new code from their
  next call, which `importlib.reload` alone doesn't give you.
- Class methods are rebound the same way, including static and class methods
  and property accessors.
- New functions, methods and classes are added, with the module's own globals.
- Module-level values are kept, so state survives. `--values` replaces them.
- A function whose closure variables changed is skipped and reported.
- Calls already running finish on the old code.
- `__main__` works: its `if __name__ == "__main__"` block doesn't run again.
- **`reload --undo`** restores the old code and removes what was added.
- It runs between frames, with eval's gate and a confirmation.

**Bug found while building it, fixed everywhere:** `compile()` inherits the
calling module's `__future__` flags. The agent's own
`from __future__ import annotations` was leaking into code it compiled for the
target (eval/exec, logpoints, breakpoint conditions and reload), which turned
the target's annotations into strings. Every such `compile()` now passes
`dont_inherit=True`, and a regression test checks annotations stay real
objects.

## 5. Browser debug panel (1ff956a)

`exposure panel PID [--preset NAME] [--port N] [--no-open]` starts a local web
page for one target. A client-side stdlib HTTP server holds one agent
connection, so nothing new runs in the target. The page is a single
self-contained file (`exposure/panel.html`, no CDN, light and dark) that polls
at up to 10 Hz. It shows:

- live values with sparkline graphs;
- sliders and number boxes for tweakable values;
- buttons for policy actions, with an argument box;
- pause / step / resume;
- a "break when" box;
- a log pane for logpoints, traces and breakpoints.

Presets gain:

- `range = { path = [lo, hi] }` for tweak sliders;
- `[[presets.NAME.sliders]]` entries that read one path and, when moved, call
  an action with arguments plus the value (or set a path).

`examples/pysynthrack.toml` has a `panel` preset: Osc 1 amp and Output gain
sliders through `_mirror_param` (so the synth's own sliders follow), plus
engine readouts:

    exposure panel <pid> --policy examples/pysynthrack.toml --preset panel

**Security**, because any web page in the browser can try local ports:

- 127.0.0.1 only, and a random token required on every request (in the URL
  for the page, a header for the API);
- the Host header must be 127.0.0.1 or localhost on our port, which defeats
  DNS rebinding;
- changes are POST-only, with `Content-Type: application/json` (a plain HTML
  form can't send that) and a matching Origin if one is sent;
- no CORS headers.

I checked it by hand in a browser against a game-loop test target: pause, a
typed slider value, an armed break condition firing, and the log pane. HTTP
tests cover the token, Host check, content type, origin, and
set/slider/do/pause/step.

## Other fixes on the way

- **Non-Python processes:** attaching to one (e.g. a shell's PID on Windows,
  where there's no `python3XX.dll`) now says so, instead of PEP 768's
  "PyRuntime address lookup failed".
- **Stale pause output:** the panel's frame counter, and `paused` printing
  "iteration None", are fixed.

## Known gaps

- **Probes don't see calls Exposure runs on the main loop** (CPython limit,
  above).
- **Logpoints can't target module-level code that has already run.** Only
  functions can be probed.
- **The panel polls rather than streams.** That's fine at 10 Hz on localhost.
- **The panel's fps readout** shows only when the main-loop hook is running.
- **Hot reload re-runs the module's top-level statements**, as
  `importlib.reload` does. Side effects there (opening files, starting
  threads) happen again.
