"""Athena solver internals.

`baseline.my_new_algorithm` remains the public compatibility shim; this package
contains the split implementation modules.
"""
from __future__ import annotations

from .entrypoint import algorithm

__all__ = ["algorithm"]
