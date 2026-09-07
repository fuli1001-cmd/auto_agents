from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from auto_agents.artifact_store import ArtifactStore, DAY


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_AGENTS_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("AUTO_AGENTS_STORAGE_MAINTENANCE", "off")
    return ArtifactStore()


def artifact(store, tmp_path, kind="scratch", **kwargs):
    path = tmp_path / ("artifact-" + str(time.time_ns()))
    path.mkdir()
    (path / "data").write_text("kept content")
    identity = store.register(path, kind=kind, **kwargs)
    store.release(identity)
    age(store, identity)
    return identity, path


def age(store, identity, days=40):
    row = store.get(identity)
    row.update(last_used=time.time() - days * DAY, released=time.time() - days * DAY)
    store._save(row)


def apply(store):
    return store.apply(store.plan()["id"])


def test_status_does_not_create_storage(store):
    assert store.status()["items"] == []
    assert not store.root.exists()


def test_expired_scratch_deleted_but_unknown_neighbor_retained(store, tmp_path):
    identity, path = artifact(store, tmp_path)
    unknown = tmp_path / "unknown.tmp"
    unknown.write_text("user data")
    result = apply(store)
    assert result["ok"]
    assert not path.exists()
    assert unknown.read_text() == "user data"
    assert store.get(identity)["state"] == "deleted"


def test_active_owner_is_protected_even_with_old_directory_mtime(store, tmp_path):
    path = tmp_path / "active"
    path.mkdir()
    identity = store.register(path)
    age(store, identity)
    os.utime(path, (1, 1))
    assert store.plan()["items"][0]["reason"] == "active_process"
    apply(store)
    assert path.exists()


