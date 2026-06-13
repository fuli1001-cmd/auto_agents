"""Repo map parse cache.

Version 2 stores summaries per file so a task-sized edit only reparses the
files that actually changed. Legacy cache payloads are treated as misses.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .parser import BaseParser, FileSummary, Symbol


CACHE_VERSION = 2


def _file_content_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def compute_cache_key(project_root: Path, rel_paths: Sequence[str]) -> str:
    hasher = hashlib.sha256()
    for rel in sorted(rel_paths):
        path = project_root / rel
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(_file_content_hash(path).encode("ascii"))
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
    """JSON-backed cache for repo map parses, keyed per file."""

    def __init__(self, project_root: Path, cache_path: Optional[Path] = None) -> None:
        self.project_root = Path(project_root)
        if cache_path is None:
            cache_path = self.project_root / ".auto-agents" / "state" / "repomap_cache.json"
        self.cache_path = Path(cache_path)
        self.last_hit: Optional[bool] = None
        self.last_hits: int = 0
        self.last_misses: int = 0

    @staticmethod
    def _fingerprint(parser: BaseParser, path: Path) -> Dict[str, object]:
        try:
            stat = path.stat()
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
        except OSError:
            size = 0
            mtime_ns = 0
        return {
            "cache_version": int(getattr(parser, "cache_version", 1) or 1),
            "parser": parser.__class__.__name__,
            "size": size,
            "mtime_ns": mtime_ns,
        }

    def get_or_build(
        self,
        rel_paths: Sequence[str],
        parser: BaseParser,
    ) -> List[FileSummary]:
        cached = self._read()
        entries = dict(cached.get("entries", {})) if cached else {}
        summaries: List[FileSummary] = []
        next_entries: Dict[str, Dict[str, object]] = {}
        hits = 0
        misses = 0
        dirty = False

        for rel_path in rel_paths:
            fingerprint = self._fingerprint(parser, self.project_root / rel_path)
            cached_entry = entries.get(rel_path)
            if (
                isinstance(cached_entry, dict)
                and cached_entry.get("fingerprint") == fingerprint
                and isinstance(cached_entry.get("summary"), dict)
            ):
                summaries.append(_summary_from_json(cached_entry["summary"]))
                next_entries[rel_path] = cached_entry
                hits += 1
                continue

            summary = parser.parse(self.project_root, rel_path)
            summaries.append(summary)
            next_entries[rel_path] = {
                "fingerprint": fingerprint,
                "summary": _summary_to_json(summary),
            }
            misses += 1
            dirty = True

        for rel_path, cached_entry in entries.items():
            if rel_path in next_entries:
                continue
            if (self.project_root / rel_path).exists():
                next_entries[rel_path] = cached_entry
            else:
                dirty = True

        self.last_hits = hits
        self.last_misses = misses
        self.last_hit = misses == 0 and bool(rel_paths)
        if dirty or set(next_entries) != set(entries):
            self._write(next_entries)
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
            if isinstance(data, dict) and int(data.get("version", 0) or 0) == CACHE_VERSION:
                return data
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _write(self, entries: Dict[str, Dict[str, object]]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": CACHE_VERSION,
                "entries": entries,
            }
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.cache_path)
        except OSError:
            # Cache failures should never break the run.
            pass
