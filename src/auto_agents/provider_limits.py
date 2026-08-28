from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from .models import ProviderConfig


@dataclass(frozen=True)
class ProviderLimit:
    initial_workers: int
    worker_ceiling: int


_LIMITS: Dict[str, Dict[str, ProviderLimit]] = {
    "codex": {
        "default": ProviderLimit(initial_workers=2, worker_ceiling=2),
        "plus": ProviderLimit(initial_workers=2, worker_ceiling=2),
        "pro": ProviderLimit(initial_workers=2, worker_ceiling=4),
        "team": ProviderLimit(initial_workers=2, worker_ceiling=4),
        "enterprise": ProviderLimit(initial_workers=3, worker_ceiling=6),
    },
    "claude-code": {
        "default": ProviderLimit(initial_workers=2, worker_ceiling=2),
        "free": ProviderLimit(initial_workers=1, worker_ceiling=1),
        "pro": ProviderLimit(initial_workers=2, worker_ceiling=3),
        "max": ProviderLimit(initial_workers=2, worker_ceiling=4),
        "team": ProviderLimit(initial_workers=2, worker_ceiling=4),
        "enterprise": ProviderLimit(initial_workers=3, worker_ceiling=6),
    },
    "copilot-cli": {
        "default": ProviderLimit(initial_workers=2, worker_ceiling=2),
        "free": ProviderLimit(initial_workers=1, worker_ceiling=1),
        "pro": ProviderLimit(initial_workers=2, worker_ceiling=3),
        "pro+": ProviderLimit(initial_workers=2, worker_ceiling=4),
        "business": ProviderLimit(initial_workers=2, worker_ceiling=4),
        "enterprise": ProviderLimit(initial_workers=2, worker_ceiling=5),
    },
    "antigravity": {
        "default": ProviderLimit(initial_workers=2, worker_ceiling=2),
        "google-ai-pro": ProviderLimit(initial_workers=2, worker_ceiling=3),
        "google-ai-ultra": ProviderLimit(initial_workers=2, worker_ceiling=4),
    },
    "mock": {
        "default": ProviderLimit(initial_workers=4, worker_ceiling=8),
    },
}


def provider_limit(config: ProviderConfig) -> ProviderLimit:
    kind_limits = _LIMITS.get(config.kind, _LIMITS["codex"])
    tier = config.subscription_tier.strip().lower() or "default"
    return kind_limits.get(tier, kind_limits.get("default", ProviderLimit(2, 2)))


class ParallelTuningStore:
    def __init__(self, project_root: Path, *, time_fn: Callable[[], float] = time.time) -> None:
        self.path = Path(project_root) / ".auto-agents" / "state" / "parallel_tuning.json"
        self._time_fn = time_fn

    def get_workers(self, key: str) -> Optional[int]:
        entry = self.get_entry(key)
        workers = entry.get("workers") if entry else None
        return workers if isinstance(workers, int) and workers >= 1 else None

    def get_entry(self, key: str, *, legacy_keys: Iterable[str] = ()) -> Optional[Dict[str, object]]:
        payload = self._read()
        entry = payload.get(key)
        source_key = key
        if not isinstance(entry, dict):
            for legacy_key in legacy_keys:
                candidate = payload.get(legacy_key)
                if isinstance(candidate, dict):
                    entry = candidate
                    source_key = legacy_key
                    break
        if not isinstance(entry, dict):
            return None
        normalized = dict(entry)
        normalized["source_key"] = source_key
        return normalized

    def resolve_workers(
        self,
        key: str,
        *,
        initial_workers: int,
        cooldown_seconds: int,
        legacy_keys: Iterable[str] = (),
    ) -> Dict[str, object]:
        entry = self.get_entry(key, legacy_keys=legacy_keys)
        if entry is None:
            return {
                "workers": max(1, int(initial_workers)),
                "event": "default",
                "cooldown_active": False,
                "source_key": "",
                "updated_at": 0,
            }
        raw_workers = entry.get("workers")
        workers = raw_workers if isinstance(raw_workers, int) and raw_workers >= 1 else initial_workers
        event = str(entry.get("event", "legacy"))
        updated_at = int(entry.get("updated_at", 0) or 0)
        pressure_event = event in {"provider_pressure", "hard_pressure", "soft_pressure"}
        elapsed = max(0, int(self._time_fn()) - updated_at)
        cooldown_active = pressure_event and workers == 1 and elapsed < cooldown_seconds
        canary = pressure_event and workers == 1 and not cooldown_active
        return {
            "workers": 2 if canary else workers,
            "stored_workers": workers,
            "event": "canary" if canary else event,
            "cooldown_active": cooldown_active,
            "cooldown_remaining_seconds": max(0, cooldown_seconds - elapsed) if cooldown_active else 0,
            "source_key": str(entry.get("source_key", "")),
            "updated_at": updated_at,
            "soft_pressure_count": int(entry.get("soft_pressure_count", 0) or 0),
        }

    def put_workers(
        self,
        key: str,
        workers: int,
        *,
        event: str,
        soft_pressure_count: int = 0,
    ) -> None:
        payload = self._read()
        payload[key] = {
            "workers": max(1, int(workers)),
            "event": event,
            "updated_at": int(self._time_fn()),
            "soft_pressure_count": max(0, int(soft_pressure_count)),
        }
        self._write(payload)

    def _read(self) -> Dict[str, object]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: Dict[str, object]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            pass
