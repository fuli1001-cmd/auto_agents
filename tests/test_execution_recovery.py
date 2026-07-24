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
