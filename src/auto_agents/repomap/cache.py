"""Repo map parse cache.

Cache key combines:
    - the current git HEAD SHA (or "no-git")
    - SHA256 over sorted ``(rel_path, mtime_ns)`` tuples for tracked Python files

Cache value is the parsed list of FileSummary serialized to JSON. Cache misses
trigger a fresh parse pass via the supplied parser.

The cache lives under ``.auto-agents/state/repomap_cache.json`` and stores a
single entry; older entries are overwritten when the key changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .parser import BaseParser, FileSummary, Symbol


def _git_head(project_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or "no-git"
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return "no-git"


def compute_cache_key(project_root: Path, rel_paths: Sequence[str]) -> str:
    head = _git_head(project_root)
    hasher = hashlib.sha256()
    hasher.update(head.encode("utf-8"))
    hasher.update(b"\0")
    for rel in sorted(rel_paths):
        try:
            mtime = (project_root / rel).stat().st_mtime_ns
        except OSError:
            mtime = 0
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(mtime).encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _summary_to_json(summary: FileSummary) -> Dict[str, object]:
    return {
        "path": summary.path,
        "language": summary.language,
        "imports": list(summary.imports),
        "parse_error": summary.parse_error,
        "sloc": summary.sloc,
        "symbols": [
            {
                "kind": sym.kind,
                "name": sym.name,
                "signature": sym.signature,
                "docstring": sym.docstring,
                "lineno": sym.lineno,
                "children": [
                    {
                        "kind": child.kind,
                        "name": child.name,
                        "signature": child.signature,
                        "docstring": child.docstring,
                        "lineno": child.lineno,
                    }
                    for child in sym.children
                ],
            }
            for sym in summary.symbols
        ],
    }


def _summary_from_json(data: Dict[str, object]) -> FileSummary:
    symbols: List[Symbol] = []
    for raw in data.get("symbols", []) or []:
        children = [
            Symbol(
                kind=str(c.get("kind", "")),
                name=str(c.get("name", "")),
                signature=str(c.get("signature", "")),
                docstring=str(c.get("docstring", "")),
                lineno=int(c.get("lineno", 0) or 0),
            )
            for c in raw.get("children", []) or []
        ]
        symbols.append(
            Symbol(
                kind=str(raw.get("kind", "")),
                name=str(raw.get("name", "")),
                signature=str(raw.get("signature", "")),
                docstring=str(raw.get("docstring", "")),
                lineno=int(raw.get("lineno", 0) or 0),
                children=children,
            )
        )
    return FileSummary(
        path=str(data.get("path", "")),
        language=str(data.get("language", "python")),
        symbols=symbols,
        imports=[str(i) for i in (data.get("imports", []) or [])],
        parse_error=data.get("parse_error") or None,
        sloc=int(data.get("sloc", 0) or 0),
    )


class RepoMapCache:
    """JSON-backed cache for repo map parses, keyed by git+mtime fingerprint."""

    def __init__(self, project_root: Path, cache_path: Optional[Path] = None) -> None:
        self.project_root = Path(project_root)
        if cache_path is None:
            cache_path = self.project_root / ".auto-agents" / "state" / "repomap_cache.json"
        self.cache_path = Path(cache_path)
        self.last_hit: Optional[bool] = None

    def get_or_build(
        self,
        rel_paths: Sequence[str],
        parser: BaseParser,
    ) -> List[FileSummary]:
        key = compute_cache_key(self.project_root, rel_paths)
        cached = self._read()
        if cached and cached.get("key") == key:
            self.last_hit = True
            return [_summary_from_json(s) for s in cached.get("summaries", [])]

        self.last_hit = False
        summaries = [parser.parse(self.project_root, rp) for rp in rel_paths]
        self._write(key, summaries)
        return summaries

    def invalidate(self) -> None:
        try:
            self.cache_path.unlink()
        except FileNotFoundError:
            pass

    def _read(self) -> Optional[Dict[str, object]]:
        try:
            with self.cache_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _write(self, key: str, summaries: Sequence[FileSummary]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "key": key,
                "summaries": [_summary_to_json(s) for s in summaries],
            }
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError:
            # Cache failures should never break the run.
            pass
