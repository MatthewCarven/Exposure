"""``exposure.toml``: the policy file form of the explicit scope.

    [agent]
    scope = "both"              # reachable | explicit | both
    entry = "App.run"           # entry frame (qualname, or module:qualname)
    pressure = "<entry>:self.backend._xruns"   # counter the agent backs off on

    [expose]
    "app.main:config"       = { read = true, write = ["debug", "retries"] }
    "app.workers"           = { read = true, call = ["restart"] }
    "<entry>:self.patch"    = { read = true }

``[safety]`` holds ``deny`` (patterns refused as call targets) and ``eval``
(allow eval/exec outside the reachable scope).

A key is ``module:attr.path``. ``<entry>`` as the module means "the entry
frame's locals". Without a colon, the longest dotted prefix that is an
already-imported module is the module (nothing is ever imported to resolve a
policy). Permissions are ``read``, ``write`` and ``call``: each true/false or
a list of attribute/method names. M1 enforces ``read`` only; ``write`` and
``call`` are parsed and reported.

Standard library only (tomllib).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from . import paths

ENTRY = "<entry>"
SCOPES = ("reachable", "explicit", "both")


class PolicyError(ValueError):
    pass


@dataclass
class Rule:
    key: str
    module: str | None          # None: resolve by longest imported prefix
    root: str                   # first name after the module
    segments: list = field(default_factory=list)
    read: bool = True
    write: bool | list[str] = False
    call: bool | list[str] = False


@dataclass
class Policy:
    path: str | None = None
    scope: str | None = None
    entry: str | None = None
    pressure: str | None = None
    rules: list[Rule] = field(default_factory=list)
    agent: dict = field(default_factory=dict)
    actions: dict = field(default_factory=dict)   # name -> {"call": path, "on": ...}
    presets: dict = field(default_factory=dict)   # name -> {"watch": [...], "tweak": [...]}
    safety: dict = field(default_factory=dict)    # {"deny": [patterns], "eval": bool}


def _perm(value, key, what):
    if isinstance(value, bool):
        return value
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise PolicyError(f"{key}: {what} must be true/false or a list of names")


def parse_rule(key: str, spec) -> Rule:
    if isinstance(spec, bool):
        spec = {"read": spec}
    if not isinstance(spec, dict):
        raise PolicyError(f"{key}: expected a table like {{ read = true }}")
    unknown = set(spec) - {"read", "write", "call"}
    if unknown:
        raise PolicyError(f"{key}: unknown permission(s) {sorted(unknown)}")
    if ":" in key:
        module, _, rest = key.partition(":")
        module = module.strip() or None
    else:
        module, rest = None, key
    try:
        (kind, root), segs = paths.parse(rest)
    except paths.PathError as exc:
        raise PolicyError(f"{key}: {exc}") from None
    if kind != "name":
        raise PolicyError(f"{key}: handles can't be used in a policy")
    for k, v in segs:
        if k == "attr" and paths.is_dunder(v):
            raise PolicyError(f"{key}: dunder names can't be exposed")
    return Rule(key=key, module=module, root=root, segments=segs,
                read=_perm(spec.get("read", True), key, "read"),
                write=_perm(spec.get("write", False), key, "write"),
                call=_perm(spec.get("call", False), key, "call"))


def parse(data: dict, path: str | None = None) -> Policy:
    agent = data.get("agent", {}) or {}
    if not isinstance(agent, dict):
        raise PolicyError("[agent] must be a table")
    scope = agent.get("scope")
    if scope is not None and scope not in SCOPES:
        raise PolicyError(f"[agent] scope must be one of {SCOPES}")
    expose = data.get("expose", {}) or {}
    if not isinstance(expose, dict):
        raise PolicyError("[expose] must be a table")
    rules = [parse_rule(k, v) for k, v in expose.items()]
    actions = {}
    raw_actions = data.get("actions", {}) or {}
    if not isinstance(raw_actions, dict):
        raise PolicyError("[actions] must be a table")
    for name, spec in raw_actions.items():
        if isinstance(spec, str):
            spec = {"call": spec}
        if not isinstance(spec, dict) or not isinstance(spec.get("call"), str):
            raise PolicyError(f"[actions] {name}: needs call = \"path.to.callable\"")
        on = spec.get("on")
        if on is not None and on not in ("main", "agent") and not str(on).startswith("thread:"):
            raise PolicyError(f"[actions] {name}: on must be main, agent or thread:NAME")
        try:
            paths.parse(spec["call"])
        except paths.PathError as exc:
            raise PolicyError(f"[actions] {name}: {exc}") from None
        actions[name] = {"call": spec["call"], "on": on}
    presets = {}
    raw_presets = data.get("presets", {}) or {}
    if not isinstance(raw_presets, dict):
        raise PolicyError("[presets] must be a table of [presets.NAME] tables")
    for name, spec in raw_presets.items():
        if not isinstance(spec, dict):
            raise PolicyError(f"[presets.{name}] must be a table")
        unknown = set(spec) - {"watch", "tweak", "rate", "step"}
        if unknown:
            raise PolicyError(f"[presets.{name}]: unknown key(s) {sorted(unknown)}")
        preset = {}
        for key in ("watch", "tweak"):
            items = spec.get(key, [])
            if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
                raise PolicyError(f"[presets.{name}] {key} must be a list of paths")
            preset[key] = list(items)
        if "rate" in spec:
            if not isinstance(spec["rate"], (int, float)) or spec["rate"] <= 0:
                raise PolicyError(f"[presets.{name}] rate must be a positive number (Hz)")
            preset["rate"] = float(spec["rate"])
        if "step" in spec:
            steps = spec["step"]
            if not isinstance(steps, dict) or not all(
                    isinstance(v, (int, float)) for v in steps.values()):
                raise PolicyError(f"[presets.{name}] step must map paths to numbers")
            preset["step"] = dict(steps)
        presets[name] = preset
    safety = parse_safety(data.get("safety", {}) or {})
    return Policy(path=path, scope=scope, entry=agent.get("entry"),
                  pressure=agent.get("pressure"), rules=rules, agent=dict(agent),
                  actions=actions, presets=presets, safety=safety)


def parse_safety(spec) -> dict:
    """[safety]: ``deny`` (fnmatch patterns on call targets, e.g. "os.system",
    "subprocess.*") and ``eval`` (allow eval/exec outside the reachable scope)."""
    if not isinstance(spec, dict):
        raise PolicyError("[safety] must be a table")
    unknown = set(spec) - {"deny", "eval"}
    if unknown:
        raise PolicyError(f"[safety]: unknown key(s) {sorted(unknown)}")
    deny = spec.get("deny", [])
    if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
        raise PolicyError("[safety] deny must be a list of patterns")
    ev = spec.get("eval", False)
    if not isinstance(ev, bool):
        raise PolicyError("[safety] eval must be true or false")
    return {"deny": list(deny), "eval": ev}


def load(path: str) -> Policy:
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise PolicyError(f"{path}: {exc}") from None
    return parse(data, os.path.abspath(path))


def find_default(candidates) -> str | None:
    """First ``exposure.toml`` found in the given directories."""
    for d in candidates:
        if not d:
            continue
        p = os.path.join(d, "exposure.toml")
        if os.path.isfile(p):
            return p
    return None
