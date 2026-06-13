from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

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
    "copilot-cli": {
        "default": ProviderLimit(initial_workers=2, worker_ceiling=2),
        "free": ProviderLimit(initial_workers=1, worker_ceiling=1),
        "pro": ProviderLimit(initial_workers=2, worker_ceiling=3),
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
    def __init__(self, project_root: Path) -> None:
        self.path = Path(project_root) / ".auto-agents" / "state" / "parallel_tuning.json"

    def get_workers(self, key: str) -> Optional[int]:
        payload = self._read()
        entry = payload.get(key)
        if not isinstance(entry, dict):
            return None
        workers = entry.get("workers")
        return workers if isinstance(workers, int) and workers >= 1 else None

    def put_workers(self, key: str, workers: int, *, event: str) -> None:
        payload = self._read()
        payload[key] = {
            "workers": max(1, int(workers)),
            "event": event,
            "updated_at": int(time.time()),
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
