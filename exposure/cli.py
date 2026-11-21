"""``exposure`` command line.

    exposure ps                          Python processes and whether they're attachable
    exposure attach PID [options]        inject the agent and show the scope
    exposure ls PID [TARGET]             list roots, or what TARGET holds
    exposure show PID TARGET             details of one object
    exposure threads PID | stack PID [THREAD] | scope PID | ping PID
    exposure release PID [@N ...]        drop handles (all if none given)
    exposure detach PID                  remove the agent
    exposure sessions [--clean]          session files on this machine

TARGET is a path: a name from ``ls``, a handle like ``@3``, or either followed
by ``.attr`` and ``[key]`` steps, e.g. ``@3.items[0]`` or ``self.patch.modules[1]``.
Any command that needs the agent injects it first if it isn't running.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from . import client as client_mod
from . import config, exprs, paths, procs

ATTACH_OPTS = ("entry", "policy", "scope", "pressure", "duty", "wait", "read_only",
               "idle_timeout", "frame_budget_ms", "max_handles", "drop_grace")


def _attach_parent():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("pid", type=int)
    g = p.add_argument_group("attach options (used when injecting)")
    g.add_argument("--entry", help="entry frame: qualname or module:qualname (default: main)")
    g.add_argument("--policy", help="exposure.toml to load (default: next to the target's "
                                    "main script, or its cwd)")
    g.add_argument("--scope", choices=["reachable", "explicit", "both"])
    g.add_argument("--pressure", help="counter path the agent backs off on, "
                                      "e.g. '<entry>:self.backend._xruns'")
    g.add_argument("--duty", type=float, help="max share of wall time the agent may be busy "
                                              "(default 0.25)")
    g.add_argument("--wait", type=float, default=10.0, help="seconds to wait for the target")
    g.add_argument("--read-only", action="store_true", dest="read_only",
                   help="refuse every call and set for this session")
    g.add_argument("--idle-timeout", type=float, dest="idle_timeout",
                   help="seconds without a client before the agent detaches itself "
                        "(default 1800; 0 = never)")
    g.add_argument("--frame-budget", type=float, dest="frame_budget_ms",
                   help="ms of queued work per main-loop iteration (default 2)")
    g.add_argument("--drop-grace", type=float, dest="drop_grace",
                   help="seconds before the agent tidies up after a client vanishes "
                        "without saying bye (default 60)")
    g.add_argument("--max-handles", type=int, dest="max_handles",
                   help="handle table size before the least recently used are evicted "
                        "(default 10000)")
    return p


def _run_opts(p):
    p.add_argument("--on", help="where it runs: main (default; between frames), agent "
                                "(a worker thread), or thread:NAME")
    p.add_argument("--timeout", type=float, help="seconds to wait (default 10); the job keeps "
                                                 "running after a timeout")


def build_parser():
    ap = argparse.ArgumentParser(prog="exposure", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print raw JSON results")
    sub = ap.add_subparsers(dest="cmd", required=True)
    parent = _attach_parent()
    sub.add_parser("ps", help="list Python processes")
    s = sub.add_parser("sessions", help="list session files")
    s.add_argument("--clean", action="store_true", help="remove files of dead processes")
    sub.add_parser("attach", parents=[parent], help="inject the agent")
    sub.add_parser("ping", parents=[parent])
    sub.add_parser("threads", parents=[parent])
    s = sub.add_parser("stack", parents=[parent])
    s.add_argument("thread", nargs="?", help="thread name or ident (default: main)")
    s.add_argument("--limit", type=int)
    s = sub.add_parser("ls", parents=[parent])
    s.add_argument("target", nargs="?")
    s.add_argument("pattern", nargs="?", help="only names matching this glob, e.g. '*widget*'")
    s.add_argument("--limit", type=int)
    s.add_argument("--offset", type=int)
    s = sub.add_parser("show", parents=[parent])
    s.add_argument("target")
    s = sub.add_parser("doc", parents=[parent], help="signature, docstring, where defined")
    s.add_argument("target")
    s = sub.add_parser("src", parents=[parent], help="source code of a function or class")
    s.add_argument("target")
    sub.add_parser("scope", parents=[parent])
    s = sub.add_parser("release", parents=[parent])
    s.add_argument("handles", nargs="*")
    sub.add_parser("detach", parents=[parent])
    # M2
    s = sub.add_parser("call", parents=[parent], help="call a function or method")
    s.add_argument("expr", help='e.g. \'self._mirror_param(2, "gain", 0.1)\'')
    s.add_argument("--no-capture", dest="capture", action="store_false",
                   help="let print() output go to the program's own stdout")
    _run_opts(s)
    s = sub.add_parser("set", parents=[parent], help="set a variable, attribute or item")
    s.add_argument("target", help="path, e.g. config.debug or @3[0], or 'path = value'")
    s.add_argument("value", nargs="?", help="literal, name or handle (strings need quotes)")
    _run_opts(s)
    s = sub.add_parser("do", parents=[parent], help="run a named action from exposure.toml")
    s.add_argument("action")
    s.add_argument("values", nargs="*")
    _run_opts(s)
    s = sub.add_parser("undo", parents=[parent], help="revert the last N sets")
    s.add_argument("n", nargs="?", type=int, default=1)
    s = sub.add_parser("history", parents=[parent], help="list changes made this session")
    s.add_argument("--limit", type=int)
    sub.add_parser("jobs", parents=[parent], help="recent calls and sets")
    s = sub.add_parser("result", parents=[parent], help="result of a job")
    s.add_argument("job", type=int)
    s.add_argument("--timeout", type=float, help="wait up to this long for it to finish")
    sub.add_parser("executors", parents=[parent], help="how work reaches each thread")
    s = sub.add_parser("frames", parents=[parent], help="main-loop iteration time stats")
    s.add_argument("n", nargs="?", type=int, default=300)
    # M3
    s = sub.add_parser("pause", parents=[parent], help="freeze the main loop between frames")
    s.add_argument("--for", dest="seconds", type=float, help="resume by itself after S seconds")
    sub.add_parser("resume", parents=[parent], help="let the main loop run again")
    s = sub.add_parser("step", parents=[parent], help="while paused, advance N frames")
    s.add_argument("n", nargs="?", type=int, default=1)
    sub.add_parser("paused", parents=[parent], help="pause status")
    for name, hlp in (("watch", "live panel of values"), ("tweak", "nudge numbers live")):
        s = sub.add_parser(name, parents=[parent], help=hlp)
        s.add_argument("paths", nargs="*", help="paths; len(path) for a container's length")
        s.add_argument("--rate", type=float, help="refreshes per second (default 10)")
        s.add_argument("--count", type=int, help="stop after N refreshes (scripts, tests)")
        s.add_argument("--plain", action="store_true", help="no colours or redraw")
        s.add_argument("--preset", help="take paths from [presets.NAME] in exposure.toml")
        if name == "tweak":
            s.add_argument("--step", type=float, default=1.0, help="nudge size (default 1)")
            s.add_argument("--watch", nargs="*", default=[], help="also show these paths")
    s = sub.add_parser("preset", parents=[parent], help="open a [presets.NAME] panel")
    s.add_argument("name")
    s.add_argument("--count", type=int)
    s.add_argument("--plain", action="store_true")
    sub.add_parser("presets", parents=[parent], help="list presets from exposure.toml")
    s = sub.add_parser("nudge", parents=[parent], help="add DELTA to a number (undoable)")
    s.add_argument("target")
    s.add_argument("delta", type=float)
    _run_opts(s)
    s = sub.add_parser("find", parents=[parent], help="find objects: type:Name or name:pattern")
    s.add_argument("query")
    s.add_argument("--via", choices=["graph", "gc"], help="graph (default: walk the reachable "
                   "scope, budgeted) or gc (whole heap, one long C call)")
    s.add_argument("--limit", type=int)
    s.add_argument("--depth", type=int)
    s = sub.add_parser("every", parents=[parent], help="call something repeatedly")
    s.add_argument("interval", help="e.g. 1s, 250ms, 0.5")
    s.add_argument("expr")
    s.add_argument("--count", type=int, help="stop after N calls")
    s.add_argument("--on")
    # M4
    for name, hlp in (("eval", "evaluate a Python expression in the target"),
                      ("exec", "run Python statements in the target")):
        s = sub.add_parser(name, parents=[parent], help=hlp)
        s.add_argument("source", help="Python source; @3 stands for handle 3")
        s.add_argument("--frame", help="run in this frame's namespace (e.g. main, or @N); "
                                       "default: the session's scratch namespace")
        s.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
        s.add_argument("--no-capture", dest="capture", action="store_false",
                       help="let print() output go to the program's own stdout")
        _run_opts(s)
    sub.add_parser("shell", parents=[parent], help="interactive session (REPL)")
    # M6: logpoints, traces, breakpoints
    s = sub.add_parser("log", parents=[parent], help="logpoint: print EXPR whenever FILE:LINE "
                                                     "runs (no pause)")
    s.add_argument("location", help="FILE:LINE, e.g. game.py:42 or ui/app.py:511")
    s.add_argument("expr", help="Python expression evaluated in that frame")
    s.add_argument("--when", help="only when this condition is true")
    s.add_argument("--every", type=int, help="only every Nth hit")
    s.add_argument("--rate", type=float, help="max lines per second (default 20)")
    s.add_argument("--for", dest="seconds", type=float, help="remove itself after S seconds")
    s.add_argument("--yes", "-y", action="store_true")
    s.add_argument("--follow", "-f", action="store_true", help="then stream its output")
    s = sub.add_parser("trace", parents=[parent], help="show every call of a function live")
    s.add_argument("target", help="function or method path, e.g. self._mirror_param")
    s.add_argument("--rate", type=float, help="max lines per second (default 20)")
    s.add_argument("--for", dest="seconds", type=float)
    s.add_argument("--follow", "-f", action="store_true", help="then stream its output")
    s = sub.add_parser("break", parents=[parent], help="pause the main loop at FILE:LINE "
                                                       "and/or when a condition is true")
    s.add_argument("location", nargs="?", help="FILE:LINE (pauses at the next frame boundary)")
    s.add_argument("--when", help="condition; alone, it's checked once per frame")
    s.add_argument("--keep", action="store_true", help="stay armed after it fires")
    s.add_argument("--yes", "-y", action="store_true")
    s = sub.add_parser("logs", parents=[parent], help="stream logpoint/trace/break output")
    s.add_argument("--for", dest="seconds", type=float, help="stop after S seconds")
    s.add_argument("--count", type=int, help="stop after N lines")
    s.add_argument("--all", action="store_true", help="include what's already buffered")
    sub.add_parser("probes", parents=[parent], help="list logpoints, traces and breakpoints")
    s = sub.add_parser("snap", parents=[parent], help="snapshot a subtree (for diff)")
    s.add_argument("name")
    s.add_argument("target")
    s.add_argument("--depth", type=int, help="levels to record (default 4)")
    s = sub.add_parser("diff", parents=[parent], help="what changed since a snapshot")
    s.add_argument("a")
    s.add_argument("b", nargs="?", help="another snapshot (default: now)")
    sub.add_parser("snaps", parents=[parent], help="list snapshots")
    s = sub.add_parser("panel", parents=[parent], help="open a debug panel in the browser")
    s.add_argument("--preset", help="[presets.NAME] to show (watch list, sliders)")
    s.add_argument("--port", type=int, default=0, help="local port (default: any free one)")
    s.add_argument("--no-open", action="store_true", help="print the URL, don't open a browser")
    s = sub.add_parser("reload", parents=[parent], help="hot reload: re-run a module's source "
                                                        "and rebind its functions in place")
    s.add_argument("module", nargs="?", help="module name, e.g. game.enemies or __main__")
    s.add_argument("--undo", action="store_true", help="put the last reload's old code back")
    s.add_argument("--values", action="store_true",
                   help="also replace module-level values (default: keep state)")
    s.add_argument("--yes", "-y", action="store_true")
    _run_opts(s)
    for name in ("unlog", "untrace", "unbreak", "unprobe"):
        s = sub.add_parser(name, parents=[parent], help="remove a probe (or all)")
        s.add_argument("probe", nargs="?", default="all")
    sub.add_parser("repeats", parents=[parent], help="list repeated calls")
    s = sub.add_parser("stop", parents=[parent], help="stop a repeated call (or all)")
    s.add_argument("repeat", nargs="?", default="all")
    return ap


# -- printing -------------------------------------------------------------------------

def _fit(text, width):
    text = str(text)
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


def _table(rows, headers, flex=None):
    """Print an aligned table that fits the terminal. ``flex`` is the column
    that gives way when it doesn't (default: the widest)."""
    if not rows:
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    term = term_width()
    over = sum(widths) + 2 * (len(widths) - 1) - term
    if over > 0 and flex is not None:            # the flexible column gives way first
        cut = min(over, widths[flex] - 10)
        widths[flex] -= max(0, cut)
        over -= max(0, cut)
    while over > 0:                              # then the widest, down to 4 each
        i = widths.index(max(widths))
        if widths[i] <= 4:
            break
        cut = min(over, widths[i] - 4, max(1, widths[i] // 4))
        widths[i] -= cut
        over -= cut
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format(*[_fit(h, w) for h, w in zip(headers, widths)]).rstrip())
    for r in rows:
        print(fmt.format(*[_fit(c, w) for c, w in zip(r, widths)]).rstrip())


def term_width() -> int:
    """Columns to fit output into: the terminal's, or 100 when piped."""
    real = sys.__stdout__   # the shell pages captured output, so ask the real one
    if real is not None and real.isatty():
        return max(40, shutil.get_terminal_size((100, 40)).columns - 1)
    try:
        return max(40, int(os.environ["COLUMNS"]) - 1)
    except (KeyError, ValueError):
        return 100


def _line(text):
    print(_fit(text, term_width()))


def _short_path(path, keep=3):
    parts = str(path).replace("\\", "/").split("/")
    return path if len(parts) <= keep else "…/" + "/".join(parts[-keep:])


def _short_kind(kind):
    for prefix, short in (("policy:", "policy"), ("expose:", "exposed")):
        if str(kind).startswith(prefix):
            return short
    return kind


def _perm_str(perm):
    if not perm:
        return ""
    parts = []
    for k in ("read", "write", "call"):
        v = perm.get(k)
        if v is True:
            parts.append(k[0])
        elif isinstance(v, list) and v:
            parts.append(f"{k[0]}({','.join(v)})")
    return "".join(parts) if all(len(p) == 1 for p in parts) else " ".join(parts)


def print_ls(res):
    if res.get("target") is None:
        e = res.get("entry")
        if e:
            more = f", {e['matches']} matches" if e.get("matches", 1) > 1 else ""
            _line(f"entry {e['function']} {e['handle']} ({_short_path(e['file'])}:{e['line']}, "
                  f"thread {e['thread']}{more})  scope: {res['scope']}"
                  + ("  STRICT" if res.get("strict") else ""))
        elif res.get("entry_missing"):
            _line(f"entry {res['entry_missing']!r} not on any stack  scope: {res['scope']}")
    else:
        _line(f"{res['target']}  {res['preview']}")
    rows = [(e.get("handle", ""), e["name"], _short_kind(e["kind"]), _perm_str(e.get("perm")),
             e["preview"]) for e in res["entries"]]
    _table(rows, ["handle", "name", "kind", "perm", "value"], flex=4)
    if res.get("more"):
        nxt = res["offset"] + len(res["entries"])
        print(f"... {res['total'] - nxt} more (use --offset {nxt})")


def split_ls_args(target, pattern):
    """`ls TARGET PATTERN`; a lone glob (`ls '*speed*'`) filters the roots."""
    if (pattern is None and target and any(ch in target for ch in "*?")
            and not target.startswith("@")):
        return None, target
    return target, pattern


def print_doc(res):
    _line(f"{res['target']}  {res['preview']}" + (f"  ({res['about']})" if res.get("about")
                                                   else ""))
    if res.get("signature"):
        _line(f"  {res['signature']}")
    if res.get("defined"):
        _line(f"  defined at {_short_path(res['defined'], 4)}")
    if res.get("doc"):
        print()
        for ln in res["doc"].splitlines():
            _line(f"  {ln}")
        if res.get("doc_lines", 0) > len(res["doc"].splitlines()):
            _line(f"  ... ({res['doc_lines']} lines in all)")


def print_src(res):
    _line(f"{_short_path(res['file'], 4)}:{res['start']}")
    width = len(str(res["start"] + len(res["lines"])))
    for i, ln in enumerate(res["lines"]):
        _line(f"{res['start'] + i:>{width}}  {ln}")
    if res.get("truncated"):
        print("  ... (truncated)")


def print_reload(res):
    if "report" not in res:
        print_job(res)
        return
    r = res["report"]
    _line(f"reloaded {r['module']} on {res.get('thread')} ({res.get('on')}), "
          f"blocked {res.get('blocked_ms')} ms")
    for key, label in (("rebound", "rebound"), ("added", "added"), ("skipped", "skipped"),
                       ("removed_in_source", "gone from the source"),
                       ("updated_values", "values replaced"),
                       ("kept_values", "values kept (use --values to replace)"),
                       ("running", "running now (old code until they return)")):
        if r.get(key):
            _line(f"  {label}: {', '.join(r[key])}")
    if not r.get("rebound") and not r.get("added"):
        _line("  nothing changed")


def print_diff(res):
    n = res["counts"]
    _line(f"{res['target']}: {res['a']} -> {res['b']} ({res['seconds_apart']} s apart): "
          f"{n['changed']} changed, {n['added']} added, {n['removed']} removed, "
          f"{n['same']} the same" + ("  (snapshot truncated)" if res["truncated"] else ""))
    for path, old, new in res["changed"]:
        _line(f"  ~ {path}: {old} -> {new}")
    for path, value in res["added"]:
        _line(f"  + {path}: {value}")
    for path, value in res["removed"]:
        _line(f"  - {path}: {value}")


def print_probe(res):
    what = res.get("spec") or res.get("when")
    extra = f"  when {res['source']}" if res.get("kind") == "break" and res.get("source") \
        else ""
    _line(f"{res.get('kind')} {res['id']} added: {what}{extra}")
    if res.get("note"):
        _line(f"  {res['note']}")


def print_event(ev):
    stamp = time.strftime("%H:%M:%S", time.localtime(ev["t"])) + f".{int(ev['t'] * 1000) % 1000:03d}"
    pid = f"#{ev['probe']}" if ev.get("probe") else "  "
    _line(f"{stamp} {pid:>4} [{ev['thread']}] {ev['text']}")


def _follow(c, since, seconds=None, count=None):
    print("(streaming; Ctrl+C to stop)", file=sys.stderr)
    try:
        c.stream_events(print_event, since=since, seconds=seconds, count=count)
    except KeyboardInterrupt:
        pass
    return 0


def _confirm(question) -> bool:
    if not sys.stdin.isatty():
        print("exposure: eval/exec needs confirmation; pass --yes when not at a terminal",
              file=sys.stderr)
        return False
    try:
        return input(f"{question}  [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        print(file=sys.stderr)
        return False


def _panel(c, args):
    from . import tui
    watch, tweak, steps, rate = list(getattr(args, "paths", []) or []), [], {}, None
    preset_name = args.name if args.cmd == "preset" else getattr(args, "preset", None)
    if preset_name:
        presets = c.presets()
        if preset_name not in presets:
            print(f"exposure: no preset {preset_name!r} (have: {sorted(presets)})",
                  file=sys.stderr)
            return 2
        p = presets[preset_name]
        watch += p.get("watch", [])
        tweak += p.get("tweak", [])
        steps.update(p.get("step", {}))
        rate = p.get("rate")
    if args.cmd == "tweak":
        tweak = list(args.paths) + tweak
        watch = list(args.watch) + [w for w in watch if w not in args.paths]
        for t in args.paths:
            steps.setdefault(t, args.step)
    if not watch and not tweak:
        print("exposure: nothing to show (give paths or --preset)", file=sys.stderr)
        return 2
    rate = getattr(args, "rate", None) or rate or 10.0
    tui.run_panel(c, watch, tweak, rate=rate, steps=steps, count=args.count,
                  plain=args.plain)
    return 0


def print_job(res, hide_none=False):
    head = f"job {res['job']} {res['state']}"
    if res.get("thread"):
        head += f" on {res['thread']} ({res['on']}), blocked {res['blocked_ms']} ms"
    print(head)
    if "result" in res and not (hide_none and res["result"].get("preview") == "None"
                                and "handle" not in res["result"]):
        r = res["result"]
        _line(f"  -> {r.get('handle', '')} {r['preview']}".replace("->  ", "-> "))
    if "new" in res:
        print(f"  {res['old']}  ->  {res['new']}   (change {res['change']}; `undo` reverts)")
    if res.get("output"):
        more = " (truncated)" if res.get("output_truncated") else ""
        print(f"  output{more}:")
        print("    " + res["output"].rstrip("\n").replace("\n", "\n    "))
    for w in res.get("warnings", ()):
        print(f"  warning: {w}")
    if "error" in res:
        print(f"  raised {res['error']}  {res['exception'].get('handle', '')}")
        if res.get("traceback"):
            print("  " + res["traceback"].rstrip().replace("\n", "\n  "))
    if "note" in res:
        print(f"  {res['note']}")


def print_result(cmd, res):
    if cmd == "ls":
        print_ls(res)
    elif cmd == "threads":
        _table([(t["ident"], t["name"], "main" if t.get("main") else "",
                 t.get("top", "")) for t in res], ["ident", "name", "", "top frame"])
    elif cmd == "stack":
        print(f"thread {res['thread']}")
        _table([(f.get("handle", ""), f["depth"], ("*" if f.get("entry") else "") + f["function"],
                 f"{f['file']}:{f['line']}") for f in res["frames"]],
               ["handle", "depth", "function", "where"])
    elif cmd == "ps":
        _table([(p.pid, p.version or "?", p.attachable, p.name, p.exe or p.cmdline or "")
                for p in res], ["pid", "python", "attachable", "name", "exe"])
    elif cmd in ("call", "set", "do", "result", "nudge"):
        print_job(res)
    elif cmd == "doc":
        print_doc(res)
    elif cmd == "diff":
        print_diff(res)
    elif cmd == "reload":
        print_reload(res)
    elif cmd == "snaps":
        if not res:
            print("no snapshots")
        _table([(x["name"], x["target"], x["values"], x["age_s"]) for x in res],
               ["name", "target", "values", "age s"], flex=1)
    elif cmd == "probes":
        if not res:
            print("no probes")
        _table([(p["id"], p["kind"], p.get("spec", ""), p.get("hits", ""), p.get("emitted", ""),
                 p.get("suppressed", ""), "on" if p.get("enabled") else
                 f"off: {p.get('disabled_reason')}") for p in res],
               ["id", "kind", "where", "hits", "shown", "dropped", "state"], flex=2)
    elif cmd == "src":
        print_src(res)
    elif cmd == "find":
        _line(f"{res['query']} via {res['via']}: {len(res['results'])} found, "
              f"{res['scanned']} objects scanned in {res['ms']} ms"
              + (" (truncated)" if res["truncated"] else ""))
        _table([(e.get("handle", ""), e.get("path") or e["name"], e["preview"])
                for e in res["results"]], ["handle", "where", "value"])
    elif cmd in ("pause", "resume", "step", "paused"):
        state = "PAUSED" if res.get("paused") else "running"
        extra = f", advanced {res['advanced']} frame(s)" if "advanced" in res else ""
        it = f" at iteration {res['iteration']}" if res.get("iteration") is not None else ""
        print(f"{state}{it}{extra}")
        for k in ("auto_resume_in_s", "note", "warning"):
            if k in res:
                print(f"  {k.replace('_', ' ')}: {res[k]}")
    elif cmd == "repeats":
        _table([(r["id"], "running" if r["running"] else "stopped", r["interval"], r["runs"],
                 r["errors"], r["target"]) for r in res],
               ["id", "state", "every s", "runs", "errors", "call"])
    elif cmd == "history":
        _table([(h["change"], h["target"], h["old"], h["new"], "undone" if h["undone"] else "")
                for h in res], ["change", "target", "old", "new", ""])
    elif cmd == "jobs":
        _table([(j["job"], j["state"], j["on"], j["blocked_ms"] if j["blocked_ms"] is not None
                 else "", j["label"]) for j in res], ["job", "state", "on", "blocked ms", "what"])
    elif cmd == "undo":
        for u in res["undone"]:
            extra = f"  error: {u['error']}" if "error" in u else ""
            print(f"change {u['change']}: {u['target']} -> {u.get('restored')} "
                  f"[{u.get('state', 'failed')}]{extra}")
    elif cmd == "sessions":
        if not res:
            print("no sessions")
        _table([(s["pid"], "alive" if s["alive"] else "dead", s.get("port"), s.get("agent"))
                for s in res], ["pid", "process", "port", "agent"])
    else:
        for k, v in res.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, indent=2)
            print(f"{k}: {v}")


# -- main ----------------------------------------------------------------------------------

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "ps":
            res = procs.list_python()
            if args.json:
                print(json.dumps([{**vars(p), "attachable": p.attachable} for p in res],
                                 indent=2))
            else:
                print_result("ps", res)
            return 0
        if args.cmd == "sessions":
            if args.clean:
                removed = client_mod.clean_stale()
                print(f"removed stale files for {len(removed)} process(es): {removed}")
            res = client_mod.list_sessions()
            print(json.dumps(res, indent=2) if args.json else "", end="")
            if not args.json:
                print_result("sessions", res)
            return 0
        opts = {k: getattr(args, k) for k in ATTACH_OPTS if getattr(args, k, None) is not None}
        cfg = config.load()
        if cfg["safety"].get("deny"):
            opts["deny"] = cfg["safety"]["deny"]
        c = client_mod.Client.attach(args.pid, **opts)
        info = c.attach_info
        if not info.get("reused"):
            note = f" (pid {info['launcher']} is a venv launcher)" if "launcher" in info else ""
            print(f"attached to {c.pid} in {info['attach_ms']} ms{note}", file=sys.stderr)
        for w in info.get("warnings", ()):
            print(f"exposure: warning: {w}", file=sys.stderr)
        with c:
            if args.cmd == "attach":
                res = c.scope()
            elif args.cmd == "ping":
                res = c.ping()
            elif args.cmd == "threads":
                res = c.threads()
            elif args.cmd == "stack":
                res = c.stack(args.thread, args.limit)
            elif args.cmd == "ls":
                target, pattern = split_ls_args(args.target, args.pattern)
                res = c.ls(target, args.limit, args.offset, pattern)
            elif args.cmd == "doc":
                res = c.doc(args.target)
            elif args.cmd == "src":
                res = c.src(args.target)
            elif args.cmd == "show":
                res = c.show(args.target)
            elif args.cmd == "scope":
                res = c.scope()
            elif args.cmd == "release":
                res = c.release(args.handles or None)
            elif args.cmd == "detach":
                res = c.detach()
            elif args.cmd == "call":
                res = c.call(args.expr, on=args.on, timeout=args.timeout,
                             capture=None if args.capture else False)
            elif args.cmd in ("eval", "exec"):
                if not args.yes and cfg["client"]["confirm"]:
                    if not _confirm(f"run in pid {c.pid}: {args.source!r}"):
                        print("not run", file=sys.stderr)
                        return 1
                res = c.eval(args.source, mode=args.cmd, frame=args.frame, on=args.on,
                             timeout=args.timeout, capture=None if args.capture else False,
                             confirmed=True)
                args.cmd = "call"   # print like a job
            elif args.cmd == "shell":
                from . import repl
                repl.run(c)
                return 0
            elif args.cmd == "log":
                if not args.yes and cfg["client"]["confirm"]:
                    if not _confirm(f"logpoint in pid {c.pid} evaluating {args.expr!r}"):
                        print("not added", file=sys.stderr)
                        return 1
                res = c.log(args.location, args.expr, when=args.when, every=args.every,
                            rate=args.rate, seconds=args.seconds, confirmed=True)
                print_probe(res)
                if args.follow:
                    return _follow(c, None)
                return 0
            elif args.cmd == "trace":
                res = c.trace(args.target, rate=args.rate, seconds=args.seconds)
                print_probe(res)
                if args.follow:
                    return _follow(c, None)
                return 0
            elif args.cmd == "break":
                needs = bool(args.when)
                if needs and not args.yes and cfg["client"]["confirm"]:
                    if not _confirm(f"breakpoint condition in pid {c.pid}: {args.when!r}"):
                        print("not added", file=sys.stderr)
                        return 1
                res = c.break_(args.location, when=args.when, keep=args.keep or None,
                               confirmed=needs)
                print_probe(res)
                return 0
            elif args.cmd == "logs":
                return _follow(c, 0 if args.all else None, args.seconds, args.count)
            elif args.cmd == "probes":
                res = c.probes()
            elif args.cmd == "snap":
                res = c.snap(args.name, args.target, depth=args.depth)
            elif args.cmd == "diff":
                res = c.diff(args.a, args.b)
            elif args.cmd == "snaps":
                res = c.snaps()
            elif args.cmd == "panel":
                from . import webpanel
                return webpanel.run(c, args.preset, args.port, not args.no_open)
            elif args.cmd == "reload":
                what = f"undo the last reload of {args.module or 'any module'}" if args.undo \
                    else f"re-run {args.module}'s source in pid {c.pid}"
                if not args.yes and cfg["client"]["confirm"] and not _confirm(what):
                    print("not done", file=sys.stderr)
                    return 1
                res = c.reload(args.module, undo=args.undo, values=args.values, on=args.on,
                               timeout=args.timeout, confirmed=True)
            elif args.cmd in ("unlog", "untrace", "unbreak", "unprobe"):
                res = c.unprobe(args.probe)
            elif args.cmd == "set":
                if args.value is None:
                    res = c.assign(args.target, on=args.on, timeout=args.timeout)
                else:
                    res = c.set(args.target, args.value, on=args.on, timeout=args.timeout)
            elif args.cmd == "do":
                res = c.do(args.action, *args.values, on=args.on, timeout=args.timeout)
            elif args.cmd == "undo":
                res = c.undo(args.n)
            elif args.cmd == "history":
                res = c.history(args.limit)
            elif args.cmd == "jobs":
                res = c.jobs()
            elif args.cmd == "result":
                res = c.result(args.job, args.timeout)
            elif args.cmd == "executors":
                res = c.executors()
            elif args.cmd == "frames":
                res = c.frames(args.n)
            elif args.cmd == "pause":
                res = c.pause(args.seconds)
            elif args.cmd == "resume":
                res = c.resume()
            elif args.cmd == "step":
                res = c.step(args.n)
            elif args.cmd == "paused":
                res = c.paused()
            elif args.cmd in ("watch", "tweak", "preset"):
                return _panel(c, args)
            elif args.cmd == "presets":
                res = c.presets()
            elif args.cmd == "nudge":
                d = int(args.delta) if args.delta.is_integer() else args.delta
                res = c.nudge(args.target, d, on=args.on, timeout=args.timeout)
            elif args.cmd == "find":
                res = c.find(args.query, via=args.via, limit=args.limit, depth=args.depth)
            elif args.cmd == "every":
                res = c.every(args.interval, args.expr, count=args.count, on=args.on)
            elif args.cmd == "repeats":
                res = c.repeats()
            elif args.cmd == "stop":
                res = c.stop(args.repeat)
            else:  # pragma: no cover
                raise SystemExit(f"unknown command {args.cmd}")
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print_result(args.cmd, res)
        return 0
    except client_mod.AttachError as exc:
        print(f"exposure: {exc}", file=sys.stderr)
        return 2
    except (exprs.ExprError, paths.PathError) as exc:
        print(f"exposure: {exc}", file=sys.stderr)
        return 2
    except client_mod.ConnectionLost as exc:
        print(f"exposure: {exc}", file=sys.stderr)
        return 3
    except client_mod.RemoteError as exc:
        print(f"exposure: {exc}", file=sys.stderr)
        if exc.traceback:
            print(exc.traceback, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
