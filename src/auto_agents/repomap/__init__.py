"""Aider-inspired repository map: token-budgeted code overview injected into prompts.

Public API:
    RepoMapConfig         - dataclass configuration (mirrored in ProjectConfig)
    RepoMapBuilder        - main entry point used by orchestrator/session
    RepoMapResult         - return value carrying text + metrics
    estimate_tokens(text) - cheap token estimator (chars // 4)
"""
from __future__ import annotations

from .config import RepoMapConfig, DEFAULT_INCLUDE_GLOBS, DEFAULT_EXCLUDE_GLOBS

__all__ = [
    "RepoMapConfig",
    "DEFAULT_INCLUDE_GLOBS",
    "DEFAULT_EXCLUDE_GLOBS",
]


def __getattr__(name: str):
    # Lazy re-export so importing the package never fails when
    # building the module incrementally.
    if name in {"RepoMapBuilder", "RepoMapResult", "estimate_tokens"}:
        from . import builder as _builder  # noqa: WPS433
        return getattr(_builder, name)
    raise AttributeError(name)
