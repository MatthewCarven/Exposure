"""``exposure shell PID``: an interactive session on one connection.

All the one-shot commands work without retyping the PID, plus Python:
``!expr`` (or ``eval``/``exec``) runs code in the target after asking, and
``py`` switches to a mode where every line is Python. Tab completes command
names, paths (asked of the agent, read-only) and handles when ``readline``
is available (POSIX; on Windows install ``pyreadline3`` for it). History is
kept per user. Long listings are paged.

Client side only.
"""
from __future__ import annotations

import cmd
import contextlib
import io
import os
import shlex
import shutil
import sys

from . import cli, config, exprs
from .client import RemoteError

try:  # readline: completion and history (POSIX, or pyreadline3 on Windows)
    import readline  # type: ignore
except ImportError:  # pragma: no cover - depends on the platform
    readline = None

HELP = """\
Browse:   ls [PATH] [GLOB]  show PATH  ?PATH (signature, doc)  src PATH  threads  stack
          scope  ping  find type:X|name:pat
Change:   call EXPR  set PATH VALUE | set PATH = VALUE  nudge PATH DELTA  undo [N]  history
Jobs:     jobs  result N  every INTERVAL EXPR  repeats  stop [ID|all]
Loop:     pause [S]  step [N]  resume  frames [N]  executors
Panels:   watch PATH...  tweak PATH [STEP]  preset NAME  presets      (Ctrl+C returns here)
Probes:   log FILE:LINE EXPR [--when C]  trace FUNC  break FILE:LINE | break when COND
          logs (stream)  probes  unlog|untrace|unbreak [ID|all]
Compare:  snap NAME PATH  diff NAME [OTHER]  snaps
Reload:   reload MODULE [--values]  reload --undo
Python:   !EXPR | eval EXPR | exec CODE (alone: type lines, blank line runs) | py (toggle)
          frame PATH (run Python in that frame; `frame` alone = scratch)  scratch [clear]
Session:  confirm on|off  release [@N...]  detach  quit
Paths:    names from `ls`, handles like @3, then .attr and [key] steps.  Tab completes."""


