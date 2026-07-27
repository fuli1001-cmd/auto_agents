from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import gate_baseline_cache_path
from .gates import build_failure_identity_diagnostic_command, extract_failure_info
from .models import CommandResult, GateResult


CACHE_VERSION = 4
EXECUTION_MODE_VERSION = 5
MAX_SUMMARY_BYTES = 8 * 1024
MAX_ROWS = 5_000
MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _normalized_commands(commands: Sequence[str]) -> List[str]:
    return [str(command).strip() for command in commands if str(command).strip()]


def _command_modes(
    commands: Sequence[str], parallel_groups: Sequence[object]
) -> List[Tuple[str, str]]:
    items = [(command, "sequential") for command in _normalized_commands(commands)]
    for group in parallel_groups:
        name = str(getattr(group, "name", "")).strip() or "parallel"
        items.extend(
            (command, f"parallel:{name}")
            for command in _normalized_commands(getattr(group, "commands", []))
        )
    return items


def make_cache_key(
    baseline_ref: str,
    commands: Sequence[str],
    *,
    collect_all: bool,
    parallel_groups: Sequence[object] = (),
    environment_fingerprint: str = "",
) -> str:
    payload = {
        "baseline_ref": str(baseline_ref).strip(),
        "commands": _command_modes(commands, parallel_groups),
        "collect_all": bool(collect_all),
        "execution_mode_version": EXECUTION_MODE_VERSION,
        "environment_fingerprint": str(environment_fingerprint),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _entry_key(
    baseline_ref: str,
    command: str,
    mode: str,
    collect_all: bool,
    environment_fingerprint: str = "",
) -> str:
    payload = {
        "baseline_ref": str(baseline_ref).strip(),
        "command": str(command).strip(),
        "mode": str(mode),
        "collect_all": bool(collect_all),
        "execution_mode_version": EXECUTION_MODE_VERSION,
        "environment_fingerprint": str(environment_fingerprint),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class GateBaselineCache:
    """Best-effort command-level clean-head baseline cache backed by SQLite."""

    def __init__(
        self,
        project_root: Path,
        cache_path: Optional[Path] = None,
        *,
        environment_fingerprint: str = "",
    ) -> None:
        self.project_root = Path(project_root)
        self.cache_path = cache_path or gate_baseline_cache_path(self.project_root)
        self.environment_fingerprint = str(environment_fingerprint)
        self.disabled = False

    def get(
        self,
        baseline_ref: str,
        commands: Sequence[str],
        *,
        collect_all: bool,
        parallel_groups: Sequence[object] = (),
    ) -> Optional[List[str]]:
        entries = _command_modes(commands, parallel_groups)
        if not entries or self.disabled:
            return [] if not entries else None
        try:
            with self._connect() as connection:
                failures: List[str] = []
                for command, mode in entries:
                    row = connection.execute(
                        "SELECT failure_ids, mutation_detected FROM command_entries WHERE cache_key = ?",
                        (
                            _entry_key(
                                baseline_ref,
                                command,
                                mode,
                                collect_all,
                                self.environment_fingerprint,
                            ),
                        ),
                    ).fetchone()
                    if row is None or bool(row[1]):
                        return None
                    decoded = json.loads(row[0])
                    if not isinstance(decoded, list):
                        return None
                    failures.extend(str(item) for item in decoded if str(item).strip())
                return list(dict.fromkeys(failures))
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            self.disabled = True
            return None

    def missing_commands(
        self,
        baseline_ref: str,
        commands: Sequence[str],
        *,
        collect_all: bool,
        parallel_groups: Sequence[object] = (),
    ) -> List[str]:
        if self.disabled:
            return [command for command, _ in _command_modes(commands, parallel_groups)]
        missing: List[str] = []
        try:
            with self._connect() as connection:
                for command, mode in _command_modes(commands, parallel_groups):
                    row = connection.execute(
                        "SELECT mutation_detected FROM command_entries WHERE cache_key = ?",
                        (
                            _entry_key(
                                baseline_ref,
                                command,
                                mode,
                                collect_all,
                                self.environment_fingerprint,
                            ),
                        ),
                    ).fetchone()
                    if row is None or bool(row[0]):
                        missing.append(command)
        except (OSError, sqlite3.Error):
            self.disabled = True
            return [command for command, _ in _command_modes(commands, parallel_groups)]
        return missing

    def promote(
        self,
        source_ref: str,
        target_ref: str,
        commands: Sequence[str],
        *,
        collect_all: bool,
        parallel_groups: Sequence[object] = (),
    ) -> int:
        """Copy known pre-implementation command results to a new run ref.

        A successful task advances HEAD and an interrupted task changes the
        worktree fingerprint, but neither event should redefine the baseline
        captured before implementation. Promotion preserves that baseline
        while leaving new or previously missing commands uncached.
        """
        if self.disabled or not source_ref or source_ref == target_ref:
            return 0
        promoted = 0
        now = int(time.time())
        try:
            with self._connect() as connection:
                for command, mode in _command_modes(commands, parallel_groups):
                    row = connection.execute(
                        """
                        SELECT failure_ids, mutation_detected, summary
                        FROM command_entries WHERE cache_key = ?
                        """,
                        (
                            _entry_key(
                                source_ref,
                                command,
                                mode,
                                collect_all,
                                self.environment_fingerprint,
                            ),
                        ),
                    ).fetchone()
                    if row is None or bool(row[1]):
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO command_entries (
                            cache_key, baseline_ref, command, mode, collect_all,
                            execution_mode_version, failure_ids, mutation_detected,
                            summary, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _entry_key(
                                target_ref,
                                command,
                                mode,
                                collect_all,
                                self.environment_fingerprint,
                            ),
                            str(target_ref).strip(),
                            command,
                            mode,
                            int(bool(collect_all)),
                            EXECUTION_MODE_VERSION,
                            row[0],
                            0,
                            row[2],
                            now,
                        ),
                    )
                    promoted += 1
                self._prune(connection, now)
        except (OSError, sqlite3.Error):
            self.disabled = True
            return 0
        return promoted

    def put(
        self,
        baseline_ref: str,
        commands: Sequence[str],
        *,
        collect_all: bool,
        failure_ids: Sequence[str],
        mutation_detected: bool = False,
        summary: str = "",
        parallel_groups: Sequence[object] = (),
        command_results: Optional[Sequence[CommandResult]] = None,
    ) -> None:
        if self.disabled:
            return
        modes = _command_modes(commands, parallel_groups)
        if not modes:
            return
        results_by_command: Dict[str, List[CommandResult]] = {}
        for result in command_results or []:
            results_by_command.setdefault(str(result.command).strip(), []).append(result)
        # Compatibility for callers that already hold only an aggregate result.
        # New orchestrator paths always provide command_results, preserving true
        # per-command ownership of failures.
        aggregate_fallback = not results_by_command
        now = int(time.time())
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                for mode_index, (command, mode) in enumerate(modes):
                    result_list = results_by_command.get(command, [])
                    if result_list:
                        result = result_list.pop(0)
                        extraction = extract_failure_info(
                            GateResult(ok=result.ok, commands=[result], summary="")
                        )
                        invalid_test_identity = bool(
                            not result.ok
                            and not extraction.comparable
                            and (
                                build_failure_identity_diagnostic_command(command)
                                or "unittest" in command
                            )
                        )
                        if (
                            result.termination_reason
                            or result.cleanup_incomplete
                            or result.infrastructure_error
                            or invalid_test_identity
                        ):
                            connection.execute(
                                "DELETE FROM command_entries WHERE cache_key = ?",
                                (
                                    _entry_key(
                                        baseline_ref,
                                        command,
                                        mode,
                                        collect_all,
                                        self.environment_fingerprint,
                                    ),
                                ),
                            )
                            continue
                        command_failures = extraction.failure_ids if not result.ok else []
                        command_summary = result.stderr or result.stdout or summary
                    elif aggregate_fallback:
                        command_failures = [
                            str(item).strip() for item in failure_ids if str(item).strip()
                        ] if mode_index == 0 else []
                        command_summary = summary
                    else:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO command_entries (
                            cache_key, baseline_ref, command, mode, collect_all,
                            execution_mode_version, failure_ids, mutation_detected,
                            summary, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _entry_key(
                                baseline_ref,
                                command,
                                mode,
                                collect_all,
                                self.environment_fingerprint,
                            ),
                            str(baseline_ref).strip(),
                            command,
                            mode,
                            int(bool(collect_all)),
                            EXECUTION_MODE_VERSION,
                            json.dumps(command_failures, ensure_ascii=False),
                            int(bool(mutation_detected)),
                            _bounded_summary(command_summary),
                            now,
                        ),
                    )
                self._prune(connection, now)
        except (OSError, sqlite3.Error):
            self.disabled = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.cache_path), timeout=1.0)
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
            connection.execute("DROP TABLE IF EXISTS command_entries")
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('version', ?)",
            (str(CACHE_VERSION),),
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS command_entries (
                cache_key TEXT PRIMARY KEY,
                baseline_ref TEXT NOT NULL,
                command TEXT NOT NULL,
                mode TEXT NOT NULL,
                collect_all INTEGER NOT NULL,
                execution_mode_version INTEGER NOT NULL,
                failure_ids TEXT NOT NULL,
                mutation_detected INTEGER NOT NULL,
                summary TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        return connection

    @staticmethod
    def _prune(connection: sqlite3.Connection, now: int) -> None:
        connection.execute(
            "DELETE FROM command_entries WHERE updated_at < ?",
            (now - MAX_AGE_SECONDS,),
        )
        count = int(connection.execute("SELECT COUNT(*) FROM command_entries").fetchone()[0])
        if count > MAX_ROWS:
            connection.execute(
                """
                DELETE FROM command_entries WHERE rowid IN (
                    SELECT rowid FROM command_entries ORDER BY updated_at ASC LIMIT ?
                )
                """,
                (count - MAX_ROWS,),
            )


def _bounded_summary(summary: str) -> str:
    encoded = str(summary).strip().encode("utf-8")
    if len(encoded) <= MAX_SUMMARY_BYTES:
        return encoded.decode("utf-8")
    suffix = b"\n[truncated]"
    return (encoded[: MAX_SUMMARY_BYTES - len(suffix)] + suffix).decode("utf-8", errors="ignore")
