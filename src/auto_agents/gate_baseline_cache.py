from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import gate_baseline_cache_path


CACHE_VERSION = 1
EXECUTION_MODE_VERSION = 1


def _normalized_commands(commands: Sequence[str]) -> List[str]:
    return [str(command).strip() for command in commands if str(command).strip()]


def make_cache_key(
    baseline_ref: str,
    commands: Sequence[str],
    *,
    collect_all: bool,
) -> str:
    payload = {
        "baseline_ref": str(baseline_ref).strip(),
        "commands": _normalized_commands(commands),
        "collect_all": bool(collect_all),
        "execution_mode_version": EXECUTION_MODE_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class GateBaselineCache:
    def __init__(self, project_root: Path, cache_path: Optional[Path] = None) -> None:
        self.project_root = Path(project_root)
        self.cache_path = cache_path or gate_baseline_cache_path(self.project_root)

    def get(
        self,
        baseline_ref: str,
        commands: Sequence[str],
        *,
        collect_all: bool,
    ) -> Optional[List[str]]:
        payload = self._read()
        if payload is None:
            return None
        key = make_cache_key(baseline_ref, commands, collect_all=collect_all)
        entry = payload.get("entries", {}).get(key)
        if not isinstance(entry, dict):
            return None
        if entry.get("mutation_detected"):
            return None
        return [str(item) for item in entry.get("failure_ids", []) if str(item).strip()]

    def put(
        self,
        baseline_ref: str,
        commands: Sequence[str],
        *,
        collect_all: bool,
        failure_ids: Sequence[str],
        mutation_detected: bool = False,
        summary: str = "",
    ) -> None:
        payload = self._read() or {"version": CACHE_VERSION, "entries": {}}
        key = make_cache_key(baseline_ref, commands, collect_all=collect_all)
        payload.setdefault("entries", {})[key] = {
            "baseline_ref": str(baseline_ref).strip(),
            "commands": _normalized_commands(commands),
            "collect_all": bool(collect_all),
            "execution_mode_version": EXECUTION_MODE_VERSION,
            "failure_ids": [str(item).strip() for item in failure_ids if str(item).strip()],
            "mutation_detected": bool(mutation_detected),
            "summary": str(summary).strip(),
        }
        self._write(payload)

    def _read(self) -> Optional[Dict[str, object]]:
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version", 0) or 0) != CACHE_VERSION:
            return None
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return None
        return payload

    def _write(self, payload: Dict[str, object]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp_path, self.cache_path)
        except OSError:
            pass
