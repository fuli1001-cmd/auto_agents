"""RepoMapBuilder: top-level orchestrator for the repo map pipeline.

Pipeline:
    detector -> file discovery -> cache (parser) -> ranker -> render under budget

Public:
    estimate_tokens(text)  cheap chars/4 estimator
    RepoMapResult          dataclass returned to callers
    RepoMapBuilder.build(task, *, budget_tokens) -> RepoMapResult
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .anchors import (
    extract_anchor_paths,
    extract_dotted_names,
    extract_keywords,
)
from .cache import RepoMapCache
from .config import RepoMapConfig
from .detector import is_python_project
from .parser import BaseParser, FileSummary, PythonAstParser, Symbol
from .ranker import KeywordRanker, RankedFile


HEADER = (
    "## Repo Map (partial, ranked by relevance to current task)\n"
    "This is a token-budgeted view of the repository structure. "
    "If you need a symbol that is not listed here, use grep/view to look it up.\n"
)


def estimate_tokens(text: str) -> int:
    """Cheap token estimator: roughly chars/4 (matches OpenAI rule of thumb)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass
class RepoMapResult:
    text: str = ""
    enabled: bool = True
    skipped_reason: Optional[str] = None
    files_included: int = 0
    tokens_actual: int = 0
    tokens_budget: int = 0
    cache_hit: bool = False

    def to_metrics(self) -> Dict[str, object]:
        return {
            "repo_map_enabled": self.enabled,
            "repo_map_skipped_reason": self.skipped_reason,
            "repo_map_files_included": self.files_included,
            "repo_map_tokens_actual": self.tokens_actual,
            "repo_map_tokens_budget": self.tokens_budget,
            "repo_map_cache_hit": self.cache_hit,
        }


def _matches_globs(rel_path: str, patterns: Sequence[str]) -> bool:
    rel = rel_path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        # also match against any path tail (handles "**/*.py" intent)
        if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
            return True
    return False


def _discover_files(
    project_root: Path,
    config: RepoMapConfig,
) -> List[str]:
    out: List[str] = []
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(project_root).as_posix()
        except ValueError:
            continue
        if _matches_globs(rel, config.exclude_globs):
            continue
        if config.include_globs and not _matches_globs(rel, config.include_globs):
            continue
        out.append(rel)
        if len(out) >= config.max_files_scanned:
            break
    out.sort()
    return out


def _render_symbol(sym: Symbol, indent: str = "  ") -> List[str]:
    lines: List[str] = []
    if sym.kind == "class":
        lines.append(f"{indent}{sym.signature}")
        if sym.docstring:
            lines.append(f"{indent}  # {sym.docstring}")
        for child in sym.children:
            lines.append(f"{indent}    {child.signature}")
    else:
        lines.append(f"{indent}{sym.signature}")
        if sym.docstring:
            lines.append(f"{indent}  # {sym.docstring}")
    return lines


def _render_file(summary: FileSummary, *, anchor: bool) -> str:
    lines: List[str] = [f"{summary.path}"]
    if summary.parse_error:
        lines.append(f"  # parse_error: {summary.parse_error}")
        return "\n".join(lines) + "\n"
    if not summary.symbols:
        lines.append("  # (no top-level symbols)")
        return "\n".join(lines) + "\n"
    for sym in summary.symbols:
        lines.extend(_render_symbol(sym))
    return "\n".join(lines) + "\n"


def _render_file_compact(summary: FileSummary) -> str:
    """Path-only line - used as low-cost filler when budget is tight."""
    if summary.parse_error:
        return f"{summary.path}  # parse_error\n"
    names = []
    for sym in summary.symbols[:6]:
        names.append(sym.name)
    if names:
        return f"{summary.path}  # {', '.join(names)}\n"
    return f"{summary.path}\n"


class RepoMapBuilder:
    """Build the injected repo map text for a given task and stage."""

    def __init__(
        self,
        project_root: Path,
        config: RepoMapConfig,
        *,
        parser: Optional[BaseParser] = None,
        cache: Optional[RepoMapCache] = None,
        ranker: Optional[KeywordRanker] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config
        self.parser = parser or PythonAstParser()
        self.cache = cache or RepoMapCache(self.project_root)
        self.ranker = ranker or KeywordRanker()

    def build(
        self,
        task: object,
        *,
        budget_tokens: Optional[int] = None,
        extra_anchor_paths: Sequence[str] = (),
    ) -> RepoMapResult:
        budget = (
            int(budget_tokens) if budget_tokens is not None and budget_tokens > 0
            else self.config.budget_tokens
        )
        if not self.config.enabled:
            return RepoMapResult(
                text="",
                enabled=False,
                skipped_reason="disabled",
                tokens_budget=budget,
            )

        eligible, reason = is_python_project(self.project_root)
        if not eligible:
            return RepoMapResult(
                text="",
                enabled=True,
                skipped_reason=reason,
                tokens_budget=budget,
            )

        rel_paths = _discover_files(self.project_root, self.config)
        if not rel_paths:
            return RepoMapResult(
                text="",
                enabled=True,
                skipped_reason="no_files_after_filters",
                tokens_budget=budget,
            )

        summaries = self.cache.get_or_build(rel_paths, self.parser)
        cache_hit = bool(self.cache.last_hit)

        anchor_paths = list(extract_anchor_paths(task))
        anchor_paths.extend(str(p) for p in extra_anchor_paths if p)
        keywords = list(extract_keywords(task))
        # Promote dotted names: their last segment is often a class/function name
        for dotted in extract_dotted_names(task):
            tail = dotted.rsplit(".", 1)[-1].lower()
            if len(tail) >= 3 and tail not in keywords:
                keywords.append(tail)

        ranked = self.ranker.rank(summaries, keywords=keywords, anchor_paths=anchor_paths)

        text, files_included, tokens = self._fit_budget(ranked, budget)

        return RepoMapResult(
            text=text,
            enabled=True,
            skipped_reason=None,
            files_included=files_included,
            tokens_actual=tokens,
            tokens_budget=budget,
            cache_hit=cache_hit,
        )

    def _fit_budget(
        self,
        ranked: Sequence[RankedFile],
        budget: int,
    ) -> tuple:
        if not ranked:
            return "", 0, 0
        body_lines: List[str] = []
        used_tokens = estimate_tokens(HEADER)
        files_included = 0

        # Phase 1: anchors must always fit (full signatures)
        for rf in ranked:
            if not rf.is_anchor:
                continue
            block = _render_file(rf.summary, anchor=True)
            cost = estimate_tokens(block)
            body_lines.append(block)
            used_tokens += cost
            files_included += 1

        # Phase 2: fill remaining budget with full signatures of top-ranked files
        remaining = [rf for rf in ranked if not rf.is_anchor]
        for rf in remaining:
            block = _render_file(rf.summary, anchor=False)
            cost = estimate_tokens(block)
            if used_tokens + cost > budget:
                continue
            body_lines.append(block)
            used_tokens += cost
            files_included += 1

        # Phase 3: fill leftover budget with compact path lines
        for rf in remaining:
            if any(line.startswith(rf.summary.path + "\n") or line.startswith(rf.summary.path + " ")
                   for line in body_lines):
                continue
            block = _render_file_compact(rf.summary)
            cost = estimate_tokens(block)
            if used_tokens + cost > budget:
                break
            body_lines.append(block)
            used_tokens += cost
            files_included += 1

        text = HEADER + "\n" + "".join(body_lines)
        return text.rstrip() + "\n", files_included, used_tokens
