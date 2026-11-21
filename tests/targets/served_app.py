"""Test target: starts Exposure itself with exposure.serve() (strict mode).

    python served_app.py POLICY [AUDIT_LOG]

Meant to run with PYTHON_DISABLE_REMOTE_DEBUG=1, so nothing can be injected.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import exposure  # noqa: E402


class Config:
    def __init__(self):
        self.debug = False
        self.retries = 3


def restart(n=1):
    return f"restarted {n}"


def main():
    config = Config()
    secret = "not exposed"
    exposure.expose(config, name="config", writable=["debug"])
    exposure.expose(restart, name="restart")
    audit = sys.argv[2] if len(sys.argv) > 2 else None
    exposure.serve(policy=sys.argv[1], audit_log=audit)
    print("ready", flush=True)
    end = time.monotonic() + 60
    while time.monotonic() < end:
        time.sleep(0.01)
    return secret


if __name__ == "__main__":
    main()
