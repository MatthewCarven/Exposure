"""Test target: a game-style ``while running:`` main loop.

    python game_loop.py [--seconds N] [--policy-dir]

Prints "ready" once the loop runs. Objects and functions here are the
things M2's call/set tests poke at.
"""
import sys
import threading
import time

settings = {"volume": 0.5, "difficulty": "normal"}
calls = []


class Player:
    __slots__ = ("hp", "speed", "pos", "_name")

    def __init__(self):
        self.hp = 100
        self.speed = 1.5
        self.pos = [0.0, 0.0]
        self._name = "p1"

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        calls.append(("name.setter", value))
        self._name = str(value).upper()

    def heal(self, amount, *, cap=100):
        self.hp = min(cap, self.hp + amount)
        calls.append(("heal", threading.current_thread().name))
        return self.hp


def make_counter():
    count = 0

    def bump(by=1):
        nonlocal count
        count += by
        return count

    return bump


def where():
    return threading.current_thread().name


def slow(seconds):
    time.sleep(seconds)
    return "slept"


def boom():
    raise ValueError("kaboom")


def blocked_by_audit():   # the --audit hook refuses calls to this one
    calls.append(("blocked_by_audit ran",))
    return "ran"


audit_log = []


def _audit(event, args):
    if event.startswith("exposure."):
        audit_log.append((event, args[0] if args else None))
        if args and args[0] == "blocked_by_audit":
            raise RuntimeError("audit hook says no")


def main():
    if "--audit" in sys.argv:
        sys.addaudithook(_audit)
    seconds = 60.0
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    player = Player()
    bump = make_counter()
    mode = "normal"
    turbo_frames = 0
    frame = 0
    end = time.monotonic() + seconds
    print("ready", flush=True)
    while time.monotonic() < end:
        frame += 1
        player.pos[0] += player.speed
        if mode == "turbo":
            turbo_frames += 1
        time.sleep(0.004)


if __name__ == "__main__":
    main()
