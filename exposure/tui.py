"""The terminal panel behind ``watch``, ``tweak`` and ``preset``.

One panel: watched paths sampled at a fixed rate (read on the agent thread),
plus optional tweak targets. A value that changed since the last refresh is
highlighted. With tweak targets: Up/Down nudge the selected one by its step
(Shift or PgUp/PgDn for x10), Tab/1-9 select, typing a value then Enter sets
it, ``u`` undoes the last change, ``p`` toggles pause, ``s`` steps one frame
while paused, ``q``/Esc/Ctrl+C quit. Every change goes through the agent's
main-loop executor and lands in ``history``.

Standard library only (msvcrt on Windows, termios elsewhere).
"""
from __future__ import annotations

import os
import sys
import time

CSI = "\x1b["
HIGHLIGHT = CSI + "1;33m"
DIM = CSI + "2m"
RESET = CSI + "0m"


def enable_vt() -> bool:
    """Turn on ANSI escape handling in a Windows console. True if usable."""
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


class Keys:
    """Non-blocking key reader yielding names: 'up', 'down', 'pgup', 'pgdn',
    'tab', 'enter', 'esc', 'backspace', or the character typed. Shift+arrow
    comes back as 'shift-up' / 'shift-down'."""

    def __enter__(self):
        if os.name != "nt" and sys.stdin.isatty():
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if os.name != "nt" and getattr(self, "_old", None) is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read(self):
        if os.name == "nt":
            return self._read_windows()
        return self._read_posix()

    @staticmethod
    def _shift_down():
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
        except Exception:
            return False

    def _read_windows(self):
        import msvcrt
        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            name = {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn"}.get(code)
            if name in ("up", "down") and self._shift_down():
                name = "shift-" + name
            return name
        return {"\r": "enter", "\t": "tab", "\x1b": "esc", "\x08": "backspace",
                "\x03": "ctrl-c"}.get(ch, ch)

    def _read_posix(self):
        import select
        if not sys.stdin.isatty() or not select.select([sys.stdin], [], [], 0)[0]:
            return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = ""
            while select.select([sys.stdin], [], [], 0.01)[0]:
                seq += sys.stdin.read(1)
            return {"[A": "up", "[B": "down", "[5~": "pgup", "[6~": "pgdn",
                    "[1;2A": "shift-up", "[1;2B": "shift-down", "": "esc"}.get(seq)
        return {"\n": "enter", "\r": "enter", "\t": "tab", "\x7f": "backspace",
                "\x03": "ctrl-c"}.get(ch, ch)


def _fmt_row(label, value, changed, selected, ansi, width=40):
    mark = ">" if selected else " "
    text = f"{mark} {label:<{width}} {value}"
    if ansi and changed:
        return f"{mark} {label:<{width}} {HIGHLIGHT}{value}{RESET}"
    return text


def run_panel(client, watch, tweak=(), *, rate=10.0, steps=None, count=None, plain=False,
              out=None):
    """Show the panel until quit (or ``count`` refreshes). Returns the last sample."""
    out = out or sys.stdout
    steps = dict(steps or {})
    ansi = (not plain) and enable_vt()
    paths = list(dict.fromkeys([*watch, *tweak]))
    period = 1.0 / max(0.1, float(rate))
    prev = {}
    selected = 0
    typed = ""
    status = ""
    drawn = 0
    n = 0
    last = None
    interactive = bool(tweak) and sys.stdin.isatty() and count is None
    keys = Keys() if interactive else None
    if keys:
        keys.__enter__()
    try:
        while count is None or n < count:
            t0 = time.perf_counter()
            last = client.sample(paths)
            rows = []
            paused = ""
            if tweak:
                try:
                    paused = "  [PAUSED]" if client.paused().get("paused") else ""
                except Exception:
                    paused = ""
            header = (f"exposure pid {client.pid}  {rate:g} Hz  "
                      f"{time.strftime('%H:%M:%S')}{paused}")
            rows.append(header)
            width = max([len(p) for p in paths] + [10])
            for v in last["values"]:
                p = v["path"]
                value = v.get("preview", v.get("error", "?"))
                changed = p in prev and prev[p] != value
                prev[p] = value
                sel = bool(tweak) and p == tweak[selected]
                label = p + (f"  (step {steps.get(p, 1)})" if p in tweak else "")
                rows.append(_fmt_row(label, value, changed, sel, ansi, width + 12))
            if tweak and interactive:
                rows.append(f"{DIM if ansi else ''}Up/Down nudge (Shift or PgUp/PgDn x10), "
                            "Tab select, type a value + Enter, u undo, p pause, s step, "
                            f"q quit{RESET if ansi else ''}")
                rows.append(f"value: {typed}" + (f"   {status}" if status else ""))
            if ansi and drawn:
                out.write(f"{CSI}{drawn}F{CSI}J")
            out.write("\n".join(rows) + "\n")
            out.flush()
            drawn = len(rows) if ansi else 0
            n += 1
            # keys, until the next refresh is due
            while True:
                if keys:
                    k = keys.read()
                    if k is not None:
                        quit_, typed, status, selected = _handle_key(
                            client, k, tweak, steps, selected, typed)
                        if quit_:
                            return last
                        break   # redraw at once
                left = period - (time.perf_counter() - t0)
                if left <= 0:
                    break
                time.sleep(min(left, 0.02))
    except KeyboardInterrupt:
        pass
    finally:
        if keys:
            keys.__exit__(None, None, None)
    return last


def _handle_key(client, k, tweak, steps, selected, typed):
    """Returns (quit, typed, status, selected)."""
    target = tweak[selected]
    step = steps.get(target, 1)
    status = ""
    try:
        if k in ("q", "esc", "ctrl-c"):
            return True, typed, status, selected
        if k == "tab":
            return False, "", status, (selected + 1) % len(tweak)
        if k.isdigit() and not typed and 1 <= int(k) <= len(tweak) and len(tweak) > 1:
            return False, "", status, int(k) - 1
        if k in ("up", "down", "shift-up", "shift-down", "pgup", "pgdn"):
            mult = 10 if k in ("shift-up", "shift-down", "pgup", "pgdn") else 1
            sign = 1 if k in ("up", "shift-up", "pgup") else -1
            r = client.nudge(target, sign * mult * step)
            status = f"{r.get('old')} -> {r.get('new')}" if "new" in r else r.get("error", "")
        elif k == "enter" and typed:
            r = client.set(target, typed)
            status = f"{r.get('old')} -> {r.get('new')}" if "new" in r else r.get("error", "")
            typed = ""
        elif k == "backspace":
            typed = typed[:-1]
        elif k == "u" and not typed:
            res = client.undo(1)["undone"]
            status = f"undid {res[0]['target']}" if res else "nothing to undo"
        elif k == "p" and not typed:
            status = "resumed" if client.paused().get("paused") else "paused"
            client.resume() if status == "resumed" else client.pause()
        elif k == "s" and not typed:
            r = client.step(1)
            status = f"stepped {r.get('advanced')} frame(s)"
        elif len(k) == 1 and k.isprintable():
            typed += k
    except Exception as exc:
        status = f"{type(exc).__name__}: {exc}"
    return False, typed, status, selected
