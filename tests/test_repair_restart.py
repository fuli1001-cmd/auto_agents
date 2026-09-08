from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.repair_control import Repository, Store, atomic_json, git, start_ticks
from auto_agents.repair_restart import import_cancelled_repair
from auto_agents.repair_worker import carry_continuous_work
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairExperiment, SelfRepairExperimentStore
from test_repair_control import configuration, make_remote, registration


@pytest.fixture
def restart(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    project = tmp_path / "project"
    project.mkdir()
    (project / "user.txt").write_text("keep target untouched")
    payload = {
        "project": str(project), "base": base, "fingerprint": "root",
        "contract": {"expected_postconditions": ["resume the retained child"]},
        "environment": "engine-python", "provider": "codex", "autonomy": "max",
        "invocation": {"session_id": "session", "auto_approve": True},
    }
    store = Store(config["root"])
    old = store.submit(store.register(registration(project, "old")), payload)
    old_root = store.root / "jobs" / old
    retained = old_root / "continuous/repair"
    retained.parent.mkdir(parents=True)
    git(repository.cache, "worktree", "add", "--detach", str(retained), base)
    (retained / "bug.py").write_text("reviewed partial repair\n")
    (retained / "remove.py").write_text("obsolete\n")
    git(retained, "add", "-A")
    git(retained, "commit", "-m", "reviewed candidate")
    commit = git(retained, "rev-parse", "HEAD")
    (retained / "bug.py").write_text("staged partial repair\n")
    git(retained, "add", "bug.py")
    (retained / "bug.py").write_text("unfinished latest repair\n")
    (retained / "remove.py").unlink()
    (retained / "new regression.py").write_text("new test from cancelled attempt\n")
    atomic_json(retained.parent / "base.json", {"revision": base})
    atomic_json(retained.parent / "provider.json", {"continuation": "old native session"})
    evidence = SelfRepairExperimentStore(old_root / "working-evidence", "session-session", "root")
    experiment = SelfRepairExperiment.create(
        run_id="session-session", root_fingerprint="root", category="engine", base_commit=base,
        expected_postconditions=["resume the retained child"],
    )
    experiment.candidates["c1"] = SelfRepairCandidateRecord(
        candidate_id="c1", candidate_commit=commit, candidate_ref="refs/candidate/c1",
        status="candidate_full_suite_inconclusive", validation_stage="full_suite", validation_rank=90,
        passed_obligations=["validation:focused", "validation:full_suite"],
        patch_fingerprint="old-fingerprint", component_receipts={"ownership": commit},
        provider_session_id="old-native-session", provider_prompt_hash="old-hash",
    )
    experiment.attempt_count = 1
    experiment.best_search_candidate_id = "c1"
    experiment.best_search_ref = commit
    evidence.save(experiment)
    evidence.write_candidate_artifact("c1", "result.json", {
        "candidate_id": "c1", "experiment_id": experiment.experiment_id,
        "candidate_commit": commit, "review_findings": [{"finding_id": "retain-owner", "reason": "keep the child's owner"}],
    })
    evidence.write_candidate_artifact("c1", "full-suite-checkpoint.json", {"ok": True})
    store.cancel(job=old)
    return SimpleNamespace(config=config, engine=engine, repository=repository, store=store,
                           payload=payload, old=old, source=retained, evidence=evidence, project=project)


def _new_job(restart, payload):
    subscriber = restart.store.register(registration(Path(payload["project"]), "new"))
    identity = restart.store.submit(subscriber, payload)
    job = restart.store.job(identity)
    working = restart.store.root / "jobs" / identity / "working-evidence"
    working.mkdir(parents=True)
    return job, working


@pytest.mark.parametrize("advance", [False, True])
def test_cancel_then_original_invocation_retains_code_and_history_but_requires_new_proof(restart, advance):
    before = (git(restart.source, "status", "--porcelain"), git(restart.source, "diff"), git(restart.source, "diff", "--cached"))
    revision = restart.payload["base"]
    if advance:
        (restart.engine / "upstream.txt").write_text("new engine version\n")
        git(restart.engine, "add", "upstream.txt")
        git(restart.engine, "commit", "-m", "engine advanced")
        git(restart.engine, "push", restart.config["remote"], "HEAD:master")
        revision, _ = restart.repository.fetch()
    job, working = _new_job(restart, {**restart.payload, "base": revision})
    assert job["id"] != restart.old
    assert import_cancelled_repair(restart.store, job, working, restart.repository)
    directory = working.parent
    retained = directory / "continuous/repair"
    carry_continuous_work(SimpleNamespace(_continuous_workspace=retained.parent), revision)
    assert (retained / "bug.py").read_text() == "unfinished latest repair\n"
    assert not (retained / "remove.py").exists()
    assert (retained / "new regression.py").read_text() == "new test from cancelled attempt\n"
    if advance:
        assert (retained / "upstream.txt").read_text() == "new engine version\n"
    copied = SelfRepairExperimentStore(working, "session-session", "root")
    experiment = copied.load()
    assert experiment.experiment_id == restart.evidence.load().experiment_id
    assert experiment.attempt_count == 1
    assert experiment.best_search_candidate_id == experiment.best_safe_candidate_id == "base"
    record = experiment.candidates["c1"]
    assert record.validation_rank == 0 and record.fatal
    assert not record.passed_obligations and not record.component_receipts
    assert not record.patch_fingerprint and not record.provider_session_id
    assert not (retained.parent / "provider.json").exists()
    assert not (copied.candidate_root("c1") / "full-suite-checkpoint.json").exists()
    assert json.loads((copied.candidate_root("c1") / "result.json").read_text())["review_findings"]
    receipt = json.loads((directory / "prior-repair-import.json").read_text())
    assert receipt["source_job"] == restart.old and receipt["verification_required"]
    assert receipt["candidate_ids"] == ["c1"]
    from auto_agents.repair_client import _repair_progress_message
    restart.store.transition(job["id"], "repairing")
    message = _repair_progress_message(restart.store.job(job["id"], include_progress=True), {"state": "waiting"})
    assert "已接续上次候选" in message and restart.old[:8] in message
    assert restart.store.job(restart.old)["state"] == "cancelled"
    assert restart.store.subscriptions(restart.old)[0]["state"] == "cancelled"
    assert (restart.project / "user.txt").read_text() == "keep target untouched"
    assert before == (git(restart.source, "status", "--porcelain"), git(restart.source, "diff"), git(restart.source, "diff", "--cached"))
    (retained / "latest.txt").write_text("new job progress")
    assert not import_cancelled_repair(restart.store, job, working, restart.repository)
    assert (retained / "latest.txt").read_text() == "new job progress"


@pytest.mark.parametrize("change", ["project", "session", "contract", "environment", "provider", "autonomy", "live_worker", "live_child"])
def test_restart_never_adopts_different_scope_or_still_running_work(restart, change):
    payload = json.loads(json.dumps(restart.payload))
    if change == "project":
        payload["project"] += "-other"
    elif change == "session":
        payload["invocation"]["session_id"] = "another-session"
    elif change == "contract":
        payload["contract"]["expected_postconditions"] = ["another request"]
    elif change in {"environment", "provider", "autonomy"}:
        payload[change] = "changed"
    elif change == "live_worker":
        atomic_json(restart.source.parent.parent / "repair-g1-lease.json", {"pid": os.getpid(), "ticks": start_ticks(os.getpid())})
    elif change == "live_child":
        atomic_json(restart.source.parent.parent / "processes.json", {"processes": [{"pid": os.getpid(), "start_ticks": start_ticks(os.getpid())}]})
    job, working = _new_job(restart, payload)
    assert not import_cancelled_repair(restart.store, job, working, restart.repository)
    assert not (working.parent / "continuous/repair").exists()


def test_restart_does_not_replace_an_attempt_already_started(restart):
    job, working = _new_job(restart, restart.payload)
    state = SelfRepairExperimentStore(working, "session-session", "root")
    experiment = SelfRepairExperiment.create(run_id="session-session", root_fingerprint="root", category="engine", base_commit=restart.payload["base"])
    experiment.attempt_count = 1
    state.save(experiment)
    assert not import_cancelled_repair(restart.store, job, working, restart.repository)
    assert state.load().experiment_id == experiment.experiment_id