def test_exited_process_lease_can_be_reconciled(store, tmp_path):
    path = tmp_path / "crashed"
    path.mkdir()
    child = subprocess.run([sys.executable, "-c",
        "from auto_agents.artifact_store import ArtifactStore; import sys; print(ArtifactStore().register(sys.argv[1]))", str(path)],
        capture_output=True, text=True, check=True, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    identity = child.stdout.strip()
    age(store, identity)
    assert apply(store)["ok"]
    assert not path.exists()


def test_plan_revalidates_new_reference_and_new_inode(store, tmp_path):
    identity, path = artifact(store, tmp_path)
    plan = store.plan()
    store.pin(identity, "needed for review")
    assert store.apply(plan["id"])["results"][0]["result"] == "skipped_changed"
    assert path.exists()
    store.pin(identity, "")
    plan = store.plan()
    path.rename(path.with_name("saved"))
    path.mkdir()
    (path / "new").write_text("new owner")
    result = store.apply(plan["id"])
    assert result["results"][0]["result"] == "skipped"
    assert (path / "new").read_text() == "new owner"


def test_cache_quarantine_restore_and_purge(store, tmp_path):
    identity, path = artifact(store, tmp_path, "cache")
    assert apply(store)["results"][0]["result"] == "quarantined"
    assert not path.exists()
    row = store.get(identity)
    assert Path(row["trash"]).exists()
    assert apply(store)["results"] == []
    store.restore(identity)
    assert (path / "data").read_text() == "kept content"
    age(store, identity)
    apply(store)
    row = store.get(identity)
    row["purge_after"] = time.time() - 1
    store._save(row)
    assert apply(store)["results"][0]["result"] == "deleted"
    assert not Path(row["trash"]).exists()


def test_symlinks_cannot_escape_and_hardlinks_are_not_modified(store, tmp_path):
    identity, path = artifact(store, tmp_path)
    user = tmp_path / "user-data"
    user.write_text("preserve")
    (path / "link").symlink_to(user)
    os.link(user, path / "hardlink")
    apply(store)
    assert user.read_text() == "preserve"
    linked = tmp_path / "external"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        store.register(linked / "user-data")


def test_parent_replacement_blocks_deletion(store, tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    identity, path = artifact(store, parent)
    plan = store.plan()
    parent.rename(tmp_path / "moved")
    parent.symlink_to(tmp_path / "moved", target_is_directory=True)
    result = store.apply(plan["id"])
    assert result["results"][0]["result"] == "skipped"
    assert path.exists()


def test_blocked_workflow_and_necessary_evidence_remain_protected(store, tmp_path):
    project = tmp_path / "project"
    state = project / ".auto-agents/state"
    state.mkdir(parents=True)
    (state / "run_state.json").write_text('{"status":"blocked"}')
    identity, path = artifact(store, tmp_path, "recovery", metadata={"project": str(project), "disposable": True})
    assert "recoverable_workflow" in store.plan()["items"][0]["reason"]
    assert apply(store)["results"] == []
    (state / "run_state.json").write_text('{"status":"completed"}')
    assert apply(store)["results"][0]["result"] == "quarantined"
    identity, evidence = artifact(store, tmp_path, "evidence")
    assert store.classify(store.get(identity), time.time()) == "evidence_contract_requires_retention"


def test_shared_resource_requires_all_leases_and_references_to_release(store, tmp_path):
    path = tmp_path / "shared"
    path.mkdir()
    identity = store.register(path, kind="environment", reference="job:one")
    store.register(path, kind="environment", reference="job:two")
    store.release(identity, reference="job:one")
    age(store, identity)
    assert "job:two" in store.plan()["items"][0]["reason"]
    store.release(identity, reference="job:two")
    age(store, identity)
    assert apply(store)["results"][0]["result"] == "quarantined"


def test_expired_plan_is_rejected(store, tmp_path):
    identity, path = artifact(store, tmp_path)
    plan = store.plan()
    plan["expires"] = 0
    with store.connect(True) as db:
        db.execute("UPDATE plans SET data=? WHERE id=?", (json.dumps(plan), plan["id"]))
    with pytest.raises(ValueError, match="expired"):
        store.apply(plan["id"])
    assert path.exists()


def test_temporary_directory_producer_registers_and_releases(store, tmp_path):
    from auto_agents.artifact_runtime import command_context
    from auto_agents import artifact_temp
    with command_context(tmp_path):
        with artifact_temp.TemporaryDirectory(dir=tmp_path) as path:
            assert any(r["path"] == path for r in store.rows())
        assert not Path(path).exists()
    store.plan()
    assert store.rows()[0]["state"] == "deleted"


def test_cli_storage_plan_and_apply(store, tmp_path, capsys):
    from auto_agents.cli import main
    identity, path = artifact(store, tmp_path)
    assert main(["storage", "plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert path.exists()
    assert main(["storage", "apply", "--plan", plan["id"]]) == 0
    assert not path.exists()


def test_old_short_runtime_marker_does_not_authorize_deletion(tmp_path, monkeypatch):
    from auto_agents.gate_execution import short_job_runtime_root
    # The producer no longer performs prefix/mtime-based garbage collection.
    first = short_job_runtime_root("storage-test-first")
    try:
        os.utime(first, (1, 1))
        second = short_job_runtime_root("storage-test-second")
        assert first.exists()
    finally:
        import shutil
        shutil.rmtree(first, ignore_errors=True)
        if "second" in locals():
            shutil.rmtree(second, ignore_errors=True)


def test_crash_before_quarantine_rename_preserves_original(store, tmp_path, monkeypatch):
    identity, path = artifact(store, tmp_path, "cache")
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "rename", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("interrupted")))
        assert not apply(store)["ok"]
    assert store.get(identity)["state"] == "quarantining"
    store.plan()
    assert path.exists()
    assert store.get(identity)["state"] == "released"
    age(store, identity)
    assert apply(store)["ok"]


def test_partial_deletion_is_retried_after_restart(store, tmp_path, monkeypatch):
    from auto_agents import artifact_store
    identity, path = artifact(store, tmp_path)
    with monkeypatch.context() as scoped:
        def interrupted(fd, name, device, deadline):
            (path / "data").unlink(missing_ok=True)
            raise TimeoutError("worker interrupted")
        scoped.setattr(artifact_store, "_remove_at", interrupted)
        assert not apply(store)["ok"]
    assert store.get(identity)["state"] == "deleting"
    restored = ArtifactStore(store.root)
    assert apply(restored)["ok"]
    assert not path.exists()


def test_pressure_cannot_override_pin_or_recovery_references(store, tmp_path):
    identity, path = artifact(store, tmp_path, "environment")
    store.pin(identity, "baseline Python")
    plan = store.plan(pressure=True)
    assert "pinned" in plan["items"][0]["reason"]
    store.apply(plan["id"])
    assert path.exists()


def test_worker_artifacts_require_matching_durable_ack(store, tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from auto_agents.workers import worker_ack_artifacts
    worker = tmp_path / "worker"
    archive = worker / "artifacts/job-one.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"transferred archive")
    jobs = worker / "jobs"
    jobs.mkdir()
    job = jobs / "job-one.json"
    job.write_text(json.dumps({"job_id": "job-one", "state": "terminal", "artifact_archive": str(archive)}))
    identity = store.register(archive, kind="evidence", scope="worker:" + str(worker),
        metadata={"worker_root": str(worker), "job": "job-one", "disposable": True})
    store.release(identity)
    age(store, identity)
    monkeypatch.setattr("auto_agents.workers.load_local_worker_config", lambda: SimpleNamespace(managed_root=worker))
    assert store.classify(store.get(identity), time.time()) == "artifact_not_acknowledged"
    with pytest.raises(ValueError, match="hash mismatch"):
        worker_ack_artifacts("job-one", "wrong")
    assert "artifact_ack" not in json.loads(job.read_text())
    worker_ack_artifacts("job-one", hashlib.sha256(archive.read_bytes()).hexdigest())
    assert apply(store)["results"][0]["result"] == "quarantined"
    assert job.exists()


def test_cache_row_pruning_releases_only_unreferenced_blobs(store, tmp_path):
    import sqlite3
    from auto_agents.artifact_cache import maintain_caches
    database = tmp_path / "proofs.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE gate_proof_certificates(result_payload TEXT,updated_at REAL)")
        db.execute("INSERT INTO gate_proof_certificates VALUES(?,?)", (json.dumps({"artifacts": {"proof.txt": "blob-id"}}), time.time() - 40 * DAY))
    db_id = store.register(database, kind="permanent", metadata={"cache_database": True})
    store.release(db_id)
    blob = tmp_path / "blob-id"
    blob.write_text("proof")
    identity = store.register(blob, kind="cache", metadata={"proof_database": str(database), "blob": "blob-id"})
    store.release(identity)
    age(store, identity)
    assert store.classify(store.get(identity), time.time()) == "referenced_proof_blob"
    results = maintain_caches(store, time.monotonic() + 5)
    assert results[0]["result"] == "cache_rows_pruned"
    assert store.classify(store.get(identity), time.time()) == "eligible"
    assert database.exists()


def test_dirty_worktree_is_preserved_then_clean_worktree_removed(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "base")
    worktree = tmp_path / "worktree"
    git("worktree", "add", "--detach", str(worktree))
    identity = store.register(worktree, kind="worktree", metadata={"repository": str(repo)})
    store.release(identity)
    age(store, identity)
    (worktree / "unfinished.bin").write_bytes(b"\x00\xff")
    assert "dirty" in store.plan()["items"][0]["reason"]
    assert apply(store)["results"] == []
    (worktree / "unfinished.bin").unlink()
    assert apply(store)["ok"]
    assert not worktree.exists()
    assert str(worktree) not in git("worktree", "list", "--porcelain").stdout


def test_status_uses_index_without_scanning_live_worktrees(store, tmp_path, monkeypatch):
    identity, path = artifact(store, tmp_path)
    store.plan()
    monkeypatch.setattr(store, "protection", lambda row: (_ for _ in ()).throw(AssertionError("status must not scan references")))
    assert store.status()["items"][0]["reason"] == "eligible"


def test_completed_session_cannot_delete_live_recovery_history(store, tmp_path):
    from auto_agents.config import create_session, save_session_state, delete_session
    state = create_session(tmp_path, "collab")
    state.conversation = [{"role": "agent", "content": "pending recovery"}]
    save_session_state(tmp_path, state)
    with pytest.raises(ValueError, match="recoverable"):
        delete_session(tmp_path, state.session_id)


def test_two_consumers_in_one_worker_keep_distinct_leases(store, tmp_path):
    from auto_agents.artifact_store import process_identity
    path = tmp_path / "shared-environment"
    path.mkdir()
    one = {**process_identity(), "token": "thread-one"}
    two = {**process_identity(), "token": "thread-two"}
    identity = store.register(path, kind="environment", owner=one)
    store.register(path, kind="environment", owner=two)
    store.release(identity, owner=one)
    age(store, identity)
    assert store.classify(store.get(identity), time.time()) == "active_process"
    store.release(identity, owner=two)
    age(store, identity)
    assert store.classify(store.get(identity), time.time()) == "eligible"


def test_deleted_tombstones_do_not_starve_later_scan_pages(store, tmp_path):
    identity, path = artifact(store, tmp_path)
    apply(store)
    other, existing = artifact(store, tmp_path)
    plan = store.plan(limit=1)
    assert [item["id"] for item in plan["items"]] == [other]
    store.apply(plan["id"])
    assert not existing.exists()


def test_configured_budget_and_retention_are_visible_and_pin_still_wins(store, tmp_path):
    identity, path = artifact(store, tmp_path, "cache")
    (store.root / "policy.json").write_text(json.dumps({"budgets": {"user": 1024}, "retention_days": {"cache": 365}}))
    assert store.status()["policy"]["budgets"]["user"] == 1024
    assert store.plan()["items"][0]["reason"] == "retention"
    store.pin(identity, "necessary")
    assert "pinned" in store.plan(pressure=True)["items"][0]["reason"]


def test_restore_does_not_overwrite_a_dangling_user_symlink(store, tmp_path):
    identity, path = artifact(store, tmp_path, "cache")
    apply(store)
    path.symlink_to(tmp_path / "not-created-yet")
    with pytest.raises(ValueError, match="occupied"):
        store.restore(identity)
    assert path.is_symlink()


def test_unknown_lifecycle_version_cannot_be_deleted(store, tmp_path):
    identity, path = artifact(store, tmp_path)
    row = store.get(identity)
    row["schema_version"] = 999
    store._save(row)
    assert "unsupported" in store.plan()["items"][0]["reason"]
    assert apply(store)["results"] == []
    assert path.exists()


def test_legacy_remote_worker_does_not_receive_unsafe_gc(monkeypatch):
    from auto_agents.worker_service import WorkerClient
    client = object.__new__(WorkerClient)
    monkeypatch.setattr(client, "probe", lambda: {"protocol_version": 4})
    monkeypatch.setattr(client, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not mutate legacy worker")))
    assert not client.gc(0)["ok"]
    assert not client.cleanup_plan("project", "plan")["ok"]
