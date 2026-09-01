from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional

from .config import run_path, state_dir


TRACE_SCHEMA_VERSION = 1


class PerformanceTrace:
    """Append-only performance spans shared across workflow resumes."""

    def __init__(
        self,
        project_root: Path,
        *,
        workflow_kind: str,
        subject_id: str,
        workflow_id: str = "",
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.workflow_kind = str(workflow_kind)
        self.subject_id = str(subject_id)
        self.workflow_id = str(workflow_id)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        if self.workflow_kind == "run":
            return run_path(self.project_root, self.subject_id) / "performance_trace.jsonl"
        return (
            state_dir(self.project_root)
            / "sessions"
            / self.subject_id
            / "performance_trace.jsonl"
        )

    def event(
        self,
        kind: str,
        name: str,
        *,
        duration_seconds: float = 0.0,
        active_seconds: Optional[float] = None,
        wait_seconds: float = 0.0,
        parent_span_id: str = "",
        metadata: Optional[Mapping[str, object]] = None,
        span_id: str = "",
    ) -> str:
        identifier = span_id or uuid.uuid4().hex[:16]
        payload: Dict[str, object] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workflow_kind": self.workflow_kind,
            "workflow_id": self.workflow_id,
            "subject_id": self.subject_id,
            "kind": str(kind),
            "name": str(name),
            "span_id": identifier,
            "parent_span_id": str(parent_span_id),
            "duration_seconds": round(max(0.0, float(duration_seconds)), 6),
            "active_seconds": round(
                max(
                    0.0,
                    float(
                        duration_seconds
                        if active_seconds is None
                        else active_seconds
                    ),
                ),
                6,
            ),
            "wait_seconds": round(max(0.0, float(wait_seconds)), 6),
            "metadata": dict(metadata or {}),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            descriptor = os.open(
                path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o644,
            )
            try:
                os.write(descriptor, encoded.encode("utf-8"))
            finally:
                os.close(descriptor)
        return identifier

    @contextmanager
    def span(
        self,
        kind: str,
        name: str,
        *,
        parent_span_id: str = "",
        metadata: Optional[Mapping[str, object]] = None,
    ) -> Iterator[str]:
        span_id = uuid.uuid4().hex[:16]
        started = time.monotonic()
        try:
            yield span_id
        finally:
            self.event(
                kind,
                name,
                duration_seconds=time.monotonic() - started,
                parent_span_id=parent_span_id,
                metadata=metadata,
                span_id=span_id,
            )

    def summary(self) -> Dict[str, object]:
        totals: Dict[str, Dict[str, object]] = {}
        metrics: Dict[str, object] = {
            "duration_seconds": 0.0,
            "active_seconds": 0.0,
            "wait_seconds": 0.0,
            "agent_calls": 0,
            "provider_session_resumes": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "gate_calls": 0,
            "gate_cache_hits": 0,
            "gate_cache_miss_reasons": {},
        }
        event_count = 0
        if not self.path.is_file():
            return {"events": 0, "metrics": metrics, "totals": {}}
        for line in self.path.read_text("utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_count += 1
            key = f"{payload.get('kind', '')}:{payload.get('name', '')}"
            entry = totals.setdefault(
                key,
                {
                    "count": 0,
                    "duration_seconds": 0.0,
                    "active_seconds": 0.0,
                    "wait_seconds": 0.0,
                },
            )
            entry["count"] = int(entry["count"]) + 1
            for field in ("duration_seconds", "active_seconds", "wait_seconds"):
                value = float(payload.get(field, 0.0) or 0.0)
                entry[field] = float(entry[field]) + value
                metrics[field] = float(metrics[field]) + value
            metadata = payload.get("metadata", {})
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            if payload.get("kind") == "agent":
                metrics["agent_calls"] = int(metrics["agent_calls"]) + 1
                if metadata.get("provider_session_resumed"):
                    metrics["provider_session_resumes"] = (
                        int(metrics["provider_session_resumes"]) + 1
                    )
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                ):
                    metrics[field] = int(metrics[field]) + int(
                        metadata.get(field, 0) or 0
                    )
            elif payload.get("kind") == "gate":
                metrics["gate_calls"] = int(metrics["gate_calls"]) + 1
                if metadata.get("cached"):
                    metrics["gate_cache_hits"] = (
                        int(metrics["gate_cache_hits"]) + 1
                    )
                else:
                    reason = str(
                        metadata.get("cache_miss_reason", "unknown") or "unknown"
                    )
                    reasons = dict(metrics["gate_cache_miss_reasons"])
                    reasons[reason] = int(reasons.get(reason, 0)) + 1
                    metrics["gate_cache_miss_reasons"] = reasons
        return {"events": event_count, "metrics": metrics, "totals": totals}
