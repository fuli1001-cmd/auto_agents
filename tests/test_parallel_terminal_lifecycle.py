from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    load_project_config,
    load_run_state,
    requirements_trace_path,
    save_project_config,
    save_run_state,
    save_task_plan,
)
from auto_agents.execution_recovery import (
    ExecutionIncidentStore,
    IncidentDiagnosis,
)
from auto_agents.gates import GateCommandInfrastructureError
from auto_agents.git_ops import (
    add_worktree,
    changed_paths,
    checkpoint_repository_fingerprints,
    commit_all,
    head_ref,
    ref_exists,
    remove_worktree,
    update_ref as persist_ref,
)
from auto_agents.io_utils import write_json
from auto_agents.models import CommandResult, RunState, TaskSpec
from auto_agents.orchestrator import Orchestrator


def _project(tmp_path: Path) -> tuple[Path, Orchestrator]:
    root = tmp_path / "project"
    Orchestrator.init_project(root, "project", "mock")
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"],
        cwd=root,
        check=True,
    )
    config = load_project_config(root)
    config.gates.require_clean_git_before_task = False
    config.execution.parallel_tasks.enabled = True
    config.execution.parallel_tasks.workers = 2
    config.execution.recovery.enabled = False
    save_project_config(root, config)
    commit_all(root, "test: baseline")
    return root, Orchestrator(root)


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="Exercise an independent implementation lane.",
        acceptance=["the lane completes"],
        depends_on=[],
    )


def _apply_retained_checkpoint(
    tmp_path: Path,
    root: Path,
    orchestrator: Orchestrator,
    state: RunState,
    owner: TaskSpec,
) -> str:
    candidate_path = "checkpoint-owned.txt"
    (root / candidate_path).write_text("baseline\n", encoding="utf-8")
    commit_all(root, "test: checkpoint ownership baseline")
    checkpoint_worktree = tmp_path / f"retained-{owner.task_id}"
    add_worktree(root, checkpoint_worktree, ref=head_ref(root))
    try:
        (checkpoint_worktree / candidate_path).write_text(
            "retained candidate\n",
            encoding="utf-8",
        )
        checkpoint_sha = commit_all(
            checkpoint_worktree,
            "test: retained task candidate",
        )
    finally:
        remove_worktree(root, checkpoint_worktree, force=True)
    checkpoint_ref = (
        f"refs/auto-agents/tests/checkpoint-ownership/{owner.task_id}"
    )
    persist_ref(root, checkpoint_ref, checkpoint_sha)
    state.task_failure_checkpoints[owner.task_id] = {
        "schema_version": 1,
        "task_id": owner.task_id,
        "ref": checkpoint_ref,
        "commit_sha": checkpoint_sha,
        "base_ref": head_ref(root),
        "changed_paths": [candidate_path],
        "has_candidate_changes": True,
        "implementation_completed": True,
        "resume_mode": "gate_recheck",
        "status": "recoverable",
    }
    assert orchestrator._restore_task_failure_checkpoint(
        state,
        owner,
        root,
    ) == checkpoint_ref
    assert state.task_failure_checkpoints[owner.task_id]["status"] == (
        "applied"
    )
    return checkpoint_ref


def _mark_localized_checkpoint_owner(
    state: RunState,
    owner: TaskSpec,
) -> None:
    owner.status = "blocked"
    owner.review_summary = "the retained candidate remains blocked"
    blocker = {
        "schema_version": 1,
        "source": "parallel_lane_failure",
        "owner": "target_project",
        "category": "parallel_lane_failure",
        "reason": owner.review_summary,
        "task_id": owner.task_id,
        "affected_task_ids": [owner.task_id],
        "lineage_id": owner.task_id,
        "failure_checkpoint": dict(
            state.task_failure_checkpoints[owner.task_id]
        ),
        "status": "localized",
    }
    state.localized_blockers = [dict(blocker)]
    state.active_blocker = dict(blocker)
    state.status = "pending"


def _require_missing_frontend_contract(
    root: Path,
    owner: TaskSpec,
) -> None:
    requirement_id = "REQ-CHECKPOINT-FRONTEND"
    owner.requirement_ids = [requirement_id]
    write_json(
        requirements_trace_path(root),
        {
            "version": 1,
            "frontend_scope": {
                "requested": True,
                "surfaces": [
                    {
                        "id": "checkpoint-surface",
                        "name": "Checkpoint surface",
                        "route": "/checkpoint",
                        "priority": "core",
                        "purpose": "Exercise prerequisite recovery.",
                        "key_states": ["default"],
                        "requirement_ids": [requirement_id],
                    }
                ],
            },
            "frontend_surfaces": [
                {
                    "name": "Checkpoint surface",
                    "route": "/checkpoint",
                    "prototype_refs": [],
                    "viewports": ["1440x900"],
                    "requirement_ids": [requirement_id],
                }
            ],
            "requirements": [
                {
                    "id": requirement_id,
                    "status": "active",
                    "priority": "mandatory",
                    "text": "The surface matches its approved prototype.",
                    "acceptance_oracles": [
                        "The rendered surface matches the approved prototype."
                    ],
                    "oracle_type": "mixed",
                    "oracle_strength": "human",
                    "evidence_boundary": "system_boundary",
                    "forbidden_proxy_oracles": [],
                }
            ],
        },
    )


def _applied_frontend_checkpoint_state(
    tmp_path: Path,
    root: Path,
    orchestrator: Orchestrator,
    *,
    run_id: str,
) -> tuple[Path, TaskSpec, RunState, str, dict[str, str]]:
    owner = _task("checkpoint-owner")
    _require_missing_frontend_contract(root, owner)
    spec_file = root / "spec.md"
    spec_file.write_text("# Frontend checkpoint recovery\n", encoding="utf-8")
    state = RunState(
        run_id=run_id,
        status="pending",
        current_stage="implement",
        stage_summaries={
            "clarify": "done",
            "prototype": "Skipped before the contract was required.",
            "design": "done",
            "plan": "done",
            "provider_research": "done",
        },
        approved_gates=[
            "requirements",
            "prototype",
            "architecture",
            "release",
        ],
        tasks=[owner],
    )
    checkpoint_ref = _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )
    checkpoint = state.task_failure_checkpoints[owner.task_id]
    expected_prestate = dict(
        checkpoint["application_transaction"]["pre_application"][
            "fingerprints"
        ]
    )
    save_task_plan(root, {"tasks": [owner.to_dict()]})
    save_run_state(root, state)
    return spec_file, owner, state, checkpoint_ref, expected_prestate


def _installed_repair_diagnosis() -> dict[str, object]:
    return {
        "final": {
            "expected_postconditions": [
                "the affected parallel lane can resume without losing peers"
            ]
        }
    }


def _prepare_loop(
    orchestrator: Orchestrator,
    results: dict[str, dict[str, object]],
) -> None:
    orchestrator._parallel_execution_fallback_reason = lambda _tasks: ""
    orchestrator._parallel_worker_count = lambda: 2
    orchestrator._log_parallel_worker_resolution = Mock()
    orchestrator._ensure_evidence_preflight = lambda _state, _task: None
    orchestrator._require_clean_tree_excluding_agent_instructions = Mock()
    orchestrator._deferred_parallel_task_reasons = lambda _tasks: []
    orchestrator._run_parallel_task_batch = Mock(return_value=results)


