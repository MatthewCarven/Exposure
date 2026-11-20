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
import sys

from . import client as client_mod
from . import procs

ATTACH_OPTS = ("entry", "policy", "scope", "pressure", "duty", "wait")


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
    return p


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
    s.add_argument("--limit", type=int)
    s.add_argument("--offset", type=int)
    s = sub.add_parser("show", parents=[parent])
    s.add_argument("target")
    sub.add_parser("scope", parents=[parent])
    s = sub.add_parser("release", parents=[parent])
    s.add_argument("handles", nargs="*")
    sub.add_parser("detach", parents=[parent])
    return ap


# -- printing -------------------------------------------------------------------------

def _table(rows, headers):
    if not rows:
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = min(max(widths[i], len(str(c))), 70)
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*[str(c)[:70] for c in r]).rstrip())


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
            print(f"entry {e['function']} {e['handle']} ({e['file']}:{e['line']}, "
                  f"thread {e['thread']}{more})  scope: {res['scope']}")
        elif res.get("entry_missing"):
            print(f"entry {res['entry_missing']!r} not on any stack  scope: {res['scope']}")
    else:
        print(f"{res['target']}  {res['preview']}")
    rows = [(e.get("handle", ""), e["name"], e["kind"], e["preview"], _perm_str(e.get("perm")))
            for e in res["entries"]]
    _table(rows, ["handle", "name", "kind", "value", "perm"])
    if res.get("more"):
        nxt = res["offset"] + len(res["entries"])
        print(f"... {res['total'] - nxt} more (use --offset {nxt})")


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
                res = c.ls(args.target, args.limit, args.offset)
            elif args.cmd == "show":
                res = c.show(args.target)
            elif args.cmd == "scope":
                res = c.scope()
            elif args.cmd == "release":
                res = c.release(args.handles or None)
            elif args.cmd == "detach":
                res = c.detach()
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
    except client_mod.RemoteError as exc:
        print(f"exposure: {exc}", file=sys.stderr)
        if exc.traceback:
            print(exc.traceback, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
