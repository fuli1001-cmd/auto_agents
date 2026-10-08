"""Compatibility facade. Business execution is owned by control.Engine."""

from .control.compat import Orchestrator

__all__ = ["Orchestrator"]