class Shell(cmd.Cmd):
    def __init__(self, client, *, confirm=None, stdin=None, stdout=None, page=None):
        super().__init__(stdin=stdin, stdout=stdout)
        if stdin is not None:
            self.use_rawinput = False
        self.c = client
        cfg = config.load()
        self.confirm = cfg["client"]["confirm"] if confirm is None else confirm
        self.rate = float(cfg["client"].get("rate", 10))
        self.history_len = int(cfg["client"].get("history", 1000))
        self.py_mode = False
        self.frame = None
        self.stream_seconds = None    # tests set this; interactive use streams until Ctrl+C
        self.page = sys.stdout.isatty() if page is None else page
        self.prompt = self._prompt()
        self.intro = (f"exposure shell on pid {client.pid} "
                      f"(Python {client.ping()['python']}). "
                      "`help` lists commands, `quit` leaves (the agent stays attached).")
        self._setup_readline()

    # -- plumbing -----------------------------------------------------------------------
    def _prompt(self):
        mode = "py" if self.py_mode else "exposure"
        where = f" [{self.frame}]" if self.frame else ""
        return f"{mode} {self.c.pid}{where}> "

    def _setup_readline(self):
        if readline is None or not self.use_rawinput:
            return
        try:
            readline.set_completer_delims(" \t\n'\"=(),")
            os.makedirs(config.config_dir(), exist_ok=True)
            readline.read_history_file(config.history_path())
        except (OSError, AttributeError):
            pass

    def _save_history(self):
        if readline is None or not self.use_rawinput:
            return
        try:
            readline.set_history_length(self.history_len)
            readline.write_history_file(config.history_path())
        except (OSError, AttributeError):
            pass

    def _emit(self, fn, *args):
        """Run a printing function, paging its output if it's long."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args)
        text = buf.getvalue()
        rows = shutil.get_terminal_size((100, 40)).lines - 2
        lines = text.splitlines()
        if not self.page or len(lines) <= rows:
            self.stdout.write(text)
            return
        for i in range(0, len(lines), rows):
            self.stdout.write("\n".join(lines[i:i + rows]) + "\n")
            if i + rows < len(lines):
                ans = input(f"-- {len(lines) - i - rows} more lines: Enter for more, q to stop --")
                if ans.strip().lower().startswith("q"):
                    break

    def _show(self, cmdname, res):
        self._emit(cli.print_result, cmdname, res)

    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except (RemoteError, exprs.ExprError, ValueError, TypeError, LookupError) as exc:
            self.stdout.write(f"error: {exc}\n")
            tb = getattr(exc, "traceback", None)
            if tb:
                self.stdout.write(tb.rstrip() + "\n")
        except KeyboardInterrupt:
            self.stdout.write("\n")
        except (ConnectionError, OSError) as exc:
            self.stdout.write(f"connection lost: {exc}\n")
            return True
        return False

    def precmd(self, line):
        if self.py_mode and line.strip() not in ("py", "exit", "quit", "EOF") \
                and not line.startswith("!"):
            return "!" + line if line.strip() else line
        return line

    def postcmd(self, stop, line):
        self.prompt = self._prompt()
        return stop

    def emptyline(self):
        return False

    def default(self, line):
        if line.startswith("!"):
            return self._python(line[1:], "auto")
        if line.startswith("?"):
            return self.do_doc(line[1:])
        first = line.split()[0].lower()
        if "-m exposure" in line or first.endswith(("python", "python.exe")):
            self.stdout.write("that's a PowerShell command, and you're already in the exposure "
                              "shell: type `quit` to go back to PowerShell (or `detach` to "
                              "also remove the agent, e.g. to attach with other options)\n")
            return
        self.stdout.write(f"unknown command {line.split()[0]!r}; `help` lists them, "
                          "`!expr` runs Python\n")

    def do_help(self, arg):
        if arg.strip():
            return self.do_doc(arg)           # `help thing` = `?thing`
        self.stdout.write(HELP + "\n")

    def do_doc(self, arg):
        self._show("doc", self.c.doc(arg.strip()))

    do_sig = do_doc

    def do_src(self, arg):
        self._show("src", self.c.src(arg.strip()))

    # -- Python -------------------------------------------------------------------------------
    def _ask(self, source):
        if not self.confirm:
            return True
        preview = source if len(source) < 200 else source[:200] + "..."
        try:
            ans = input(f"run in pid {self.c.pid}: {preview!r}  [y/N/a=always] ").strip().lower()
        except EOFError:
            return False
        if ans == "a":
            self.confirm = False
            return True
        return ans in ("y", "yes")

    def _python(self, source, mode):
        if not source.strip():
            return False
        if not self._ask(source):
            self.stdout.write("not run\n")
            return False
        res = self.c.eval(source, mode=mode, frame=self.frame, confirmed=True)
        self._emit(cli.print_job, res, True)        # like the REPL: no "-> None"
        return False

    def do_eval(self, arg):
        return self._python(arg, "eval")

    def do_exec(self, arg):
        if not arg.strip():
            lines = []
            while True:
                try:
                    line = input("... ") if self.use_rawinput else self.stdin.readline()
                except EOFError:
                    break
                if not self.use_rawinput and line == "":
                    break
                line = line.rstrip("\n")
                if not line.strip():
                    break
                lines.append(line)
            arg = "\n".join(lines)
        return self._python(arg, "exec")

    def do_py(self, arg):
        self.py_mode = not self.py_mode
        self.stdout.write("Python mode: every line runs in the target; `py` to leave\n"
                          if self.py_mode else "command mode\n")

    def do_frame(self, arg):
        self.frame = arg.strip() or None
        self.stdout.write(f"Python runs in frame {self.frame}\n" if self.frame
                          else "Python runs in the session's scratch namespace\n")

    def do_scratch(self, arg):
        self.stdout.write(", ".join(self.c.scratch(clear=arg.strip() == "clear")) + "\n")

    def do_confirm(self, arg):
        self.confirm = arg.strip().lower() not in ("off", "no", "0", "false")
        self.stdout.write(f"confirm before Python: {'on' if self.confirm else 'off'}\n")

    # -- browsing ------------------------------------------------------------------------------
    def do_ls(self, arg):
        parts = arg.split()
        target, pattern = cli.split_ls_args(parts[0] if parts else None,
                                            parts[1] if len(parts) > 1 else None)
        self._show("ls", self.c.ls(target, pattern=pattern))

    def do_show(self, arg):
        self._show("show", self.c.show(arg.strip()))

    def do_threads(self, arg):
        self._show("threads", self.c.threads())

    def do_stack(self, arg):
        self._show("stack", self.c.stack(arg.strip() or None))

    def do_scope(self, arg):
        self._show("scope", self.c.scope())

    def do_ping(self, arg):
        self._show("ping", self.c.ping())

    def do_find(self, arg):
        parts = shlex.split(arg)
        via = None
        if "--via" in parts:
            i = parts.index("--via")
            via = parts[i + 1]
            del parts[i:i + 2]
        if via == "gc" and not self._heavy_ok("find --via gc scans the whole heap in one long "
                                              "C call"):
            return
        self._show("find", self.c.find(parts[0], via=via))

    # -- changes --------------------------------------------------------------------------------
    def do_call(self, arg):
        self._show("call", self.c.call(arg.strip()))

    def do_set(self, arg):
        arg = arg.strip()
        try:
            res = self.c.assign(arg)
        except exprs.ExprError:
            target, _, value = arg.partition(" ")
            res = self.c.set(target, value.strip())
        self._show("set", res)

    def do_nudge(self, arg):
        target, delta = arg.split()
        d = float(delta)
        self._show("nudge", self.c.nudge(target, int(d) if d.is_integer() else d))

    def do_undo(self, arg):
        self._show("undo", self.c.undo(int(arg) if arg.strip() else 1))

    def do_history(self, arg):
        self._show("history", self.c.history())

    def do_jobs(self, arg):
        self._show("jobs", self.c.jobs())

    def do_result(self, arg):
        self._show("result", self.c.result(int(arg)))

    def do_every(self, arg):
        interval, _, expr = arg.strip().partition(" ")
        self._show("every", self.c.every(interval, expr.strip()))

    def do_repeats(self, arg):
        self._show("repeats", self.c.repeats())

    def do_stop(self, arg):
        self._show("stop", self.c.stop(arg.strip() or None))

    def do_release(self, arg):
        self._show("release", self.c.release(arg.split() or None))

    # -- main loop ------------------------------------------------------------------------------
    def do_pause(self, arg):
        self._show("pause", self.c.pause(float(arg) if arg.strip() else None))

    def do_resume(self, arg):
        self._show("resume", self.c.resume())

    def do_step(self, arg):
        self._show("step", self.c.step(int(arg) if arg.strip() else 1))

    def do_paused(self, arg):
        self._show("paused", self.c.paused())

    def do_frames(self, arg):
        self._show("frames", self.c.frames(int(arg) if arg.strip() else 300))

    def do_executors(self, arg):
        self._show("executors", self.c.executors())

    # -- logpoints, traces, breakpoints ----------------------------------------------------------
    @staticmethod
    def _split_opt(arg, name):
        """'EXPR --when COND' -> ('EXPR', 'COND')"""
        marker = f" --{name} "
        if marker in f" {arg} ":
            head, _, tail = f" {arg} ".partition(marker)
            return head.strip(), tail.strip()
        return arg.strip(), None

    def _stream(self, seconds=None):
        self.stdout.write("(streaming; Ctrl+C returns to the prompt)\n")
        out = self.stdout

        def show(ev):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.print_event(ev)
            out.write(buf.getvalue())
            out.flush()

        try:
            self.c.stream_events(show, seconds=seconds or self.stream_seconds)
        except KeyboardInterrupt:
            self.stdout.write("\n")

    def do_log(self, arg):
        rest, when = self._split_opt(arg, "when")
        loc, _, expr = rest.partition(" ")
        if not expr.strip():
            self.stdout.write("usage: log FILE:LINE EXPR [--when COND]\n")
            return
        if not self._ask(f"logpoint {expr.strip()}" + (f" when {when}" if when else "")):
            self.stdout.write("not added\n")
            return
        res = self.c.log(loc, expr.strip(), when=when, confirmed=True)
        self._emit(cli.print_probe, res)
        self._stream()

    def do_trace(self, arg):
        res = self.c.trace(arg.strip())
        self._emit(cli.print_probe, res)
        self._stream()

    def do_break(self, arg):
        rest, when = self._split_opt(arg, "when")
        rest, keep = (rest.replace("--keep", "").strip(), True) if "--keep" in rest \
            else (rest, False)
        if rest.startswith("when "):            # `break when COND` reads naturally
            rest, when = "", rest[5:].strip()
        if when and not self._ask(f"breakpoint condition {when}"):
            self.stdout.write("not added\n")
            return
        res = self.c.break_(rest or None, when=when, keep=keep or None, confirmed=bool(when))
        self._emit(cli.print_probe, res)

    def do_snap(self, arg):
        name, _, target = arg.strip().partition(" ")
        if not target.strip():
            self.stdout.write("usage: snap NAME PATH\n")
            return
        self._show("snap", self.c.snap(name, target.strip()))

    def do_diff(self, arg):
        parts = arg.split()
        self._show("diff", self.c.diff(parts[0], parts[1] if len(parts) > 1 else None))

    def do_reload(self, arg):
        parts = arg.split()
        undo = "--undo" in parts
        values = "--values" in parts
        names = [x for x in parts if not x.startswith("--")]
        module = names[0] if names else None
        if not undo and not module:
            self.stdout.write("usage: reload MODULE [--values] | reload [MODULE] --undo\n")
            return
        if not self._ask(f"reload {module or ''}{' --undo' if undo else ''}"):
            self.stdout.write("not done\n")
            return
        self._show("reload", self.c.reload(module, undo=undo, values=values, confirmed=True))

    def do_snaps(self, arg):
        self._show("snaps", self.c.snaps())

    def do_logs(self, arg):
        self._stream()

    def do_probes(self, arg):
        self._show("probes", self.c.probes())

    def do_unlog(self, arg):
        self._show("unprobe", self.c.unprobe(arg.strip() or "all"))

    do_untrace = do_unbreak = do_unprobe = do_unlog

    # -- panels ---------------------------------------------------------------------------------
    def do_watch(self, arg):
        from . import tui
        tui.run_panel(self.c, shlex.split(arg), rate=self.rate, out=self.stdout)

    def do_tweak(self, arg):
        from . import tui
        parts = shlex.split(arg)
        step = float(parts[1]) if len(parts) > 1 else 1.0
        tui.run_panel(self.c, [], [parts[0]], rate=self.rate, steps={parts[0]: step},
                      out=self.stdout)

    def do_preset(self, arg):
        from . import tui
        p = self.c.presets().get(arg.strip())
        if p is None:
            self.stdout.write(f"no preset {arg.strip()!r}\n")
            return
        tui.run_panel(self.c, p.get("watch", []), p.get("tweak", []),
                      rate=p.get("rate", self.rate), steps=p.get("step", {}), out=self.stdout)

    def do_presets(self, arg):
        self._show("presets", self.c.presets())

    # -- leaving --------------------------------------------------------------------------------
    def do_detach(self, arg):
        self._show("detach", self.c.detach())
        return True

    def do_quit(self, arg):
        return True

    do_exit = do_quit

    def do_EOF(self, arg):
        self.stdout.write("\n")
        return True

    def _heavy_ok(self, what):
        """Warn once per session before heavy work on a real-time target (one with
        a pressure probe, e.g. PySynthRack's xrun counter)."""
        if getattr(self, "_heavy_warned", False):
            return True
        try:
            realtime = self.c.ping()["budget"]["probe"]
        except Exception:
            realtime = False
        if not realtime:
            return True
        self._heavy_warned = True
        self.stdout.write(f"warning: {what}; this target has a pressure probe (audio?), so "
                          "it may cause a dropout.\n")
        if not self.use_rawinput:
            return True
        try:
            return input("go ahead? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    # -- completion ------------------------------------------------------------------------------
    def complete_do(self, text, line, begidx, endidx):
        try:
            return [a for a in self.c.scope().get("actions", []) if a.startswith(text)]
        except Exception:
            return []

    def complete_preset(self, text, line, begidx, endidx):
        try:
            return [p for p in self.c.presets() if p.startswith(text)]
        except Exception:
            return []

    def completedefault(self, text, line, begidx, endidx):
        try:
            return self.c.complete(text)
        except Exception:
            return []

    def postloop(self):
        self._save_history()


def run(client, **kw):
    sh = Shell(client, **kw)
    try:
        sh.cmdloop()
    except KeyboardInterrupt:
        sh.stdout.write("\n")
    finally:
        sh._save_history()
