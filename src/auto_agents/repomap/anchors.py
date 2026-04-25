"""Anchor extraction: pull explicit file paths and symbol references from a task.

Anchors are forced into the repo map regardless of ranker score so that a task
that explicitly names ``src/foo/bar.py`` always gets that file's full signature
list in the injected prompt.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Set


# matches things like  src/foo/bar.py   ./pkg/baz.py   tests/test_x.py
_PATH_RE = re.compile(r"(?<![\w./-])((?:\./)?(?:[\w.-]+/)+[\w.-]+\.py)\b")
# matches dotted names like  auto_agents.session.Session  or pkg.mod
_DOTTED_RE = re.compile(r"\b([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*){1,5})\b")
# bare filenames like foo.py mentioned without a path prefix
_FILENAME_RE = re.compile(r"\b([\w-]+\.py)\b")


def _gather_text(task: object) -> str:
    """Collect any plausible task description text into one blob."""
    if task is None:
        return ""
    if isinstance(task, str):
        return task
    parts: List[str] = []
    for attr in ("title", "description", "acceptance", "scope_boundaries", "commit_message"):
        value = getattr(task, attr, None)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value if item is not None)
    return "\n".join(parts)


def extract_anchor_paths(task: object) -> List[str]:
    """Return relative POSIX paths mentioned in the task description.

    The list is deduped while preserving first-seen order.
    """
    text = _gather_text(task)
    seen: Set[str] = set()
    out: List[str] = []
    for match in _PATH_RE.findall(text):
        norm = match.lstrip("./")
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    # also catch bare filenames - they will be matched against any file basename
    for match in _FILENAME_RE.findall(text):
        if match in seen:
            continue
        if "/" in match:
            continue
        seen.add(match)
        out.append(match)
    return out


def extract_dotted_names(task: object) -> List[str]:
    """Return dotted module/symbol names mentioned in the task text."""
    text = _gather_text(task)
    seen: Set[str] = set()
    out: List[str] = []
    for match in _DOTTED_RE.findall(text):
        # filter out things like 'self.foo' / numeric versions / file paths
        if match.startswith("self.") or match.startswith("cls."):
            continue
        if any(ch.isdigit() for ch in match.split(".")[0]):
            continue
        if match in seen:
            continue
        seen.add(match)
        out.append(match)
    return out


_STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "from", "into", "this", "that",
    "task", "tasks", "config", "configure", "configuration",
    "implement", "implementation", "review", "verify", "verification",
    "should", "must", "will", "have", "has", "use", "using", "support",
    "feature", "features", "test", "tests", "code", "file", "files",
    "function", "functions", "method", "methods", "class", "classes",
    "project", "module", "modules",
}


def extract_keywords(task: object, min_len: int = 4) -> List[str]:
    """Extract keyword tokens from the task text for the ranker.

    Tokens are lower-cased, stop-word filtered, deduped.
    """
    text = _gather_text(task).lower()
    tokens = re.findall(r"[a-zA-Z_][\w]*", text)
    seen: Set[str] = set()
    out: List[str] = []
    for tok in tokens:
        if len(tok) < min_len:
            continue
        if tok in _STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def matches_anchor(
    anchor: str,
    candidate_path: str,
) -> bool:
    """Return True if `anchor` references `candidate_path` (relative posix)."""
    candidate = candidate_path.replace("\\", "/")
    anchor_norm = anchor.replace("\\", "/").lstrip("./")
    if not anchor_norm:
        return False
    if "/" in anchor_norm:
        return candidate == anchor_norm or candidate.endswith("/" + anchor_norm)
    # bare filename anchor
    return candidate.split("/")[-1] == anchor_norm
