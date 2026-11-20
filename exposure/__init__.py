"""Exposure: inspect (and, from M2, control) a running Python 3.14+ process.

Importing this package in a target is optional. It gives ``exposure.expose``
for the explicit scope and starts nothing.
"""
from .registry import expose, unexpose

__version__ = "0.1.0"
__all__ = ["expose", "unexpose", "__version__"]
