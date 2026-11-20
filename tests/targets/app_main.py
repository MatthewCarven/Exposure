"""Test target: a main() that holds a few kinds of state and loops.

    python app_main.py [--expose] [--seconds N]

Prints "ready" once main() is looping. With --expose it imports exposure
(from the repo checkout) and registers two objects for the explicit scope.
"""
import os
import sys
import threading
import time


class Config:
    def __init__(self):
        self.debug = False
        self.retries = 3
        self.name = "demo"


class Slotted:
    __slots__ = ("a", "b")


class Counter:
    reads = 0

    @property
    def value(self):  # must never run while being inspected
        Counter.reads += 1
        return 42

    def __repr__(self):  # must never run either
        Counter.reads += 1000
        return "Counter()"


class LoudDict(dict):
    def items(self):  # overridden: the agent must not call it
        Counter.reads += 1_000_000
        return super().items()


def worker(stop):
    while not stop.is_set():
        time.sleep(0.01)


def main():
    seconds = 60.0
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    config = Config()
    items = [1, "two", {"k": 3}, (4, 5)]
    table = {"a": 1, (1, 2): "tuple key", 3: [7, 8]}
    counter = Counter()
    slotted = Slotted()
    slotted.a = 1
    loud = LoudDict(x=1)
    secret = "do-not-show-in-explicit"
    stop = threading.Event()
    t = threading.Thread(target=worker, args=(stop,), name="worker-1", daemon=True)
    t.start()
    if "--expose" in sys.argv:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))))
        import exposure
        exposure.expose(config, name="config", writable=["debug"])
        exposure.expose(items, name="items", read_only=True)
    tick = 0
    print("ready", flush=True)
    if "--block" in sys.argv:   # one long C-level sleep: no bytecode runs
        time.sleep(float(sys.argv[sys.argv.index("--block") + 1]))
        print("woke", flush=True)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        tick += 1
        time.sleep(0.01)
    stop.set()


if __name__ == "__main__":
    main()
