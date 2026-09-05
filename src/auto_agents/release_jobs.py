from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

from .git_ops import head_ref, worktree_fingerprint


def release_jobs_path(project_root: Path) -> Path:
    return Path(project_root) / ".auto-agents" / "state" / "release_jobs.sqlite3"


def candidate_identity(project_root: Path) -> tuple[str, str]:
    import hashlib

    sha = head_ref(project_root)
    payload = {
        "head": sha,
        "worktree": worktree_fingerprint(project_root),
    }
    candidate_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return candidate_id, sha


class ReleaseJobStore:
    """Crash-safe latest-candidate release queue and attestation history."""

    def __init__(self, project_root: Path, *, path: Optional[Path] = None) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.path = Path(path) if path is not None else release_jobs_path(self.project_root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(
        self,
        *,
        source: str,
        affected_proof_ids: Iterable[str],
    ) -> dict[str, object]:
        candidate_id, candidate_sha = candidate_identity(self.project_root)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM release_jobs WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "passed":
                return self._row(existing)
            job_id = str(existing["job_id"]) if existing is not None else uuid.uuid4().hex[:16]
            connection.execute(
                """
                UPDATE release_jobs
                   SET status = 'superseded', superseded_by = ?, updated_at = ?
                 WHERE candidate_id <> ? AND status IN ('pending', 'running', 'recovering')
                """,
                (job_id, now, candidate_id),
            )
            payload = json.dumps(
                list(dict.fromkeys(str(item) for item in affected_proof_ids if str(item))),
                ensure_ascii=False,
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO release_jobs (
                        job_id, candidate_id, candidate_sha, status, source,
                        affected_proof_ids, release_proof_ids, failure_payload,
                        logical_commands, executed_commands, certificate_hits,
                        recovery_attempts, infrastructure_attempts, reason,
                        queued_at, updated_at, started_at, completed_at,
                        superseded_by, recovery_commit
                    ) VALUES (?, ?, ?, 'pending', ?, ?, '[]', '{}', 0, 0, 0,
                              0, 0, '', ?, ?, 0, 0, '', '')
                    """,
                    (job_id, candidate_id, candidate_sha, source, payload, now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE release_jobs
                       SET candidate_sha = ?, status = 'pending', source = ?,
                           affected_proof_ids = ?, failure_payload = '{}', reason = '',
                           queued_at = ?, updated_at = ?, started_at = 0,
                           completed_at = 0, superseded_by = ''
                     WHERE job_id = ?
                    """,
                    (candidate_sha, source, payload, now, now, job_id),
                )
            row = connection.execute(
                "SELECT * FROM release_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._row(row)

    def claim_latest(self, *, idle_delay_seconds: int = 0) -> Optional[dict[str, object]]:
        cutoff = time.time() - max(0, int(idle_delay_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM release_jobs
                 WHERE status = 'pending' AND queued_at <= ?
                 ORDER BY queued_at DESC LIMIT 1
                """,
                (cutoff,),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            connection.execute(
                "UPDATE release_jobs SET status = 'running', started_at = ?, updated_at = ? WHERE job_id = ?",
                (now, now, row["job_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM release_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            return self._row(claimed)

    def requeue_abandoned(self) -> int:
        """Recover jobs left active after the sole worker lock was released."""
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE release_jobs
                   SET status = 'pending', queued_at = ?, updated_at = ?,
                       reason = 'release worker restarted after interruption'
                 WHERE status IN ('running', 'recovering')
                """,
                (now, now),
            )
            return int(cursor.rowcount)

    def latest(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM release_jobs ORDER BY queued_at DESC LIMIT 1"
            ).fetchone()
        return self._row(row) if row is not None else {}

    def get(self, job_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM release_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row(row) if row is not None else {}

    def is_current(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or str(job.get("status")) == "superseded":
            return False
        current_id, _ = candidate_identity(self.project_root)
        return str(job.get("candidate_id")) == current_id

    def mark_running(self, job_id: str) -> dict[str, object]:
        return self._update(job_id, status="running", started_at=time.time())

    def mark_recovering(
        self,
        job_id: str,
        *,
        failure_payload: Mapping[str, object],
        reason: str,
    ) -> dict[str, object]:
        job = self.get(job_id)
        return self._update(
            job_id,
            status="recovering",
            failure_payload=json.dumps(dict(failure_payload), ensure_ascii=False),
            reason=reason,
            recovery_attempts=int(job.get("recovery_attempts", 0)) + 1,
        )

    def record_infrastructure_retry(
        self,
        job_id: str,
        *,
        failure_payload: Mapping[str, object],
        reason: str,
    ) -> dict[str, object]:
        job = self.get(job_id)
        return self._update(
            job_id,
            status="pending",
            failure_payload=json.dumps(dict(failure_payload), ensure_ascii=False),
            reason=reason,
            infrastructure_attempts=int(job.get("infrastructure_attempts", 0)) + 1,
            queued_at=time.time(),
        )

    def defer(self, job_id: str, *, reason: str) -> dict[str, object]:
        return self._update(
            job_id,
            status="pending",
            reason=reason,
            queued_at=time.time(),
        )

    def complete(
        self,
        job_id: str,
        result: Mapping[str, object],
        *,
        recovery_commit: str = "",
    ) -> dict[str, object]:
        return self._update(
            job_id,
            status="passed" if bool(result.get("ok")) else "failed",
            release_proof_ids=json.dumps(list(result.get("proof_ids", []))),
            logical_commands=int(result.get("logical_commands", 0)),
            executed_commands=int(result.get("executed_commands", 0)),
            certificate_hits=int(result.get("certificate_hits", 0)),
            reason=str(result.get("reason", "")),
            failure_payload=json.dumps(
                {} if bool(result.get("ok")) else dict(result), ensure_ascii=False
            ),
            completed_at=time.time(),
            recovery_commit=recovery_commit,
        )

    def needs_user(
        self,
        job_id: str,
        *,
        reason: str,
        failure_payload: Optional[Mapping[str, object]] = None,
    ) -> dict[str, object]:
        return self._update(
            job_id,
            status="needs_user",
            reason=reason,
            failure_payload=json.dumps(dict(failure_payload or {}), ensure_ascii=False),
            completed_at=time.time(),
        )

    def supersede(self, job_id: str, *, superseded_by: str = "") -> dict[str, object]:
        return self._update(
            job_id,
            status="superseded",
            superseded_by=superseded_by,
            completed_at=time.time(),
        )

    def set_worker(
        self,
        *,
        status: str,
        pid: int = 0,
        job_id: str = "",
        reason: str = "",
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO release_worker (singleton, status, pid, job_id, reason, heartbeat_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    status=excluded.status, pid=excluded.pid, job_id=excluded.job_id,
                    reason=excluded.reason, heartbeat_at=excluded.heartbeat_at
                """,
                (status, int(pid), job_id, reason, now),
            )

    def worker_status(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM release_worker WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return {}
        return {
            "status": str(row["status"]),
            "pid": int(row["pid"]),
            "job_id": str(row["job_id"]),
            "reason": str(row["reason"]),
            "heartbeat_at": _iso(float(row["heartbeat_at"])),
        }

    def _update(self, job_id: str, **values: object) -> dict[str, object]:
        if not values:
            return self.get(job_id)
        values["updated_at"] = time.time()
        columns = ", ".join(f"{key} = ?" for key in values)
        params = [*values.values(), job_id]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE release_jobs SET {columns} WHERE job_id = ?", params
            )
            row = connection.execute(
                "SELECT * FROM release_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row(row) if row is not None else {}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            with connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=30000")
                yield connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS release_jobs (
                    job_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE,
                    candidate_sha TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    affected_proof_ids TEXT NOT NULL,
                    release_proof_ids TEXT NOT NULL,
                    failure_payload TEXT NOT NULL,
                    logical_commands INTEGER NOT NULL,
                    executed_commands INTEGER NOT NULL,
                    certificate_hits INTEGER NOT NULL,
                    recovery_attempts INTEGER NOT NULL,
                    infrastructure_attempts INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    queued_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL NOT NULL,
                    superseded_by TEXT NOT NULL,
                    recovery_commit TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS release_jobs_status_queue
                    ON release_jobs(status, queued_at DESC);
                CREATE TABLE IF NOT EXISTS release_worker (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    status TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    job_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    heartbeat_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        for key in ("affected_proof_ids", "release_proof_ids", "failure_payload"):
            try:
                result[key] = json.loads(str(result.get(key, "")))
            except json.JSONDecodeError:
                result[key] = [] if key.endswith("proof_ids") else {}
        for key in ("queued_at", "updated_at", "started_at", "completed_at"):
            result[key] = _iso(float(result.get(key, 0) or 0))
        return result


def _iso(value: float) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat()
