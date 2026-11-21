"""Per-user client settings: ``config.toml``.

Found at ``$EXPOSURE_CONFIG``, else ``%APPDATA%\\exposure\\config.toml`` on
Windows or ``~/.config/exposure/config.toml`` elsewhere. Everything is
optional:

    [client]
    confirm = true          # ask before every eval/exec (false = never ask)
    rate = 10               # default watch/tweak refresh rate (Hz)
    history = 1000          # shell history lines kept

    [safety]
    deny = ["os.system", "subprocess.*", "shutil.rmtree"]   # sent to the agent

Client side only (tomllib is stdlib).
"""
from __future__ import annotations

import os
import sys
import tomllib

DEFAULTS = {"client": {"confirm": True, "rate": 10.0, "history": 1000},
            "safety": {"deny": []}}


def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return os.path.join(base, "exposure")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "exposure")


def config_path() -> str:
    return os.environ.get("EXPOSURE_CONFIG") or os.path.join(config_dir(), "config.toml")


def history_path() -> str:
    return os.path.join(config_dir(), "history")


def load() -> dict:
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    path = config_path()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return out
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"exposure: ignoring {path}: {exc}", file=sys.stderr)
        return out
    for section in ("client", "safety"):
        if isinstance(data.get(section), dict):
            out[section].update(data[section])
    return out
