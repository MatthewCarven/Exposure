# M1 results: read-only attach

21 Nov 2026. Windows 11, Python 3.14.4. The suite also passes on Linux with 3.14.6
(the coordinator's cloud run).

## What M1 delivers

- **`exposure` package**: a stdlib-only agent, a client library, the CLI
  (`python -m exposure` or the `exposure` console script) and 34 tests
  (`python -m pytest`).
- **Injection**: `sys.remote_exec` runs a generated bootstrap stub. The stub
  puts the package root on `sys.path` just long enough to import
  `exposure.agent`, so the target doesn't need Exposure installed and agent
  tracebacks show real file names.
  - The stub deletes itself when it runs.
  - It carries an expiry time: if the client gave up because the target was
    blocked in C, a late run exits quietly without starting an agent.
  - If the agent fails to start, the stub writes the traceback to
    `<pid>.error.txt`, which the client reports.
- **Transport**: loopback TCP on an ephemeral port, length-prefixed JSON. The
  first message is a 64-hex-character token, compared in constant time. Session
  files are written atomically with mode 0600 in a per-user folder.
- **Commands** (all read-only):

  | Command | What it does |
  |---|---|
  | `ps` | Python processes, with version and whether they're attachable |
  | `attach` | Inject the agent |
  | `ping` | Agent status |
  | `threads` | All threads |
  | `stack [THREAD]` | One thread's stack |
  | `ls [TARGET]` | Roots, or what TARGET holds |
  | `show TARGET` | Details of one object |
  | `scope` | The scope in force |
  | `release [@N...]` | Drop handles |
  | `detach` | Remove the agent |
  | `sessions [--clean]` | Session files on this machine |

  TARGET is a path such as `@3.items[0]`, `table[(1, 2)]` or
  `self.patch.modules[1].params`.
- **Handles**: `@N` ids are shared by every connection to the agent, so they
  work across one-shot CLI calls. They're cleared on `release` and on detach.
- **Safe inspection**: no `__repr__`, no properties or Python-level
  descriptors, and no subclass overrides of `dict`/`list`/`__iter__` ever
  run. Reads use `inspect.getattr_static`, C slot/getset descriptors and the
  built-in types' own unbound methods. Dunder names are refused. The tests use
  spy objects that count any call, and the count stays 0.
- **Scopes**: `reachable` (the entry frame's locals, the globals its code uses,
  running frames), `explicit` (only allowlisted items and what they hold), and
  `both`.
  - Explicit items come from `exposure.toml` (`[expose]`, with keys like
    `"module:path"` or `"<entry>:self.patch"`) and from `exposure.expose()`
    in the target.
  - `read`/`write`/`call` are parsed and reported for every item. M1
    enforces `read` only.
  - In explicit scope, frames and modules below an exposed item stay hidden.
- **Rate limiting**:
  - Work is cut into 2 ms slices with a 1 ms GIL-releasing sleep between
    them.
  - A duty-cycle cap (default 25%) spaces requests out.
  - An optional pressure counter (`--pressure '<entry>:self.backend._xruns'`)
    backs the agent off up to 16x when it rises.
- **Housekeeping**: the client follows a Windows venv launcher to the real
  interpreter. `sessions --clean` removes the files of dead processes, and
  the agent removes its files at exit. Re-attaching with different options
  warns, because options apply only when injecting.

## PySynthRack check (audio on, gain 0.02)

The frame time was measured at the top of the render loop by the M0 spike's
`sys.monitoring` hook, running alongside the M1 agent. The browse load cycled
through:

- `ls` (roots)
- `ls self.patch.modules`
- `ls self.patch.modules[1]`
- `ls self.patch.modules[1].params`
- `ls self.backend`
- `ls self`
- `threads`
- `stack`

| Scenario | Commands | Frame mean | p99 | max | fps | xruns Δ | overloads Δ |
|---|---|---|---|---|---|---|---|
| Audio on, no agent activity | – | 6.94 ms | 7.91 | – | 144 | 0 | 0 |
| M1, back-to-back commands (default duty 25%) | 6,053 in 11 s | 6.94 | 8.27 | 18.0 | 144 | **0** | 0 |
| M1, one command per 100 ms | 107 in 11 s | 6.94 | 8.04 | 8.8 | 144 | **0** | 0 |
| M1, back-to-back, duty cap off (`--duty 1`), agent busy 76% | 34,915 in 11 s | 7.00 | 8.20 | 47.6 | 143 | **0** | 0 |
| Control: M0's unthrottled loop (no slicing) | 590 k reads in ~5 s | 17.7 | 48.6 | – | 56 | **+39** | +113 |

- **Attach**: 41 ms the first time, 19 ms on re-inject. Command latency while
  hammering: mean 1.8 ms, p99 5.9 ms (0.3 ms / 0.9 ms with the duty cap off).
- **The slicing does the protecting.** Even with the duty cap off, the agent
  causes no xruns. The same work without slices (the control) still breaks
  the audio under today's conditions. This time the window was in front, so
  DearPyGui ran at 144 fps, not the ~24 fps cadence seen in M0.
- **The pressure back-off triggers.** With M0's unthrottled loop running at the
  same time (24 xruns, caused by that loop), the M1 agent saw 11 pressure
  events. It went to its top level (duty 6%, 16 ms yields) and dropped from
  about 550 to 80 commands a second.
- PySynthRack's stderr stayed empty. Detach left no session files, and the
  app closed normally.

## Known gaps, carried forward

- **The handle table grows without bound** while a session is attached: about
  850 handles per 11 s of hammering `ls self`. Fine for a person browsing, but
  M2 should add weak references where possible and/or a cap with LRU release
  (design section 2.2).
- **Grace-period tidy-up isn't done yet.** The agent doesn't notice an
  abandoned session (design section 5). One-shot CLI calls drop the connection
  after every command, so this needs an idle timeout rather than "connection
  closed".
- **No `show -r` / real `repr()`**, no `find`, no asyncio tasks. Those are
  planned for later milestones.
- **`ps` can't see `-X disable-remote-debug` or the environment of another
  process on Windows**, so it shows "unknown" there. Linux reads
  `/proc/<pid>/environ` and `cmdline`.
- **`ps` is not implemented on macOS.**
