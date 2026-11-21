"""Test target: threads start and finish all the time (stress tests)."""
import sys
import threading
import time

made = [0]


def short_job(n):
    total = 0
    for i in range(200):
        total += i * n
    time.sleep(0.001)
    return total


def main():
    seconds = float(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 60
    print("ready", flush=True)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        threads = [threading.Thread(target=short_job, args=(i,), name=f"churn-{made[0] + i}")
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        made[0] += len(threads)


if __name__ == "__main__":
    main()
