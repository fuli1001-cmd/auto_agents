"""Lightweight relevance ranker for repo map files.

Score formula (per file):
    keyword_score  = sum(occurrences of any task keyword in path/symbol-name/docstring)
    reference_in   = how many other scanned files import this module
    anchor_bonus   = large fixed bonus if file is an anchor (forces inclusion)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .parser import FileSummary
from . import anchors as anchors_mod


@dataclass
class RankedFile:
    path: str
    score: float
    is_anchor: bool
    summary: FileSummary


def _path_to_module(path: str) -> str:
    """Convert ``src/auto_agents/foo/bar.py`` -> ``auto_agents.foo.bar`` (best effort)."""
    rel = path.replace("\\", "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = [p for p in rel.split("/") if p]
    # Drop common src layout prefix
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _build_reference_index(summaries: Sequence[FileSummary]) -> Dict[str, int]:
    """Count how many summaries import each scanned module (best-effort)."""
    module_to_path: Dict[str, str] = {}
    for s in summaries:
        mod = _path_to_module(s.path)
        if mod:
            module_to_path[mod] = s.path
    counts: Dict[str, int] = {s.path: 0 for s in summaries}
    for s in summaries:
        for imp in s.imports:
            # match exact or any prefix that resolves to a known module
            target_module = imp
            while target_module and target_module not in module_to_path:
                if "." not in target_module:
                    target_module = ""
                    break
                target_module = target_module.rsplit(".", 1)[0]
            if target_module and target_module in module_to_path:
                target_path = module_to_path[target_module]
                if target_path != s.path:
                    counts[target_path] = counts.get(target_path, 0) + 1
    return counts


def _keyword_score(summary: FileSummary, keywords: Sequence[str]) -> float:
    if not keywords:
        return 0.0
    lower_path = summary.path.lower()
    score = 0.0
    for kw in keywords:
        if not kw:
            continue
        if kw in lower_path:
            score += 3.0
    for sym in summary.symbols:
        sym_blob = (sym.name + " " + (sym.docstring or "")).lower()
        for kw in keywords:
            if kw and kw in sym_blob:
                score += 2.0
        for child in sym.children:
            child_blob = (child.name + " " + (child.docstring or "")).lower()
            for kw in keywords:
                if kw and kw in child_blob:
                    score += 1.0
    return score


class KeywordRanker:
    """Combines keyword matching, reference counts and anchor bonuses."""

    ANCHOR_BONUS = 10_000.0
    REFERENCE_WEIGHT = 0.5

    def rank(
        self,
        summaries: Sequence[FileSummary],
        *,
        keywords: Sequence[str] = (),
        anchor_paths: Sequence[str] = (),
    ) -> List[RankedFile]:
        ref_index = _build_reference_index(summaries)
        anchor_set: Set[str] = set()
        for anchor in anchor_paths:
            for s in summaries:
                if anchors_mod.matches_anchor(anchor, s.path):
                    anchor_set.add(s.path)

        ranked: List[RankedFile] = []
        for s in summaries:
            base = _keyword_score(s, [k.lower() for k in keywords])
            base += self.REFERENCE_WEIGHT * ref_index.get(s.path, 0)
            is_anchor = s.path in anchor_set
            if is_anchor:
                base += self.ANCHOR_BONUS
            ranked.append(RankedFile(path=s.path, score=base, is_anchor=is_anchor, summary=s))

        ranked.sort(key=lambda rf: (-rf.score, rf.path))
        return ranked
