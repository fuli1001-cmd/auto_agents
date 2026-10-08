import os, json
from pathlib import Path
import pytest
from auto_agents.control import Store
from auto_agents.control.cleanup import register, release, collect


def test_completed_resources_are_deleted_but_active_inputs_survive(tmp_path):
    store = Store(tmp_path)
    owned = tmp_path / ".auto-agents/state/owned"
    owned.mkdir()
    disposable = owned / "operation"
    disposable.mkdir()
    (disposable / "data").write_bytes(b"x" * 1024)
    current = owned / "active"
    current.mkdir()
    register(store, disposable, "scratch", "done", references=("done",))
    register(store, current, "scratch", "running", references=("running",))
    assert collect(store)["removed"] == []
    release(store, "done")
    assert collect(store)["ok"] and not disposable.exists() and current.exists()
    assert collect(store)["removed"] == []


def test_cleanup_rejects_replaced_or_external_resource(tmp_path):
    store = Store(tmp_path)
    owned = tmp_path / ".auto-agents/state/owned"
    owned.mkdir()
    path = owned / "original"
    path.mkdir()
    register(store, path, "scratch", "done")
    release(store, "done")
    path.rename(owned / "saved")
    path.symlink_to(tmp_path, target_is_directory=True)
    result = collect(store)
    assert not result["ok"] and (owned / "saved").exists()


def test_raw_results_compact_only_after_consumed_terminal_work(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "goal", "source")
    work = store.transition(work, "RUNNING")
    op = store.reserve(work, "model", 0, {}, model=True)
    store.settle(op["id"], {"raw": "x" * 100000})
    store.transition(work, "COMPLETED", operation=op["id"])
    pending = store.create_workflow("fix", "pending", "source")
    pending = store.transition(pending, "RUNNING")
    unknown = store.reserve(pending, "model", 0, {}, model=True)
    store.settle(unknown["id"], {"raw": "x" * 100000}, state="UNKNOWN")
    collect(store)
    rows = {x["id"]: x for x in store.operations()}
    assert rows[op["id"]]["result"] == {"compacted": True}
    assert len(rows[unknown["id"]]["result"]["raw"]) == 100000
    assert store.workflow(work["workflow"])["calls"] == 1


def test_replaced_owned_parent_cannot_redirect_cleanup_to_user_files(tmp_path):
    store = Store(tmp_path)
    owned = tmp_path / ".auto-agents/state/owned"
    owned.mkdir()
    cache = owned / "cache"
    cache.mkdir()
    (cache / "evidence").write_text("preserve")
    register(store, cache, "cache", "terminal")
    release(store, "terminal")
    saved = tmp_path / "user-preserved"
    owned.rename(saved)
    owned.symlink_to(saved, target_is_directory=True)
    report = collect(store)
    assert not report["ok"] and (saved / "cache/evidence").read_text() == "preserve"


def test_cancel_cannot_delete_cache_used_by_an_unknown_operation(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Keep unknown lease", "source")
    work = store.transition(work, "RUNNING")
    operation = store.reserve(work, "verify", 0, {}, model=False)
    store.settle(operation["id"], {"reason": "children alive"}, state="UNKNOWN")
    cache = tmp_path / ".auto-agents/state/owned/verification" / work["id"]
    cache.mkdir(parents=True)
    (cache / "trace").write_text("retained live input")
    register(store, cache, "cache", work["id"], references=[work["id"]])
    store.transition(work, "CANCELLED")
    release(store, work["id"])
    assert not collect(store)["removed"] and (cache / "trace").is_file()


def test_new_control_storage_is_ignored_without_hiding_domain_docs(tmp_path):
    from auto_agents.config import ensure_auto_gitignore
    from auto_agents.control.workspace import git

    git(tmp_path, "init")
    ensure_auto_gitignore(tmp_path)
    for name in [
        ".auto-agents/state/owned/cache/data",
        ".auto-agents/state/sessions/old.json",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime")
        assert git(tmp_path, "check-ignore", name) == name
    docs = tmp_path / ".auto-agents/docs/result.md"
    docs.parent.mkdir()
    docs.write_text("public result")
    import subprocess

    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", str(docs)], capture_output=True
        ).returncode
        == 1
    )
