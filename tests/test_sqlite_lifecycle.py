from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from auto_agents.gate_baseline_cache import GateBaselineCache
from auto_agents.gate_result_cache import GateResultCache
from auto_agents.gate_timing import GateTimingStore
from auto_agents.models import CommandResult
from auto_agents.release_jobs import ReleaseJobStore
from auto_agents.requirements_audit_cache import RequirementsAuditCache
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore


@pytest.mark.parametrize("store_kind", ["baseline", "results", "timing", "release", "workflow"])
def test_sqlite_operations_release_connections_without_garbage_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_kind: str,
) -> None:
    connections = []
    original_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    path = tmp_path / "cache.sqlite3"
    result = CommandResult(command="check", ok=True, returncode=0, duration_seconds=2.0)
    try:
        if store_kind == "baseline":
            store = GateBaselineCache(tmp_path, path)
            store.put("head", ["check"], collect_all=True, failure_ids=[], summary="passed")
            assert store.get("head", ["check"], collect_all=True) == []
        elif store_kind == "results":
            store = GateResultCache(tmp_path, cache_path=path)
            metadata = dict(source_fingerprint="head", cache_scope="source",
                            result_cache_scope="candidate", metadata_signature="metadata")
            store.record("check", result, **metadata)
            assert store.lookup("check", **metadata).ok
        elif store_kind == "timing":
            store = GateTimingStore(tmp_path, cache_path=path)
            store.record("check", result)
            assert store.estimate("check") == 2.0
        elif store_kind == "release":
            store = ReleaseJobStore(tmp_path, path=path)
            store.set_worker(status="idle")
            assert store.worker_status()["status"] == "idle"
        else:
            store = WorkflowStore(tmp_path)
            snapshot = store.create_root(WorkflowRef("run", "run-1"))
            store.append_event(snapshot, "progress")
            assert len(store.events(snapshot.workflow_id)) == 2

        assert connections
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("cache_type", [GateBaselineCache, RequirementsAuditCache])
def test_failed_cache_initialization_closes_connection(tmp_path: Path, monkeypatch, cache_type) -> None:
    connections = []
    original_connect = sqlite3.connect

    class BrokenConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("cache initialization failed")

    def broken_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=BrokenConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", broken_connect)
    try:
        cache = cache_type(tmp_path, tmp_path / "broken.sqlite3")
        if cache_type is GateBaselineCache:
            assert cache.get("head", ["check"], collect_all=True) is None
        else:
            assert cache.get("patterns", "source.py", "hash") is None
        assert cache.disabled
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].cursor()
    finally:
        for connection in connections:
            connection.close()


def test_audit_cache_closes_connection_when_commit_fails(tmp_path: Path, monkeypatch) -> None:
    class FailedCommitConnection(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("commit failed")

    connection = sqlite3.connect(tmp_path / "audit.sqlite3", factory=FailedCommitConnection)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)
    cache = RequirementsAuditCache(tmp_path, tmp_path / "audit.sqlite3")
    try:
        cache.put("patterns", "source.py", "hash", [1], [1])
        cache.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.cursor()
    finally:
        connection.close()
