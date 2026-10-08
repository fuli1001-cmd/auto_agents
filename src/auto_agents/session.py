"""Compatibility facade. Business execution is owned by control.Engine."""

from .control.compat import Session

__all__ = ["Session"]
