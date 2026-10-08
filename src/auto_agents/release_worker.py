"""Deferred verification facade. Background workers never repair source."""

from .control.release import ensure_worker as ensure_release_worker
from .control.release import worker as run_release_worker

__all__ = ["ensure_release_worker", "run_release_worker"]
