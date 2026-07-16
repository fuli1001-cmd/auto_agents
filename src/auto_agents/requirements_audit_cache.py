from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .config import requirements_audit_cache_path


CACHE_VERSION = 1
MAX_ROWS = 20_000
MAX_AGE_SECONDS = 30 * 24 * 60 * 60


class RequirementsAuditCache:
    """Best-effort file/pattern match cache.

    Cache failures never weaken the audit: callers treat misses and database
    errors as a request to perform the real scan.
    """

    def __init__(self, project_root: Path, cache_path: Optional[Path] = None) -> None:
        self.path = cache_path or requirements_audit_cache_path(Path(project_root))
        self.disabled = False
        self._connection: Optional[sqlite3.Connection] = None
        self._writes = 0

    def get(
        self,
        pattern_set_hash: str,
        path: str,
        content_sha256: str,
    ) -> Optional[Tuple[Sequence[int], Sequence[int]]]:
        if self.disabled:
            return None
        try:
            row = self._connect().execute(
                """
                SELECT matched_indexes, non_negated_indexes
                FROM file_matches
                WHERE pattern_set_hash = ? AND path = ? AND content_sha256 = ?
                """,
                (pattern_set_hash, path, content_sha256),
            ).fetchone()
            if row is None:
                return None
            return self._indexes(row[0]), self._indexes(row[1])
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            self.disabled = True
            return None

    def put(
        self,
        pattern_set_hash: str,
        path: str,
        content_sha256: str,
        matched_indexes: Sequence[int],
        non_negated_indexes: Sequence[int],
    ) -> None:
        if self.disabled:
            return
        now = int(time.time())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            connection.execute(
                """
                INSERT OR REPLACE INTO file_matches (
                    pattern_set_hash, path, content_sha256,
                    matched_indexes, non_negated_indexes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern_set_hash,
                    path,
                    content_sha256,
                    json.dumps(sorted(set(int(item) for item in matched_indexes))),
                    json.dumps(sorted(set(int(item) for item in non_negated_indexes))),
                    now,
                ),
            )
            self._writes += 1
            if self._writes % 250 == 0:
                connection.execute(
                    "DELETE FROM file_matches WHERE updated_at < ?",
                    (now - MAX_AGE_SECONDS,),
                )
                count = int(connection.execute("SELECT COUNT(*) FROM file_matches").fetchone()[0])
                if count > MAX_ROWS:
                    connection.execute(
                        """
                        DELETE FROM file_matches WHERE rowid IN (
                            SELECT rowid FROM file_matches
                            ORDER BY updated_at ASC LIMIT ?
                        )
                        """,
                        (count - MAX_ROWS,),
                    )
        except (OSError, sqlite3.Error):
            self.disabled = True

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        connection = sqlite3.connect(str(self.path), timeout=1.0)
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'version'"
        ).fetchone()
        if version is not None and int(version[0]) != CACHE_VERSION:
            connection.execute("DROP TABLE IF EXISTS file_matches")
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('version', ?)",
            (str(CACHE_VERSION),),
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS file_matches (
                pattern_set_hash TEXT NOT NULL,
                path TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                matched_indexes TEXT NOT NULL,
                non_negated_indexes TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (pattern_set_hash, path, content_sha256)
            )
            """
        )
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.commit()
            self._connection.close()
        except sqlite3.Error:
            pass
        finally:
            self._connection = None

    @staticmethod
    def _indexes(raw: str) -> Sequence[int]:
        payload = json.loads(raw)
        if not isinstance(payload, list) or any(not isinstance(item, int) for item in payload):
            raise ValueError("invalid cached indexes")
        return payload
