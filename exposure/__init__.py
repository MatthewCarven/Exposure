"""Exposure: inspect and control a running Python 3.14+ process.

Importing this package in a target is optional. It gives ``exposure.expose``
for the explicit scope and ``exposure.serve`` for strict mode, and starts
nothing by itself.
"""
from .registry import expose, unexpose

__version__ = "0.1.0"
__all__ = ["expose", "unexpose", "serve", "__version__"]


def serve(policy=None, *, strict=True, entry=None, scope=None, audit_log=None,
          pressure=None, read_only=False):
    """Start the Exposure agent from inside this program (strict mode).

    Use it when attach-by-PID isn't wanted or isn't possible, e.g. a program
    started with ``PYTHON_DISABLE_REMOTE_DEBUG=1`` or ``-X disable-remote-debug``
    so that nothing can be injected into it. Clients find it through the same
    session file and connect with the same CLI.

    With ``strict=True`` (the default) the policy is loaded here, at startup,
    and a client can't change it; ``eval``/``exec`` are disabled outright;
    the scope comes from the policy (default ``explicit``: only what the policy
    or ``exposure.expose`` lists). A client's ``detach`` ends its session but
    the agent keeps serving. Returns the agent; calling it again is a no-op.
    """
    import os

    from . import agent as _agent
    from . import protocol
    config = {"served": True, "strict": bool(strict),
              "error_file": protocol.error_path(os.getpid())}
    if policy:
        config["policy"] = os.path.abspath(policy)
    for key, value in (("entry", entry), ("scope", scope), ("audit_log", audit_log),
                       ("pressure", pressure)):
        if value:
            config[key] = value
    if read_only:
        config["read_only"] = True
    return _agent.start(config)
