from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_agents.config import (
    bootstrap_project,
    load_project_config,
    load_run_state,
    save_project_config,
    save_run_state,
)
from auto_agents.health_watch import (
    HealthSnapshot,
    HealthActionRequest,
    HealthAnomaly,
    HealthSelfRepairRequired,
    ProgressVector,
    RunHealthEvaluator,
    RunHealthSupervisor,
    advance_run_health_control,
    build_progress_vector,
    replay_health_events,
)
from auto_agents.models import HealthWatchConfig, SmartTimeoutConfig, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.health_watchdog import (
    IndependentHealthAuditor,
    run_health_sidecar,
    start_health_sidecar,
)
from auto_agents.health_control import (
    HealthActionRecord,
    HealthActionStore,
    HealthControlChannel,
    action_store_path,
    control_path,
    evidence_digest,
    load_active_manifest,
    request_health_state,
    subject_health_root,
)
from auto_agents.repair_cases import RepairCase, RepairCaseStore
from auto_agents.process_supervision import process_start_ticks
from auto_agents.repair_checkpoint import (
    create_repair_checkpoint,
    restore_repair_control_checkpoint,
)
from auto_agents.validation import validate_project_config_payload
from auto_agents.self_repair import SelfRepairDecision, SelfRepairTriageResult


def _vector(*atoms: str) -> ProgressVector:
    return ProgressVector(
        durable_atoms=tuple(sorted(atoms)),
        unresolved_roots=(),
        root_occurrences=(),
        completed_stages=(),
        done_lineages=(),
        verified_proofs=(),
    )


def _snapshot(
    sequence: int,
    observed: float,
    *,
    atoms=(),
    activity="a",
    active_tools=0,
    stage="implement",
    task_id="task-1",
    retry_pressure=0,
    root_occurrences=(),
    control_history=(),
    rewind_epoch=0,
) -> HealthSnapshot:
    progress = _vector(*atoms)
    progress = ProgressVector(
        durable_atoms=progress.durable_atoms,
        unresolved_roots=tuple(item[0] for item in root_occurrences),
        root_occurrences=tuple(root_occurrences),
        completed_stages=progress.completed_stages,
        done_lineages=progress.done_lineages,
        verified_proofs=progress.verified_proofs,
    )
    return HealthSnapshot(
        sequence=sequence,
        observed_at=str(observed),
        observed_epoch=observed,
        run_id="run-1",
        run_status="pending",
        stage=stage,
        task_id=task_id,
        progress=progress,
        activity_digest=activity,
        head_ref="head",
        worktree_fingerprint=activity,
        active_tool_count=active_tools,
        active_tool="tool" if active_tools else "",
        retry_pressure=retry_pressure,
        control_fingerprint="",
        control_history=tuple(control_history),
        rewind_epoch=rewind_epoch,
    )


