from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from statistics import median
from typing import Optional

from .config import gate_baseline_cache_path
from .models import CommandResult


TIMING_VERSION = 1
MAX_SAMPLES = 7
MAX_ROWS = 5_000
MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _metadata_value(metadata: object, name: str, default: object) -> object:
    return getattr(metadata, name, default)


def _resource_signature(metadata: object) -> str:
    payload = {
        "resource_class": str(
            _metadata_value(metadata, "resource_class", "normal")
        ).strip().lower(),
        "cpu_slots": max(
            0,
            int(_metadata_value(metadata, "cpu_slots", 0) or 0),
        ),
        "requires": sorted(
            str(item).strip().lower()
            for item in (_metadata_value(metadata, "requires", []) or [])
            if str(item).strip()
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _timing_key(
    command: str,
    environment_fingerprint: str,
    resource_signature: str,
) -> str:
    payload = {
        "command": str(command).strip(),
        "environment_fingerprint": str(environment_fingerprint),
        "resource_signature": resource_signature,
        "timing_version": TIMING_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class GateTimingStore:
    """Best-effort rolling command duration history for gate scheduling."""

    def __init__(
        self,
        project_root: Path,
        *,
        cache_path: Optional[Path] = None,
        environment_fingerprint: str = "",
    ) -> None:
        self.cache_path = cache_path or gate_baseline_cache_path(Path(project_root))
        self.environment_fingerprint = str(environment_fingerprint)
        self.disabled = False
        self._lock = threading.Lock()

    def estimate(self, command: str, metadata: object = None) -> Optional[float]:
        if self.disabled:
            return None
        signature = _resource_signature(metadata)
        key = _timing_key(command, self.environment_fingerprint, signature)
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT samples, updated_at
                    FROM command_timings
                    WHERE timing_key = ?
                    """,
                    (key,),
                ).fetchone()
                if row is None or int(row[1]) < int(time.time()) - MAX_AGE_SECONDS:
                    return None
                decoded = json.loads(row[0])
                samples = [
                    float(item)
                    for item in decoded
                    if isinstance(item, (int, float)) and float(item) >= 0
                ]
                return float(median(samples)) if samples else None
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            self.disabled = True
            return None

    def estimate_any_environment(
        self,
        command: str,
        metadata: object = None,
    ) -> Optional[float]:
        """Return recent timing history usable for first-pass batch balancing.

        Execution timeout and cache decisions remain environment-specific.
        Batch construction only needs a rough relative cost, so it may combine
        successful samples from equivalent resource signatures across workers.
        """
        if self.disabled:
            return None
        signature = _resource_signature(metadata)
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT samples
                    FROM command_timings
                    WHERE command = ?
                      AND resource_signature = ?
                      AND updated_at >= ?
                    """,
                    (
                        str(command).strip(),
                        signature,
                        int(time.time()) - MAX_AGE_SECONDS,
                    ),
                ).fetchall()
                samples = [
                    float(item)
                    for row in rows
                    for item in json.loads(row[0])
                    if isinstance(item, (int, float)) and float(item) >= 0
                ]
                return float(median(samples)) if samples else None
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            self.disabled = True
            return None

    def record(
        self,
        command: str,
        result: CommandResult,
        metadata: object = None,
    ) -> None:
        duration = float(result.duration_seconds or 0.0)
        if (
            self.disabled
            or result.termination_reason
            or result.cleanup_incomplete
            or result.infrastructure_error
            or duration < 0
        ):
            return
        signature = _resource_signature(metadata)
        key = _timing_key(command, self.environment_fingerprint, signature)
        now = int(time.time())
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT samples FROM command_timings WHERE timing_key = ?",
                    (key,),
                ).fetchone()
                samples: list[float] = []
                if row is not None:
                    decoded = json.loads(row[0])
                    samples = [
                        float(item)
                        for item in decoded
                        if isinstance(item, (int, float)) and float(item) >= 0
                    ]
                samples.append(duration)
                samples = samples[-MAX_SAMPLES:]
                connection.execute(
                    """
                    INSERT OR REPLACE INTO command_timings (
                        timing_key, command, environment_fingerprint,
                        resource_signature, samples, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        str(command).strip(),
                        self.environment_fingerprint,
                        signature,
                        json.dumps(samples),
                        now,
                    ),
                )
                self._prune(connection, now)
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            self.disabled = True

    def quarantined_commands(self) -> set[str]:
        if self.disabled:
            return set()
        cutoff = int(time.time()) - MAX_AGE_SECONDS
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "DELETE FROM parallel_quarantine WHERE updated_at < ?",
                    (cutoff,),
                )
                rows = connection.execute(
                    """
                    SELECT command
                    FROM parallel_quarantine
                    WHERE environment_fingerprint = ?
                    """,
                    (self.environment_fingerprint,),
                ).fetchall()
                return {
                    str(row[0]).strip()
                    for row in rows
                    if str(row[0]).strip()
                }
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self.disabled = True
            return set()

    def quarantine_parallel_command(self, command: str) -> None:
        if self.disabled or not str(command).strip():
            return
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO parallel_quarantine (
                        command, environment_fingerprint, updated_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        str(command).strip(),
                        self.environment_fingerprint,
                        int(time.time()),
                    ),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self.disabled = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.cache_path), timeout=1.0)
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS command_timings (
                timing_key TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                environment_fingerprint TEXT NOT NULL,
                resource_signature TEXT NOT NULL,
                samples TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS parallel_quarantine (
                command TEXT NOT NULL,
                environment_fingerprint TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (command, environment_fingerprint)
            )
            """
        )
        return connection

    @staticmethod
    def _prune(connection: sqlite3.Connection, now: int) -> None:
        connection.execute(
            "DELETE FROM command_timings WHERE updated_at < ?",
            (now - MAX_AGE_SECONDS,),
        )
        count = int(
            connection.execute("SELECT COUNT(*) FROM command_timings").fetchone()[0]
        )
        if count > MAX_ROWS:
            connection.execute(
                """
                DELETE FROM command_timings WHERE rowid IN (
                    SELECT rowid FROM command_timings
                    ORDER BY updated_at ASC
                    LIMIT ?
                )
                """,
                (count - MAX_ROWS,),
            )
