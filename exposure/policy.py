"""``exposure.toml``: the policy file form of the explicit scope.

    [agent]
    scope = "both"              # reachable | explicit | both
    entry = "App.run"           # entry frame (qualname, or module:qualname)
    pressure = "<entry>:self.backend._xruns"   # counter the agent backs off on

    [expose]
    "app.main:config"       = { read = true, write = ["debug", "retries"] }
    "app.workers"           = { read = true, call = ["restart"] }
    "<entry>:self.patch"    = { read = true }

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
    return Policy(path=path, scope=scope, entry=agent.get("entry"),
                  pressure=agent.get("pressure"), rules=rules, agent=dict(agent),
                  actions=actions)


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
