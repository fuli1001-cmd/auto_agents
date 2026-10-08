"""Compatibility facade. Business execution is owned by control.Engine."""

from .control.compat import WorkflowCoordinator

__all__ = ["WorkflowCoordinator"]
