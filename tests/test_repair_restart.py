from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_control import Repository, Store, atomic_json, git, start_ticks
from auto_agents.repair_restart import import_cancelled_repair
from auto_agents.repair_worker import carry_continuous_work
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairExperiment, SelfRepairExperimentStore
from test_repair_control import configuration, make_remote, registration


@pytest.fixture
def restart(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_agents.repair_restart.RESTART_QUIESCENCE_SECONDS", 0)
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


@pytest.mark.parametrize("advance", [False, True, "stale-receipt"])
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
    if advance == "stale-receipt":
        atomic_json(retained.parent / "base.json", {"revision": revision})
    carry_continuous_work(SimpleNamespace(_continuous_workspace=retained.parent), revision)
    assert (retained / "bug.py").read_text() == "unfinished latest repair\n"
    assert not (retained / "remove.py").exists()
    assert (retained / "new regression.py").read_text() == "new test from cancelled attempt\n"
    if advance:
        assert (retained / "upstream.txt").read_text() == "new engine version\n"
        assert git(retained, "merge-base", "--is-ancestor", revision, "HEAD", check=False).returncode == 0
        assert git(retained, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode != 0
        assert not git(retained, "status", "--porcelain")
    copied = SelfRepairExperimentStore(working, "session-session", "root")
    experiment = copied.load()
    assert experiment.base_commit == revision
    assert experiment.candidates["base"].candidate_commit == revision
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
    if change.startswith("live_"):
        with pytest.raises(RuntimeError, match="still stopping"):
            import_cancelled_repair(restart.store, job, working, restart.repository)
    else:
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


@pytest.mark.parametrize("stage", ["base", "receipt", "event"])
def test_interrupted_import_can_be_retried_without_losing_retained_code(restart, stage):
    job, working = _new_job(restart, restart.payload)
    def interrupted_write(path, payload):
        if Path(path).name == ("base.json" if stage == "base" else "prior-repair-import.json"):
            raise KeyboardInterrupt("interrupted import")
        return atomic_json(path, payload)
    with patch("auto_agents.repair_restart.atomic_json", side_effect=interrupted_write) if stage != "event" else patch.object(restart.store, "event", side_effect=KeyboardInterrupt("interrupted event")):
        with pytest.raises(KeyboardInterrupt):
            import_cancelled_repair(restart.store, job, working, restart.repository)
    # A restart either completes the pending import or recognizes its committed
    # state; it must never leave history without the corresponding code.
    import_cancelled_repair(restart.store, job, working, restart.repository)
    retained = working.parent / "continuous/repair"
    assert (retained / "bug.py").read_text() == "unfinished latest repair\n"
    assert (retained / "new regression.py").exists()
    assert (working.parent / "prior-repair-import.json").exists()


def test_still_stopping_latest_job_does_not_silently_start_fresh(restart):
    job, working = _new_job(restart, restart.payload)
    with patch("auto_agents.repair_restart._quiescent", return_value=False):
        with pytest.raises(RuntimeError, match="still stopping"):
            import_cancelled_repair(restart.store, job, working, restart.repository)


def test_process_death_before_import_rollback_is_recovered_on_restart(restart):
    job, working = _new_job(restart, restart.payload)
    def interrupted(path, payload):
        if Path(path).name == "prior-repair-import.json":
            raise KeyboardInterrupt()
        return atomic_json(path, payload)
    with patch("auto_agents.repair_restart.atomic_json", side_effect=interrupted), \
         patch("auto_agents.repair_restart._discard_incomplete_import", side_effect=KeyboardInterrupt()):
        with pytest.raises(KeyboardInterrupt):
            import_cancelled_repair(restart.store, job, working, restart.repository)
    assert (working.parent / "prior-repair-importing.json").exists()
    assert import_cancelled_repair(restart.store, job, working, restart.repository)
    assert (working.parent / "continuous/repair/bug.py").read_text() == "unfinished latest repair\n"
    assert not (working.parent / "prior-repair-importing.json").exists()


def test_import_waits_for_cancelled_worker_cleanup_before_reusing_it(restart):
    job, working = _new_job(restart, restart.payload)
    with patch("auto_agents.repair_restart.RESTART_QUIESCENCE_SECONDS", 1), \
         patch("auto_agents.repair_restart.time.sleep") as sleep, \
         patch("auto_agents.repair_restart._quiescent", side_effect=[False, True, True]):
        assert import_cancelled_repair(restart.store, job, working, restart.repository)
    sleep.assert_called_once()


@pytest.mark.parametrize("interrupted", [False, True])
def test_restart_after_legacy_fallback_uses_newest_deep_candidate(restart, interrupted):
    import hashlib
    state = restart.evidence.load()
    deep = restart.repository.worktree(state.candidates["c1"].candidate_commit, "deep-search")
    (deep / "bug.py").write_text("newer deep-search code\n")
    git(deep, "add", "bug.py")
    git(deep, "commit", "-m", "deep candidate")
    commit = git(deep, "rev-parse", "HEAD")
    state.candidates["c2"] = SelfRepairCandidateRecord("c2", candidate_ref=commit, candidate_commit=commit,
                                                     status="candidate_review_rejected")
    state.attempt_count = 2
    expected = "newer deep-search code\n"
    if interrupted:
        state.current_candidate_id = "c3"
        expected = "interrupted newest deep-search code\n"
        (deep / "bug.py").write_text(expected)
        (deep / "binary.dat").write_bytes(b"\x00\xffretained")
        git(deep, "add", "-A")
        diff = git(deep, "diff", "--cached", "--binary", "HEAD", check=False).stdout
        path = restart.evidence.candidate_root("c3")
        path.mkdir(parents=True)
        (path / "partial-candidate.diff").write_text(diff)
        restart.evidence.write_candidate_artifact("c3", "partial-candidate.json", {
            "status": "interrupted", "candidate_id": "c3", "base_ref": commit,
            "patch_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        })
    atomic_json(restart.source.parent / "fallback.json", {"reason": "deeper diagnosis"})
    state.repair_design = {"contract_fingerprint": state.contract_fingerprint, "strategy_id": "retained-design"}
    state.repair_design_fingerprint = "retained-design"
    state.finding_groups = [{"group_id": "binding", "status": "pending"}]
    restart.evidence.save(state)
    job, working = _new_job(restart, restart.payload)
    destination = SelfRepairExperimentStore(working, "session-session", "root")
    fresh = SelfRepairExperiment.create(run_id="session-session", root_fingerprint="root", category="engine",
                                        base_commit=restart.payload["base"], expected_postconditions=["resume the retained child"])
    destination.save(fresh)
    assert import_cancelled_repair(restart.store, job, working, restart.repository)
    retained = working.parent / "continuous/repair"
    assert (retained / "bug.py").read_text() == expected
    assert (restart.source / "bug.py").read_text() == "unfinished latest repair\n"
    if interrupted:
        assert (retained / "binary.dat").read_bytes() == b"\x00\xffretained"
    assert (retained.parent / "fallback.json").exists()
    assert destination.load().repair_design["strategy_id"] == "retained-design"
    assert destination.load().finding_groups[0]["status"] == "pending"
    receipt = json.loads((working.parent / "prior-repair-import.json").read_text())
    assert receipt["source_candidate"] == ("c3" if interrupted else "c2")


@pytest.mark.parametrize('pending', [False, True])
def test_deep_restart_preserves_descendant_manual_correction_without_claiming_proof(restart, pending):
    state = restart.evidence.load()
    # The retained checkout starts at the latest recorded candidate, with a
    # subsequent manual correction made after the stopped automatic attempt.
    git(restart.source, 'add', '-A')
    git(restart.source, 'commit', '-m', 'manual correction after cancellation')
    corrected = git(restart.source, 'rev-parse', 'HEAD')
    if pending:
        (restart.source / 'bug.py').write_text('latest manual correction\n')
        (restart.source / 'manual-test.py').write_text('new regression\n')
    expected = (restart.source / 'bug.py').read_bytes()
    atomic_json(restart.source.parent / 'fallback.json', {'reason': 'deep repair'})
    state.status = 'stalled'
    state.consecutive_non_improvements = 3
    restart.evidence.save(state)
    job, working = _new_job(restart, restart.payload)
    assert import_cancelled_repair(restart.store, job, working, restart.repository)
    retained = working.parent / 'continuous/repair'
    assert git(retained, 'rev-parse', 'HEAD') == corrected
    assert (retained / 'bug.py').read_bytes() == expected
    if pending:
        assert (retained / 'manual-test.py').read_text() == 'new regression\n'
    copied = SelfRepairExperimentStore(working, 'session-session', 'root').load()
    assert copied.status == 'stalled'
    assert copied.consecutive_non_improvements == 3
    assert copied.progress_credits == state.progress_credits
    assert copied.candidates['c1'].fatal
    assert not copied.candidates['c1'].component_receipts
    receipt = json.loads((working.parent / 'prior-repair-import.json').read_text())
    assert receipt['source_commit'] == corrected
    assert receipt['source_candidate'] == 'c1'
    assert receipt['verification_required']


def test_restart_retains_independent_planning_artifacts_and_historical_completion(restart):
    state = restart.evidence.load()
    state.historical_completed_groups['custody'] = {'completed_by': 'c1', 'status': 'completed'}
    state.planning_attempts['audit'] = 2
    state.planning_receipts['audit'] = {'decision': 'REVISE', 'feedback': ['missing recovery case']}
    from auto_agents.repair_memory import remember_revision, read_record
    memory_runner = SimpleNamespace(_experiment=state, _experiment_store=restart.evidence)
    reference = remember_revision(memory_runner, {'contract_obligation_ids': ['owned'],
        'touched_paths': ['bug.py'], 'focused_tests': []},
        {'draft': {'implementation_steps': ['preserve latest source']}, 'status': 'draft'})
    state.repair_episodes['bounded'] = {'semantic_attempts': 2, 'format_corrections': 1,
                                       'pending_round': True, 'phase': 'draft'}
    restart.evidence.save(state)
    artifact = restart.evidence.root / 'planning' / 'independent-request'
    artifact.mkdir(parents=True)
    (artifact / 'input.json').write_text('{"source":"retained"}')
    job, working = _new_job(restart, restart.payload)
    assert import_cancelled_repair(restart.store, job, working, restart.repository)
    copied = SelfRepairExperimentStore(working, 'session-session', 'root')
    assert (copied.root / 'planning/independent-request/input.json').read_bytes() == (artifact / 'input.json').read_bytes()
    migrated = copied.load()
    assert migrated.historical_completed_groups == state.historical_completed_groups
    assert migrated.planning_attempts == state.planning_attempts
    assert migrated.planning_receipts == state.planning_receipts
    assert migrated.plan_revisions == state.plan_revisions
    assert migrated.component_memory == state.component_memory
    assert migrated.repair_episodes == state.repair_episodes
    assert read_record(SimpleNamespace(_experiment_store=copied), reference)['draft'] == {
        'implementation_steps': ['preserve latest source']}
