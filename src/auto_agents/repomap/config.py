"""Configuration dataclass for the RepoMap feature."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


DEFAULT_INCLUDE_GLOBS: List[str] = ["**/*.py"]
DEFAULT_EXCLUDE_GLOBS: List[str] = [
    ".auto-agents/**",
    ".conda/**",
    ".conda-pkgs/**",
    ".venv/**",
    "venv/**",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "tests/**",
    "test/**",
    "build/**",
    "dist/**",
    ".git/**",
]


@dataclass
class RepoMapConfig:
    """Aider-inspired repo map injection settings."""

    enabled: bool = True
    budget_tokens: int = 1500              # implement stage budget
    review_budget_tokens: int = 750        # review / fix stage budget
    max_files_scanned: int = 2000          # safety cap
    include_globs: List[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_GLOBS))
    exclude_globs: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RepoMapConfig":
        if not isinstance(data, dict):
            data = {}
        include = data.get("include_globs")
        exclude = data.get("exclude_globs")
        return cls(
            enabled=bool(data.get("enabled", True)),
            budget_tokens=int(data.get("budget_tokens", 1500)),
            review_budget_tokens=int(data.get("review_budget_tokens", 750)),
            max_files_scanned=int(data.get("max_files_scanned", 2000)),
            include_globs=(
                [str(g) for g in include] if isinstance(include, list) and include
                else list(DEFAULT_INCLUDE_GLOBS)
            ),
            exclude_globs=(
                [str(g) for g in exclude] if isinstance(exclude, list) and exclude
                else list(DEFAULT_EXCLUDE_GLOBS)
            ),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "budget_tokens": self.budget_tokens,
            "review_budget_tokens": self.review_budget_tokens,
            "max_files_scanned": self.max_files_scanned,
            "include_globs": list(self.include_globs),
            "exclude_globs": list(self.exclude_globs),
        }
