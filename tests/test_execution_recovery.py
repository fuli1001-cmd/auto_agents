import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.execution_recovery import (
    ExecutionIncident,
    ExecutionIncidentStore,
    IncidentDiagnosis,
    command_incident,
    deterministic_diagnosis,
    parse_incident_diagnosis,
    provider_incident,
)
from auto_agents.models import AgentResult, AgentTermination, CommandResult, RunState, TaskSpec
from auto_agents.gates import GateCommandTimeoutError
from auto_agents.orchestrator import Orchestrator
from auto_agents.config import load_run_state, load_task_plan, save_run_state


class ExecutionRecoveryTests(unittest.TestCase):
    def test_command_incident_redacts_secrets_and_is_stable(self) -> None:
        result = CommandResult(
            command="pytest -q --token=abc",
            ok=False,
            returncode=124,
            stderr="API_KEY=secret-value",
            termination_reason="stalled",
            timeout_seconds=7200,
            last_activity_seconds=42,
            activity_kind="output",
            process_snapshot={"pgid": 12},
        )
        incident = command_incident(
            run_id="run-1",
            stage="implement",
            context="baseline",
            result=result,
            baseline=True,
        )

        self.assertEqual(incident.kind, "gate_stall")
        self.assertNotIn("secret-value", incident.stderr_tail)
        self.assertEqual(incident.process_snapshot["pgid"], 12)

    def test_command_incident_dynamic_output_changes_evidence_not_identity(self) -> None:
        first = command_incident(
            run_id="run-1",
            stage="implement",
            context="baseline",
            result=CommandResult(
                command="npm exec -- vitest run browser.test.ts",
                ok=False,
                returncode=125,
                stderr=(
                    "worker memory capacity remained unavailable for 30.0s: "
                    "4597 MiB available, 6144 MiB required"
                ),
                termination_reason="remote_lane_state_lost",
            ),
            baseline=True,
            head_ref="head-1",
        )
        second = command_incident(
            run_id="run-1",
            stage="implement",
            context="baseline",
            result=CommandResult(
                command="npm exec -- vitest run browser.test.ts",
                ok=False,
                returncode=125,
                stderr=(
                    "worker memory capacity remained unavailable for 30.0s: "
                    "4590 MiB available, 6144 MiB required"
                ),
                termination_reason="remote_lane_state_lost",
            ),
            baseline=True,
            head_ref="head-1",
        )

        self.assertEqual(first.incident_fingerprint, second.incident_fingerprint)
        self.assertNotEqual(first.evidence_fingerprint, second.evidence_fingerprint)

    def test_reported_infrastructure_incident_preserves_worker_evidence(self) -> None:
        incident = command_incident(
            run_id="run-1",
            stage="implement",
            context="task verification",
            result=CommandResult(
                command="npm test",
                ok=False,
                returncode=1,
                infrastructure_error=True,
                infrastructure_failure_id="browser_launch_failed",
                infrastructure_attempts=[
                    {"worker_id": "worker-1", "returncode": 1},
                    {"worker_id": "worker-2", "returncode": 1},
                ],
            ),
        )

        self.assertEqual(
            incident.kind,
            "gate_reported_infrastructure_error",
        )
        self.assertEqual(
            len(incident.process_snapshot["infrastructure_attempts"]),
            2,
        )

    def test_provider_incident_dynamic_output_changes_evidence_not_identity(self) -> None:
        first = provider_incident(
            run_id="run-1",
            stage="implement",
            provider="codex",
            result=AgentResult(
                ok=False,
                command=["codex"],
                output_path=Path("first.md"),
                stderr="provider request req-123 failed after 5.1s",
                returncode=1,
                termination=AgentTermination(
                    reason="provider_error",
                    elapsed_seconds=5.1,
                ),
            ),
            head_ref="head-1",
        )
        second = provider_incident(
            run_id="run-1",
            stage="implement",
            provider="codex",
            result=AgentResult(
                ok=False,
                command=["codex"],
                output_path=Path("second.md"),
                stderr="provider request req-456 failed after 6.2s",
                returncode=1,
                termination=AgentTermination(
                    reason="provider_error",
                    elapsed_seconds=6.2,
                ),
            ),
            head_ref="head-1",
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.incident_fingerprint, second.incident_fingerprint)
        self.assertNotEqual(first.evidence_fingerprint, second.evidence_fingerprint)

    def test_deterministic_routes_watch_mode_to_plan(self) -> None:
        incident = ExecutionIncident(
            incident_id="i1",
            run_id="r1",
            source="gate",
            kind="gate_stall",
            stage="implement",
            context="baseline",
            command="npm exec vitest watch",
            termination_reason="stalled",
            baseline=True,
        )

        diagnosis = deterministic_diagnosis(incident)

        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis.owner, "verification_contract")
        self.assertEqual(diagnosis.action, "REWIND_PLAN")

    def test_cleanup_uncertainty_stops_automatic_recovery(self) -> None:
        incident = ExecutionIncident(
            incident_id="i2",
            run_id="r1",
            source="gate",
            kind="gate_timeout",
            stage="implement",
            context="baseline",
            termination_reason="timeout",
            cleanup_incomplete=True,
        )

        diagnosis = deterministic_diagnosis(incident)

        self.assertEqual(diagnosis.action, "STOP")
        self.assertEqual(diagnosis.confidence, 1.0)

    def test_recover_target_with_redacted_command_pauses_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="redacted-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_timeout",
                stage="implement",
                context="baseline",
                command="pytest -q --token=<redacted>",
                termination_reason="timeout",
                baseline=True,
            )

            recovered = orchestrator._apply_execution_incident_diagnosis(
                state,
                incident,
                IncidentDiagnosis(
                    owner="target_project",
                    action="RECOVER_TARGET",
                    confidence=0.9,
                    reason="repair the target command",
                ),
            )

            self.assertFalse(recovered)
            self.assertEqual(state.status, "blocked")
            self.assertIn("cannot be reproduced safely", state.last_error)

    def test_reported_infrastructure_target_owner_forces_scoped_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="infra-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="npm test",
                incident_fingerprint="infra-fingerprint",
                evidence_fingerprint="infra-evidence",
                process_snapshot={
                    "infrastructure_attempts": [
                        {"worker_id": "worker-1", "returncode": 1}
                    ]
                },
            )

            recovered = orchestrator._apply_execution_incident_diagnosis(
                state,
                incident,
                IncidentDiagnosis(
                    owner="target_project",
                    action="RETRY",
                    confidence=0.95,
                    reason="browser launcher is broken in the target project",
                ),
            )

            self.assertTrue(recovered)
            self.assertEqual(incident.diagnosis["action"], "RECOVER_TARGET")
            tasks = load_task_plan(root)["tasks"]
            self.assertEqual(tasks[0]["title"], "Repair verification infrastructure")
            self.assertIn("all currently eligible workers", tasks[0]["description"])

    def test_store_persists_incident_and_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = RunState(run_id="run-1")
            incident = ExecutionIncident(
                incident_id="i3",
                run_id="run-1",
                source="provider",
                kind="provider_tool_stalled",
                stage="design",
                context="provider:codex",
            )
            store = ExecutionIncidentStore(root, state.run_id)

            store.save(incident, state)

            self.assertEqual(state.active_execution_incident_id, "i3")
            self.assertEqual(store.load("i3").kind, "provider_tool_stalled")
            incident.status = "resolved"
            store.save(incident, state)
            self.assertEqual(state.active_execution_incident_id, "")

    def test_run_state_round_trips_incident_budget_and_blocker(self) -> None:
        state = RunState(
            run_id="run-1",
            execution_incident_budget_epoch=3,
            execution_incident_budget_checkpoint={
                "epoch": 3,
                "reason": "baseline passed",
            },
            active_blocker={
                "owner": "auto_agents",
                "category": "gate_timeout",
            },
        )

        restored = RunState.from_dict(state.to_dict())

        self.assertEqual(restored.execution_incident_budget_epoch, 3)
        self.assertEqual(
            restored.execution_incident_budget_checkpoint["reason"],
            "baseline passed",
        )
        self.assertEqual(restored.active_blocker["owner"], "auto_agents")

    def test_new_iteration_resets_incident_and_blocker_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            previous_run_id = state.run_id
            state.status = "completed"
            state.active_execution_incident_id = "incident-1"
            state.execution_incidents = [
                {
                    "incident_id": "incident-1",
                    "incident_fingerprint": "fingerprint-1",
                    "budget_epoch": 4,
                }
            ]
            state.execution_incident_budget_epoch = 4
            state.execution_incident_budget_checkpoint = {
                "epoch": 4,
                "reason": "old checkpoint",
            }
            state.active_blocker = {"owner": "auto_agents", "status": "blocked"}
            state.recovery_loop_events = [{"event": "old recovery"}]
            state.last_recovery_route = {"action": "RETRY"}
            save_run_state(root, state)

            next_state = orchestrator._start_new_iteration(state)

            self.assertNotEqual(next_state.run_id, previous_run_id)
            self.assertEqual(next_state.execution_incidents, [])
            self.assertEqual(next_state.active_execution_incident_id, "")
            self.assertEqual(next_state.execution_incident_budget_epoch, 0)
            self.assertEqual(next_state.execution_incident_budget_checkpoint, {})
            self.assertEqual(next_state.active_blocker, {})
            self.assertEqual(next_state.recovery_loop_events, [])
            self.assertEqual(next_state.last_recovery_route, {})

    def test_run_incident_budget_counts_only_the_current_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.current_stage = "implement"
            state.execution_incident_budget_epoch = 1
            state.execution_incidents = [
                {
                    "incident_id": f"old-{index}",
                    "incident_fingerprint": f"old-fingerprint-{index}",
                    "budget_epoch": 0,
                    "status": "resolved",
                }
                for index in range(10)
            ]
            incident = ExecutionIncident(
                incident_id="current-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_remote_lane_state_lost",
                stage="implement",
                context="baseline",
                command="npm exec -- vitest run browser.test.ts",
                termination_reason="remote_lane_state_lost",
                incident_fingerprint="current-fingerprint",
                evidence_fingerprint="current-evidence",
            )
            incident = orchestrator._merge_or_save_execution_incident(
                state, incident
            )

            recovered = orchestrator._apply_execution_incident_diagnosis(
                state,
                incident,
                IncidentDiagnosis(
                    owner="external_provider",
                    action="RETRY",
                    confidence=0.95,
                    reason="retry the current execution lane",
                ),
            )

            self.assertTrue(recovered)
            self.assertEqual(incident.budget_epoch, 1)
            self.assertEqual(incident.recovery_round, 1)
            self.assertEqual(state.status, "pending")

    def test_run_incident_budget_blocks_seventh_identity_in_current_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.current_stage = "implement"
            state.execution_incident_budget_epoch = 2
            state.execution_incidents = [
                {
                    "incident_id": f"current-{index}",
                    "incident_fingerprint": f"current-fingerprint-{index}",
                    "budget_epoch": 2,
                    "status": "recovering",
                }
                for index in range(6)
            ]
            incident = ExecutionIncident(
                incident_id="current-7",
                run_id=state.run_id,
                source="gate",
                kind="gate_remote_lane_state_lost",
                stage="implement",
                context="baseline",
                command="npm exec -- vitest run browser.test.ts",
                termination_reason="remote_lane_state_lost",
                incident_fingerprint="current-fingerprint-7",
                evidence_fingerprint="current-evidence-7",
            )
            incident = orchestrator._merge_or_save_execution_incident(
                state, incident
            )

            recovered = orchestrator._apply_execution_incident_diagnosis(
                state,
                incident,
                IncidentDiagnosis(
                    owner="external_provider",
                    action="RETRY",
                    confidence=0.95,
                    reason="retry the current execution lane",
                ),
            )

            self.assertFalse(recovered)
            self.assertEqual(state.status, "blocked")
            self.assertEqual(
                state.last_error,
                "run-level incident budget was exhausted",
            )

    def test_resolved_incident_advances_budget_epoch_at_stable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            task = TaskSpec(
                task_id="task-1",
                title="Task",
                description="Task",
                acceptance=["passes"],
            )
            incident = ExecutionIncident(
                incident_id="incident-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_stalled",
                stage="implement",
                context="task verification",
                task_id=task.task_id,
                status="recovering",
                incident_fingerprint="fingerprint-1",
                evidence_fingerprint="evidence-1",
            )
            orchestrator._merge_or_save_execution_incident(state, incident)

            orchestrator._resolve_inline_task_incident(state, task)

            self.assertEqual(state.execution_incident_budget_epoch, 1)
            self.assertEqual(
                state.execution_incident_budget_checkpoint["incident_id"],
                incident.incident_id,
            )
            self.assertEqual(
                state.execution_incident_budget_checkpoint["reason"],
                "task retry passed",
            )
            self.assertEqual(state.active_execution_incident_id, "")

    def test_completed_stage_checkpoint_closes_legacy_incident_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.current_stage = "design"
            state.execution_incidents = [
                {
                    "incident_id": "legacy-resolved",
                    "incident_fingerprint": "legacy-fingerprint",
                    "stage": "clarify",
                    "status": "resolved",
                }
            ]

            advanced = orchestrator._advance_execution_incident_budget_epoch(
                state,
                reason="stage design completed",
            )

            self.assertTrue(advanced)
            self.assertEqual(state.execution_incident_budget_epoch, 1)
            self.assertEqual(
                state.execution_incident_budget_checkpoint["reason"],
                "stage design completed",
            )

    def test_same_incident_identity_is_not_merged_across_budget_epochs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            previous = ExecutionIncident(
                incident_id="previous",
                run_id=state.run_id,
                source="gate",
                kind="gate_timeout",
                stage="implement",
                context="baseline",
                incident_fingerprint="same-fingerprint",
                evidence_fingerprint="old-evidence",
                status="resolved",
            )
            orchestrator._merge_or_save_execution_incident(state, previous)
            orchestrator._advance_execution_incident_budget_epoch(
                state,
                reason="baseline passed",
                incident=previous,
            )
            current = ExecutionIncident(
                incident_id="current",
                run_id=state.run_id,
                source="gate",
                kind="gate_timeout",
                stage="implement",
                context="baseline",
                incident_fingerprint="same-fingerprint",
                evidence_fingerprint="new-evidence",
            )

            merged = orchestrator._merge_or_save_execution_incident(state, current)

            self.assertEqual(merged.incident_id, "current")
            self.assertEqual(merged.budget_epoch, 1)
            self.assertEqual(len(state.execution_incidents), 2)

    def test_agent_diagnosis_requires_strict_bounded_json(self) -> None:
        diagnosis = parse_incident_diagnosis(
            '{"owner":"target_project","action":"RECOVER_TARGET",'
            '"confidence":0.91,"reason":"deadlock","evidence":["two workers"]}'
        )
        self.assertEqual(diagnosis.action, "RECOVER_TARGET")
        with self.assertRaises(ValueError):
            parse_incident_diagnosis(
                '{"owner":"target_project","action":"DELETE_TESTS",'
                '"confidence":1,"reason":"fast","evidence":[]}'
            )

    def test_baseline_stall_schedules_prebaseline_recovery_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = RunState(run_id="run-incident", current_stage="implement")
            result = CommandResult(
                command="python -m pytest -q",
                ok=False,
                returncode=124,
                termination_reason="stalled",
                timeout_seconds=7200,
            )
            error = GateCommandTimeoutError(
                "baseline stalled",
                result=result,
                context="implement verify baseline commands",
            )

            recovered = orchestrator._handle_gate_execution_incident(
                state, "implement", error
            )

            self.assertTrue(recovered)
            tasks = load_task_plan(root)["tasks"]
            self.assertGreaterEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["task_origin"], "stage_recovery")
            self.assertEqual(
                tasks[0]["recovery_history"][0]["kind"], "execution_incident"
            )
            self.assertEqual(
                tasks[0]["verification_refs"],
                ["cmd:python -m pytest -q"],
            )
            self.assertEqual(state.status, "pending")

            recovery_task = orchestrator._load_tasks_from_plan()[0]
            self.assertEqual(
                orchestrator._build_task_verify_commands(recovery_task),
                ["python -m pytest -q"],
            )

    def test_prebaseline_lane_does_not_run_the_same_baseline_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = RunState(run_id="run-lane", current_stage="implement")
            result = CommandResult(
                command="python -m pytest -q",
                ok=False,
                returncode=124,
                termination_reason="timeout",
                timeout_seconds=7200,
            )
            orchestrator._handle_gate_execution_incident(
                state,
                "implement",
                GateCommandTimeoutError("timeout", result=result, context="baseline"),
            )

            with (
                patch.object(orchestrator, "_commit_planning_baseline_if_needed"),
                patch.object(orchestrator, "_ensure_implement_verify_baseline") as baseline,
                patch.object(orchestrator, "_execute_task_in_main_worktree", return_value=None) as execute,
            ):
                orchestrator._run_implementation_loop(state, max_tasks=None)

            baseline.assert_not_called()
            self.assertTrue(execute.called)

    def test_prebaseline_lane_runs_ready_repair_before_incident_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            repair = TaskSpec(
                task_id="repair-recovery-r2-1",
                title="Repair owned evidence",
                description="Repair the failed evidence.",
                acceptance=["evidence passes"],
                status="pending",
                parent_task_id="recover-execution-i1-r1",
                task_origin="evidence_repair",
                verification_refs=["tests/test_owned.py::test_contract"],
            )
            parent = TaskSpec(
                task_id="recover-execution-i1-r1",
                title="Repair stalled verification command",
                description="Repair the original command.",
                acceptance=["command passes"],
                status="in_progress",
                depends_on=[repair.task_id],
                task_origin="stage_recovery",
                verification_refs=["cmd:python -m pytest -q tests/test_owned.py"],
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": "i1",
                        "verification_command": "python -m pytest -q tests/test_owned.py",
                        "result": "scheduled",
                    }
                ],
            )
            orchestrator._persist_tasks([repair, parent])
            state = load_run_state(root)

            with (
                patch.object(orchestrator, "_commit_planning_baseline_if_needed"),
                patch.object(orchestrator, "_ensure_implement_verify_baseline") as baseline,
                patch.object(
                    orchestrator,
                    "_execute_task_in_main_worktree",
                    return_value=None,
                ) as execute,
            ):
                orchestrator._run_implementation_loop(state, max_tasks=None)

            baseline.assert_not_called()
            self.assertEqual(execute.call_args.args[2].task_id, repair.task_id)

    def test_prebaseline_dependency_deadlock_records_self_repair_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            parent = TaskSpec(
                task_id="recover-execution-i1-r1",
                title="Recovery",
                description="Recovery",
                acceptance=["passes"],
                depends_on=["missing-repair"],
                verification_refs=["cmd:python -m pytest -q tests/test_owned.py"],
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": "i1",
                        "verification_command": "python -m pytest -q tests/test_owned.py",
                    }
                ],
            )

            with self.assertRaisesRegex(RuntimeError, "no runnable task"):
                orchestrator._ready_prebaseline_recovery_task(state, [parent])

            self.assertEqual(
                state.last_recovery_route["engine_invariant"],
                "execution_recovery_dependency_deadlock",
            )

    def test_legacy_unscoped_recovery_discards_only_pending_generated_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            repair = TaskSpec(
                task_id="repair-recover-execution-i1-r1-r2-1",
                title="Legacy unrelated repair",
                description="Generated from the accidental full gate.",
                acceptance=["passes"],
                status="pending",
                parent_task_id="recover-execution-i1-r1",
                task_origin="evidence_repair",
                verification_refs=["tests/test_unrelated.py::test_other"],
            )
            parent = TaskSpec(
                task_id="recover-execution-i1-r1",
                title="Repair stalled verification command",
                description="Repair the original command.",
                acceptance=["command passes"],
                status="in_progress",
                depends_on=[repair.task_id],
                task_origin="stage_recovery",
                recovery_round=2,
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": "i1",
                        "verification_command": "python -m pytest -q tests/test_owned.py",
                        "result": "scheduled",
                    },
                    {
                        "round": 2,
                        "result": "scheduled",
                        "repair_task_ids": [repair.task_id],
                    },
                ],
            )
            tasks = [repair, parent]
            state = load_run_state(root)

            changed = orchestrator._normalize_legacy_execution_recovery_tasks(
                state, tasks
            )

            self.assertTrue(changed)
            self.assertEqual([task.task_id for task in tasks], [parent.task_id])
            self.assertEqual(
                parent.verification_refs,
                ["cmd:python -m pytest -q tests/test_owned.py"],
            )
            self.assertEqual(parent.depends_on, [])
            self.assertEqual(parent.recovery_round, 1)
            self.assertTrue(parent.recovery_history[1]["superseded"])

    def test_legacy_recovery_with_partial_repair_blocks_without_discarding_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            repair = TaskSpec(
                task_id="repair-recovery",
                title="Partial repair",
                description="May own worktree changes.",
                acceptance=["passes"],
                status="in_progress",
                parent_task_id="recover-execution-i1-r1",
                task_origin="evidence_repair",
            )
            parent = TaskSpec(
                task_id="recover-execution-i1-r1",
                title="Recovery",
                description="Recovery",
                acceptance=["passes"],
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": "i1",
                        "verification_command": "python -m pytest -q tests/test_owned.py",
                    }
                ],
            )
            tasks = [repair, parent]
            state = load_run_state(root)

            orchestrator._normalize_legacy_execution_recovery_tasks(state, tasks)

            self.assertEqual(state.status, "blocked")
            self.assertEqual(
                state.active_blocker["category"], "legacy_recovery_migration"
            )
            self.assertEqual([task.task_id for task in tasks], [repair.task_id, parent.task_id])
            self.assertIn("partially executed unscoped repair", state.last_error)

    def test_gate_timeout_without_result_blocks_for_safe_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = RunState(run_id="run-no-result", current_stage="implement")

            recovered = orchestrator._handle_gate_execution_incident(
                state,
                "implement",
                GateCommandTimeoutError("worker ended without a command result"),
            )

            self.assertFalse(recovered)
            self.assertEqual(state.status, "blocked")
            self.assertEqual(
                state.active_blocker["category"], "gate_timeout_missing_result"
            )

    def test_provider_termination_is_persisted_as_an_execution_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            result = AgentResult(
                ok=False,
                command=["mock"],
                output_path=root / "out.txt",
                stderr="provider stalled",
                returncode=124,
                termination=AgentTermination(
                    reason="semantic_stall",
                    elapsed_seconds=10,
                    last_provider_activity_seconds=4,
                ),
            )

            incident = orchestrator._record_provider_execution_incident(
                "design", "mock", result
            )

            self.assertIsNotNone(incident)
            self.assertEqual(incident.source, "provider")
            state = orchestrator.status()
            self.assertEqual(state["active_execution_incident_id"], incident.incident_id)

    def test_run_reopens_legacy_blocked_incident_without_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "paused"
            incident = ExecutionIncident(
                incident_id="interactive-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_stall",
                stage="implement",
                context="baseline",
                termination_reason="stalled",
                status="needs_human",
                diagnosis={
                    "owner": "auto_agents",
                    "action": "RETRY",
                    "confidence": 0.99,
                },
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            save_run_state(root, state)
            incident.recovery_round = 2
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            save_run_state(root, state)
            orchestrator = Orchestrator(root)

            changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.current_stage, "implement")
            reopened = ExecutionIncidentStore(root, state.run_id).load(
                incident.incident_id
            )
            self.assertEqual(reopened.status, "recovering")
            self.assertEqual(reopened.recovery_round, 0)
            self.assertEqual(state.active_blocker["status"], "retrying")

    def test_auto_agents_block_waits_for_self_repair_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.status = "blocked"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "scheduler_invariant",
                "reason": "scheduler state is inconsistent",
                "status": "blocked",
            }

            changed = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertEqual(state.status, "blocked")

    def test_self_repair_commit_reopens_incident_for_new_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="repair-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_infrastructure_error",
                stage="implement",
                context="baseline",
                status="self_repair",
                recovery_round=2,
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            state.status = "blocked"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": incident.kind,
                "status": "blocked",
            }
            save_run_state(root, state)

            reopened = orchestrator.mark_self_repair_applied("abc123")

            self.assertEqual(reopened.status, "pending")
            self.assertEqual(reopened.active_blocker["status"], "retrying")
            self.assertEqual(reopened.active_blocker["self_repair_commit"], "abc123")
            saved = ExecutionIncidentStore(root, state.run_id).load(incident.incident_id)
            self.assertEqual(saved.status, "recovering")
            self.assertEqual(saved.recovery_round, 0)

    def test_runtime_interruption_resumes_once_then_uses_read_only_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.current_stage = "implement"
            state.tasks = [
                TaskSpec(
                    task_id="task-1",
                    title="Task",
                    description="Task",
                    acceptance=["passes"],
                    status="in_progress",
                )
            ]
            state.resume_context["implementation_ready_tasks"] = {"task-1": True}
            save_run_state(root, state)
            snapshot = {
                "detected_at": "2026-07-21T00:00:00+00:00",
                "owner": {"pid": 123, "run_token": "old"},
                "control": {
                    "project": str(root.resolve()),
                    "updated_at": "2026-07-21T00:00:01+00:00",
                    "processes": [{"kind": "gate", "pid": 456}],
                },
            }

            first = orchestrator.reconcile_runtime_interruption(snapshot)

            self.assertEqual(first.status, "pending")
            self.assertEqual(
                first.recovery_loop_events[-1]["action"], "resume_checkpoint"
            )
            with patch.object(
                orchestrator,
                "_agent_diagnose_execution_incident",
                return_value=IncidentDiagnosis(
                    owner="target_project",
                    action="RETRY",
                    confidence=0.9,
                    reason="resume the persisted task checkpoint",
                ),
            ) as diagnose:
                second = orchestrator.reconcile_runtime_interruption(snapshot)

            diagnose.assert_called_once()
            self.assertEqual(second.status, "pending")
            self.assertEqual(second.recovery_loop_events[-1]["occurrence_count"], 2)
            self.assertEqual(
                second.recovery_loop_events[-1]["action"], "resume_checkpoint"
            )
            status = orchestrator.status()
            self.assertEqual(
                status["last_runtime_interruption"]["occurrence_count"], 2
            )

    def test_runtime_interruption_repetition_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            snapshot = {
                "detected_at": "2026-07-21T00:00:00+00:00",
                "owner": {"pid": 123},
                "control": {
                    "project": str(root.resolve()),
                    "updated_at": "2026-07-21T00:00:01+00:00",
                    "processes": [],
                },
            }
            orchestrator.reconcile_runtime_interruption(snapshot)
            with patch.object(
                orchestrator,
                "_agent_diagnose_execution_incident",
                return_value=IncidentDiagnosis(
                    owner="external_provider",
                    action="RETRY",
                    confidence=0.9,
                    reason="retry provider resume",
                ),
            ):
                orchestrator.reconcile_runtime_interruption(snapshot)

            third = orchestrator.reconcile_runtime_interruption(snapshot)

            self.assertEqual(third.status, "blocked")
            self.assertIn("interrupted repeatedly", third.last_error)
            self.assertEqual(
                third.recovery_loop_events[-1]["action"], "block_repeated_interruption"
            )


if __name__ == "__main__":
    unittest.main()
