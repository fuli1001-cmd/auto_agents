from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Optional

from .config import gate_baseline_cache_path
from .models import CommandResult


RESULT_CACHE_VERSION = 4
MAX_AGE_SECONDS = 14 * 24 * 60 * 60
MAX_ROWS = 20_000


def _stable_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _identity(
    command: str,
    environment_fingerprint: str,
    metadata_signature: str,
) -> str:
    return _stable_hash(
        {
            "command": str(command).strip(),
            "environment": str(environment_fingerprint),
            "metadata": str(metadata_signature),
            "version": RESULT_CACHE_VERSION,
        }
    )


def _candidate_key(
    identity: str,
    source_fingerprint: str,
    context_fingerprint: str,
) -> str:
    return _stable_hash(
        {
            "identity": identity,
            "source": source_fingerprint,
            "context": context_fingerprint,
        }
    )


def _path_digest(path: Path) -> Optional[str]:
    try:
        if path.is_symlink():
            return f"link:{path.readlink()}"
        if path.is_dir():
            return "dir:" + _stable_hash(sorted(item.name for item in path.iterdir()))
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return "file:" + digest.hexdigest()
    except OSError:
        return None
    return None


class GateResultCache:
    """Persistent proof certificates for isolated verification commands.

    Success certificates may be reused across candidates when a complete
    observed-input manifest still matches. Failed proofs are never reused:
    they must execute again so a transient or timing-sensitive failure cannot
    become a sticky negative result that blocks verification recovery.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        cache_path: Optional[Path] = None,
        environment_fingerprint: str = "",
        context_fingerprint: str = "",
        max_age_seconds: int = MAX_AGE_SECONDS,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.cache_path = cache_path or gate_baseline_cache_path(self.project_root)
        self.environment_fingerprint = str(environment_fingerprint)
        self.context_fingerprint = str(context_fingerprint)
        self.max_age_seconds = max(1, int(max_age_seconds))
        self.disabled = False
        self._lock = threading.Lock()

    def lookup(
        self,
        command: str,
        *,
        source_fingerprint: str,
        cache_scope: str,
        result_cache_scope: str,
        metadata_signature: str,
    ) -> Optional[CommandResult]:
        result, _reason = self.lookup_with_reason(
            command,
            source_fingerprint=source_fingerprint,
            cache_scope=cache_scope,
            result_cache_scope=result_cache_scope,
            metadata_signature=metadata_signature,
        )
        return result

    def lookup_with_reason(
        self,
        command: str,
        *,
        source_fingerprint: str,
        cache_scope: str,
        result_cache_scope: str,
        metadata_signature: str,
    ) -> tuple[Optional[CommandResult], str]:
        if self.disabled or result_cache_scope == "off":
            return None, "cache_disabled" if self.disabled else "scope_off"
        identity = _identity(
            command,
            self.environment_fingerprint,
            metadata_signature,
        )
        context = (
            self.context_fingerprint
            if str(cache_scope).strip().lower() != "source"
            else ""
        )
        key = _candidate_key(identity, source_fingerprint, context)
        now = int(time.time())
        try:
            with self._lock, self._connect() as connection:
                certificate = connection.execute(
                    """
                    SELECT result_payload
                    FROM gate_proof_certificates
                    WHERE cache_key = ? AND updated_at >= ?
                    """,
                    (key, now - self.max_age_seconds),
                ).fetchone()
                if certificate is not None:
                    return self._certificate_result(command, certificate[0]), "hit"
                row = connection.execute(
                    """
                    SELECT observed_inputs
                    FROM gate_result_successes
                    WHERE cache_key = ? AND updated_at >= ?
                    """,
                    (key, now - self.max_age_seconds),
                ).fetchone()
                if row is not None:
                    return self._cached_result(command, "result-cache-candidate"), "hit"
                if result_cache_scope not in {"observed_inputs", "auto"}:
                    return None, "candidate_key_miss"
                rows = connection.execute(
                    """
                    SELECT observed_inputs
                    FROM gate_result_successes
                    WHERE identity_key = ? AND context_fingerprint = ''
                      AND trace_complete = 1 AND network_observed = 0
                      AND updated_at >= ?
                    ORDER BY updated_at DESC
                    LIMIT 20
                    """,
                    (identity, now - self.max_age_seconds),
                ).fetchall()
                for candidate in rows:
                    manifest = json.loads(candidate[0] or "{}")
                    if self._manifest_matches(manifest):
                        return (
                            self._cached_result(
                                command,
                                "result-cache-observed-inputs",
                            ),
                            "hit",
                        )
                if rows:
                    return None, "observed_inputs_changed"
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            self.disabled = True
            return None, "cache_error"
        return None, "not_found"

    def record(
        self,
        command: str,
        result: CommandResult,
        *,
        source_fingerprint: str,
        cache_scope: str,
        result_cache_scope: str,
        metadata_signature: str,
    ) -> None:
        if (
            self.disabled
            or result_cache_scope == "off"
            or result.termination_reason
            or result.cleanup_incomplete
            or result.infrastructure_error
            or result.mutation_paths
            or result.artifacts
            or result.cached
            or not result.ok
        ):
            return
        identity = _identity(
            command,
            self.environment_fingerprint,
            metadata_signature,
        )
        context = (
            self.context_fingerprint
            if str(cache_scope).strip().lower() != "source"
            else ""
        )
        key = _candidate_key(identity, source_fingerprint, context)
        observed_inputs = (
            dict(result.observed_inputs)
            if result_cache_scope in {"observed_inputs", "auto"}
            and result.input_trace_complete
            and not result.network_observed
            else {}
        )
        now = int(time.time())
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO gate_proof_certificates (
                        cache_key, identity_key, command, source_fingerprint,
                        context_fingerprint, result_payload, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        identity,
                        str(command).strip(),
                        source_fingerprint,
                        context,
                        json.dumps(
                            {
                                "ok": bool(result.ok),
                                "returncode": int(result.returncode),
                                "stdout": result.stdout[-200_000:],
                                "stderr": result.stderr[-200_000:],
                                "comparable_failures": bool(result.comparable_failures),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM gate_proof_certificates WHERE updated_at < ?",
                    (now - self.max_age_seconds,),
                )
                connection.execute(
                    """
                    DELETE FROM gate_proof_certificates
                    WHERE cache_key IN (
                        SELECT cache_key FROM gate_proof_certificates
                        ORDER BY updated_at DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (MAX_ROWS,),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO gate_result_successes (
                        cache_key, identity_key, command, source_fingerprint,
                        context_fingerprint, observed_inputs, trace_complete,
                        network_observed, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        identity,
                        str(command).strip(),
                        source_fingerprint,
                        context,
                        json.dumps(observed_inputs, ensure_ascii=False, sort_keys=True),
                        int(bool(observed_inputs)),
                        int(bool(result.network_observed)),
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM gate_result_successes WHERE updated_at < ?",
                    (now - self.max_age_seconds,),
                )
                connection.execute(
                    """
                    DELETE FROM gate_result_successes
                    WHERE cache_key IN (
                        SELECT cache_key FROM gate_result_successes
                        ORDER BY updated_at DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (MAX_ROWS,),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self.disabled = True

    def _manifest_matches(self, manifest: Mapping[str, object]) -> bool:
        if not manifest:
            return False
        for raw_path, expected in manifest.items():
            relative = str(raw_path).replace("\\", "/").strip()
            missing = relative.startswith("!")
            if missing:
                relative = relative[1:]
            if (
                not relative
                or relative.startswith("/")
                or ".." in Path(relative).parts
            ):
                return False
            if missing:
                if (self.project_root / relative).exists():
                    return False
                continue
            if _path_digest(self.project_root / relative) != str(expected):
                return False
        return True

    @staticmethod
    def _cached_result(command: str, backend: str) -> CommandResult:
        return CommandResult(
            command=command,
            ok=True,
            returncode=0,
            duration_seconds=0.0,
            backend=backend,
            cached=True,
        )

    @staticmethod
    def _certificate_result(command: str, raw_payload: str) -> CommandResult:
        payload = json.loads(raw_payload or "{}")
        return CommandResult(
            command=command,
            ok=bool(payload.get("ok")),
            returncode=int(payload.get("returncode", 0)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            comparable_failures=bool(payload.get("comparable_failures", False)),
            duration_seconds=0.0,
            backend="proof-certificate-candidate",
            cached=True,
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(self.cache_path), timeout=2.0)) as connection:
            with connection:
                self._initialize_connection(connection)
                yield connection

    @staticmethod
    def _initialize_connection(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_proof_certificates (
                cache_key TEXT PRIMARY KEY,
                identity_key TEXT NOT NULL,
                command TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                context_fingerprint TEXT NOT NULL,
                result_payload TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS gate_proof_certificate_identity
            ON gate_proof_certificates(identity_key, context_fingerprint, updated_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_result_successes (
                cache_key TEXT PRIMARY KEY,
                identity_key TEXT NOT NULL,
                command TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                context_fingerprint TEXT NOT NULL,
                observed_inputs TEXT NOT NULL,
                trace_complete INTEGER NOT NULL,
                network_observed INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS gate_result_success_identity
            ON gate_result_successes(identity_key, context_fingerprint, updated_at)
            """
        )