def test_failed_parallel_lane_becomes_structured_blocker_instead_of_runtimeerror(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    tasks = [_task("lane-a"), _task("lane-b"), _task("independent-lane")]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    state = RunState(
        run_id="parallel-blocker",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    results: dict[str, dict[str, object]] = {}
    for task in tasks[:2]:
        failed = TaskSpec.from_dict(task.to_dict())
        failed.status = "blocked"
        failed.review_summary = f"review for {task.task_id}"
        results[task.task_id] = {
            "ok": False,
            "task": failed.to_dict(),
            "reason": f"failed {task.task_id}",
            "review": failed.review_summary,
            "failure_ids": [f"reason:{task.task_id}"],
            "comparable_failures": True,
        }
    _prepare_loop(orchestrator, results)

    continued: list[str] = []

    def complete_independent(
        _state: RunState,
        _tasks: list[TaskSpec],
        task: TaskSpec,
    ) -> None:
        continued.append(task.task_id)
        task.status = "done"
        return None

    orchestrator._execute_task_in_main_worktree = Mock(
        side_effect=complete_independent
    )

    returned = orchestrator._run_parallel_implementation_loop(
        state,
        tasks,
        max_tasks=3,
    )

    assert returned is state
    assert state.status == "blocked"
    assert state.active_blocker["source"] == "parallel_lane_failure"
    assert state.active_blocker["resumable"] is True
    assert {item["task_id"] for item in state.localized_blockers} == {
        "lane-a",
        "lane-b",
    }
    assert [task.status for task in tasks] == ["blocked", "blocked", "done"]
    assert continued == ["independent-lane"]
    persisted = load_run_state(root)
    assert persisted.status == "blocked"
    assert persisted.active_blocker["parallel_lane_failure"]["schema_version"] == 1


def test_unhealthy_infrastructure_lane_is_resumable_and_peer_stays_integrated(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    failed_task = _task("infra-lane")
    peer_task = _task("peer-lane")
    tasks = [failed_task, peer_task]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    state = RunState(
        run_id="parallel-infrastructure",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    verification_id = "tests/test_boundary.py::test_external_service"
    failed_snapshot = TaskSpec.from_dict(failed_task.to_dict())
    failed_snapshot.status = "blocked"
    failed_snapshot.verify_baseline_failures = [verification_id]
    gate_failure = {
        "ok": False,
        "reason": "the owned boundary remains unavailable",
        "review": "the owned boundary remains unavailable",
        "failure_ids": [verification_id],
        "current_failure_ids": [verification_id],
        "baseline_failure_ids": [verification_id],
        "new_failure_ids": [],
        "owned_failure_ids": [verification_id],
        "failure_class": "baseline_only_owned",
        "comparable_failures": True,
        "baseline_comparison_comparable": True,
        "redacted_command_evidence": (
            "$ python -m pytest -q tests/test_boundary.py::test_external_service\n"
            "FAILED tests/test_boundary.py::test_external_service"
        ),
    }
    failure = orchestrator._parallel_task_failure_result(
        failed_snapshot,
        gate_failure,
        operation="verification",
        automatic_retryable=False,
        base_ref="baseline-head",
        checkpoint={
            "schema_version": 1,
            "task_id": failed_task.task_id,
            "status": "recoverable",
            "ref": "",
            "has_candidate_changes": False,
            "implementation_completed": True,
            "resume_mode": "gate_recheck",
        },
        implementation_completed=True,
    )
    peer_snapshot = TaskSpec.from_dict(peer_task.to_dict())
    peer_snapshot.status = "done"
    results = {
        failed_task.task_id: failure,
        peer_task.task_id: {
            "ok": True,
            "task": peer_snapshot.to_dict(),
            "reason": "",
            "review": "peer passed",
            "commit_sha": "a" * 40,
            "result_ref": "",
            "base_ref": "baseline-head",
            "changed_paths": ["peer.py"],
            "verify_current_failure_ids": [],
        },
    }
    _prepare_loop(orchestrator, results)
    orchestrator._integrate_parallel_task_result = Mock(return_value="b" * 40)
    orchestrator._warm_clean_head_verify_baseline = Mock()
    orchestrator._schedule_repair_tasks_for_failure = Mock(
        side_effect=AssertionError(
            "baseline-only infrastructure evidence must bypass candidate repair"
        )
    )
    diagnosis = IncidentDiagnosis(
        owner="verification_infrastructure",
        action="REPAIR_INFRASTRUCTURE",
        confidence=0.99,
        reason="the external verification prerequisite is unhealthy",
        evidence=["the command failed before the owned assertion"],
        cause_status="confirmed",
    )

    with patch.object(
        orchestrator,
        "_agent_diagnose_execution_incident",
        return_value=diagnosis,
    ):
        returned = orchestrator._run_parallel_implementation_loop(
            state,
            tasks,
            max_tasks=2,
        )

    assert returned.status == "blocked"
    assert peer_task.status == "done"
    assert failed_task.status == "blocked"
    orchestrator._integrate_parallel_task_result.assert_called_once()
    assert returned.active_blocker["owner"] == "verification_infrastructure"
    incident_id = returned.active_blocker["incident_id"]
    assert incident_id
    assert returned.task_failure_checkpoints[failed_task.task_id][
        "resume_mode"
    ] == "gate_recheck"

    assert orchestrator._resume_blocked_run(returned)
    assert failed_task.status == "pending"
    assert peer_task.status == "done"
    assert orchestrator._parallel_lane_gate_recheck_pending(
        returned,
        failed_task,
    )

    # A still-unhealthy recheck remains the same infrastructure blocker even
    # when the generic execution-loop threshold would otherwise be crossed.
    orchestrator.config.execution.recovery.max_occurrences_per_root_cause = 1
    recheck_result = CommandResult(
        command="python -m pytest -q tests/test_boundary.py::test_external_service",
        ok=False,
        returncode=1,
        stderr="the verification prerequisite is still unavailable",
        infrastructure_error=True,
        infrastructure_failure_id="external-service-unavailable",
    )
    unhealthy_recheck = Mock(
        side_effect=GateCommandInfrastructureError(
            "verification infrastructure failed during lane recheck",
            result=recheck_result,
            context="parallel lane verification recheck",
            baseline=False,
            task_id=failed_task.task_id,
        )
    )
    with (
        patch.object(
            orchestrator,
            "_ensure_task_verify_baseline",
            return_value=False,
        ),
        patch.object(
            orchestrator,
            "_restore_task_failure_checkpoint",
            return_value="",
        ),
        patch.object(
            orchestrator,
            "_execute_task_with_retries",
            unhealthy_recheck,
        ),
        patch.object(
            orchestrator,
            "_agent_diagnose_execution_incident",
            return_value=diagnosis,
        ),
    ):
        unhealthy_outcome = orchestrator._execute_task_in_main_worktree(
            returned,
            tasks,
            failed_task,
        )
    assert unhealthy_outcome is returned
    assert unhealthy_recheck.call_args.kwargs["gate_recheck_first"] is True
    recurring = returned.active_blocker
    assert recurring["owner"] == "verification_infrastructure"
    assert recurring["category"] != "execution_recovery_semantic_loop"
    assert failed_task.status == "blocked"
    assert peer_task.status == "done"
    recurring_incident = ExecutionIncidentStore(root, returned.run_id).load(
        recurring["incident_id"]
    )
    assert recurring_incident is not None
    assert recurring_incident.task_id == failed_task.task_id
    assert recurring_incident.diagnosis["owner"] == (
        "verification_infrastructure"
    )
    assert recurring["resumable"] is True
    assert returned.active_execution_incident_id == (
        recurring_incident.incident_id
    )
    assert orchestrator._resume_blocked_run(returned)

    execute_calls: list[bool] = []

    def healthy_recheck(
        _state: RunState,
        _task: TaskSpec,
        *,
        resume_existing: bool = False,
        gate_recheck_first: bool = False,
    ) -> dict[str, object]:
        del resume_existing
        execute_calls.append(gate_recheck_first)
        return {
            "ok": True,
            "reason": "all commands passed",
            "review": "verification recovered",
            "verify_current_failure_ids": [],
        }

    with (
        patch.object(
            orchestrator,
            "_ensure_task_verify_baseline",
            return_value=False,
        ),
        patch.object(
            orchestrator,
            "_restore_task_failure_checkpoint",
            return_value="",
        ),
        patch.object(
            orchestrator,
            "_execute_task_with_retries",
            side_effect=healthy_recheck,
        ),
        patch.object(orchestrator, "_run_task_persistence_action"),
        patch.object(orchestrator, "_warm_clean_head_verify_baseline"),
        patch("auto_agents.orchestrator.commit_all", return_value="c" * 40),
    ):
        outcome = orchestrator._execute_task_in_main_worktree(
            returned,
            tasks,
            failed_task,
        )

    assert outcome is None
    assert execute_calls == [True]
    assert failed_task.status == "done"
    assert peer_task.status == "done"
    assert not returned.localized_blockers
    assert not returned.active_blocker
    incident = ExecutionIncidentStore(root, returned.run_id).load(
        recurring_incident.incident_id
    )
    assert incident is not None
    assert incident.status == "resolved"


def test_legacy_failed_parallel_state_materializes_exact_blocker(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    affected = _task("route-owned-lane")
    peer = _task("completed-peer")
    peer.status = "done"
    peer.commit_sha = "a" * 40
    unrelated = _task("unrelated-pending")
    tasks = [affected, peer, unrelated]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    original_error = (
        "parallel task batch failed: route-owned-lane: worker setup failed"
    )
    state = RunState(
        run_id="legacy-route-owned-lane",
        status="failed",
        current_stage="implement",
        tasks=tasks,
        last_error=original_error,
        last_recovery_route={
            "outcome": "exhausted",
            "task_id": affected.task_id,
            "lineage_id": affected.task_id,
        },
    )

    assert not orchestrator._resume_blocked_run(state)

    assert state.status == "blocked"
    assert state.last_error == original_error
    assert state.active_blocker["source"] == "parallel_lane_failure"
    assert state.active_blocker["category"] == (
        "parallel_failure_lifecycle_bypass"
    )
    assert state.active_blocker["affected_task_ids"] == [affected.task_id]
    assert state.active_blocker["recovery_readiness"] == "ready"
    assert {
        item["task_id"]
        for item in state.localized_blockers
        if item.get("source") == "parallel_lane_failure"
    } == {affected.task_id}
    assert {task.task_id: task.status for task in state.tasks} == {
        affected.task_id: "blocked",
        peer.task_id: "done",
        unrelated.task_id: "pending",
    }
    assert next(
        task for task in state.tasks if task.task_id == peer.task_id
    ).commit_sha == "a" * 40
    persisted = load_run_state(root)
    assert persisted.status == "blocked"
    assert persisted.active_blocker["affected_task_ids"] == [
        affected.task_id
    ]


def test_repaired_legacy_failed_parallel_run_crosses_resume_boundary(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    spec = root / "spec.md"
    spec.write_text("# Synthetic parallel recovery\n", encoding="utf-8")
    commit_all(root, "test: add recovery specification")
    affected = _task("affected-lane")
    affected.status = "blocked"
    peer = _task("completed-peer")
    peer.status = "done"
    peer.commit_sha = "a" * 40
    unrelated = _task("unrelated-pending")
    tasks = [affected, peer, unrelated]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    reason = "legacy parallel recovery awaits the installed engine"
    state = RunState(
        run_id="legacy-installed-repair",
        workflow_version=2,
        status="pending",
        current_stage="implement",
        tasks=tasks,
        stage_summaries={
            stage: "complete"
            for stage in (
                "clarify",
                "prototype",
                "design",
                "plan",
                "provider_research",
            )
        },
        last_error="",
        active_blocker={
            "schema_version": 1,
            "source": "parallel_lane_failure",
            "owner": "auto_agents",
            "category": "parallel_failure_lifecycle_bypass",
            "status": "retrying",
            "reason": reason,
            "legacy_migration": True,
            "legacy_last_error": reason,
            "recovery_readiness": "ready",
            "affected_task_ids": [affected.task_id],
            "self_repair_commit": "uninstalled-repair",
            "root_cause_diagnosis": _installed_repair_diagnosis(),
        },
        last_recovery_route={
            "outcome": "exhausted",
            "task_id": affected.task_id,
            "lineage_id": affected.task_id,
        },
    )
    save_run_state(root, state)

    implementation_loop = Mock(
        side_effect=AssertionError(
            "an uninstalled repair must not reach implementation"
        )
    )
    with (
        patch.object(orchestrator, "_prepare_project_config_for_supervision"),
        patch.object(orchestrator, "_ensure_agent_instructions_synced"),
        patch.object(orchestrator, "_cleanup_failed_verification_logs"),
        patch.object(orchestrator, "_start_health_supervision"),
        patch.object(orchestrator, "stop_health_supervision"),
        patch.object(orchestrator, "_process_health_action", return_value=None),
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:without-repair",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=False,
        ),
        patch(
            "auto_agents.orchestrator.verify_blocker_postconditions",
            return_value=([Mock()], [Mock(result="fail")]),
        ),
        patch.object(
            orchestrator,
            "_run_implementation_loop",
            implementation_loop,
        ),
    ):
        blocked = orchestrator.run(spec, auto_approve=True, skip_validate=True)

    assert blocked.status == "blocked"
    assert blocked.active_blocker["self_repair_commit"] == (
        "uninstalled-repair"
    )
    assert blocked.active_blocker["status"] == "blocked"
    assert next(
        task for task in blocked.tasks if task.task_id == affected.task_id
    ).status == "blocked"
    implementation_loop.assert_not_called()

    implementation_attempts: list[str] = []

    def continue_implementation(
        resumed: RunState,
        *,
        max_tasks: int | None,
    ) -> RunState:
        del max_tasks
        implementation_attempts.extend(
            task.task_id
            for task in resumed.tasks
            if task.status == "pending"
            and orchestrator._parallel_lane_task_is_recovering(
                resumed,
                task,
            )
        )
        orchestrator._task_budget_exhausted = True
        return resumed

    with (
        patch.object(orchestrator, "_prepare_project_config_for_supervision"),
        patch.object(orchestrator, "_ensure_agent_instructions_synced"),
        patch.object(orchestrator, "_cleanup_failed_verification_logs"),
        patch.object(orchestrator, "_start_health_supervision"),
        patch.object(orchestrator, "stop_health_supervision"),
        patch.object(orchestrator, "_process_health_action", return_value=None),
        patch.object(orchestrator, "_ensure_preconditions"),
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:with-repair",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=True,
        ),
        patch(
            "auto_agents.orchestrator.verify_blocker_postconditions",
            return_value=([], []),
        ),
        patch.object(
            orchestrator,
            "_run_implementation_loop",
            side_effect=continue_implementation,
        ),
    ):
        resumed = orchestrator.run(spec, auto_approve=True, skip_validate=True)

    assert implementation_attempts == [affected.task_id]
    assert resumed.status == "pending"
    assert resumed.active_blocker == {}
    assert {task.task_id: task.status for task in resumed.tasks} == {
        affected.task_id: "pending",
        peer.task_id: "done",
        unrelated.task_id: "pending",
    }
    assert next(
        task for task in resumed.tasks if task.task_id == peer.task_id
    ).commit_sha == "a" * 40
    resumed_lane = next(
        task for task in resumed.tasks if task.task_id == affected.task_id
    )
    assert orchestrator._parallel_lane_task_is_recovering(
        resumed,
        resumed_lane,
    )


def test_legacy_parallel_recovery_ignores_unrelated_historical_checkpoint(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    affected = _task("active-failed-lane")
    affected.status = "blocked"
    peer = _task("completed-peer")
    peer.status = "done"
    unrelated = _task("historical-checkpoint-owner")
    tasks = [affected, peer, unrelated]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    affected_checkpoint = {
        "schema_version": 1,
        "task_id": affected.task_id,
        "status": "recoverable",
        "has_candidate_changes": False,
        "implementation_completed": False,
        "resume_mode": "implementation",
    }
    historical_checkpoint = {
        "schema_version": 1,
        "task_id": unrelated.task_id,
        "status": "recoverable",
        "ref": "refs/auto-agents/tests/old-candidate",
        "has_candidate_changes": True,
        "implementation_completed": True,
        "resume_mode": "gate_recheck",
    }
    state = RunState(
        run_id="legacy-historical-checkpoint",
        status="failed",
        current_stage="implement",
        tasks=tasks,
        last_error="parallel task batch failed: one active lane failed",
        last_recovery_route={
            "outcome": "exhausted",
            "task_id": affected.task_id,
            "lineage_id": affected.task_id,
        },
        task_failure_checkpoints={
            affected.task_id: affected_checkpoint,
            unrelated.task_id: historical_checkpoint,
        },
    )
    save_run_state(root, state)

    assert orchestrator._normalize_legacy_parallel_failure_lifecycle(state)

    assert state.active_blocker["affected_task_ids"] == [affected.task_id]
    assert set(state.active_blocker["legacy_failure_checkpoints"]) == {
        affected.task_id
    }
    assert {
        item["task_id"]
        for item in state.localized_blockers
        if item.get("source") == "parallel_lane_failure"
    } == {affected.task_id}
    assert next(
        task for task in state.tasks if task.task_id == unrelated.task_id
    ).status == "pending"
    assert set(state.task_failure_checkpoints) == {
        affected.task_id,
        unrelated.task_id,
    }
    state.active_blocker["root_cause_diagnosis"] = (
        _installed_repair_diagnosis()
    )
    save_run_state(root, state)

    marked = orchestrator.mark_self_repair_applied("engine-repair")
    with (
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:exact-lane-repair",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=True,
        ),
    ):
        assert orchestrator._resume_blocked_run(marked)

    requeue = marked.resume_context["parallel_lane_recovery_history"][-1][
        "requeue_checkpoint"
    ]
    assert requeue["task_ids"] == [affected.task_id]
    assert unrelated.task_id not in requeue["task_failure_checkpoints"]
    assert next(
        task for task in marked.tasks if task.task_id == unrelated.task_id
    ).status == "pending"


def _assert_awaiting_evidence_marker_requires_installed_repair_proof(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    affected = _task("checkpointed-lane")
    affected.status = "blocked"
    peer = _task("completed-peer")
    peer.status = "done"
    tasks = [affected, peer]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    checkpoint_ref = "refs/auto-agents/tests/restored-lane-candidate"
    checkpoint = {
        "schema_version": 1,
        "task_id": affected.task_id,
        "status": "recoverable",
        "ref": checkpoint_ref,
        "has_candidate_changes": True,
        "changed_paths": ["candidate.txt"],
        "implementation_completed": True,
        "resume_mode": "gate_recheck",
    }
    state = RunState(
        run_id="legacy-missing-checkpoint-ref",
        status="failed",
        current_stage="implement",
        tasks=tasks,
        last_error=(
            "parallel task batch failed: candidate checkpoint is unavailable"
        ),
        last_recovery_route={
            "outcome": "exhausted",
            "task_id": affected.task_id,
            "lineage_id": affected.task_id,
        },
        task_failure_checkpoints={affected.task_id: checkpoint},
    )
    save_run_state(root, state)

    marked = orchestrator.mark_self_repair_applied("engine-repair")

    assert marked.status == "blocked"
    assert marked.active_blocker["status"] == "blocked"
    assert marked.active_blocker["recovery_readiness"] == "awaiting_evidence"
    assert marked.active_blocker["unreplayable_checkpoint_task_ids"] == [
        affected.task_id
    ]
    assert marked.active_blocker["recovery_condition"][
        "missing_checkpoint_refs"
    ] == [{"task_id": affected.task_id, "ref": checkpoint_ref}]
    marked.active_blocker["root_cause_diagnosis"] = (
        _installed_repair_diagnosis()
    )
    save_run_state(root, marked)

    original_category = marked.active_blocker["category"]
    original_fingerprint = marked.active_blocker["fingerprint"]
    with (
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            side_effect=AssertionError(
                "checkpoint evidence must be ready before installed proof"
            ),
        ),
        patch(
            "auto_agents.orchestrator.verify_blocker_postconditions",
            side_effect=AssertionError(
                "checkpoint evidence must be ready before postconditions"
            ),
        ),
    ):
        assert orchestrator._resume_blocked_run(marked)
        assert not orchestrator._resume_blocked_run(marked)

    assert marked.status == "blocked"
    assert marked.active_blocker["category"] == (
        "parallel_repair_evidence_unavailable"
    )
    assert marked.active_blocker["fingerprint"] != original_fingerprint
    assert marked.active_blocker["repair_root_category"] == original_category
    assert marked.active_blocker["repair_root_fingerprint"] == (
        original_fingerprint
    )
    assert marked.active_blocker["installed_repair_authorization"][
        "status"
    ] == "awaiting_evidence"
    assert next(
        task for task in marked.tasks if task.task_id == affected.task_id
    ).status == "blocked"

    persist_ref(root, checkpoint_ref, "HEAD")

    failed_receipt = Mock(result="fail")
    with (
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:without-repair",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=False,
        ) as contains_commit,
        patch(
            "auto_agents.orchestrator.verify_blocker_postconditions",
            return_value=([Mock()], [failed_receipt]),
        ) as verify_postconditions,
    ):
        assert not orchestrator._resume_blocked_run(marked)

    contains_commit.assert_called_with("engine-repair")
    verify_postconditions.assert_called()
    assert marked.status == "blocked"
    assert marked.active_blocker["status"] == "blocked"
    assert marked.active_blocker["recovery_readiness"] == "ready"
    assert marked.active_blocker["category"] == original_category
    assert marked.active_blocker["fingerprint"] == original_fingerprint
    assert marked.active_blocker["self_repair_commit"] == "engine-repair"
    assert not marked.active_blocker["unreplayable_checkpoint_task_ids"]
    assert next(
        task for task in marked.tasks if task.task_id == affected.task_id
    ).status == "blocked"
    assert "parallel_sequential_retry_tasks" not in marked.resume_context

    with (
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:with-repair",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=True,
        ),
        patch(
            "auto_agents.orchestrator.verify_blocker_postconditions",
            return_value=([], []),
        ),
    ):
        assert orchestrator._resume_blocked_run(marked)

    assert marked.status == "pending"
    assert marked.active_blocker == {}
    assert {
        task.task_id: task.status for task in marked.tasks
    } == {
        affected.task_id: "pending",
        peer.task_id: "done",
    }
    requeue = marked.resume_context["parallel_lane_recovery_history"][-1][
        "requeue_checkpoint"
    ]
    assert requeue["task_ids"] == [affected.task_id]


def test_legacy_parallel_missing_checkpoint_ref_becomes_ready_without_authorization(
    tmp_path: Path,
) -> None:
    _assert_awaiting_evidence_marker_requires_installed_repair_proof(tmp_path)


def test_awaiting_evidence_marker_requires_installed_repair_proof_before_requeue(
    tmp_path: Path,
) -> None:
    _assert_awaiting_evidence_marker_requires_installed_repair_proof(tmp_path)


def test_repaired_failed_parallel_state_crosses_terminal_boundary(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    affected = _task("affected-lane")
    affected.status = "blocked"
    peer = _task("completed-peer")
    peer.status = "done"
    tasks = [affected, peer]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    state = RunState(
        run_id="legacy-parallel",
        status="failed",
        current_stage="implement",
        tasks=tasks,
        last_error="parallel task batch failed: affected-lane: snapshot failed",
        last_recovery_route={
            "outcome": "exhausted",
            "task_id": affected.task_id,
            "lineage_id": affected.task_id,
            "reason": "the old aggregate route was terminal",
        },
    )
    save_run_state(root, state)

    marked = orchestrator.mark_self_repair_applied("engine-repair")
    marked.active_blocker["root_cause_diagnosis"] = (
        _installed_repair_diagnosis()
    )
    assert marked.status == "blocked"
    assert marked.active_blocker["self_repair_commit"] == "engine-repair"

    def decline_requeue(
        _state: RunState,
        blocker: dict[str, object],
    ) -> list[str]:
        blocker["prepared_self_repair_commit"] = "engine-repair"
        return []

    with (
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:terminal-boundary",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=True,
        ),
        patch.object(
            orchestrator,
            "_prepare_self_repair_task_retries",
            side_effect=decline_requeue,
        ),
    ):
        assert not orchestrator._resume_blocked_run(marked)

    assert marked.status == "blocked"
    assert marked.active_blocker["status"] == "blocked"
    assert marked.active_blocker["self_repair_commit"] == "engine-repair"
    assert "prepared_self_repair_commit" not in marked.active_blocker
    assert marked.active_blocker["installed_repair_authorization"][
        "status"
    ] == "verified_requeue_blocked"
    assert {task.task_id: task.status for task in marked.tasks} == {
        "affected-lane": "blocked",
        "completed-peer": "done",
    }

    with (
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:terminal-boundary",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=True,
        ),
    ):
        assert orchestrator._resume_blocked_run(marked)
    assert marked.status == "pending"
    assert marked.active_blocker == {}
    assert marked.last_recovery_route["outcome"] == "self_repair_requeued"
    assert {task.task_id: task.status for task in marked.tasks} == {
        "affected-lane": "pending",
        "completed-peer": "done",
    }
    resumed_task = next(
        task for task in marked.tasks if task.task_id == "affected-lane"
    )
    assert orchestrator._parallel_lane_task_is_recovering(
        marked,
        resumed_task,
    )
    history = marked.resume_context["parallel_lane_recovery_history"]
    assert history[-1]["event"] == "self_repair_blocker_retired"
    assert history[-1]["requeue_checkpoint"]["task_ids"] == [
        "affected-lane"
    ]


def test_checkpoint_unavailable_forces_safe_reimplementation(
    tmp_path: Path,
) -> None:
    _root, orchestrator = _project(tmp_path)
    task = _task("checkpoint-lane")
    state = load_run_state(orchestrator.project_root)
    state.tasks = [task]
    worker_state = RunState.from_dict(state.to_dict())
    orchestrator._set_implementation_ready_marker(worker_state, task, True)

    with patch.object(
        orchestrator,
        "_preserve_failed_task_checkpoint",
        side_effect=RuntimeError("temporary index failed"),
    ):
        checkpoint, error = orchestrator._checkpoint_parallel_lane_failure(
            state,
            worker_state,
            orchestrator,
            task,
            orchestrator.project_root,
            {
                "reason": "verification failed",
                "review": "verification failed",
                "failure_ids": [],
            },
            dependency_links=(),
            base_ref="base",
        )

    assert error
    assert checkpoint["status"] == "unavailable"
    assert checkpoint["resume_mode"] == "implementation"
    assert checkpoint["implementation_completed"] is False
    assert not orchestrator._implementation_ready_markers(worker_state).get(
        task.task_id
    )
    orchestrator._set_implementation_ready_marker(state, task, True)
    task.status = "blocked"
    failure_result = orchestrator._parallel_task_failure_result(
        task,
        {
            "reason": "verification failed",
            "review": "verification failed",
            "failure_ids": [],
        },
        operation="checkpointing",
        owner="auto_agents",
        automatic_retryable=False,
        checkpoint=checkpoint,
        implementation_completed=False,
    )
    blocker = orchestrator._upsert_parallel_lane_blocker(
        state,
        task,
        orchestrator._parallel_lane_failure_from_result(
            task,
            failure_result,
        ),
    )
    assert blocker["resume_mode"] == "implementation"
    assert not orchestrator._implementation_ready_markers(state).get(task.task_id)
    assert not orchestrator._parallel_lane_gate_recheck_pending(state, task)


def test_repaired_aggregate_blocker_requeues_only_affected_lanes(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    affected = _task("blocked-lane")
    affected.status = "blocked"
    peer = _task("integrated-peer")
    peer.status = "done"
    untouched = _task("unrelated-pending")
    tasks = [affected, peer, untouched]
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    state = RunState(
        run_id="aggregate-repair",
        status="blocked",
        current_stage="implement",
        tasks=tasks,
        active_blocker={
            "owner": "auto_agents",
            "category": "parallel_failure_lifecycle_bypass",
            "status": "blocked",
            "reason": "parallel lanes ended without a recovery object",
            "root_cause_diagnosis": _installed_repair_diagnosis(),
        },
        last_recovery_route={
            "outcome": "exhausted",
            "task_id": affected.task_id,
            "lineage_id": affected.task_id,
        },
    )
    save_run_state(root, state)

    marked = orchestrator.mark_self_repair_applied("engine-repair")
    with (
        patch.object(
            orchestrator,
            "_installed_engine_revision",
            return_value="installed-engine:aggregate-repair",
        ),
        patch.object(
            orchestrator,
            "_installed_engine_contains_commit",
            return_value=True,
        ),
    ):
        assert orchestrator._resume_blocked_run(marked)

    assert marked.active_blocker == {}
    assert {task.task_id: task.status for task in marked.tasks} == {
        "blocked-lane": "pending",
        "integrated-peer": "done",
        "unrelated-pending": "pending",
    }
    checkpoint = marked.resume_context["parallel_lane_recovery_history"][-1][
        "requeue_checkpoint"
    ]
    assert checkpoint["task_ids"] == ["blocked-lane"]


def test_parallel_batch_collects_every_ordinary_future_failure(
    tmp_path: Path,
) -> None:
    _root, orchestrator = _project(tmp_path)
    failed = _task("failed-future")
    passed = _task("passed-future")
    tasks = [failed, passed]
    state = RunState(run_id="future-results", tasks=tasks)

    def execute(
        _state: RunState,
        task_snapshots: list[TaskSpec],
        task_id: str,
    ) -> dict[str, object]:
        if task_id == failed.task_id:
            raise RuntimeError("worker boundary failed")
        task = next(item for item in task_snapshots if item.task_id == task_id)
        return {"ok": True, "task": task.to_dict(), "commit_sha": "a" * 40}

    with patch.object(
        orchestrator,
        "_run_task_in_worktree",
        side_effect=execute,
    ):
        results = orchestrator._run_parallel_task_batch(state, tasks, tasks)

    assert set(results) == {failed.task_id, passed.task_id}
    assert results[failed.task_id]["ok"] is False
    assert results[failed.task_id]["failure_kind"] == "parallel_lane_failure"
    assert results[failed.task_id]["operation"] == "worker_future"
    assert results[passed.task_id]["ok"] is True


def test_parallel_worker_converts_gate_error_before_worktree_cleanup(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    task = _task("gate-error-lane")
    save_task_plan(root, {"tasks": [task.to_dict()]})
    state = load_run_state(root)
    state.run_id = "gate-error"
    state.tasks = [task]
    command_result = CommandResult(
        command="python -m pytest -q tests/test_boundary.py",
        ok=False,
        returncode=1,
        stderr="API_KEY=classified infrastructure unavailable",
        infrastructure_error=True,
        infrastructure_failure_id="external-service-unavailable",
    )

    def fail_baseline(
        _worker: Orchestrator,
        worker_task: TaskSpec,
        *,
        state: RunState,
    ) -> bool:
        raise GateCommandInfrastructureError(
            "verification infrastructure failed",
            result=command_result,
            context="task baseline verification",
            baseline=True,
            task_id=worker_task.task_id,
        )

    with patch.object(
        Orchestrator,
        "_ensure_task_verify_baseline",
        new=fail_baseline,
    ):
        result = orchestrator._run_task_in_worktree(
            state,
            [task],
            task.task_id,
        )

    assert result["ok"] is False
    assert result["failure_kind"] == "parallel_lane_failure"
    assert result["operation"] == "baseline_verification"
    assert result["owner"] == "verification_infrastructure"
    assert result["failure_checkpoint"]["status"] == "recoverable"
    incident_result = result["command_incident"]["result"]
    assert incident_result["infrastructure_error"] is True
    assert "classified" not in incident_result["stderr"]

    orchestrator._apply_parallel_task_failure_snapshot(task, result["task"])
    task.status = "blocked"
    orchestrator._record_task_failure_checkpoint(state, task, result)
    blocker = orchestrator._materialize_parallel_lane_failure(
        state,
        [task],
        task,
        result,
    )
    assert blocker is not None
    assert blocker["owner"] == "verification_infrastructure"
    assert blocker["incident_id"]
    incident = ExecutionIncidentStore(root, state.run_id).load(
        blocker["incident_id"]
    )
    assert incident is not None
    assert incident.kind == "gate_reported_infrastructure_error"


def test_result_publication_failure_retains_committed_candidate_for_resume(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    task = _task("published-result-lane")
    save_task_plan(root, {"tasks": [task.to_dict()]})
    state = load_run_state(root)
    state.run_id = "result-publication"
    state.current_stage = "implement"
    state.tasks = [task]
    candidate_contents = "candidate survived result publication failure\n"

    def implement_and_verify(
        worker: Orchestrator,
        worker_state: RunState,
        worker_task: TaskSpec,
        **_kwargs: object,
    ) -> dict[str, object]:
        (worker.project_root / "candidate.txt").write_text(
            candidate_contents,
            encoding="utf-8",
        )
        worker._set_implementation_ready_marker(
            worker_state,
            worker_task,
            True,
        )
        return {
            "ok": True,
            "reason": "all commands passed",
            "review": "verification passed",
            "verify_current_failure_ids": [],
        }

    def publish_ref(
        project_root: Path,
        ref_name: str,
        commit_sha: str,
    ) -> None:
        if "/tasks/" in ref_name and "/failed-tasks/" not in ref_name:
            raise RuntimeError("result ref publication failed")
        persist_ref(project_root, ref_name, commit_sha)

    with (
        patch.object(
            Orchestrator,
            "_ensure_task_verify_baseline",
            return_value=False,
        ),
        patch.object(
            Orchestrator,
            "_execute_task_with_retries",
            new=implement_and_verify,
        ),
        patch("auto_agents.orchestrator.update_ref", side_effect=publish_ref),
    ):
        result = orchestrator._run_task_in_worktree(
            state,
            [task],
            task.task_id,
        )

    assert result["ok"] is False
    assert result["operation"] == "result_publication"
    checkpoint = result["failure_checkpoint"]
    assert checkpoint["status"] == "recoverable"
    assert checkpoint["candidate_source"] == "committed_worker_result"
    assert checkpoint["has_candidate_changes"] is True
    assert checkpoint["implementation_completed"] is True
    assert checkpoint["resume_mode"] == "gate_recheck"
    assert checkpoint["changed_paths"] == ["candidate.txt"]
    assert checkpoint["ref"]
    assert ref_exists(root, checkpoint["ref"])
    assert not (root / "candidate.txt").exists()

    orchestrator._apply_parallel_task_failure_snapshot(task, result["task"])
    orchestrator._record_task_failure_checkpoint(state, task, result)
    blockers = orchestrator._persist_parallel_lane_failures(
        state,
        [task],
        [(task, result)],
    )
    assert len(blockers) == 1
    assert state.status == "blocked"
    assert state.active_blocker["owner"] == "auto_agents"
    state.active_blocker["self_repair_commit"] = "engine-fix"
    assert orchestrator._resume_blocked_run(state)
    assert task.status == "pending"
    assert orchestrator._parallel_lane_gate_recheck_pending(state, task)

    restored = orchestrator._restore_task_failure_checkpoint(
        state,
        task,
        root,
    )

    assert restored == checkpoint["ref"]
    assert (root / "candidate.txt").read_text(encoding="utf-8") == (
        candidate_contents
    )


def test_localized_applied_checkpoint_is_detached_before_independent_task(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    orchestrator.config.execution.parallel_tasks.enabled = False
    owner = _task("checkpoint-owner")
    independent = _task("independent-task")
    tasks = [owner, independent]
    state = RunState(
        run_id="localized-applied-checkpoint",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    checkpoint_ref = _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )
    _mark_localized_checkpoint_owner(state, owner)
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    save_run_state(root, state)

    executed: list[str] = []

    def complete_independent(
        _state: RunState,
        _tasks: list[TaskSpec],
        task: TaskSpec,
    ) -> None:
        executed.append(task.task_id)
        assert changed_paths(root) == []
        task.status = "done"
        return None

    with (
        patch.object(
            orchestrator,
            "_commit_planning_baseline_if_needed",
        ),
        patch.object(orchestrator, "_ensure_implement_verify_baseline"),
        patch.object(
            orchestrator,
            "_execute_task_in_main_worktree",
            side_effect=complete_independent,
        ),
    ):
        returned = orchestrator._run_implementation_loop(
            state,
            max_tasks=1,
        )

    assert returned is state
    assert executed == [independent.task_id]
    checkpoint = state.task_failure_checkpoints[owner.task_id]
    assert checkpoint["status"] == "recoverable"
    assert checkpoint["application_transaction"]["status"] == "detached"
    assert checkpoint["detachment"]["proof"] == "exact_prestate_restored"
    assert (root / "checkpoint-owned.txt").read_text(
        encoding="utf-8"
    ) == "baseline\n"
    assert ref_exists(root, checkpoint_ref)


def test_interrupted_applying_checkpoint_is_detached_before_dispatch(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    owner = _task("interrupted-checkpoint-owner")
    independent = _task("independent-task")
    tasks = [owner, independent]
    state = RunState(
        run_id="interrupted-applying-checkpoint",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    checkpoint_ref = _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )
    _mark_localized_checkpoint_owner(state, owner)
    checkpoint = state.task_failure_checkpoints[owner.task_id]
    transaction = checkpoint["application_transaction"]
    transaction["status"] = "applying"
    transaction.pop("applied_state")
    checkpoint["status"] = "applying"

    assert not orchestrator._checkpoint_ownership_barrier(state, tasks)

    assert checkpoint["status"] == "recoverable"
    assert transaction["status"] == "detached"
    assert checkpoint["detachment"]["checkpoint_status"] == "applying"
    assert checkpoint["detachment"]["proof"] == "exact_prestate_restored"
    assert changed_paths(root) == []
    assert (root / "checkpoint-owned.txt").read_text(
        encoding="utf-8"
    ) == "baseline\n"
    assert ref_exists(root, checkpoint_ref)


def test_unprovable_checkpoint_detachment_returns_structured_owner_blocker_before_clean_tree_gate(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    orchestrator.config.gates.require_clean_git_before_task = True
    owner = _task("checkpoint-owner")
    independent = _task("independent-task")
    tasks = [owner, independent]
    state = RunState(
        run_id="unprovable-applied-checkpoint",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    checkpoint_ref = _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )
    _mark_localized_checkpoint_owner(state, owner)
    changed_contents = "candidate changed after checkpoint application\n"
    (root / "checkpoint-owned.txt").write_text(
        changed_contents,
        encoding="utf-8",
    )
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    save_run_state(root, state)

    with (
        patch.object(
            orchestrator,
            "_restore_persisted_evidence_repair_ownership",
            side_effect=AssertionError(
                "checkpoint barrier must run before retained ownership migration"
            ),
        ),
        patch.object(
            orchestrator,
            "_require_clean_tree_for_task",
            side_effect=AssertionError(
                "ordinary clean-tree gate must not own this failure"
            ),
        ),
        patch.object(
            orchestrator,
            "_execute_task_in_main_worktree",
            side_effect=AssertionError("no task may be dispatched"),
        ),
    ):
        returned = orchestrator._run_implementation_loop(
            state,
            max_tasks=1,
        )

    assert returned is state
    assert state.status == "blocked"
    blocker = state.active_blocker
    assert blocker["source"] == "parallel_lane_failure"
    assert blocker["category"] == (
        "checkpoint_ownership_detachment_unproven"
    )
    assert blocker["task_id"] == owner.task_id
    assert blocker["checkpoint_owner_task_id"] == owner.task_id
    assert blocker["retained_ref"] == checkpoint_ref
    assert blocker["checkpoint_status"] == "applied"
    assert blocker["missing_or_mismatched_proof"] == (
        "applied_state_mismatch"
    )
    assert blocker["expected_fingerprints"]
    assert blocker["observed_fingerprints"]
    assert blocker["expected_fingerprints"] != (
        blocker["observed_fingerprints"]
    )
    assert state.task_failure_checkpoints[owner.task_id]["status"] == (
        "applied"
    )
    assert (root / "checkpoint-owned.txt").read_text(
        encoding="utf-8"
    ) == changed_contents
    assert ref_exists(root, checkpoint_ref)


def test_checkpoint_restore_failure_blocker_reports_prestate_and_current_fingerprints(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    orchestrator.config.execution.parallel_tasks.enabled = False
    orchestrator.config.gates.require_clean_git_before_task = True
    owner = _task("checkpoint-owner")
    independent = _task("independent-task")
    tasks = [owner, independent]
    state = RunState(
        run_id="failed-checkpoint-restore",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    checkpoint_ref = _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )
    _mark_localized_checkpoint_owner(state, owner)
    checkpoint = state.task_failure_checkpoints[owner.task_id]
    transaction = checkpoint["application_transaction"]
    expected_prestate = dict(
        transaction["pre_application"]["fingerprints"]
    )
    stale_applied = dict(transaction["applied_state"]["fingerprints"])
    partial_contents = "partially restored before rollback failed\n"
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    save_run_state(root, state)

    def fail_after_partial_restore(
        _project_root: Path,
        _transaction: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        (root / "checkpoint-owned.txt").write_text(
            partial_contents,
            encoding="utf-8",
        )
        raise RuntimeError("simulated rollback verification failure")

    with (
        patch(
            "auto_agents.git_ops.rollback_checkpoint_application",
            side_effect=fail_after_partial_restore,
        ),
        patch.object(
            orchestrator,
            "_restore_persisted_evidence_repair_ownership",
            side_effect=AssertionError(
                "checkpoint barrier must run before ownership migration"
            ),
        ),
        patch.object(
            orchestrator,
            "_require_clean_tree_for_task",
            side_effect=AssertionError(
                "ordinary clean-tree gate must not own this failure"
            ),
        ),
        patch.object(
            orchestrator,
            "_execute_task_in_main_worktree",
            side_effect=AssertionError("no task may be dispatched"),
        ),
    ):
        returned = orchestrator._run_implementation_loop(
            state,
            max_tasks=1,
        )

    assert returned is state
    assert state.status == "blocked"
    blocker = state.active_blocker
    assert blocker["category"] == (
        "checkpoint_ownership_detachment_unproven"
    )
    assert blocker["missing_or_mismatched_proof"] == (
        "prestate_restore_unproven"
    )
    assert blocker["expected_fingerprints"] == expected_prestate
    assert blocker["observed_fingerprints"] == (
        checkpoint_repository_fingerprints(root)
    )
    assert blocker["observed_fingerprints"] != stale_applied
    assert checkpoint["detachment"]["expected_fingerprints"] == (
        blocker["expected_fingerprints"]
    )
    assert checkpoint["detachment"]["observed_fingerprints"] == (
        blocker["observed_fingerprints"]
    )
    assert checkpoint["detachment"]["fingerprint_boundary"] == (
        "post_restore_attempt"
    )
    assert checkpoint["status"] == "applied"
    assert (root / "checkpoint-owned.txt").read_text(
        encoding="utf-8"
    ) == partial_contents
    assert independent.status == "pending"
    assert ref_exists(root, checkpoint_ref)


def test_applied_checkpoint_owner_is_the_only_runnable_lane(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    owner = _task("checkpoint-owner")
    independent = _task("independent-task")
    tasks = [owner, independent]
    state = RunState(
        run_id="applied-checkpoint-owner-lane",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )
    executed: list[str] = []

    def complete_owner(
        current_state: RunState,
        _tasks: list[TaskSpec],
        task: TaskSpec,
    ) -> None:
        executed.append(task.task_id)
        assert task.task_id == owner.task_id
        assert (root / "checkpoint-owned.txt").read_text(
            encoding="utf-8"
        ) == "retained candidate\n"
        task.status = "done"
        task.commit_sha = commit_all(root, "test: commit checkpoint owner")
        orchestrator._clear_task_failure_checkpoint(
            current_state,
            task.task_id,
        )
        return None

    run_batch = Mock(
        side_effect=AssertionError(
            "an applied checkpoint owner must never enter a parallel batch"
        )
    )
    orchestrator._run_parallel_task_batch = run_batch
    with patch.object(
        orchestrator,
        "_execute_task_in_main_worktree",
        side_effect=complete_owner,
    ):
        returned = orchestrator._run_parallel_implementation_loop(
            state,
            tasks,
            max_tasks=1,
        )

    assert returned is state
    assert executed == [owner.task_id]
    assert owner.status == "done"
    assert independent.status == "pending"
    run_batch.assert_not_called()
    assert owner.task_id not in state.task_failure_checkpoints


@pytest.mark.parametrize(
    ("owner_status", "continue_independent_tasks"),
    [
        pytest.param("in_progress", True, id="interrupted-owner"),
        pytest.param("in_progress", False, id="interrupted-owner-serial-policy"),
        pytest.param("pending", True, id="pending-owner"),
        pytest.param("pending", False, id="independent-work-disabled"),
    ],
)
def test_detached_checkpoint_owner_reaches_sequential_retry_lane(
    tmp_path: Path,
    owner_status: str,
    continue_independent_tasks: bool,
) -> None:
    root, orchestrator = _project(tmp_path)
    orchestrator.config.execution.autonomy.continue_independent_tasks = (
        continue_independent_tasks
    )
    owner = _task("checkpoint-owner")
    independent = _task("independent-task")
    tasks = [independent, owner]
    state = RunState(
        run_id="detached-checkpoint-owner-retry",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    checkpoint_ref = _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )
    _mark_localized_checkpoint_owner(state, owner)
    owner.status = owner_status
    state.localized_blockers[0]["status"] = "retrying"
    state.active_blocker["status"] = "retrying"
    save_task_plan(root, {"tasks": [task.to_dict() for task in tasks]})
    save_run_state(root, state)

    setup_observed: list[str] = []
    executed: list[str] = []

    def observe_first_setup_step(
        current_state: RunState,
        _tasks: list[TaskSpec],
    ) -> list[str]:
        checkpoint = current_state.task_failure_checkpoints[owner.task_id]
        assert checkpoint["status"] == "recoverable"
        assert checkpoint["application_transaction"]["status"] == (
            "detached"
        )
        assert changed_paths(root) == []
        assert orchestrator._parallel_sequential_retry_ids(
            current_state
        )[0] == owner.task_id
        assert current_state.status == "pending"
        setup_observed.append(owner.task_id)
        return []

    def execute_owner(
        current_state: RunState,
        _tasks: list[TaskSpec],
        task: TaskSpec,
    ) -> None:
        executed.append(task.task_id)
        assert task.task_id == owner.task_id
        checkpoint = current_state.task_failure_checkpoints[owner.task_id]
        assert checkpoint["status"] == "recoverable"
        assert orchestrator._restore_task_failure_checkpoint(
            current_state,
            task,
            root,
        ) == checkpoint_ref
        assert (root / "checkpoint-owned.txt").read_text(
            encoding="utf-8"
        ) == "retained candidate\n"
        task.status = "done"
        task.commit_sha = commit_all(root, "test: commit resumed owner")
        orchestrator._clear_task_failure_checkpoint(
            current_state,
            task.task_id,
        )
        return None

    run_batch = Mock(
        side_effect=AssertionError(
            "a detached checkpoint owner must use its sequential retry lane"
        )
    )
    with (
        patch.object(
            orchestrator,
            "_restore_persisted_evidence_repair_ownership",
            side_effect=observe_first_setup_step,
        ),
        patch.object(orchestrator, "_ensure_implement_verify_baseline"),
        patch.object(
            orchestrator,
            "_execute_task_in_main_worktree",
            side_effect=execute_owner,
        ),
        patch.object(
            orchestrator,
            "_run_parallel_task_batch",
            run_batch,
        ),
    ):
        returned = orchestrator._run_implementation_loop(
            state,
            max_tasks=1,
        )

    assert returned is state
    assert setup_observed == [owner.task_id]
    assert executed == [owner.task_id]
    assert owner.status == "done"
    assert independent.status == "pending"
    run_batch.assert_not_called()
    assert owner.task_id not in state.task_failure_checkpoints
    assert not ref_exists(root, checkpoint_ref)


def test_applied_owner_checkpoint_detaches_before_evidence_reroute(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    owner = _task("checkpoint-owner")
    tasks = [owner]
    state = RunState(
        run_id="checkpoint-owner-evidence-reroute",
        status="pending",
        current_stage="implement",
        tasks=tasks,
    )
    checkpoint_ref = _apply_retained_checkpoint(
        tmp_path,
        root,
        orchestrator,
        state,
        owner,
    )

    def observe_evidence_reroute(
        current_state: RunState,
        _tasks: list[TaskSpec],
        _task: TaskSpec,
        _route: dict[str, object],
    ) -> RunState:
        checkpoint = current_state.task_failure_checkpoints[owner.task_id]
        assert checkpoint["status"] == "recoverable"
        assert checkpoint["application_transaction"]["status"] == (
            "detached"
        )
        assert changed_paths(root) == []
        current_state.status = "waiting_user"
        return current_state

    with (
        patch.object(
            orchestrator,
            "_route_frontend_design_contract_prerequisite",
            return_value=None,
        ),
        patch.object(
            orchestrator,
            "_ensure_evidence_preflight",
            return_value={"route": "operator_input"},
        ),
        patch.object(
            orchestrator,
            "_route_evidence_preflight",
            side_effect=observe_evidence_reroute,
        ),
    ):
        returned = orchestrator._execute_task_in_main_worktree(
            state,
            tasks,
            owner,
        )

    assert returned is state
    assert state.status == "waiting_user"
    assert ref_exists(root, checkpoint_ref)


def test_applied_checkpoint_owner_prerequisite_rewind_detaches_before_prototype_dispatch(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    (
        spec_file,
        owner,
        _state,
        checkpoint_ref,
        expected_prestate,
    ) = _applied_frontend_checkpoint_state(
        tmp_path,
        root,
        orchestrator,
        run_id="checkpoint-prerequisite-rewind",
    )

    def observe_prototype_dispatch(
        current_state: RunState,
        received_spec_file: Path,
    ) -> RunState:
        assert received_spec_file == spec_file
        checkpoint = current_state.task_failure_checkpoints[
            owner.task_id
        ]
        assert checkpoint["status"] == "recoverable"
        assert checkpoint["application_transaction"]["status"] == (
            "detached"
        )
        assert checkpoint["detachment"]["proof"] == (
            "exact_prestate_restored"
        )
        assert checkpoint["detachment"]["fingerprints"] == expected_prestate
        observed = checkpoint_repository_fingerprints(root)
        assert {
            key: observed[key]
            for key in ("head", "worktree", "index")
        } == {
            key: expected_prestate[key]
            for key in ("head", "worktree", "index")
        }
        assert "checkpoint-owned.txt" not in changed_paths(root)
        assert (root / "checkpoint-owned.txt").read_text(
            encoding="utf-8"
        ) == "baseline\n"
        assert ref_exists(root, checkpoint_ref)
        current_state.status = "paused"
        return current_state

    with (
        patch.object(orchestrator, "_ensure_preconditions"),
        patch.object(
            orchestrator,
            "_commit_planning_baseline_if_needed",
        ),
        patch.object(orchestrator, "_ensure_implement_verify_baseline"),
        patch.object(
            orchestrator,
            "_run_prototype_stage",
            side_effect=observe_prototype_dispatch,
        ) as prototype_dispatch,
    ):
        result = orchestrator.run(
            spec_file,
            auto_approve=True,
            skip_validate=True,
            max_tasks=1,
        )

    assert result.status == "paused"
    assert result.current_stage == "prototype"
    prototype_dispatch.assert_called_once()


def test_applied_checkpoint_owner_prerequisite_rewind_blocks_unproven_detachment(
    tmp_path: Path,
) -> None:
    root, orchestrator = _project(tmp_path)
    spec_file, owner, _state, checkpoint_ref, _expected_prestate = (
        _applied_frontend_checkpoint_state(
            tmp_path,
            root,
            orchestrator,
            run_id="checkpoint-prerequisite-unproven",
        )
    )
    checkpoint_barrier = orchestrator._checkpoint_ownership_barrier

    def invalidate_saved_index_before_detachment(
        current_state: RunState,
        tasks: list[TaskSpec],
        **kwargs: object,
    ) -> bool:
        if kwargs.get("allow_owner_execution") is False:
            transaction = current_state.task_failure_checkpoints[
                owner.task_id
            ]["application_transaction"]
            transaction["pre_application"].pop("index_image", None)
        return checkpoint_barrier(current_state, tasks, **kwargs)

    with (
        patch.object(orchestrator, "_ensure_preconditions"),
        patch.object(
            orchestrator,
            "_commit_planning_baseline_if_needed",
        ),
        patch.object(orchestrator, "_ensure_implement_verify_baseline"),
        patch.object(
            orchestrator,
            "_checkpoint_ownership_barrier",
            side_effect=invalidate_saved_index_before_detachment,
        ),
        patch.object(orchestrator, "_run_prototype_stage") as prototype_dispatch,
    ):
        result = orchestrator.run(
            spec_file,
            auto_approve=True,
            skip_validate=True,
            max_tasks=1,
        )

    prototype_dispatch.assert_not_called()
    assert result.status == "blocked"
    assert result.active_blocker["category"] == (
        "checkpoint_ownership_detachment_unproven"
    )
    assert result.active_blocker["task_id"] == owner.task_id
    assert result.active_blocker["checkpoint_owner_task_id"] == (
        owner.task_id
    )
    assert result.active_blocker["retained_ref"] == checkpoint_ref
    assert result.active_blocker["missing_or_mismatched_proof"] == (
        "transaction_invalid"
    )
    checkpoint = result.task_failure_checkpoints[owner.task_id]
    assert checkpoint["status"] == "applied"
    assert (root / "checkpoint-owned.txt").read_text(
        encoding="utf-8"
    ) == "retained candidate\n"
    assert ref_exists(root, checkpoint_ref)
