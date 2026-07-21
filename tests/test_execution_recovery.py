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
from auto_agents.models import AgentResult, AgentTermination, CommandResult, RunState
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
            self.assertEqual(state.status, "pending")

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

    def test_interactive_recovery_agent_can_rewind_to_plan(self) -> None:
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
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            save_run_state(root, state)
            orchestrator = Orchestrator(root, user_input_fn=lambda _prompt: "watch mode")

            with patch.object(
                orchestrator,
                "_agent_diagnose_execution_incident",
                return_value=IncidentDiagnosis(
                    owner="verification_contract",
                    action="REWIND_PLAN",
                    confidence=0.95,
                    reason="configured command is non-terminating",
                ),
            ):
                recovered = orchestrator.recover_execution_incident(interactive=True)

            self.assertEqual(recovered.status, "pending")
            self.assertEqual(recovered.current_stage, "plan")


if __name__ == "__main__":
    unittest.main()