class HealthWatchTests(unittest.TestCase):
    def test_default_config_enables_health_watch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            config = load_project_config(project)
            self.assertTrue(config.execution.health_watch.enabled)
            self.assertTrue(config.execution.health_watch.sidecar_enabled)
            self.assertEqual(config.execution.health_watch.poll_seconds, 30)
            serialized = json.loads(
                (project / ".auto-agents" / "config.json").read_text(encoding="utf-8")
            )["execution"]["health_watch"]
            self.assertNotIn("sidecar_enabled", serialized)
            self.assertNotIn("sidecar_grace_seconds", serialized)
            self.assertNotIn("max_sidecar_restarts_per_run", serialized)

    def test_health_config_validation_rejects_short_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            payload = json.loads(
                (project / ".auto-agents" / "config.json").read_text()
            )
            payload["execution"]["health_watch"]["poll_seconds"] = 30
            payload["execution"]["health_watch"]["heartbeat_timeout_seconds"] = 60
            errors = validate_project_config_payload(payload)
            self.assertTrue(
                any("heartbeat_timeout_seconds" in error for error in errors)
            )

    def test_legacy_sidecar_config_is_ignored_warned_and_not_serialized(self) -> None:
        with self.assertWarns(FutureWarning):
            config = HealthWatchConfig.from_dict(
                {
                    "enabled": True,
                    "sidecar_enabled": False,
                    "sidecar_grace_seconds": 999,
                    "max_sidecar_restarts_per_run": 99,
                }
            )
        payload = config.to_dict()
        self.assertTrue(payload["enabled"])
        self.assertNotIn("sidecar_enabled", payload)
        self.assertNotIn("sidecar_grace_seconds", payload)
        self.assertNotIn("max_sidecar_restarts_per_run", payload)

    def test_progress_vector_counts_only_durable_task_and_proof_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            state.stage_summaries["clarify"] = "done"
            state.tasks = [
                TaskSpec(
                    task_id="task-1",
                    title="one",
                    description="",
                    acceptance=[],
                    status="done",
                    commit_sha="abc123",
                    requirement_proofs=[
                        {
                            "requirement_id": "REQ-1",
                            "oracle_index": 1,
                            "status": "verified",
                            "requirement_contract_sha256": "contract",
                            "evidence_refs": ["test:a"],
                        }
                    ],
                ),
                TaskSpec(
                    task_id="task-2",
                    title="two",
                    description="",
                    acceptance=[],
                    status="in_progress",
                ),
            ]
            vector = build_progress_vector(state)
            self.assertIn("clarify", vector.completed_stages)
            self.assertEqual(vector.done_lineages, ("task-1",))
            self.assertEqual(len(vector.verified_proofs), 1)
            self.assertFalse(any("task-2" in atom for atom in vector.durable_atoms))

    def test_activity_without_goal_progress_triggers_goal_stall(self) -> None:
        evaluator = RunHealthEvaluator(
            HealthWatchConfig(goal_stall_lease_multiplier=2.0)
        )
        self.assertIsNone(
            evaluator.evaluate(_snapshot(1, 0, activity="a"), progress_lease_seconds=60)
        )
        self.assertIsNone(
            evaluator.evaluate(_snapshot(2, 60, activity="b"), progress_lease_seconds=60)
        )
        anomaly = evaluator.evaluate(
            _snapshot(3, 121, activity="c"), progress_lease_seconds=60
        )
        self.assertIsNotNone(anomaly)
        self.assertEqual(anomaly.kind, "goal_stalled")
        self.assertEqual(anomaly.failure_scope, "task_lineage")

    def test_active_tool_defers_goal_stall(self) -> None:
        evaluator = RunHealthEvaluator(HealthWatchConfig())
        evaluator.evaluate(_snapshot(1, 0), progress_lease_seconds=60)
        anomaly = evaluator.evaluate(
            _snapshot(2, 1000, activity="b", active_tools=1),
            progress_lease_seconds=60,
        )
        self.assertIsNone(anomaly)

    def test_removed_progress_requires_declared_rewind(self) -> None:
        evaluator = RunHealthEvaluator(HealthWatchConfig())
        evaluator.evaluate(_snapshot(1, 0, atoms=("task:a",)), progress_lease_seconds=60)
        anomaly = evaluator.evaluate(_snapshot(2, 1), progress_lease_seconds=60)
        self.assertEqual(anomaly.kind, "regressing")

        evaluator = RunHealthEvaluator(HealthWatchConfig())
        evaluator.evaluate(_snapshot(1, 0, atoms=("task:a",)), progress_lease_seconds=60)
        evaluator.record_control("rewind|implement", rewind=True)
        self.assertIsNone(
            evaluator.evaluate(_snapshot(2, 1), progress_lease_seconds=60)
        )

    def test_independent_auditor_honors_durable_stage_rewind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
            }
            state.current_stage = "plan"
            save_run_state(project, state)
            manifest = {
                "run_token": "token",
                "workflow_kind": "run",
                "subject_id": state.run_id,
                "process_phase": "run",
            }
            auditor = IndependentHealthAuditor(project, manifest)
            auditor.observe_once(manifest)

            rewound = load_run_state(project)
            Orchestrator(project)._rewind_state_from_stage(rewound, "clarify")
            save_run_state(project, rewound)
            auditor.observe_once(manifest)

            self.assertEqual(rewound.health_control["rewind_epoch"], 1)
            self.assertEqual(auditor.evaluator.rewind_epoch, 1)
            self.assertIsNone(
                auditor.actions.next_pending(run_token="token")
            )

    def test_health_intervention_completion_starts_fresh_goal_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            manifest = {
                "run_token": "token",
                "workflow_kind": "run",
                "subject_id": state.run_id,
                "process_phase": "run",
            }
            auditor = IndependentHealthAuditor(project, manifest)
            auditor.observe_once(manifest)

            active = load_run_state(project)
            active.active_repair_case_id = "case-1"
            active.repair_phase = "diagnosing"
            advance_run_health_control(
                active,
                kind="health_intervention_started:test",
                intervention_active=True,
            )
            save_run_state(project, active)
            auditor.evaluator.last_progress_at = time.time() - 10_000
            auditor.evaluator.activity_since_progress = True
            auditor.observe_once(manifest)
            self.assertIsNone(
                auditor.actions.next_pending(run_token="token")
            )

            resumed = load_run_state(project)
            resumed.active_repair_case_id = ""
            resumed.repair_phase = ""
            advance_run_health_control(
                resumed,
                kind="health_intervention_resumed:test",
                intervention_active=False,
                resume=True,
            )
            save_run_state(project, resumed)
            auditor.evaluator.last_progress_at = time.time() - 10_000
            auditor.evaluator.activity_since_progress = True
            auditor.observe_once(manifest)

            self.assertEqual(auditor.last_resume_epoch, 1)
            self.assertGreater(auditor.evaluator.last_progress_at, time.time() - 2)
            self.assertIsNone(
                auditor.actions.next_pending(run_token="token")
            )

    def test_control_cycle_is_detected(self) -> None:
        evaluator = RunHealthEvaluator(
            HealthWatchConfig(oscillation_repeat_limit=3)
        )
        evaluator.evaluate(_snapshot(1, 0), progress_lease_seconds=600)
        for value in ("a", "b", "a", "b", "a", "b"):
            evaluator.record_control(value)
        anomaly = evaluator.evaluate(
            _snapshot(2, 1, activity="b"), progress_lease_seconds=600
        )
        self.assertEqual(anomaly.kind, "oscillating")

    def test_retry_pressure_without_progress_is_resource_degraded(self) -> None:
        evaluator = RunHealthEvaluator(HealthWatchConfig())
        evaluator.evaluate(_snapshot(1, 0), progress_lease_seconds=600)
        evaluator.evaluate(
            _snapshot(2, 1, activity="b", retry_pressure=1),
            progress_lease_seconds=600,
        )
        anomaly = evaluator.evaluate(
            _snapshot(3, 2, activity="c", retry_pressure=3),
            progress_lease_seconds=600,
        )
        self.assertEqual(anomaly.kind, "resource_degraded")

    def test_recovery_occurrences_without_progress_are_churn(self) -> None:
        evaluator = RunHealthEvaluator(
            HealthWatchConfig(recovery_churn_limit=3)
        )
        evaluator.evaluate(
            _snapshot(1, 0, root_occurrences=(("root", 2),)),
            progress_lease_seconds=600,
        )
        anomaly = evaluator.evaluate(
            _snapshot(
                2,
                1,
                activity="b",
                root_occurrences=(("root", 3),),
            ),
            progress_lease_seconds=600,
        )
        self.assertEqual(anomaly.kind, "recovery_churn")

    def test_repair_case_round_trip_and_health_replay(self) -> None:
        first = _snapshot(1, 0, activity="a").to_dict()
        second = _snapshot(2, 200, activity="b").to_dict()
        anomalies = replay_health_events(
            [first, second],
            config=HealthWatchConfig(goal_stall_lease_multiplier=2.0),
            progress_lease_seconds=60,
        )
        self.assertEqual([item.kind for item in anomalies], ["goal_stalled"])
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            case = RepairCase(
                case_id="case-1",
                run_id=state.run_id,
                source="health_watch",
                kind="goal_stalled",
                severity="confirmed",
                symptom="busy without progress",
                progress_history=[first, second],
                expected_postconditions=["goal progress resumes"],
            )
            store = RepairCaseStore(project, state.run_id)
            store.save(case)
            restored = store.load(case.case_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.kind, "goal_stalled")

    def test_serialized_control_history_replays_oscillation(self) -> None:
        first = _snapshot(1, 0).to_dict()
        second = _snapshot(
            2,
            1,
            activity="b",
            control_history=("a", "b", "a", "b", "a", "b"),
        ).to_dict()
        anomalies = replay_health_events(
            [first, second],
            config=HealthWatchConfig(oscillation_repeat_limit=3),
            progress_lease_seconds=600,
        )
        self.assertEqual([item.kind for item in anomalies], ["oscillating"])

    def test_control_channel_applies_dynamic_stop_without_stopping_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            events = []
            channel = HealthControlChannel(
                project,
                workflow_kind="run",
                run_token="token",
                enabled=True,
                on_enable=lambda payload: events.append("enabled"),
                on_disable=lambda payload: events.append("disabled"),
            )
            channel.start(state.run_id)
            result = request_health_state(project, enabled=False, timeout_seconds=3)
            self.assertEqual(result["applied_state"], "disabled")
            self.assertEqual(events, ["disabled"])
            self.assertTrue(Path(f"/proc/{os.getpid()}").exists())
            channel.close(reason="test complete")

    def test_control_channel_can_enable_a_command_started_without_health_watch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            events = []
            channel = HealthControlChannel(
                project,
                workflow_kind="run",
                run_token="token",
                enabled=False,
                on_enable=lambda payload: events.append("enabled"),
                on_disable=lambda payload: events.append("disabled"),
            )
            channel.start(state.run_id)
            result = request_health_state(project, enabled=True, timeout_seconds=3)
            self.assertEqual(result["applied_state"], "enabled")
            self.assertEqual(events, ["enabled"])
            channel.close(reason="test complete")

    def test_late_health_tick_cannot_overwrite_terminal_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            supervisor = RunHealthSupervisor(
                project,
                state.run_id,
                config=HealthWatchConfig(),
                smart_timeout=SmartTimeoutConfig(),
                autonomy_mode="max",
            )
            supervisor._terminal_status = "blocked"
            supervisor._terminal_reason = "run entered root-cause diagnosis"

            supervisor._write_heartbeat(status="healthy")

            heartbeat = json.loads(
                (supervisor.root / "heartbeat.json").read_text(encoding="utf-8")
            )
            self.assertEqual(heartbeat["status"], "blocked")
            self.assertEqual(
                heartbeat["reason"],
                "run entered root-cause diagnosis",
            )

    def test_sidecar_records_dead_owner_and_exits_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            control = control_path(project)
            control.parent.mkdir(parents=True, exist_ok=True)
            control.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project": str(project.resolve()),
                        "workflow_kind": "run",
                        "subject_id": state.run_id,
                        "run_token": "token",
                        "owner_pid": 999999,
                        "owner_start_ticks": 1,
                        "process_phase": "run",
                        "desired_state": "enabled",
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "auto_agents.health_watchdog.UNEXPECTED_EXIT_GRACE_SECONDS", 0
            ):
                self.assertEqual(run_health_sidecar(project), 0)
            health_root = subject_health_root(project, "run", state.run_id)
            diagnostic = json.loads(
                (health_root / "unexpected-owner-exit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["kind"], "unexpected_owner_exit")
            actions = json.loads(
                action_store_path(project, "run", state.run_id).read_text(encoding="utf-8")
            )["requests"]
            self.assertEqual(actions[0]["action"], "pending_manual_resume")
            self.assertNotIn("restart_command", diagnostic)

    def test_sidecar_follows_terminal_owner_without_signaling_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            control = control_path(project)
            control.parent.mkdir(parents=True, exist_ok=True)
            control.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project": str(project.resolve()),
                        "workflow_kind": "run",
                        "subject_id": state.run_id,
                        "run_token": "token",
                        "owner_pid": os.getpid(),
                        "owner_start_ticks": process_start_ticks(os.getpid()),
                        "process_phase": "run",
                        "desired_state": "enabled",
                    }
                ),
                encoding="utf-8",
            )
            thread = threading.Thread(
                target=run_health_sidecar,
                args=(project,),
            )
            thread.start()
            time.sleep(0.2)
            terminal = json.loads(control.read_text(encoding="utf-8"))
            terminal["process_phase"] = "terminal"
            control.write_text(json.dumps(terminal), encoding="utf-8")
            thread.join(timeout=3)

            self.assertFalse(thread.is_alive())
            self.assertTrue(Path(f"/proc/{os.getpid()}").exists())

    def test_sidecar_launch_requires_and_uses_active_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            channel = HealthControlChannel(
                project,
                workflow_kind="run",
                run_token="new-token",
                enabled=True,
                on_enable=lambda payload: None,
                on_disable=lambda payload: None,
            )
            channel.start(state.run_id)

            fake_process = type("FakeProcess", (), {"pid": 424242})()
            with patch(
                "auto_agents.health_watchdog.subprocess.Popen",
                return_value=fake_process,
            ):
                started = start_health_sidecar(
                    project_root=project,
                    run_token="new-token",
                    auto_agents_entry=Path(__file__),
                )

            self.assertIs(started, fake_process)
            manifest = load_active_manifest(project)
            self.assertEqual(manifest["sidecar_pid"], 424242)
            self.assertEqual(manifest["run_token"], "new-token")
            channel.close(reason="test complete")

    def test_repair_checkpoint_preserves_binary_untracked_and_deleted_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=project,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=project,
                check=True,
            )
            (project / "tracked.txt").write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            (project / "tracked.txt").unlink()
            binary = b"\x00\xffhealth\x00"
            (project / "new.bin").write_bytes(binary)
            os.symlink("new.bin", project / "new-link")
            manifest_path = create_repair_checkpoint(
                project, state.run_id, "case-checkpoint"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = {item["path"]: item for item in manifest["entries"]}
            self.assertEqual(entries["tracked.txt"]["kind"], "deleted")
            self.assertEqual(entries["new.bin"]["kind"], "file")
            self.assertEqual(entries["new-link"]["kind"], "symlink")
            blob = manifest_path.parent / "blobs" / entries["new.bin"]["sha256"]
            self.assertEqual(blob.read_bytes(), binary)
            original_state = (
                project / ".auto-agents" / "state" / "run_state.json"
            ).read_bytes()
            state.status = "failed"
            save_run_state(project, state)
            restore_repair_control_checkpoint(project, manifest_path)
            self.assertEqual(
                (project / ".auto-agents" / "state" / "run_state.json").read_bytes(),
                original_state,
            )

    def test_orchestrator_health_case_uses_common_triage_and_requests_self_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            Orchestrator.init_project(project, "demo", "mock")
            state = load_run_state(project)
            case = RepairCase(
                case_id="health-engine-case",
                run_id=state.run_id,
                source="health_watch",
                kind="oscillating",
                severity="confirmed",
                symptom="recovery route oscillates",
                expected_postconditions=["route advances"],
            )
            RepairCaseStore(project, state.run_id).save(case)
            anomaly = HealthAnomaly(
                kind="oscillating",
                severity="confirmed",
                stage="implement",
                root_fingerprint=case.root_fingerprint,
                reason=case.symptom,
                expected_postconditions=("route advances",),
            )
            request = HealthActionRequest(
                action="diagnose",
                anomaly=anomaly,
                repair_case_id=case.case_id,
            )
            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(
                    True,
                    category="health_route_invariant",
                    reason="engine invariant",
                ),
                source="root_cause_consensus",
                reason="approved",
            )
            orchestrator = Orchestrator(project)
            with patch(
                "auto_agents.self_repair.adjudicate_repair_case",
                return_value=triage,
            ):
                with self.assertRaises(HealthSelfRepairRequired) as raised:
                    orchestrator._handle_health_action(request)
            self.assertEqual(raised.exception.repair_case.case_id, case.case_id)
            updated = load_run_state(project)
            self.assertEqual(updated.active_repair_case_id, case.case_id)
            self.assertEqual(updated.repair_phase, "quiescing")

    def test_non_engine_health_case_is_routed_without_blocking_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            Orchestrator.init_project(project, "demo", "mock")
            state = load_run_state(project)
            state.tasks = [
                TaskSpec(
                    task_id="task-1",
                    title="one",
                    description="",
                    acceptance=[],
                    status="in_progress",
                ),
                TaskSpec(
                    task_id="task-2",
                    title="two",
                    description="",
                    acceptance=[],
                    status="pending",
                ),
            ]
            save_run_state(project, state)
            case = RepairCase(
                case_id="health-target-case",
                run_id=state.run_id,
                source="health_watch",
                kind="goal_stalled",
                severity="confirmed",
                symptom="task stopped making progress",
                task_id="task-1",
                failure_scope="task_lineage",
            )
            RepairCaseStore(project, state.run_id).save(case)
            request = HealthActionRequest(
                action="diagnose",
                anomaly=HealthAnomaly(
                    kind="goal_stalled",
                    severity="confirmed",
                    stage="implement",
                    task_id="task-1",
                    failure_scope="task_lineage",
                    root_fingerprint=case.root_fingerprint,
                    reason=case.symptom,
                    expected_postconditions=("task progress resumes",),
                ),
                repair_case_id=case.case_id,
            )
            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(False, reason="target task issue"),
                source="root_cause_consensus",
                reason="target recovery",
            )
            orchestrator = Orchestrator(project)
            with patch(
                "auto_agents.self_repair.adjudicate_repair_case",
                return_value=triage,
            ):
                orchestrator._handle_health_action(request)
            updated = load_run_state(project)
            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.tasks[0].status, "in_progress")
            self.assertEqual(len(updated.localized_blockers), 0)
            stored = RepairCaseStore(project, state.run_id).load(case.case_id)
            self.assertEqual(stored.status, "routed")

    def test_health_boundary_only_verifies_checkpoint_without_agent_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            Orchestrator.init_project(project, "demo", "mock")
            state = load_run_state(project)
            progress = build_progress_vector(state)
            case = RepairCase(
                case_id="boundary-case",
                run_id=state.run_id,
                source="health_watch",
                kind="goal_stalled",
                severity="confirmed",
                symptom="busy without progress",
                progress_before=progress.to_dict(),
                expected_postconditions=["durable progress resumes"],
                status="resuming",
            )
            checkpoint = create_repair_checkpoint(
                project, state.run_id, case.case_id
            )
            case.resume_checkpoint_ref = str(checkpoint)
            RepairCaseStore(project, state.run_id).save(case)
            state.active_repair_case_id = case.case_id
            state.repair_phase = "resuming"
            state.repair_checkpoint_ref = str(checkpoint)
            save_run_state(project, state)

            receipt = Orchestrator(project).verify_health_repair_boundary(
                case.case_id
            )

            self.assertEqual(receipt["case_id"], case.case_id)
            updated = RepairCaseStore(project, state.run_id).load(case.case_id)
            self.assertEqual(updated.status, "boundary_verified")
            self.assertEqual(load_run_state(project).repair_phase, "boundary_verified")

    def test_in_process_anomaly_is_advisory_and_emits_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            supervisor = RunHealthSupervisor(
                project,
                state.run_id,
                config=HealthWatchConfig(max_interventions_per_root=1),
                smart_timeout=SmartTimeoutConfig(),
                autonomy_mode="max",
            )
            anomaly = HealthAnomaly(
                kind="goal_stalled",
                severity="confirmed",
                stage="implement",
                root_fingerprint="same-root",
                reason="busy without progress",
                expected_postconditions=("progress resumes",),
            )
            snapshot = _snapshot(1, 1000)
            supervisor._recent_snapshots.append(snapshot.to_dict())
            supervisor._handle_anomaly(anomaly, snapshot)
            self.assertIsNone(supervisor.pop_action())
            self.assertIsNone(RepairCaseStore(project, state.run_id).latest_open())

    def test_foreground_materializes_only_token_bound_sidecar_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            store = HealthActionStore(project, "run", state.run_id)
            evidence = {
                "anomaly": {
                    "kind": "goal_stalled",
                    "severity": "confirmed",
                    "stage": "implement",
                    "root_fingerprint": "root-1",
                    "reason": "no durable progress",
                    "expected_postconditions": ["progress resumes"],
                }
            }
            store.append(
                HealthActionRecord(
                    request_id="request-1",
                    action="diagnose",
                    reason="health_anomaly:goal_stalled",
                    source="health_sidecar",
                    run_token="token",
                    subject_id=state.run_id,
                    evidence_digest=evidence_digest(evidence),
                    evidence=evidence,
                )
            )
            wrong = RunHealthSupervisor(
                project,
                state.run_id,
                config=HealthWatchConfig(),
                smart_timeout=SmartTimeoutConfig(),
                autonomy_mode="max",
                run_token="other-token",
            )
            self.assertIsNone(wrong.pop_action())
            supervisor = RunHealthSupervisor(
                project,
                state.run_id,
                config=HealthWatchConfig(),
                smart_timeout=SmartTimeoutConfig(),
                autonomy_mode="max",
                run_token="token",
            )
            request = supervisor.pop_action()
            self.assertEqual(request.durable_request_id, "request-1")
            case = RepairCaseStore(project, state.run_id).load(request.repair_case_id)
            self.assertEqual(case.root_fingerprint, "root-1")

    def test_foreground_rejects_tampered_sidecar_evidence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            store = HealthActionStore(project, "run", state.run_id)
            store.append(
                HealthActionRecord(
                    request_id="tampered",
                    action="diagnose",
                    reason="health_observer_disagreement",
                    source="health_sidecar",
                    run_token="token",
                    subject_id=state.run_id,
                    evidence_digest="not-the-evidence-digest",
                    evidence={"independent_progress_digest": "actual"},
                )
            )
            supervisor = RunHealthSupervisor(
                project,
                state.run_id,
                config=HealthWatchConfig(),
                smart_timeout=SmartTimeoutConfig(),
                autonomy_mode="max",
                run_token="token",
            )
            self.assertIsNone(supervisor.pop_action())
            requests = json.loads(store.path.read_text(encoding="utf-8"))["requests"]
            self.assertEqual(requests[0]["state"], "rejected")
            self.assertIsNone(RepairCaseStore(project, state.run_id).latest_open())

    def test_exhausted_task_health_budget_localizes_and_continues_independent_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            Orchestrator.init_project(project, "demo", "mock")
            state = load_run_state(project)
            state.tasks = [
                TaskSpec(
                    task_id="task-1",
                    title="one",
                    description="",
                    acceptance=[],
                    status="in_progress",
                ),
                TaskSpec(
                    task_id="task-2",
                    title="two",
                    description="",
                    acceptance=[],
                    status="pending",
                ),
            ]
            save_run_state(project, state)
            case = RepairCase(
                case_id="exhausted-case",
                run_id=state.run_id,
                source="health_watch",
                kind="goal_stalled",
                severity="confirmed",
                status="needs_human",
                symptom="busy without progress",
                task_id="task-1",
                failure_scope="task_lineage",
            )
            RepairCaseStore(project, state.run_id).save(case)
            request = HealthActionRequest(
                action="exhausted",
                anomaly=HealthAnomaly(
                    kind=case.kind,
                    severity=case.severity,
                    stage="implement",
                    task_id=case.task_id,
                    failure_scope=case.failure_scope,
                    root_fingerprint=case.root_fingerprint,
                    reason=case.symptom,
                    expected_postconditions=("progress resumes",),
                ),
                repair_case_id=case.case_id,
            )

            Orchestrator(project)._handle_health_action(request)

            updated = load_run_state(project)
            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.tasks[0].status, "blocked")
            self.assertEqual(updated.tasks[1].status, "pending")
            self.assertEqual(updated.repair_phase, "")
            self.assertEqual(len(updated.localized_blockers), 1)
            self.assertFalse(updated.health_control["intervention_active"])
            self.assertEqual(updated.health_control["resume_epoch"], 1)


if __name__ == "__main__":
    unittest.main()
