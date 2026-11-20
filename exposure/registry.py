"""In-code explicit scope: ``exposure.expose``.

A target that wants to name what Exposure may see (and later write or call)
imports this and registers objects. The agent picks the registry up if the
module is loaded. Standard library only; importing it starts nothing.

    import exposure

    @exposure.expose
    def restart_worker(n: int) -> bool: ...

    exposure.expose(config, name="config", writable=["debug", "retries"])
    exposure.expose(queue, name="queue", read_only=True)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

_lock = threading.Lock()


@dataclass
class Exposed:
    name: str
    obj: Any
    read: bool = True
    write: bool | list[str] = False
    call: bool | list[str] = False
    source: str = "code"
    extra: dict = field(default_factory=dict)


_registry: dict[str, Exposed] = {}


def _default_name(obj) -> str:
    mod = getattr(obj, "__module__", None)
    qual = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if qual is None:
        raise ValueError("expose() needs name= for objects without __qualname__")
    return f"{mod}.{qual}" if mod and mod != "__main__" else qual


def expose(obj=None, *, name: str | None = None, read_only: bool = False,
           writable: bool | list[str] | None = None,
           callable_: bool | list[str] | None = None):
    """Register ``obj`` for Exposure's explicit scope.

    Works as a plain call or as a decorator (with or without arguments).
    Functions and other callables default to ``call=True``; ``read_only``
    turns writes and calls off whatever else is passed.
    """
    def register(target):
        key = name or _default_name(target)
        call = callable_ if callable_ is not None else callable(target)
        write = writable if writable is not None else False
        if read_only:
            call, write = False, False
        if isinstance(write, (list, tuple)):
            write = list(write)
        if isinstance(call, (list, tuple)):
            call = list(call)
        with _lock:
            _registry[key] = Exposed(key, target, True, write, call)
        return target

    if obj is None:
        return register
    return register(obj)


def unexpose(name: str) -> None:
    with _lock:
        _registry.pop(name, None)


def snapshot() -> dict[str, Exposed]:
    with _lock:
        return dict(_registry)
