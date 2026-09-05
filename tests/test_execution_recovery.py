import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.execution_recovery import (
    BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
    ExecutionIncident,
    ExecutionIncidentStore,
    IncidentDiagnosis,
    ParallelLaneFailure,
    command_incident,
    deterministic_diagnosis,
    parse_incident_diagnosis,
    provider_incident,
    recovery_task_marker,
)
from auto_agents.models import (
    AgentRequest,
    AgentResult,
    AgentTermination,
    CommandResult,
    GateResult,
    RunState,
    TaskSpec,
    VerificationStep,
)
from auto_agents.gates import (
    GateCommandBaselineIdentityError,
    GateCommandInfrastructureError,
    GateCommandTimeoutError,
    build_failure_identity_diagnostic_command,
    classify_reported_infrastructure_failure,
)
from auto_agents.infrastructure_repair import InfrastructureRepairResult
from auto_agents.git_ops import (
    commit_all,
    commit_changed_paths,
    head_ref,
    worktree_fingerprint,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.self_repair import classify_auto_agents_error
from auto_agents.validation import (
    validate_task_dependencies,
    validate_task_plan_payload,
)
from auto_agents.config import (
    load_run_state,
    load_task_plan,
    save_run_state,
    save_task_plan,
)


class ExecutionRecoveryTests(unittest.TestCase):
    def test_parallel_lane_infrastructure_incident_round_trips(self) -> None:
        payload = ParallelLaneFailure(
            task={"task_id": "lane-a", "status": "blocked"},
            operation="verification",
            owner="verification_infrastructure",
            automatic_retryable=False,
            resumable=True,
            reason="verification token=classified was unavailable",
            redacted_evidence="API_KEY=classified\nservice unavailable",
            current_failure_ids=["tests/test_boundary.py::test_service"],
            baseline_failure_ids=["tests/test_boundary.py::test_service"],
            new_failure_ids=[],
            owned_failure_ids=["tests/test_boundary.py::test_service"],
            failure_class="baseline_only_owned",
            baseline_comparison_comparable=True,
            base_ref="abc123",
            checkpoint={
                "status": "recoverable",
                "resume_mode": "gate_recheck",
            },
            command_incident={
                "context": "parallel verification",
                "baseline": False,
            },
            implementation_completed=True,
        ).to_dict()

        restored = ParallelLaneFailure.from_dict(payload)
        round_tripped = restored.to_dict()

        self.assertEqual(round_tripped["schema_version"], 1)
        self.assertEqual(round_tripped["kind"], "parallel_lane_failure")
        self.assertEqual(round_tripped["new_failure_ids"], [])
        self.assertEqual(
            round_tripped["current_failure_ids"],
            ["tests/test_boundary.py::test_service"],
        )
        self.assertEqual(round_tripped["checkpoint"]["resume_mode"], "gate_recheck")
        self.assertTrue(round_tripped["baseline_comparison_comparable"])
        self.assertTrue(round_tripped["implementation_completed"])
        self.assertNotIn("classified", round_tripped["reason"])
        self.assertNotIn("classified", round_tripped["redacted_evidence"])

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

    def test_gate_root_cause_identity_ignores_volatile_task_context(self) -> None:
        result = CommandResult(
            command=(
                "python -m pytest -q "
                "tests/test_owned.py::test_contract"
            ),
            ok=False,
            returncode=4,
            stderr="ERROR: not found: tests/test_owned.py::test_contract",
            process_snapshot={
                "baseline_failure_identity": {
                    "status": "unresolved",
                    "contract": "stable_test_failure_ids",
                }
            },
        )
        first = command_incident(
            run_id="run-1",
            stage="implement",
            context="lazy task baseline verification (repair-task-r1)",
            result=result,
            baseline=True,
        )
        second = command_incident(
            run_id="run-1",
            stage="implement",
            context="lazy task baseline verification (repair-task-r2)",
            result=result,
            baseline=True,
        )

        self.assertNotEqual(first.incident_fingerprint, second.incident_fingerprint)
        self.assertEqual(
            first.root_cause_fingerprint,
            second.root_cause_fingerprint,
        )

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

    def test_worker_allocation_failure_has_deterministic_actionable_diagnosis(
        self,
    ) -> None:
        message = (
            "Verification could not start: no eligible worker can run this command.\n"
            "Required worker: 2 slot(s); capabilities: docker, ffmpeg.\n"
            "Suggested actions:\n- Start the Docker daemon."
        )
        incident = ExecutionIncident(
            incident_id="worker-pool-1",
            run_id="run-1",
            source="gate",
            kind="gate_infrastructure_error",
            stage="implement",
            context="task verification",
            command="pytest tests/integration",
            stderr_tail=message,
            process_snapshot={
                "worker_allocation": {
                    "status": "no_eligible_worker",
                    "user_message": message,
                    "workers": [
                        {
                            "worker_id": "local-worker",
                            "status": "ineligible",
                            "reasons": ["missing capabilities: docker"],
                        }
                    ],
                }
            },
        )

        diagnosis = deterministic_diagnosis(incident)

        self.assertIsNotNone(diagnosis)
        assert diagnosis is not None
        self.assertEqual(diagnosis.owner, "verification_infrastructure")
        self.assertEqual(diagnosis.action, "REPAIR_INFRASTRUCTURE")
        self.assertEqual(diagnosis.cause_status, "confirmed")
        self.assertEqual(diagnosis.failure_domain, "worker_pool")
        self.assertIn("Required worker: 2 slot(s)", diagnosis.reason)
        self.assertIn("missing capabilities: docker", diagnosis.evidence[0])

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

    def test_reported_infrastructure_explicit_target_scope_skips_provider_judge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            result = CommandResult(
                command="npm exec -- vitest run src/e2e/browser.test.ts",
                ok=False,
                returncode=1,
                stderr=(
                    "AUTO_AGENTS_INFRA_FAILURE "
                    "id=browser_verification_infrastructure_failed "
                    "capability=chrome contract=cdp-v1 "
                    "repair_scope=target_project: load event timed out"
                ),
            )
            classify_reported_infrastructure_failure(result)
            error = GateCommandInfrastructureError(
                "browser verification infrastructure failed",
                result=result,
                context="implement verify baseline commands",
                baseline=True,
            )

            with patch.object(
                orchestrator,
                "_agent_diagnose_execution_incident",
                side_effect=AssertionError("explicit scope must be deterministic"),
            ):
                recovered = orchestrator._handle_gate_execution_incident(
                    state, "implement", error
                )

            self.assertTrue(recovered)
            tasks = load_task_plan(root)["tasks"]
            self.assertEqual(tasks[0]["title"], "Repair verification infrastructure")
            self.assertEqual(
                state.execution_incidents[-1]["diagnosis"]["owner"],
                "target_project",
            )

    def test_unresolved_baseline_identity_routes_to_verification_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            command = "npm exec -- vitest run src/e2e/setup.test.ts"
            gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout=(
                            "Error: suite setup failed before test collection\n"
                            " Test Files  1 failed (1)\n"
                            "      Tests  17 skipped (17)\n"
                        ),
                    )
                ],
                summary="suite setup failed",
            )

            with (
                patch.object(
                    orchestrator,
                    "_run_verify_failure_identity_diagnostic",
                    return_value=gate,
                ),
                self.assertRaises(GateCommandBaselineIdentityError) as raised,
            ):
                orchestrator._validated_baseline_failures(
                    gate,
                    context="lazy task baseline verification",
                    task_id="task-setup",
                )

            result = raised.exception.result
            self.assertIsNotNone(result)
            self.assertFalse(result.termination_reason)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(result.infrastructure_error)
            self.assertFalse(result.infrastructure_failure_id)
            with patch.object(
                orchestrator,
                "_agent_diagnose_execution_incident",
                side_effect=AssertionError("repair scope must route deterministically"),
            ):
                recovered = orchestrator._handle_gate_execution_incident(
                    state,
                    "implement",
                    raised.exception,
                )

            self.assertFalse(recovered)
            incident = state.execution_incidents[-1]
            self.assertEqual(
                incident["kind"],
                BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
            )
            self.assertEqual(
                incident["diagnosis"]["owner"],
                "auto_agents",
            )
            self.assertEqual(
                incident["diagnosis"]["action"],
                "SELF_REPAIR",
            )
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.active_blocker["owner"], "auto_agents")
            tasks = load_task_plan(root)["tasks"]
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["title"], "replace-me")

    def test_workspace_conda_repair_resumes_before_repeat_route_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.status = "blocked"
            incident = ExecutionIncident(
                incident_id="conda-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="baseline",
                command="conda run -p ./.conda python -m pytest -q",
                stderr_tail=(
                    "EnvironmentLocationNotFound: Not a conda environment: "
                    f"{root / '.conda'}"
                ),
                evidence_fingerprint="same-evidence",
                history=[
                    {
                        "event": "route",
                        "action": "REPAIR_INFRASTRUCTURE",
                        "evidence_fingerprint": "same-evidence",
                    }
                ],
            )
            repair = InfrastructureRepairResult(
                repaired=True,
                capability="workspace_conda",
                action="recreated_from_pyproject",
                reason="ready",
            )

            with patch(
                "auto_agents.orchestrator.repair_workspace_local_conda",
                return_value=repair,
            ):
                recovered = orchestrator._apply_execution_incident_diagnosis(
                    state,
                    incident,
                    IncidentDiagnosis(
                        owner="execution_environment",
                        action="REPAIR_INFRASTRUCTURE",
                        confidence=0.95,
                        reason="workspace prefix is missing",
                    ),
                )

            self.assertTrue(recovered)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.last_error, "")
            self.assertEqual(
                incident.repair_history[-1]["action"],
                "recreated_from_pyproject",
            )

    def test_unmanaged_infrastructure_blocker_does_not_claim_repair_attempts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            message = (
                "Verification could not start: no eligible worker can run this command.\n"
                "Suggested actions:\n- Start a compatible worker."
            )
            incident = ExecutionIncident(
                incident_id="worker-pool-2",
                run_id=state.run_id,
                source="gate",
                kind="gate_infrastructure_error",
                stage="implement",
                context="task verification",
                command="pytest tests/integration",
                evidence_fingerprint="worker-pool-evidence",
                process_snapshot={
                    "worker_allocation": {
                        "status": "no_eligible_worker",
                        "user_message": message,
                    }
                },
            )

            recovered = orchestrator._apply_execution_incident_diagnosis(
                state,
                incident,
                IncidentDiagnosis(
                    owner="verification_infrastructure",
                    action="REPAIR_INFRASTRUCTURE",
                    confidence=1.0,
                    reason=message,
                    cause_status="confirmed",
                ),
            )

            self.assertFalse(recovered)
            self.assertEqual(state.last_error, message)
            self.assertNotIn("repair exhausted", state.last_error.lower())
            self.assertEqual(incident.repair_history, [])

    def test_missing_conda_supersedes_misrouted_target_recovery_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="runtime-1",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="baseline",
                command="./.conda/bin/python -m pytest -q tests/test_api.py",
                diagnosis={
                    "owner": "target_project",
                    "action": "RECOVER_TARGET",
                    "cause_status": "unknown",
                },
            )
            orchestrator._incident_store(state).save(incident, state)
            task = TaskSpec(
                task_id="recover-execution-runtime-1-r1",
                title="Repair verification infrastructure",
                description="repair",
                acceptance=["verification runs"],
                status="in_progress",
                task_origin="stage_recovery",
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": incident.incident_id,
                    }
                ],
            )
            state.tasks = [task]
            orchestrator._persist_tasks(state.tasks)

            changed = orchestrator._normalize_missing_workspace_dependency_recovery(
                state
            )

            self.assertTrue(changed)
            self.assertEqual(state.tasks, [])
            retired = state.resume_context["retired_workspace_recovery_tasks"]
            self.assertEqual(
                retired[0]["task_id"],
                task.task_id,
            )

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
            state.resume_context["evidence_preflight_routes"] = {
                "task-old": {"repeat": 2}
            }
            state.resume_context["provider_recovery_contract_receipts"] = {
                "old-contract": {"outcome": "consumer_contract_unsatisfied"}
            }
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
            self.assertNotIn("evidence_preflight_routes", next_state.resume_context)
            self.assertNotIn(
                "provider_recovery_contract_receipts",
                next_state.resume_context,
            )

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

    def test_recovery_task_completion_waits_for_original_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="baseline-identity",
                run_id=state.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context="lazy task baseline verification (repair-task)",
                command=(
                    "python -m pytest -q "
                    "tests/test_owned.py::test_contract"
                ),
                task_id="repair-task",
                baseline=True,
                status="recovering",
                recovery_round=1,
                incident_fingerprint="baseline-root",
                root_cause_fingerprint="baseline-root",
                evidence_fingerprint="evidence-1",
            )
            orchestrator._merge_or_save_execution_incident(state, incident)
            task = TaskSpec(
                task_id="recover-execution-baseline-identity-r1",
                title="Repair baseline identity",
                description="",
                acceptance=[],
                task_origin="stage_recovery",
                recovery_history=[
                    recovery_task_marker(
                        incident.incident_id,
                        incident.command,
                        recovery_round=1,
                    )
                ],
                commit_sha="repair-commit",
            )

            orchestrator._resolve_execution_incident_for_task(state, task)

            saved = ExecutionIncidentStore(root, state.run_id).load(
                incident.incident_id
            )
            self.assertEqual(saved.status, "repair_attempt_completed")
            self.assertEqual(state.active_execution_incident_id, incident.incident_id)
            self.assertEqual(state.execution_incident_budget_epoch, 0)
            self.assertFalse(state.execution_incident_budget_checkpoint)

    def test_original_baseline_boundary_resolves_completed_repair_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            context = "lazy task baseline verification (repair-task)"
            incident = ExecutionIncident(
                incident_id="baseline-identity",
                run_id=state.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context=context,
                command=(
                    "python -m pytest -q "
                    "tests/test_owned.py::test_contract"
                ),
                task_id="repair-task",
                baseline=True,
                status="repair_attempt_completed",
                recovery_round=1,
                incident_fingerprint="baseline-context",
                root_cause_fingerprint="baseline-root",
                evidence_fingerprint="evidence-1",
            )
            orchestrator._merge_or_save_execution_incident(state, incident)

            orchestrator._resolve_successful_baseline_execution_incident(
                state,
                context=context,
            )

            saved = ExecutionIncidentStore(root, state.run_id).load(
                incident.incident_id
            )
            self.assertEqual(saved.status, "resolved")
            self.assertEqual(state.active_execution_incident_id, "")
            self.assertEqual(state.execution_incident_budget_epoch, 1)
            progress = state.resume_context[
                orchestrator.EXECUTION_ROOT_PROGRESS_CONTEXT
            ]["baseline-root"]
            self.assertEqual(progress["occurrence_count"], 1)

    def test_same_root_cause_across_epochs_triggers_engine_circuit_breaker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.execution.recovery.max_occurrences_per_root_cause = 3
            state = load_run_state(root)
            state.current_stage = "implement"
            diagnosis = IncidentDiagnosis(
                owner="verification_contract",
                action="RECOVER_TARGET",
                confidence=1.0,
                reason="the same baseline identity is unresolved",
                evidence=["same semantic root"],
                cause_status="confirmed",
            )
            outcomes = []

            for index in range(3):
                incident = ExecutionIncident(
                    incident_id=f"same-root-{index}",
                    run_id=state.run_id,
                    source="gate",
                    kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                    stage="implement",
                    context="lazy task baseline verification (repair-task)",
                    command=(
                        "python -m pytest -q "
                        "tests/test_owned.py::test_contract"
                    ),
                    task_id="repair-task",
                    baseline=True,
                    incident_fingerprint=f"context-sensitive-{index}",
                    root_cause_fingerprint=(
                        f"legacy-context-sensitive-{index}"
                        if index < 2
                        else "stable-root"
                    ),
                    evidence_fingerprint=f"changed-evidence-{index}",
                )
                incident = orchestrator._merge_or_save_execution_incident(
                    state,
                    incident,
                )
                outcomes.append(
                    orchestrator._apply_execution_incident_diagnosis(
                        state,
                        incident,
                        diagnosis,
                    )
                )
                if index < 2:
                    # Model the old false-positive checkpoint: a repair task
                    # passed, the incident was closed, and a new epoch began.
                    incident.status = "resolved"
                    orchestrator._incident_store(state).save(incident, state)
                    orchestrator._advance_execution_incident_budget_epoch(
                        state,
                        reason="repair task passed without owner progress",
                        incident=incident,
                    )

            self.assertEqual(outcomes, [True, True, False])
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.active_blocker["owner"], "auto_agents")
            self.assertEqual(
                state.active_blocker["category"],
                "execution_recovery_semantic_loop",
            )
            self.assertEqual(
                state.last_recovery_route["engine_invariant"],
                "recovery_checkpoint_false_positive",
            )
            self_repair = classify_auto_agents_error(
                RuntimeError(state.last_error),
                state=state,
                env={},
            )
            self.assertTrue(self_repair.eligible)
            self.assertEqual(
                self_repair.category,
                "execution_recovery_semantic_loop",
            )

    def test_self_repair_resume_migrates_legacy_root_progress_before_recurrence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.execution.recovery.max_occurrences_per_root_cause = 3
            state = load_run_state(root)
            state.current_stage = "implement"
            root_fingerprint = "stable-root"
            command = "python -m pytest -q tests/test_contract.py::test_contract"
            store = ExecutionIncidentStore(root, state.run_id)
            for index, occurrence_count in enumerate((8, 9, 12)):
                incident = ExecutionIncident(
                    incident_id=f"legacy-root-{index}",
                    run_id=state.run_id,
                    source="gate",
                    kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                    stage="implement",
                    context="lazy task baseline verification",
                    command=command,
                    baseline=True,
                    status="resolved" if index < 2 else "recovering",
                    incident_fingerprint=f"context-{index}",
                    root_cause_fingerprint=root_fingerprint,
                    origin_command=command,
                    occurrence_count=occurrence_count,
                )
                if index == 2:
                    incident.history.append(
                        {
                            "event": "self_repair_applied",
                            "commit_sha": "repair-commit",
                        }
                    )
                store.save(incident, state)
            state.status = "pending"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "execution_recovery_semantic_loop",
                "status": "retrying",
                "self_repair_commit": "repair-commit",
            }
            state.resume_context.pop(
                orchestrator.EXECUTION_ROOT_PROGRESS_CONTEXT,
                None,
            )
            save_run_state(root, state)

            resumed = Orchestrator(root)
            loaded = load_run_state(root)
            self.assertTrue(resumed._resume_blocked_run(loaded))
            checkpoint = loaded.resume_context[
                resumed.EXECUTION_ROOT_PROGRESS_CONTEXT
            ][root_fingerprint]
            self.assertEqual(checkpoint["occurrence_count"], 29)
            self.assertEqual(checkpoint["acknowledged_occurrence_count"], 29)
            self.assertEqual(
                checkpoint["schema_version"],
                resumed.EXECUTION_ROOT_PROGRESS_SCHEMA_VERSION,
            )

            recurrence = ExecutionIncident(
                incident_id="post-repair-recurrence",
                run_id=loaded.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context="lazy task baseline verification",
                command=command,
                baseline=True,
                incident_fingerprint="context-2",
                root_cause_fingerprint=root_fingerprint,
                origin_command=command,
                evidence_fingerprint="post-repair-evidence",
            )
            recurrence = resumed._merge_or_save_execution_incident(
                loaded,
                recurrence,
            )

            self.assertEqual(
                resumed._execution_incident_root_occurrences_since_progress(
                    loaded,
                    recurrence,
                ),
                1,
            )
            with patch.object(
                resumed,
                "_schedule_prebaseline_recovery_task",
            ):
                recovered = resumed._apply_execution_incident_diagnosis(
                    loaded,
                    recurrence,
                    IncidentDiagnosis(
                        owner="verification_contract",
                        action="RECOVER_TARGET",
                        confidence=1.0,
                        reason="the focused baseline identity remains unresolved",
                        cause_status="confirmed",
                    ),
                )
            self.assertTrue(recovered)
            self.assertNotEqual(loaded.status, "blocked")

    def test_self_repair_resume_repairs_zeroed_legacy_root_checkpoint_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            root_fingerprint = "stable-root"
            incident = ExecutionIncident(
                incident_id="legacy-active",
                run_id=state.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context="lazy task baseline verification",
                command="python -m pytest -q tests/test_contract.py::test_contract",
                status="recovering",
                incident_fingerprint="context-sensitive-root",
                root_cause_fingerprint=root_fingerprint,
                origin_command="python -m pytest -q tests/test_contract.py::test_contract",
                occurrence_count=7,
                history=[
                    {
                        "event": "self_repair_applied",
                        "commit_sha": "repair-commit",
                    }
                ],
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            state.resume_context[orchestrator.EXECUTION_ROOT_PROGRESS_CONTEXT] = {
                root_fingerprint: {
                    "occurrence_count": 7,
                    "acknowledged_occurrence_count": 0,
                }
            }

            self.assertTrue(
                orchestrator._migrate_self_repair_execution_root_progress(
                    state,
                    incident,
                )
            )
            checkpoint = state.resume_context[
                orchestrator.EXECUTION_ROOT_PROGRESS_CONTEXT
            ][root_fingerprint]
            self.assertEqual(checkpoint["acknowledged_occurrence_count"], 7)
            self.assertFalse(
                orchestrator._migrate_self_repair_execution_root_progress(
                    state,
                    incident,
                )
            )
            self.assertEqual(checkpoint["acknowledged_occurrence_count"], 7)

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

    def test_agent_diagnosis_normalizes_nested_reason_and_evidence(self) -> None:
        diagnosis = parse_incident_diagnosis(
            '{"owner":"verification_infrastructure",'
            '"action":"REPAIR_INFRASTRUCTURE","confidence":0.98,'
            '"reason":{"cause_status":"confirmed",'
            '"causation":"No eligible worker has Docker."},'
            '"evidence":{"observed":["command did not start"],'
            '"inferred":["worker pool needs repair"]}}'
        )

        self.assertEqual(diagnosis.reason, "No eligible worker has Docker.")
        self.assertEqual(diagnosis.cause_status, "confirmed")
        self.assertEqual(
            diagnosis.evidence,
            [
                "observed: command did not start",
                "inferred: worker pool needs repair",
            ],
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

    def test_rejection_recovery_task_inherits_v2_command_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            save_task_plan(
                root,
                {
                    "verification_policy_version": 2,
                    "test_strategy": "vitest",
                    "verification_steps": [
                        {
                            "kind": "test",
                            "runner": "vitest",
                            "purpose": "Run the synthetic recovery proof.",
                            "targets": ["tests/recovery.test.ts"],
                            "parallel_safe": True,
                            "cadence": "final_only",
                            "cache_scope": "source",
                            "result_cache_scope": "observed_inputs",
                        }
                    ],
                    "tasks": [
                        {
                            "task_id": "completed-task",
                            "title": "Completed task",
                            "description": "The planned work is already complete.",
                            "acceptance": ["The planned behavior is present."],
                            "status": "done",
                            "commit_message": "",
                        }
                    ],
                },
            )
            orchestrator = Orchestrator(root)
            orchestrator.config.execution.parallel_tasks.enabled = False
            state = load_run_state(root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.rejected_stage = "implement"
            state.rejection_reason = (
                "- Failure type: full_verification\n"
                "- Reason: a synthetic final verification failed"
            )

            with (
                patch.object(
                    orchestrator,
                    "_commit_planning_baseline_if_needed",
                ),
                patch.object(
                    orchestrator,
                    "_ensure_implement_verify_baseline",
                    return_value=False,
                ),
                patch.object(
                    orchestrator,
                    "_run_sequential_implementation_loop",
                    side_effect=lambda current, _tasks, _limit: current,
                ),
            ):
                result = orchestrator._run_implementation_loop(
                    state,
                    max_tasks=0,
                )

            recovery_task = result.tasks[-1]
            self.assertEqual(recovery_task.task_origin, "stage_recovery")
            self.assertEqual(
                recovery_task.verification_refs,
                ["cmd:npm exec -- vitest run tests/recovery.test.ts"],
            )
            recovery_task.verification_refs = []
            orchestrator._persist_tasks(result.tasks)
            self.assertEqual(
                load_task_plan(root)["tasks"][-1]["verification_refs"],
                ["cmd:npm exec -- vitest run tests/recovery.test.ts"],
            )
            self.assertTrue(orchestrator.validate()["ok"])

    def test_lazy_baseline_recovery_carries_its_exact_dirty_worktree_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            command = "python -m pytest -q tests/test_owned.py"
            orchestrator.config.gates.steps = [
                VerificationStep(proof_id="owned", command=command)
            ]
            owned_test = root / "tests" / "test_owned.py"
            owned_test.parent.mkdir(parents=True)
            owned_test.write_text("def test_contract():\n    assert True\n", encoding="utf-8")
            commit_all(root, "test: add owned verification baseline")
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Produces a partial implementation before verification.",
                acceptance=["The verification command passes."],
                status="in_progress",
                verification_refs=[f"cmd:{command}"],
                verify_baseline_ref=head_ref(root),
            )
            orchestrator._persist_tasks([source])
            state.tasks = [source]
            orchestrator._set_implementation_ready_marker(state, source, True)
            (root / "partial.txt").write_text("owned recovery changes\n", encoding="utf-8")
            current_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout="FAILED tests/test_owned.py::test_contract\n",
                    )
                ],
                summary="FAILED tests/test_owned.py::test_contract",
            )
            infrastructure = CommandResult(
                command=command,
                ok=False,
                returncode=1,
                stderr=(
                    "AUTO_AGENTS_INFRA_FAILURE id=verification_contract_failed "
                    "repair_scope=verification_contract: stale baseline contract"
                ),
            )
            classify_reported_infrastructure_failure(infrastructure)
            baseline_gate = GateResult(
                ok=False,
                commands=[infrastructure],
                summary="verification infrastructure failed",
            )

            with (
                patch.object(
                    orchestrator,
                    "_run_gate_commands_for_commands",
                    side_effect=[(current_gate, ""), (baseline_gate, "")],
                ),
                self.assertRaises(GateCommandInfrastructureError) as raised,
            ):
                orchestrator._run_task_verify(source, state=state)

            self.assertEqual(raised.exception.task_id, source.task_id)
            self.assertTrue(
                orchestrator._handle_gate_execution_incident(
                    state,
                    "implement",
                    raised.exception,
                )
            )

            tasks = orchestrator._load_tasks_from_plan()
            recovery = tasks[0]
            marker = orchestrator._execution_recovery_marker(recovery)
            handoff = marker["worktree_handoff"]
            self.assertEqual(handoff["source_task_id"], source.task_id)
            self.assertEqual(handoff["changed_paths"], ["partial.txt"])

            gate_result = {
                "ok": True,
                "review": "recovery verified",
                "verify_current_failure_ids": [],
            }
            with (
                patch.object(
                    orchestrator,
                    "_route_frontend_design_contract_prerequisite",
                    return_value=None,
                ),
                patch.object(orchestrator, "_ensure_evidence_preflight", return_value={}),
                patch.object(
                    orchestrator,
                    "_execute_task_with_retries",
                    return_value=gate_result,
                ) as execute,
                patch.object(orchestrator, "_warm_clean_head_verify_baseline"),
                patch("auto_agents.orchestrator.commit_all", return_value="commit-sha"),
            ):
                result = orchestrator._execute_task_in_main_worktree(
                    state,
                    tasks,
                    recovery,
                )
            self.assertIsNone(result)
            execute.assert_called_once()
            self.assertEqual(recovery.status, "done")

    def test_recovery_commit_preserves_borrowed_owner_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            owner_path = root / "owner.py"
            owner_path.write_text("VALUE = 'base'\n", encoding="utf-8")
            commit_all(root, "test: add borrowed owner baseline")

            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Owns an uncommitted implementation candidate.",
                acceptance=["The focused verification passes."],
                status="in_progress",
            )
            state.tasks = [source]
            orchestrator._persist_tasks(state.tasks)
            orchestrator._set_implementation_ready_marker(state, source, True)
            owner_path.write_text("VALUE = 'borrowed'\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "--", "owner.py"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                orchestrator._capture_retained_worktree_ownership(
                    state,
                    [source.task_id],
                    source="implementation_ready",
                ),
                [source.task_id],
            )
            incident = ExecutionIncident(
                incident_id="borrowed-owner",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                task_id=source.task_id,
                recovery_round=1,
            )
            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            tasks = orchestrator._load_tasks_from_plan()
            recovery = tasks[0]
            borrowed_fingerprint = worktree_fingerprint(root)

            def finish_recovery(*_args, **_kwargs):
                (root / "recovery_support.py").write_text(
                    "RECOVERED = True\n",
                    encoding="utf-8",
                )
                return {
                    "ok": True,
                    "review": "recovery verified",
                    "verify_current_failure_ids": [],
                }

            with (
                patch.object(
                    orchestrator,
                    "_route_frontend_design_contract_prerequisite",
                    return_value=None,
                ),
                patch.object(orchestrator, "_ensure_evidence_preflight", return_value={}),
                patch.object(
                    orchestrator,
                    "_execute_task_with_retries",
                    side_effect=finish_recovery,
                ),
                patch.object(orchestrator, "_warm_clean_head_verify_baseline"),
            ):
                result = orchestrator._execute_task_in_main_worktree(
                    state,
                    tasks,
                    recovery,
                )

            self.assertIsNone(result)
            self.assertEqual(recovery.status, "done")
            self.assertEqual(
                owner_path.read_text(encoding="utf-8"),
                "VALUE = 'borrowed'\n",
            )
            self.assertEqual(
                worktree_fingerprint(root),
                borrowed_fingerprint,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--short", "--", "owner.py"],
                    cwd=root,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "M  owner.py",
            )
            committed = commit_changed_paths(root, recovery.commit_sha)
            self.assertIn("recovery_support.py", committed)
            self.assertNotIn("owner.py", committed)
            marker = orchestrator._execution_recovery_marker(recovery)
            self.assertEqual(
                marker["worktree_handoff"]["borrowed_paths"],
                ["owner.py"],
            )
            self.assertIn(
                "recovery_support.py",
                marker["borrowed_worktree_validation"]["recovery_delta_paths"],
            )
            owner_record = state.resume_context["retained_worktree_ownership"][
                source.task_id
            ]
            self.assertEqual(owner_record["head_ref"], head_ref(root))
            self.assertTrue(
                orchestrator._retained_worktree_snapshot_matches(
                    owner_record,
                    allow_pending_planning_changes=False,
                )
            )

    def test_recovery_blocks_when_borrowed_index_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            owner_path = root / "owner.py"
            owner_path.write_text("VALUE = 'base'\n", encoding="utf-8")
            commit_all(root, "test: add staged owner baseline")

            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Owns a staged implementation candidate.",
                acceptance=["The focused verification passes."],
                status="in_progress",
            )
            state.tasks = [source]
            orchestrator._persist_tasks(state.tasks)
            orchestrator._set_implementation_ready_marker(state, source, True)
            owner_path.write_text("VALUE = 'borrowed'\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "--", "owner.py"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            incident = ExecutionIncident(
                incident_id="borrowed-index",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                task_id=source.task_id,
                recovery_round=1,
            )
            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            tasks = orchestrator._load_tasks_from_plan()
            recovery = tasks[0]
            original_head = head_ref(root)

            def alter_only_the_index(*_args, **_kwargs):
                subprocess.run(
                    ["git", "reset", "-q", "HEAD", "--", "owner.py"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                )
                (root / "recovery_support.py").write_text(
                    "RECOVERED = True\n",
                    encoding="utf-8",
                )
                return {
                    "ok": True,
                    "review": "recovery verified",
                    "verify_current_failure_ids": [],
                }

            with (
                patch.object(
                    orchestrator,
                    "_route_frontend_design_contract_prerequisite",
                    return_value=None,
                ),
                patch.object(orchestrator, "_ensure_evidence_preflight", return_value={}),
                patch.object(
                    orchestrator,
                    "_execute_task_with_retries",
                    side_effect=alter_only_the_index,
                ),
                patch.object(orchestrator, "_warm_clean_head_verify_baseline"),
            ):
                result = orchestrator._execute_task_in_main_worktree(
                    state,
                    tasks,
                    recovery,
                )

            self.assertIs(result, state)
            self.assertEqual(head_ref(root), original_head)
            self.assertEqual(recovery.status, "blocked")
            self.assertEqual(
                state.active_blocker["category"],
                "execution_recovery_borrowed_worktree_mutation",
            )
            detail = state.active_blocker["execution_recovery_borrowed_worktree"]
            self.assertEqual(detail["changed_index_paths"], ["owner.py"])

    def test_prebaseline_recovery_preempts_stale_repair_ownership_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            owner_path = root / "owner.py"
            owner_path.write_text("VALUE = 'base'\n", encoding="utf-8")
            commit_all(root, "test: add stale ownership baseline")

            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.current_stage = "implement"
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Owns retained proof work.",
                acceptance=["The proof passes."],
                status="in_progress",
                recovery_round=1,
                verification_refs=["tests/test_owned.py::test_contract"],
            )
            state.tasks = [source]
            orchestrator._set_implementation_ready_marker(state, source, True)
            owner_path.write_text("VALUE = 'retained'\n", encoding="utf-8")
            self.assertTrue(
                orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    source,
                    {
                        "reason": "owned proof failed",
                        "failure_ids": ["tests/test_owned.py::test_contract"],
                    },
                )
            )
            repair = next(
                task for task in state.tasks if task.task_origin == "evidence_repair"
            )
            commit_all(root, "test: emulate legacy recovery absorption")
            (root / "interrupted_recovery.py").write_text(
                "PARTIAL = True\n",
                encoding="utf-8",
            )
            recovery = TaskSpec(
                task_id="recover-execution-active-r1",
                title="Resume interrupted recovery",
                description="Complete the active pre-baseline recovery.",
                acceptance=["The original command passes."],
                status="in_progress",
                task_origin="stage_recovery",
                recovery_history=[
                    recovery_task_marker(
                        "active-incident",
                        "python -m pytest -q tests/test_owned.py",
                        recovery_round=1,
                    )
                ],
                verification_refs=["cmd:python -m pytest -q tests/test_owned.py"],
            )
            state.tasks = [repair, source, recovery]
            state.status = "pending"
            orchestrator._persist_tasks(state.tasks)
            executed = []

            def execute_recovery(_state, _tasks, task):
                executed.append(task.task_id)
                return _state

            with patch.object(
                orchestrator,
                "_execute_task_in_main_worktree",
                side_effect=execute_recovery,
            ):
                result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertIs(result, state)
            self.assertEqual(executed, [recovery.task_id])
            self.assertNotEqual(state.status, "blocked")

    def test_legacy_recovery_commit_reconciles_absorbed_owner_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            owner_path = root / "owner.py"
            owner_path.write_text("VALUE = 'base'\n", encoding="utf-8")
            commit_all(root, "test: add migration baseline")

            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Owns a retained implementation candidate.",
                acceptance=["The focused verification passes."],
                status="in_progress",
            )
            state.tasks = [source]
            orchestrator._persist_tasks(state.tasks)
            orchestrator._set_implementation_ready_marker(state, source, True)
            owner_path.write_text("VALUE = 'ancestor'\n", encoding="utf-8")
            self.assertEqual(
                orchestrator._capture_retained_worktree_ownership(
                    state,
                    [source.task_id],
                    source="implementation_ready",
                ),
                [source.task_id],
            )
            repair = TaskSpec(
                task_id="repair-task",
                title="Evidence repair",
                description="Owns a superseding retained candidate.",
                acceptance=["The focused verification passes."],
                status="in_progress",
                parent_task_id=source.task_id,
                task_origin="evidence_repair",
            )
            source.depends_on = [repair.task_id]
            state.tasks = [repair, source]
            orchestrator._persist_tasks(state.tasks)
            orchestrator._set_implementation_ready_marker(state, repair, True)
            owner_path.write_text("VALUE = 'borrowed'\n", encoding="utf-8")
            self.assertEqual(
                orchestrator._capture_retained_worktree_ownership(
                    state,
                    [repair.task_id],
                    source="implementation_ready",
                    replace_existing=True,
                ),
                [repair.task_id],
            )
            incident = ExecutionIncident(
                incident_id="legacy-absorption",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                task_id=repair.task_id,
                recovery_round=1,
            )
            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            tasks = orchestrator._load_tasks_from_plan()
            recovery = tasks[0]
            recovery.status = "done"
            orchestrator._persist_tasks(tasks)
            legacy_message = orchestrator.config.git.commit_message_template.format(
                task_id=recovery.task_id,
                title=recovery.title,
            )
            commit_all(root, legacy_message)
            recovery.commit_sha = ""
            (root / "active_recovery_delta.py").write_text(
                "PARTIAL = True\n",
                encoding="utf-8",
            )

            reconciled = (
                orchestrator._reconcile_retained_worktree_absorbed_by_execution_recovery(
                    state,
                    tasks,
                )
            )

            self.assertEqual(reconciled, [repair.task_id, source.task_id])
            self.assertNotIn("retained_worktree_ownership", state.resume_context)
            migrations = state.resume_context[
                "retained_worktree_execution_recovery_migrations"
            ]
            self.assertEqual(
                migrations[-2]["migration_kind"],
                "direct_recovery_commit",
            )
            self.assertEqual(migrations[-2]["owner_task_id"], repair.task_id)
            self.assertEqual(
                migrations[-1]["migration_kind"],
                "superseded_owner_lineage",
            )
            self.assertEqual(migrations[-1]["owner_task_id"], source.task_id)
            self.assertEqual(migrations[-1]["changed_paths"], ["owner.py"])

    def test_legacy_owner_migration_rejects_unrecorded_target_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            owner_path = root / "owner.py"
            owner_path.write_text("VALUE = 'base'\n", encoding="utf-8")
            commit_all(root, "test: add untrusted migration baseline")

            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Owns a retained implementation candidate.",
                acceptance=["The focused verification passes."],
                status="in_progress",
            )
            tasks = [source]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            orchestrator._set_implementation_ready_marker(state, source, True)
            owner_path.write_text("VALUE = 'borrowed'\n", encoding="utf-8")
            self.assertEqual(
                orchestrator._capture_retained_worktree_ownership(
                    state,
                    [source.task_id],
                    source="implementation_ready",
                ),
                [source.task_id],
            )
            commit_all(root, "user: commit a target-project candidate")

            self.assertEqual(
                orchestrator._reconcile_retained_worktree_absorbed_by_execution_recovery(
                    state,
                    tasks,
                ),
                [],
            )
            self.assertIn(
                source.task_id,
                state.resume_context["retained_worktree_ownership"],
            )

    def test_recurring_incident_requeues_same_task_for_fresh_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="repeat-infra",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                recovery_round=1,
                evidence_fingerprint="evidence-1",
            )
            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            task = orchestrator._load_tasks_from_plan()[0]
            marker = orchestrator._execution_recovery_marker(task)
            marker["implementation_completed_round"] = 1
            task.status = "in_progress"
            state.tasks = [task]
            state.agent_attempts[f"implement-{task.task_id}"] = 1
            orchestrator._set_implementation_ready_marker(state, task, True)
            orchestrator._persist_tasks([task])

            incident.recovery_round = 2
            incident.evidence_fingerprint = "evidence-2"
            orchestrator._schedule_prebaseline_recovery_task(state, incident)

            requeued = orchestrator._load_tasks_from_plan()[0]
            requeued_marker = orchestrator._execution_recovery_marker(requeued)
            self.assertEqual(requeued.status, "blocked")
            self.assertEqual(requeued.recovery_round, 2)
            self.assertEqual(requeued.verify_retry_epoch, 1)
            self.assertEqual(
                requeued_marker["implementation_required_round"],
                2,
            )
            self.assertEqual(
                requeued_marker["implementation_completed_round"],
                1,
            )
            self.assertNotIn(
                requeued.task_id,
                state.resume_context.get("implementation_ready_tasks", {}),
            )
            self.assertNotIn(f"implement-{requeued.task_id}", state.agent_attempts)

    def test_completed_recovery_round_reuse_allocates_fresh_durable_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="round-reuse",
                run_id=state.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                recovery_round=1,
                evidence_fingerprint="evidence-1",
            )

            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            first_round = orchestrator._load_tasks_from_plan()
            self.assertEqual(
                orchestrator._execution_recovery_marker(first_round[0])[
                    "route_generation"
                ],
                1,
            )
            first_round[0].status = "done"
            orchestrator._persist_tasks(first_round)

            incident.recovery_round = 2
            incident.evidence_fingerprint = "evidence-2"
            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            second_round = orchestrator._load_tasks_from_plan()
            self.assertEqual(
                orchestrator._execution_recovery_marker(second_round[0])[
                    "route_generation"
                ],
                2,
            )
            second_round[0].status = "done"
            orchestrator._persist_tasks(second_round)

            state.tasks = second_round
            state.status = "blocked"
            state.active_blocker = {
                "owner": "verification_contract",
                "category": BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                "reason": "resume the bounded recovery budget",
                "status": "blocked",
            }
            incident.status = "recovering"
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            save_run_state(root, state)

            # Ordinary blocked-run resume resets the bounded budget round. The
            # next diagnosed route must not reuse its durable task namespace.
            self.assertTrue(orchestrator._resume_blocked_run(state))
            resumed_incident = ExecutionIncidentStore(
                root,
                state.run_id,
            ).load(incident.incident_id)
            self.assertIsNotNone(resumed_incident)
            assert resumed_incident is not None
            self.assertEqual(resumed_incident.recovery_round, 0)
            resumed_incident.evidence_fingerprint = "evidence-3"
            self.assertTrue(
                orchestrator._apply_execution_incident_diagnosis(
                    state,
                    resumed_incident,
                    IncidentDiagnosis(
                        owner="verification_contract",
                        action="RECOVER_TARGET",
                        confidence=1.0,
                        reason="repair the recurring verification contract",
                        cause_status="confirmed",
                    ),
                )
            )

            persisted = load_task_plan(root)
            task_ids = [task["task_id"] for task in persisted["tasks"]]
            self.assertEqual(len(task_ids), len(set(task_ids)))
            self.assertEqual(
                task_ids[0],
                "recover-execution-round-reuse-r1-g3",
            )
            self.assertEqual(
                persisted["tasks"][0]["recovery_history"][0][
                    "route_generation"
                ],
                3,
            )
            persisted_incident = ExecutionIncidentStore(
                root,
                state.run_id,
            ).load(incident.incident_id)
            self.assertIsNotNone(persisted_incident)
            assert persisted_incident is not None
            self.assertEqual(
                persisted_incident.history[-1]["route_generation"],
                3,
            )
            self.assertEqual(
                persisted_incident.history[-1]["task_id"],
                task_ids[0],
            )
            self.assertFalse(
                any(
                    "duplicates task_id" in error
                    for error in validate_task_plan_payload(persisted)
                )
            )

    def test_legacy_completed_recovery_duplicates_migrate_without_history_loss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            duplicate_id = "recover-execution-legacy-incident-r1"

            first_child = TaskSpec(
                task_id="first-repair",
                title="First repair",
                description="Preserve the first route repair.",
                acceptance=["The first repair remains recorded."],
                status="done",
                parent_task_id=duplicate_id,
                task_origin="evidence_repair",
            )
            second_child = TaskSpec(
                task_id="second-repair",
                title="Second repair",
                description="Preserve the second route repair.",
                acceptance=["The second repair remains recorded."],
                status="done",
                parent_task_id=duplicate_id,
                task_origin="evidence_repair",
            )

            def recovery_task(
                evidence_fingerprint: str,
                child_id: str,
                commit_sha: str,
            ) -> TaskSpec:
                marker = recovery_task_marker(
                    "legacy-incident",
                    "python -m pytest -q tests/test_owned.py",
                    recovery_round=1,
                )
                marker["evidence_fingerprint"] = evidence_fingerprint
                return TaskSpec(
                    task_id=duplicate_id,
                    title="Completed recovery route",
                    description="Preserve a distinct completed recovery route.",
                    acceptance=["The route history remains durable."],
                    status="done",
                    commit_sha=commit_sha,
                    task_origin="stage_recovery",
                    recovery_round=1,
                    recovery_history=[
                        marker,
                        {
                            "round": 1,
                            "result": "scheduled",
                            "repair_task_ids": [child_id],
                        },
                    ],
                    verification_refs=[
                        "cmd:python -m pytest -q tests/test_owned.py"
                    ],
                )

            first = recovery_task("evidence-first", first_child.task_id, "commit-1")
            second = recovery_task(
                "evidence-second",
                second_child.task_id,
                "commit-2",
            )
            legacy_tasks = [first, first_child, second, second_child]
            plan_payloads = []
            for task in legacy_tasks:
                item = task.to_dict()
                item.pop("commit_sha", None)
                plan_payloads.append(item)
            save_task_plan(root, {"tasks": plan_payloads})
            state = load_run_state(root)
            state.tasks = legacy_tasks
            save_run_state(root, state)
            spec_file = root / "spec.md"
            spec_file.write_text("Synthetic recovery run.\n", encoding="utf-8")
            observed_preflight = []

            def stop_at_preflight(*_args, **_kwargs):
                task_ids = [
                    task["task_id"]
                    for task in load_task_plan(root)["tasks"]
                ]
                observed_preflight.append(task_ids)
                self.assertEqual(len(task_ids), len(set(task_ids)))
                raise RuntimeError("stop after migrated preflight observation")

            with (
                patch.object(
                    orchestrator,
                    "_ensure_preconditions",
                    side_effect=stop_at_preflight,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "migrated preflight observation",
                ),
            ):
                orchestrator.run(spec_file=spec_file)

            self.assertTrue(observed_preflight)

            persisted = load_task_plan(root)
            persisted_ids = [task["task_id"] for task in persisted["tasks"]]
            migrated_id = "recover-execution-legacy-incident-r1-g2"
            self.assertEqual(len(persisted_ids), len(set(persisted_ids)))
            self.assertIn(duplicate_id, persisted_ids)
            self.assertIn(migrated_id, persisted_ids)
            second_payload = next(
                task
                for task in persisted["tasks"]
                if task["task_id"] == migrated_id
            )
            self.assertEqual(
                second_payload["recovery_history"][0]["evidence_fingerprint"],
                "evidence-second",
            )
            self.assertEqual(
                next(
                    task
                    for task in persisted["tasks"]
                    if task["task_id"] == second_child.task_id
                )["parent_task_id"],
                migrated_id,
            )
            reloaded_state = load_run_state(root)
            migrated_state_task = next(
                task
                for task in reloaded_state.tasks
                if task.task_id == migrated_id
            )
            self.assertEqual(migrated_state_task.commit_sha, "commit-2")
            self.assertEqual(
                reloaded_state.resume_context[
                    "execution_recovery_identity_migrations"
                ][0]["task_id"],
                migrated_id,
            )
            self.assertFalse(
                any(
                    "duplicates task_id" in error
                    for error in validate_task_plan_payload(persisted)
                )
            )

    def test_task_persistence_rejects_duplicate_ids_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="duplicate-task",
                title="Duplicate task",
                description="Exercise the persistence invariant.",
                acceptance=["Duplicate identities are rejected."],
            )
            original_plan = load_task_plan(root)

            with self.assertRaisesRegex(
                RuntimeError,
                "persistence refused duplicate task_id",
            ):
                orchestrator._persist_tasks([task, TaskSpec.from_dict(task.to_dict())])

            self.assertEqual(load_task_plan(root), original_plan)

    def test_recurring_recovery_does_not_borrow_its_own_partial_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Owns the original dirty candidate.",
                acceptance=["The original command passes."],
                status="in_progress",
            )
            state.tasks = [source]
            orchestrator._persist_tasks(state.tasks)
            orchestrator._set_implementation_ready_marker(state, source, True)
            (root / "owner.py").write_text("BORROWED = True\n", encoding="utf-8")
            incident = ExecutionIncident(
                incident_id="recurring-with-handoff",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                task_id=source.task_id,
                recovery_round=1,
            )
            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            tasks = orchestrator._load_tasks_from_plan()
            recovery = tasks[0]
            recovery.status = "in_progress"
            orchestrator._set_implementation_ready_marker(state, recovery, True)
            (root / "recovery_delta.py").write_text(
                "PARTIAL = True\n",
                encoding="utf-8",
            )
            orchestrator._persist_tasks(tasks)

            incident.task_id = recovery.task_id
            incident.recovery_round = 2
            orchestrator._schedule_prebaseline_recovery_task(state, incident)

            requeued = orchestrator._load_tasks_from_plan()[0]
            handoff = orchestrator._execution_recovery_marker(requeued)[
                "worktree_handoff"
            ]
            self.assertEqual(handoff["source_task_id"], source.task_id)
            self.assertEqual(handoff["borrowed_paths"], ["owner.py"])

    def test_recovery_verify_without_current_round_implementation_is_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            task = TaskSpec(
                task_id="recover-execution-i1-r1",
                title="Recovery",
                description="Recovery",
                acceptance=["passes"],
                recovery_round=2,
                task_origin="stage_recovery",
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": "i1",
                        "verification_command": "python -m pytest -q",
                        "implementation_required_round": 2,
                        "implementation_completed_round": 1,
                    }
                ],
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "without a fresh implementation attempt",
            ):
                orchestrator._assert_execution_recovery_implementation_completed(
                    state,
                    task,
                )

            self.assertEqual(
                state.last_recovery_route["engine_invariant"],
                "execution_recovery_round_without_implementation",
            )

    def test_recurring_recovery_runs_implementation_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            task = TaskSpec(
                task_id="recover-execution-i1-r1",
                title="Recovery",
                description="Recovery",
                acceptance=["passes"],
                recovery_round=2,
                task_origin="stage_recovery",
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": "i1",
                        "verification_command": "python -m pytest -q",
                        "implementation_required_round": 2,
                        "implementation_completed_round": 1,
                    }
                ],
            )
            events = []

            def implement(**_kwargs):
                events.append("implement")
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=root / "agent-output.txt",
                    summary="implemented",
                )

            def verify(*_args, **_kwargs):
                events.append("verify")
                return {
                    "ok": True,
                    "reason": "passed",
                    "current_failure_ids": [],
                }

            with (
                patch.object(
                    orchestrator,
                    "_run_agent_with_retries",
                    side_effect=implement,
                ),
                patch.object(orchestrator, "_implement_touched_code", return_value=True),
                patch.object(orchestrator, "_run_task_verify", side_effect=verify),
                patch.object(
                    orchestrator,
                    "_run_task_review",
                    return_value={"ok": True, "review": "passed", "reason": ""},
                ),
                patch.object(
                    orchestrator,
                    "_run_task_visual_judge",
                    return_value={"ok": True, "status": "skipped", "reason": "not visual"},
                ),
            ):
                result = orchestrator._execute_task_with_retries(
                    state,
                    task,
                    resume_existing=False,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(events, ["implement", "verify"])
            self.assertFalse(
                orchestrator._execution_recovery_implementation_required(task)
            )

    def test_task_recovery_rejects_a_worktree_changed_after_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Produces a partial implementation before verification.",
                acceptance=["The verification command passes."],
                status="in_progress",
            )
            orchestrator._persist_tasks([source])
            state.tasks = [source]
            orchestrator._set_implementation_ready_marker(state, source, True)
            partial = root / "partial.txt"
            partial.write_text("checkpoint contents\n", encoding="utf-8")
            incident = ExecutionIncident(
                incident_id="next-incident",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                task_id=source.task_id,
                recovery_round=1,
            )
            orchestrator._schedule_prebaseline_recovery_task(state, incident)
            tasks = orchestrator._load_tasks_from_plan()
            recovery = tasks[0]
            partial.write_text("changed after scheduling\n", encoding="utf-8")

            with (
                patch.object(
                    orchestrator,
                    "_route_frontend_design_contract_prerequisite",
                    return_value=None,
                ),
                patch.object(
                    orchestrator,
                    "_execute_task_with_retries",
                ) as execute,
                self.assertRaisesRegex(
                    RuntimeError,
                    "working tree is not clean before task",
                ),
            ):
                orchestrator._execute_task_in_main_worktree(
                    state,
                    tasks,
                    recovery,
                )

            execute.assert_not_called()

    def test_legacy_recovery_infers_unique_dirty_worktree_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            source = TaskSpec(
                task_id="source-task",
                title="Source task",
                description="Produces a partial implementation before verification.",
                acceptance=["The verification command passes."],
                status="in_progress",
            )
            state.tasks = [source]
            orchestrator._set_implementation_ready_marker(state, source, True)
            (root / "partial.txt").write_text("legacy checkpoint\n", encoding="utf-8")
            incident = ExecutionIncident(
                incident_id="persisted-incident",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                head_ref=head_ref(root),
                worktree_fingerprint=worktree_fingerprint(root),
                recovery_round=1,
                status="recovering",
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            recovery = TaskSpec(
                task_id="persisted-recovery",
                title="Persisted recovery",
                description="Recovers a previously interrupted verification.",
                acceptance=["The original verification command passes."],
                status="pending",
                task_origin="stage_recovery",
                recovery_history=[
                    recovery_task_marker(
                        incident.incident_id,
                        incident.command,
                        recovery_round=incident.recovery_round,
                    )
                ],
                verification_refs=[f"cmd:{incident.command}"],
            )
            tasks = [recovery, source]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            save_run_state(root, state)

            gate_result = {
                "ok": True,
                "review": "recovery verified",
                "verify_current_failure_ids": [],
            }
            with (
                patch.object(
                    orchestrator,
                    "_route_frontend_design_contract_prerequisite",
                    return_value=None,
                ),
                patch.object(orchestrator, "_ensure_evidence_preflight", return_value={}),
                patch.object(
                    orchestrator,
                    "_execute_task_with_retries",
                    return_value=gate_result,
                ) as execute,
                patch.object(orchestrator, "_warm_clean_head_verify_baseline"),
                patch("auto_agents.orchestrator.commit_all", return_value="commit-sha"),
            ):
                result = orchestrator._execute_task_in_main_worktree(
                    state,
                    tasks,
                    recovery,
                )

            self.assertIsNone(result)
            execute.assert_called_once()
            marker = orchestrator._execution_recovery_marker(recovery)
            self.assertTrue(
                marker["worktree_handoff"]["migrated_from_incident_checkpoint"]
            )
            self.assertTrue(
                marker["worktree_handoff"]["source_task_inferred"]
            )
            self.assertEqual(
                marker["worktree_handoff"]["source_task_id"],
                source.task_id,
            )

    def test_legacy_recovery_does_not_claim_ambiguous_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            sources = [
                TaskSpec(
                    task_id=f"source-{index}",
                    title=f"Source {index}",
                    description="Produces candidate changes before verification.",
                    acceptance=["The verification command passes."],
                    status="in_progress",
                )
                for index in (1, 2)
            ]
            for source in sources:
                orchestrator._set_implementation_ready_marker(state, source, True)
            (root / "partial.txt").write_text(
                "ambiguous checkpoint\n",
                encoding="utf-8",
            )
            incident = ExecutionIncident(
                incident_id="ambiguous-incident",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q tests/test_owned.py",
                head_ref=head_ref(root),
                worktree_fingerprint=worktree_fingerprint(root),
                recovery_round=1,
                status="recovering",
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            recovery = TaskSpec(
                task_id="persisted-recovery",
                title="Persisted recovery",
                description="Recovers a previously interrupted verification.",
                acceptance=["The original verification command passes."],
                status="pending",
                task_origin="stage_recovery",
                recovery_history=[
                    recovery_task_marker(
                        incident.incident_id,
                        incident.command,
                        recovery_round=incident.recovery_round,
                    )
                ],
                verification_refs=[f"cmd:{incident.command}"],
            )
            tasks = [recovery, *sources]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            save_run_state(root, state)

            self.assertFalse(
                orchestrator._execution_recovery_worktree_handoff_matches(
                    state,
                    tasks,
                    recovery,
                )
            )
            self.assertNotIn(
                "worktree_handoff",
                orchestrator._execution_recovery_marker(recovery),
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

    def test_diagnostic_provider_success_does_not_resolve_target_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            incident = ExecutionIncident(
                incident_id="target-provider-incident",
                run_id=state.run_id,
                source="provider",
                kind="provider_timed_out",
                stage="implement",
                context="provider:mock",
                status="open",
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            save_run_state(root, state)
            before = (root / ".auto-agents" / "state" / "run_state.json").read_bytes()

            class SuccessfulAdapter:
                def run(self, request):
                    return AgentResult(
                        ok=True,
                        command=["mock"],
                        output_path=request.output_path,
                        summary="diagnosis complete",
                    )

            result = orchestrator._run_provider_with_smart_recovery(
                SuccessfulAdapter(),
                AgentRequest(
                    stage="self_repair_investigator",
                    effort="max",
                    prompt="diagnose",
                    cwd=root,
                    output_path=(
                        root
                        / ".auto-agents"
                        / "runs"
                        / state.run_id
                        / "investigator.json"
                    ),
                    record_execution_incidents=False,
                ),
                "mock",
            )

            self.assertTrue(result.ok)
            self.assertEqual(
                (root / ".auto-agents" / "state" / "run_state.json").read_bytes(),
                before,
            )
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                incident.incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.status, "open")

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

    def test_v4_reported_infrastructure_resume_requires_fresh_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.active_blocker = {
                "owner": "target_project",
                "category": "gate_reported_infrastructure_error",
                "status": "blocked",
            }
            task = TaskSpec(
                task_id="recover-execution-infra-r1",
                title="Recovery",
                description="Recovery",
                acceptance=["passes"],
                status="in_progress",
                task_origin="stage_recovery",
                recovery_round=1,
                recovery_history=[
                    {
                        "kind": "execution_incident",
                        "execution_incident_id": "infra",
                        "initial_recovery_round": 1,
                        "verification_command": "python -m pytest -q",
                    }
                ],
            )
            state.tasks = [task]
            state.resume_context["implementation_ready_tasks"] = {
                task.task_id: True,
            }
            orchestrator = Orchestrator(root)
            orchestrator._persist_tasks([task])
            incident = ExecutionIncident(
                incident_id="infra",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                command="python -m pytest -q",
                status="needs_human",
                recovery_round=2,
                recovery_policy_version=4,
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            save_run_state(root, state)

            changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            migrated_task = orchestrator._load_tasks_from_plan()[0]
            marker = orchestrator._execution_recovery_marker(migrated_task)
            self.assertEqual(migrated_task.status, "blocked")
            self.assertEqual(marker["implementation_required_round"], 2)
            self.assertEqual(marker.get("implementation_completed_round", 0), 0)
            self.assertNotIn(
                migrated_task.task_id,
                state.resume_context.get("implementation_ready_tasks", {}),
            )
            migrated_incident = ExecutionIncidentStore(
                root,
                state.run_id,
            ).load("infra")
            self.assertEqual(migrated_incident.recovery_policy_version, 5)
            self.assertEqual(migrated_incident.recovery_round, 2)
            self.assertTrue(
                any(
                    entry.get("event") == "policy_v5_task_migration"
                    for entry in migrated_incident.history
                )
            )

    def test_interrupted_run_does_not_reopen_resolved_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            state = load_run_state(root)
            state.status = "blocked"
            state.active_blocker = {
                "owner": "user_input",
                "category": "run_interrupted",
                "status": "blocked",
            }
            incident = ExecutionIncident(
                incident_id="resolved",
                run_id=state.run_id,
                source="gate",
                kind="gate_reported_infrastructure_error",
                stage="implement",
                context="task verification",
                status="resolved",
                recovery_round=2,
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)
            state.active_execution_incident_id = incident.incident_id
            save_run_state(root, state)
            orchestrator = Orchestrator(root)

            self.assertTrue(orchestrator._resume_blocked_run(state))

            saved = ExecutionIncidentStore(root, state.run_id).load(
                incident.incident_id
            )
            self.assertEqual(saved.status, "resolved")
            self.assertEqual(saved.recovery_round, 2)
            self.assertEqual(state.active_execution_incident_id, "")

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

    def test_dirty_requeue_block_restores_retry_ownership_lost_during_handoff(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="contract-task",
                title="Resume preserved implementation",
                description="Continue the self-repair requeued task.",
                acceptance=["The preserved implementation is verified."],
                status="pending",
                verify_retry_epoch=1,
            )
            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.tasks = [task]
            state.last_recovery_route = {
                "task_id": task.task_id,
                "lineage_id": task.task_id,
                "outcome": "self_repair_requeued",
                "reason": "self-repair opened a fresh verification retry lifecycle",
            }
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "dirty_worktree_requeue_lifecycle_violation",
                "reason": "self-repair agent completed without changing auto_agents",
                "status": "blocked",
            }
            orchestrator._persist_tasks(state.tasks)
            save_run_state(root, state)

            resumed = Orchestrator(root)
            changed = resumed._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.last_error, "")
            self.assertEqual(
                state.resume_context["parallel_sequential_retry_tasks"],
                [task.task_id],
            )
            self.assertEqual(state.active_blocker["status"], "retrying")
            self.assertTrue(state.active_blocker["bootstrap_state_recovered"])
            self.assertEqual(
                state.active_blocker["requeued_task_ids"],
                [task.task_id],
            )

    def test_unrelated_auto_agents_block_does_not_reuse_stale_requeue_route(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="contract-task",
                title="Pending task",
                description="A pending task with an older recovery route.",
                acceptance=["The task passes."],
                status="pending",
            )
            state = load_run_state(root)
            state.status = "blocked"
            state.tasks = [task]
            state.last_recovery_route = {
                "task_id": task.task_id,
                "outcome": "self_repair_requeued",
            }
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "scheduler_invariant",
                "reason": "a different engine invariant failed",
                "status": "blocked",
            }

            changed = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertEqual(state.status, "blocked")
            self.assertNotIn(
                "parallel_sequential_retry_tasks",
                state.resume_context,
            )

    def test_repaired_parser_reopens_retained_baseline_identity_incident(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            node_id = "tests/test_contract.py::test_fixture"
            baseline_ref = "refs/auto-agents/gate-snapshots/baseline-contract"
            baseline_source_ref = "1" * 40
            task = TaskSpec(
                task_id="contract-task",
                title="Verify fixture contract",
                description="Retain the immutable verification baseline.",
                acceptance=["The fixture contract is verified."],
                status="in_progress",
                verify_baseline_ref=baseline_ref,
                verify_baseline_source_ref=baseline_source_ref,
            )
            state.current_stage = "implement"
            state.status = "blocked"
            state.tasks = [task]
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "diagnosed_identity_parser_gap",
                "fingerprint": "original-blocker",
                "reason": "the baseline identity extractor needs repair",
                "status": "blocked",
                "root_cause_diagnosis": {
                    "final": {"owner": "auto_agents"},
                },
            }
            incident = ExecutionIncident(
                incident_id="baseline-identity",
                run_id=state.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context="lazy task baseline verification (contract-task)",
                command=f"python -m pytest -q {node_id}",
                task_id=task.task_id,
                baseline=True,
                returncode=1,
                stdout_tail=(
                    "================ short test summary info ================\n"
                    f"ERROR {node_id}\n"
                ),
                status="self_repair",
            )
            store = ExecutionIncidentStore(root, state.run_id)
            store.save(incident, state)
            target_head = head_ref(root)

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value="engine-with-repaired-parser",
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})
            self.assertEqual(state.last_error, "")
            self.assertEqual(
                state.last_recovery_route["outcome"],
                "baseline_identity_reparsed",
            )
            self.assertEqual(state.last_recovery_route["failure_ids"], [node_id])
            self.assertEqual(task.verify_baseline_ref, baseline_ref)
            self.assertEqual(task.verify_baseline_source_ref, baseline_source_ref)
            self.assertEqual(task.verify_baseline_failures, [])
            self.assertEqual(head_ref(root), target_head)
            persisted = store.load(incident.incident_id)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.status, "recovering")
            self.assertEqual(
                persisted.history[-1]["event"],
                "baseline_failure_identity_reparsed",
            )
            self.assertEqual(persisted.history[-1]["failure_ids"], [node_id])

    def test_baseline_failure_identity_resume_routes_current_selector_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            subprocess.run(
                ["git", "config", "user.name", "Auto Agents Tests"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )

            relative_test = Path("tests/test_selector_contract.py")
            selector = f"{relative_test.as_posix()}::test_current_contract"
            test_path = root / relative_test
            test_path.parent.mkdir(parents=True)
            test_path.write_text(
                "def test_existing_contract():\n"
                "    assert True\n",
                encoding="utf-8",
            )
            baseline_ref = commit_all(root, "test: seed immutable baseline")
            baseline_source = subprocess.run(
                ["git", "show", f"{baseline_ref}:{relative_test.as_posix()}"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout

            test_path.write_text(
                baseline_source
                + "\n\n"
                + "def test_current_contract():\n"
                + "    assert True\n",
                encoding="utf-8",
            )
            current_ref = commit_all(root, "test: add current selector")
            self.assertNotIn("test_current_contract", baseline_source)
            self.assertIn("test_current_contract", test_path.read_text(encoding="utf-8"))

            command = f"python -m pytest -q {selector}"
            orchestrator = Orchestrator(root)
            orchestrator.config.gates.verification_policy_version = 3
            orchestrator.config.gates.incremental_mode = "auto"
            orchestrator.config.gates.steps = [
                VerificationStep(proof_id="selector-contract", command=command)
            ]
            task = TaskSpec(
                task_id="contract-task",
                title="Verify a selector introduced after the baseline",
                description="Retain the immutable baseline across engine repair.",
                acceptance=["The current selector failure is routed normally."],
                status="blocked",
                verification_refs=[f"cmd:{command}"],
                verify_baseline_ref=baseline_ref,
                verify_baseline_source_ref=baseline_ref,
                verify_baseline_schema_version=2,
                verify_retry_epoch=2,
            )
            baseline_identity = (
                task.verify_baseline_ref.encode("utf-8"),
                task.verify_baseline_source_ref.encode("utf-8"),
            )
            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.tasks = [task]
            state.last_error = "the immutable baseline selector was unresolved"
            state.last_recovery_route = {
                "task_id": task.task_id,
                "lineage_id": task.task_id,
                "outcome": "blocked",
            }
            state.resume_context["implementation_ready_tasks"] = {
                task.task_id: True,
            }
            incident_id = "baseline-selector-identity"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                "incident_id": incident_id,
                "reason": state.last_error,
                "status": "blocked",
            }
            missing_output = (
                f"ERROR: not found: {selector}\n"
                "(no match in any of [<Module test_selector_contract.py>])\n"
            )
            incident = ExecutionIncident(
                incident_id=incident_id,
                run_id=state.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context=f"lazy task baseline verification ({task.task_id})",
                command=command,
                task_id=task.task_id,
                baseline=True,
                returncode=4,
                stdout_tail=missing_output,
                process_snapshot={
                    "baseline_failure_identity": {
                        "status": "unresolved",
                        "contract": "stable_test_failure_ids",
                    }
                },
                status="self_repair",
            )
            store = ExecutionIncidentStore(root, state.run_id)
            store.save(incident, state)
            orchestrator._persist_tasks(state.tasks)
            save_run_state(root, state)

            marked = orchestrator.mark_self_repair_applied(
                "engine-selector-repair",
                verification="focused selector recovery passed",
            )
            resumed = Orchestrator(root)
            resumed.config.gates.verification_policy_version = 3
            resumed.config.gates.incremental_mode = "auto"
            resumed.config.gates.steps = [
                VerificationStep(proof_id="selector-contract", command=command)
            ]
            self.assertTrue(resumed._resume_blocked_run(marked))
            resumed_task = next(
                candidate
                for candidate in marked.tasks
                if candidate.task_id == task.task_id
            )
            self.assertEqual(resumed_task.status, "pending")
            self.assertEqual(resumed_task.verify_retry_epoch, 3)
            self.assertNotIn(
                task.task_id,
                marked.resume_context.get("implementation_ready_tasks", {}),
            )
            self.assertEqual(
                (
                    resumed_task.verify_baseline_ref.encode("utf-8"),
                    resumed_task.verify_baseline_source_ref.encode("utf-8"),
                ),
                baseline_identity,
            )

            current_output = "verification failed before pytest reported test ids\n"
            current_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout=current_output,
                    )
                ],
                summary="current verification contract failed",
            )
            diagnostic_gate = GateResult(
                ok=True,
                commands=[
                    CommandResult(
                        command=build_failure_identity_diagnostic_command(command),
                        ok=True,
                        returncode=0,
                        stdout=f"{selector} PASSED\n",
                    )
                ],
                summary="the exact current selector passed",
            )
            baseline_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=4,
                        stdout=missing_output,
                    )
                ],
                summary="the immutable baseline does not contain the selector",
            )

            with (
                patch.object(resumed, "_quick_verify_failure", return_value=None),
                patch.object(
                    resumed,
                    "_run_task_gate_commands_for_commands",
                    side_effect=[(current_gate, ""), (baseline_gate, "")],
                ),
                patch.object(
                    resumed,
                    "_run_verify_failure_identity_diagnostic",
                    return_value=diagnostic_gate,
                ),
                patch.object(
                    resumed,
                    "_validated_baseline_failures",
                    side_effect=AssertionError(
                        "baseline-only selector absence must bypass identity validation"
                    ),
                ),
            ):
                verify_result = resumed._run_task_verify(
                    resumed_task,
                    state=marked,
                )

            self.assertFalse(current_gate.ok)
            self.assertFalse(verify_result["ok"])
            self.assertFalse(verify_result["comparable_failures"])
            self.assertEqual(verify_result["failure_ids"], [f"cmd:{command}"])
            self.assertEqual(
                verify_result["current_failure_ids"],
                [f"cmd:{command}"],
            )
            self.assertEqual(verify_result["baseline_failure_ids"], [])
            self.assertEqual(
                verify_result["baseline_not_applicable_commands"],
                [command],
            )
            self.assertIn(current_output.strip(), verify_result["raw_output"])
            self.assertEqual(resumed_task.verify_baseline_failures, [])
            self.assertEqual(marked.active_blocker, {})
            self.assertEqual(marked.active_execution_incident_id, "")
            resolved = store.load(incident_id)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.status, "resolved")
            self.assertEqual(
                [
                    entry["incident_id"]
                    for entry in marked.execution_incidents
                    if entry.get("kind")
                    == BASELINE_FAILURE_IDENTITY_INCIDENT_KIND
                ],
                [incident_id],
            )

            for attempt in (1, 2):
                resumed._record_verify_result(
                    resumed_task,
                    attempt,
                    "fail",
                    str(verify_result["reason"]),
                    verify_result["failure_ids"],
                    comparable_failures=False,
                )
            recovery_result = {
                **verify_result,
                "review": str(verify_result["reason"]),
            }
            self.assertTrue(
                resumed._schedule_repair_tasks_for_failure(
                    marked,
                    marked.tasks,
                    resumed_task,
                    recovery_result,
                )
            )
            self.assertEqual(
                marked.last_recovery_route["outcome"],
                "repair_tasks_scheduled",
            )
            repair_tasks = [
                candidate
                for candidate in marked.tasks
                if candidate.parent_task_id == resumed_task.task_id
            ]
            self.assertEqual(len(repair_tasks), 1)
            self.assertEqual(repair_tasks[0].task_origin, "evidence_repair")
            self.assertEqual(marked.active_blocker, {})
            persisted_task = next(
                candidate
                for candidate in load_task_plan(root)["tasks"]
                if candidate["task_id"] == resumed_task.task_id
            )
            self.assertEqual(
                (
                    persisted_task["verify_baseline_ref"].encode("utf-8"),
                    persisted_task["verify_baseline_source_ref"].encode("utf-8"),
                ),
                baseline_identity,
            )
            self.assertEqual(head_ref(root), current_ref)
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "show",
                        f"{baseline_ref}:{relative_test.as_posix()}",
                    ],
                    cwd=root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout,
                baseline_source,
            )

    def test_reparsed_baseline_resume_rejects_non_identity_error_prose(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.status = "blocked"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                "status": "blocked",
            }
            incident = ExecutionIncident(
                incident_id="baseline-prose",
                run_id=state.run_id,
                source="gate",
                kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                stage="implement",
                context="baseline verification",
                command="python -m pytest -q tests",
                baseline=True,
                returncode=1,
                stdout_tail="ERROR fixture service did not become ready",
                status="self_repair",
            )
            ExecutionIncidentStore(root, state.run_id).save(incident, state)

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value="engine-with-repaired-parser",
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertEqual(state.status, "blocked")
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                incident.incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.status, "self_repair")

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

    def test_self_repair_resume_backfills_v2_stage_recovery_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            spec_file = root / "spec.md"
            spec_file.write_text("Synthetic recovery specification.\n", encoding="utf-8")
            save_task_plan(
                root,
                {
                    "verification_policy_version": 2,
                    "test_strategy": "shell",
                    "verification_commands": ["echo recovery-proof"],
                    "tasks": [
                        {
                            "task_id": "persisted-recovery",
                            "title": "Recover final verification",
                            "description": "Repair a synthetic verification failure.",
                            "acceptance": ["The configured proof command passes."],
                            "status": "in_progress",
                            "commit_message": "",
                            "task_origin": "stage_recovery",
                            "verification_refs": [],
                        }
                    ],
                },
            )
            orchestrator = Orchestrator(root)
            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.pending_approval = "release"
            state.tasks = orchestrator._load_tasks_from_plan()
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "synthetic_recovery_invariant",
                "reason": "the persisted recovery task is missing proof refs",
                "status": "blocked",
            }
            save_run_state(root, state)

            orchestrator.mark_self_repair_applied("repair123")
            resumed = Orchestrator(root)
            result = resumed.run(spec_file=spec_file, auto_approve=False)

            self.assertEqual(result.status, "paused")
            self.assertEqual(result.pending_approval, "release")
            self.assertEqual(
                load_task_plan(root)["tasks"][0]["verification_refs"],
                ["cmd:echo recovery-proof"],
            )
            self.assertEqual(
                result.tasks[0].verification_refs,
                ["cmd:echo recovery-proof"],
            )
            self.assertTrue(resumed.validate()["ok"])

    def test_self_repair_resume_repairs_dependencies_left_by_task_pruning(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            completed = TaskSpec(
                task_id="completed-proof",
                title="Completed proof",
                description="Already verified behavior.",
                acceptance=["The behavior remains verified."],
                status="done",
            )
            pending = TaskSpec(
                task_id="release-gate",
                title="Release gate",
                description="Verify the remaining release contract.",
                acceptance=["The release contract passes."],
                depends_on=["pruned-duplicate", completed.task_id],
            )
            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.tasks = [completed, pending]
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "dangling_dependencies_after_task_pruning",
                "status": "blocked",
            }
            orchestrator._persist_tasks(state.tasks)
            save_run_state(root, state)

            marked = orchestrator.mark_self_repair_applied("repair123")
            self.assertTrue(orchestrator._resume_blocked_run(marked))

            self.assertEqual(marked.tasks[1].depends_on, [completed.task_id])
            self.assertEqual(
                marked.active_blocker["repaired_dependency_references"],
                [
                    {
                        "task_id": pending.task_id,
                        "removed_task_ids": ["pruned-duplicate"],
                    }
                ],
            )
            persisted_tasks = load_task_plan(root)["tasks"]
            self.assertEqual(
                persisted_tasks[1]["depends_on"],
                [completed.task_id],
            )
            self.assertEqual(validate_task_dependencies(persisted_tasks), [])

    def test_self_repair_resume_removes_tracked_dependency_self_link(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            commit_all(root, "test: initialize project")
            dependency = root / ".conda"
            dependency.symlink_to(dependency)
            leak_commit = commit_all(root, "test: leak dependency link")

            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "worktree_lifecycle",
                "status": "blocked",
            }
            save_run_state(root, state)
            orchestrator = Orchestrator(root)
            marked = orchestrator.mark_self_repair_applied("repair123")

            self.assertTrue(orchestrator._resume_blocked_run(marked))

            cleanup_commit = head_ref(root)
            self.assertNotEqual(cleanup_commit, leak_commit)
            self.assertEqual(
                commit_changed_paths(root, cleanup_commit),
                [".conda"],
            )
            self.assertFalse(dependency.is_symlink())
            dependency_entry = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "--name-only",
                    "HEAD",
                    "--",
                    ".conda",
                ],
                cwd=str(root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(dependency_entry.stdout.strip(), "")
            self.assertEqual(
                marked.active_blocker["repaired_dependency_links"],
                [".conda"],
            )

    def test_self_repair_resume_requeues_blocked_task_with_fresh_verify_lifecycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            orchestrator = Orchestrator(root)
            failure_id = "tests/test_contract.py::test_public_contract"
            task = TaskSpec(
                task_id="contract-task",
                title="Repair public contract",
                description="Repair the observable contract.",
                acceptance=["The public contract passes."],
                status="blocked",
                recovery_round=2,
                verify_history=[
                    {
                        "attempt": 2,
                        "decision": "fail",
                        "summary": "verification failed before engine self-repair",
                        "failure_ids": [failure_id],
                        "comparable_failures": True,
                        "recovery_epoch": 0,
                        "recovery_round": 2,
                        "verify_retry_epoch": 0,
                    }
                ],
            )
            state = load_run_state(root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.tasks = [task]
            state.resume_context["implementation_ready_tasks"] = {
                task.task_id: True,
            }
            state.last_recovery_route = {
                "task_id": task.task_id,
                "lineage_id": task.task_id,
                "outcome": "exhausted",
                "round": 3,
            }
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "orchestrator_transition",
                "status": "blocked",
            }
            orchestrator._persist_tasks(state.tasks)
            save_run_state(root, state)

            marked = orchestrator.mark_self_repair_applied("abc123")
            resumed = Orchestrator(root)
            changed = resumed._resume_blocked_run(marked)
            resumed_task = marked.tasks[0]

            self.assertTrue(changed)
            self.assertEqual(marked.status, "pending")
            self.assertEqual(resumed_task.status, "pending")
            self.assertEqual(resumed_task.recovery_round, 2)
            self.assertEqual(resumed_task.verify_retry_epoch, 1)
            self.assertEqual(len(resumed_task.verify_history), 1)
            self.assertNotIn(
                task.task_id,
                marked.resume_context.get("implementation_ready_tasks", {}),
            )
            self.assertEqual(
                marked.resume_context["parallel_sequential_retry_tasks"],
                [task.task_id],
            )
            self.assertEqual(
                marked.active_blocker["prepared_self_repair_commit"],
                "abc123",
            )
            self.assertEqual(
                marked.last_recovery_route["outcome"],
                "self_repair_requeued",
            )
            persisted_task = load_task_plan(root)["tasks"][0]
            self.assertEqual(persisted_task["status"], "pending")
            self.assertEqual(persisted_task["verify_retry_epoch"], 1)
            self.assertFalse(resumed._resume_blocked_run(marked))
            self.assertEqual(resumed_task.verify_retry_epoch, 1)

            first_after_requeue = resumed._analyze_verify_failure(
                resumed_task,
                [failure_id],
            )
            self.assertFalse(first_after_requeue["stop_retry"])
            resumed._record_verify_result(
                resumed_task,
                1,
                "fail",
                "verification still fails after self-repair",
                [failure_id],
            )
            repeated_in_fresh_lifecycle = resumed._analyze_verify_failure(
                resumed_task,
                [failure_id],
            )
            self.assertTrue(repeated_in_fresh_lifecycle["stop_retry"])
            self.assertEqual(
                resumed_task.verify_history[-1]["verify_retry_epoch"],
                1,
            )

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
