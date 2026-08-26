import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    archived_run_state_path,
    archived_task_plan_path,
    gate_baseline_cache_path,
    load_project_config,
    load_run_state,
    load_task_plan,
    provider_references_lock_path,
    requirements_trace_path,
    save_project_config,
    save_run_state,
    task_plan_path,
)
from auto_agents.gates import FailureExtraction
from auto_agents.git_ops import changed_files, changed_paths, commit_all, hard_reset_clean, head_ref, worktree_fingerprint
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import (
    AgentResult,
    CommandResult,
    GateParallelGroup,
    GateResult,
    RunState,
    TaskSpec,
    VerificationStep,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.provider_contract import (
    PROVIDER_REFERENCE_CONTRACT_VERSION,
    PROVIDER_REFERENCE_V2_HEADINGS,
)
from auto_agents.requirements import requirement_contract_sha256
from auto_agents.validation import validation_report


def _strict_requirement() -> dict:
    return {
        "id": "REQ-001",
        "text": "Keep the public contract verified.",
        "source": "test scope",
        "status": "active",
        "priority": "mandatory",
        "acceptance_oracles": ["The public contract passes."],
        "oracle_type": "integration_test",
        "oracle_strength": "behavioral",
        "evidence_boundary": "system_boundary",
        "forbidden_proxy_oracles": [],
        "forbidden_patterns": [],
        "external_docs_required": False,
        "provider_reference": "",
        "notes": "",
    }


def _strict_requirement_proof(
    requirement: dict,
    evidence_ref: str,
    *,
    status: str,
) -> dict:
    return {
        "requirement_id": str(requirement["id"]),
        "oracle_index": 1,
        "acceptance_oracle": str(requirement["acceptance_oracles"][0]),
        "requirement_contract_sha256": requirement_contract_sha256(requirement),
        "proof_type": "integration_test",
        "oracle_strength": "behavioral",
        "evidence_boundary": "system_boundary",
        "evidence_refs": [evidence_ref],
        "forbidden_proxy_oracles": [],
        "proxy_oracles": [],
        "status": status,
    }


class RetryingPlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0

    def run(self, request):
        if request.stage == "plan":
            self.plan_calls += 1
            if self.plan_calls == 1:
                write_json(task_plan_path(self.project_root), {"tasks": [{"task_id": "bad id"}]})
                write_text(request.output_path, "invalid plan\n")
            else:
                write_json(
                    task_plan_path(self.project_root),
                    {
                        "test_strategy": "python-pytest",
                        "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                        "tasks": [
                            {
                                "task_id": "task-001",
                                "title": "Add CLI entrypoint",
                                "description": "Add a runnable command line entrypoint.",
                                "acceptance": ["`python -m demo --help` exits successfully."],
                                "status": "pending",
                                "commit_message": "",
                            }
                        ]
                    },
                )
                write_text(request.output_path, "valid plan\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class VerificationPlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "plan":
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Add CLI entrypoint",
                            "description": "Add a runnable command line entrypoint.",
                            "acceptance": ["`python -m demo --help` exits successfully."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            write_text(request.output_path, "valid verification plan\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class VerifyFailureClassificationTests(unittest.TestCase):
    def test_repeated_non_comparable_failures_stop_as_unresolved_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-001",
                title="demo",
                description="",
                acceptance=[],
                verify_history=[
                    {
                        "attempt": 1,
                        "decision": "fail",
                        "summary": "non-comparable verification failure",
                        "failure_ids": ["cmd:conda run -p ./.conda python -m pytest -q tests"],
                        "comparable_failures": False,
                    }
                ],
            )

            analysis = orchestrator._analyze_verify_failure(
                task,
                ["cmd:conda run -p ./.conda python -m pytest -q tests"],
                comparable=False,
            )

            self.assertTrue(analysis["stop_retry"])
            self.assertIn("non-comparable", analysis["stats"])
            self.assertIn("stop-unresolved-identity", analysis["stats"])

    def test_repeat_detection_is_scoped_to_the_active_recovery_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            failure_id = "tests/test_checkout.py::test_rejects_expired_quote"
            task = TaskSpec(
                task_id="task-checkout",
                title="Validate checkout",
                description="",
                acceptance=[],
                recovery_round=1,
                verify_history=[
                    {
                        "attempt": 2,
                        "decision": "fail",
                        "summary": "failed in the original implementation round",
                        "failure_ids": [failure_id],
                        "comparable_failures": True,
                        "recovery_epoch": 0,
                        "recovery_round": 0,
                    }
                ],
            )

            first_in_round = orchestrator._analyze_verify_failure(task, [failure_id])
            self.assertFalse(first_in_round["stop_retry"])

            task.verify_history.append(
                {
                    "attempt": 1,
                    "decision": "fail",
                    "summary": "failed in the active recovery round",
                    "failure_ids": [failure_id],
                    "comparable_failures": True,
                    "recovery_epoch": 0,
                    "recovery_round": 1,
                }
            )
            repeated_in_round = orchestrator._analyze_verify_failure(task, [failure_id])

            self.assertTrue(repeated_in_round["stop_retry"])
            self.assertEqual(repeated_in_round["first_attempt"], 1)

    def test_single_non_comparable_command_is_not_repair_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            command_ref = (
                "cmd:python -m pytest -q "
                "tests/test_checkout.py::test_rejects_expired_quote"
            )
            task = TaskSpec(
                task_id="task-checkout",
                title="Validate checkout",
                description="",
                acceptance=[],
                verify_history=[
                    {
                        "attempt": 1,
                        "decision": "fail",
                        "summary": "command failed without a test identifier",
                        "failure_ids": [command_ref],
                        "comparable_failures": False,
                    }
                ],
            )

            refs = orchestrator._candidate_repair_refs(
                task,
                {
                    "failure_ids": [command_ref],
                    "comparable_failures": False,
                },
            )

            self.assertEqual(refs, [])

    def test_vitest_display_failure_recovery_resolves_lineage_owned_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.steps.extend(
                [
                    VerificationStep(
                        kind="test",
                        runner="vitest",
                        targets=["workbench/src/e2e/video-home.test.ts"],
                    ),
                    VerificationStep(
                        kind="test",
                        runner="vitest",
                        targets=["workbench/src/e2e/consumer.test.ts"],
                    ),
                ]
            )
            desktop_ref = (
                "workbench/src/e2e/video-home.test.ts::"
                "dialog_desktop_matches_prototype"
            )
            unprefixed_desktop_ref = desktop_ref.removeprefix("workbench/")
            duration_ref = (
                "workbench/src/e2e/video-home.test.ts::"
                "dialog_duration_semantics_matches_prototype"
            )
            parent = TaskSpec(
                task_id="task-video-home",
                title="Match the video home prototype",
                description="Keep the video dialog faithful to the prototype.",
                acceptance=["The dialog matches at both viewports."],
                verification_refs=[unprefixed_desktop_ref],
            )
            completed_repair = TaskSpec(
                task_id="repair-video-home",
                title="Publish duration evidence",
                description="Publish the duration proof.",
                acceptance=["Duration evidence passes."],
                status="done",
                task_origin="evidence_repair",
                parent_task_id=parent.task_id,
                verification_refs=[duration_ref],
            )
            transitive_consumer = TaskSpec(
                task_id="task-consumer",
                title="Consume prototype evidence",
                description="Check the nested producer result.",
                acceptance=["The producer exits successfully."],
                verification_refs=[
                    "workbench/src/e2e/consumer.test.ts::producer_exits_cleanly"
                ],
            )

            result = {
                "reason": "3 new verification failures vs task baseline",
                "failure_ids": [
                    "src/e2e/video-home.test.ts > video home > "
                    "dialog_desktop_matches_prototype",
                    "src/e2e/video-home.test.ts > video home > "
                    "dialog_duration_semantics_matches_prototype",
                    "src/e2e/consumer.test.ts > evidence consumer > "
                    "producer_exits_cleanly",
                ],
                "comparable_failures": True,
            }
            tasks = [completed_repair, parent, transitive_consumer]
            refs = orchestrator._candidate_repair_refs(
                parent,
                result,
                tasks=[completed_repair, parent, transitive_consumer],
            )

            self.assertEqual(refs, [desktop_ref, duration_ref])
            state = load_run_state(project_root)
            state.tasks = tasks

            self.assertTrue(
                orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    tasks,
                    parent,
                    result,
                )
            )
            scheduled_repairs = [
                item
                for item in tasks
                if item.task_origin == "evidence_repair" and item.status == "pending"
            ]
            self.assertEqual(len(scheduled_repairs), 1)
            self.assertEqual(scheduled_repairs[0].verification_refs, refs)
            self.assertEqual(
                state.last_recovery_route["outcome"],
                "repair_tasks_scheduled",
            )

    def test_ambiguous_vitest_display_recovery_is_not_repair_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            for package in ("admin", "workbench"):
                orchestrator.config.gates.steps.append(
                    VerificationStep(
                        kind="test",
                        runner="vitest",
                        targets=[f"{package}/src/e2e/home.test.ts"],
                    )
                )
            parent = TaskSpec(
                task_id="task-home",
                title="Verify home",
                description="Verify both independently packaged home surfaces.",
                acceptance=["Both home surfaces pass."],
                verification_refs=[
                    "admin/src/e2e/home.test.ts::renders_home",
                    "workbench/src/e2e/home.test.ts::renders_home",
                ],
            )

            refs = orchestrator._candidate_repair_refs(
                parent,
                {
                    "failure_ids": [
                        "src/e2e/home.test.ts > home > renders_home",
                    ],
                    "comparable_failures": True,
                },
                tasks=[parent],
            )

            self.assertEqual(refs, [])

    def test_repeated_non_comparable_commands_schedule_evidence_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            command_refs = [
                (
                    "cmd:python -m pytest -q "
                    "tests/test_checkout.py::test_rejects_expired_quote"
                ),
                (
                    "cmd:npm --prefix web test -- "
                    "src/e2e/checkout.test.ts -t rejects_expired_quote"
                ),
            ]
            unresolved_description = (
                "src/e2e/checkout.test.ts > checkout contract > "
                "rejects_expired_quote"
            )
            repeated_set = [*command_refs, unresolved_description]
            task = TaskSpec(
                task_id="task-checkout",
                title="Validate checkout",
                description="Keep checkout evidence current.",
                acceptance=["Checkout verification passes."],
                status="blocked",
                review_summary="verification stopped with unresolved failure identity",
                verify_history=[
                    {
                        "attempt": 1,
                        "decision": "fail",
                        "summary": "command failed without stable test identifiers",
                        "failure_ids": repeated_set,
                        "comparable_failures": False,
                    },
                    {
                        "attempt": 2,
                        "decision": "fail",
                        "summary": "a diagnostic isolated a different failure",
                        "failure_ids": [
                            "tests/test_diagnostics.py::test_environment_contract"
                        ],
                        "comparable_failures": True,
                    },
                    {
                        "attempt": 3,
                        "decision": "fail",
                        "summary": "command failed without stable test identifiers",
                        "failure_ids": list(reversed(repeated_set)),
                        "comparable_failures": False,
                    },
                ],
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            tasks = state.tasks

            payload = orchestrator._task_recovery_payload_from_history(task, state)
            scheduled = orchestrator._schedule_repair_tasks_for_failure(
                state,
                tasks,
                task,
                payload,
            )

            self.assertTrue(scheduled)
            repairs = [
                item for item in tasks if item.task_origin == "evidence_repair"
            ]
            self.assertEqual(len(repairs), 2)
            self.assertEqual(
                {ref for repair in repairs for ref in repair.verification_refs},
                set(command_refs),
            )
            self.assertEqual(
                {
                    command
                    for repair in repairs
                    for command in orchestrator._build_task_verify_commands(repair)
                },
                {ref.removeprefix("cmd:") for ref in command_refs},
            )
            self.assertEqual(
                state.last_recovery_route["outcome"],
                "repair_tasks_scheduled",
            )
            self.assertEqual(task.status, "pending")
            self.assertEqual(set(task.depends_on), {repair.task_id for repair in repairs})

    def test_artifact_contract_failure_schedules_its_owned_producer_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            (project_root / "tests").mkdir(exist_ok=True)
            write_text(
                project_root / "tests" / "test_storage_smoke.py",
                "def test_publishes_receipt():\n    pass\n",
            )
            orchestrator = Orchestrator(project_root)
            producer_ref = "tests/test_storage_smoke.py::test_publishes_receipt"
            unrelated_ref = "tests/test_public_api.py::test_lists_assets"
            artifact_ref = ".tmp-tests/storage/runs/*/receipt.json"
            failure_id = (
                "verification_contract:nonportable_ignored_evidence:"
                f"{artifact_ref}"
            )
            write_json(
                task_plan_path(project_root),
                {
                    "verification_steps": [
                        {
                            "kind": "test",
                            "runner": "pytest",
                            "targets": ["tests/test_storage_smoke.py"],
                            "artifact_globs": [artifact_ref],
                        }
                    ],
                    "tasks": [
                        {
                            "task_id": "task-storage",
                            "title": "Verify durable storage",
                            "description": "Publish current-run storage evidence.",
                            "acceptance": ["The storage receipt is portable."],
                            "status": "blocked",
                            "review_summary": (
                                "current isolated verification did not publish "
                                "supporting evidence"
                            ),
                            "verification_refs": [unrelated_ref, producer_ref],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-STORAGE",
                                    "oracle_index": 1,
                                    "status": "verified",
                                    "evidence_refs": [producer_ref, artifact_ref],
                                }
                            ],
                            "verify_history": [
                                {
                                    "attempt": 2,
                                    "decision": "fail",
                                    "summary": "supporting evidence was not published",
                                    "failure_ids": [failure_id],
                                    "comparable_failures": True,
                                }
                            ],
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            repair, task = result.tasks
            self.assertEqual(repair.task_origin, "evidence_repair")
            self.assertEqual(repair.parent_task_id, task.task_id)
            self.assertEqual(repair.verification_refs, [producer_ref])
            self.assertEqual(task.status, "pending")
            self.assertEqual(
                result.last_recovery_route["outcome"],
                "repair_tasks_scheduled",
            )

    def test_artifact_contract_missing_generated_glob_routes_plan_and_syncs_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            (project_root / "tests").mkdir(exist_ok=True)
            write_text(
                project_root / "tests" / "test_storage_smoke.py",
                "def test_publishes_receipt():\n    pass\n",
            )
            orchestrator = Orchestrator(project_root)
            producer_ref = "tests/test_storage_smoke.py::test_publishes_receipt"
            artifact_ref = ".tmp-tests/storage/runs/*/receipt.json"
            covered_artifact_ref = ".tmp-tests/storage/runs/*/summary.json"
            failure_id = (
                "verification_contract:nonportable_ignored_evidence:"
                f"{artifact_ref}"
            )
            covered_failure_id = (
                "verification_contract:nonportable_ignored_evidence:"
                f"{covered_artifact_ref}"
            )
            task = TaskSpec(
                task_id="task-storage",
                title="Verify durable storage",
                description="Publish current-run storage evidence.",
                acceptance=["The storage receipt is portable."],
                status="blocked",
                verification_refs=[producer_ref],
                requirement_proofs=[
                    {
                        "evidence_refs": [
                            producer_ref,
                            artifact_ref,
                            covered_artifact_ref,
                        ],
                    }
                ],
            )
            completed_repair = TaskSpec(
                task_id="repair-task-storage-r1-1",
                title="Repair receipt producer",
                description="Repair the producer implementation.",
                acceptance=["The producer proof passes."],
                status="done",
                task_origin="evidence_repair",
                parent_task_id=task.task_id,
            )
            pending_repair = TaskSpec(
                task_id="repair-task-storage-r1-2",
                title="Repair receipt assertion",
                description="Finish the producer assertion repair.",
                acceptance=["The producer assertion passes."],
                status="pending",
                task_origin="evidence_repair",
                parent_task_id=task.task_id,
            )
            plan_payload = {
                "verification_steps": [
                    {
                        "kind": "test",
                        "runner": "pytest",
                        "targets": ["tests/test_storage_smoke.py"],
                        "artifact_globs": [covered_artifact_ref],
                    }
                ],
                "tasks": [
                    completed_repair.to_dict(),
                    pending_repair.to_dict(),
                    task.to_dict(),
                ],
            }
            write_json(task_plan_path(project_root), plan_payload)
            state = load_run_state(project_root)
            state.tasks = [completed_repair, pending_repair, task]

            recovered = orchestrator._schedule_repair_tasks_for_failure(
                state,
                state.tasks,
                task,
                {
                    "ok": False,
                    "reason": "ignored evidence was not published",
                    "failure_ids": [failure_id, covered_failure_id],
                    "comparable_failures": True,
                },
            )

            self.assertTrue(recovered)
            self.assertEqual(state.current_stage, "plan")
            self.assertEqual(state.rejected_stage, "plan")
            self.assertEqual(state.last_recovery_route["outcome"], "plan_metadata_repair")
            self.assertEqual(len(state.tasks), 3)
            self.assertEqual(completed_repair.status, "done")
            self.assertEqual(pending_repair.status, "pending")
            repair = state.resume_context["artifact_publication_metadata_repair"]
            self.assertEqual(
                [item["artifact_ref"] for item in repair["artifacts"]],
                [artifact_ref],
            )
            feedback = orchestrator._plan_validation_feedback(
                AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=project_root / "plan-output.txt",
                    summary="updated plan",
                )
            )
            self.assertIn(
                "artifact publication metadata repair requires artifact_globs",
                feedback,
            )
            self.assertTrue(
                orchestrator._artifact_publication_metadata_repair_errors(
                    plan_payload,
                    repair=repair,
                )
            )

            plan_payload["verification_steps"][0]["artifact_globs"].append(
                artifact_ref
            )
            write_json(task_plan_path(project_root), plan_payload)
            self.assertEqual(
                orchestrator._artifact_publication_metadata_repair_errors(
                    plan_payload,
                    repair=repair,
                ),
                [],
            )

            orchestrator._apply_generated_verification_config()
            state.tasks = orchestrator._load_tasks_from_plan()
            orchestrator._complete_artifact_publication_metadata_repair(state)

            self.assertIn(
                artifact_ref,
                orchestrator.config.gates.steps[0].artifact_globs,
            )
            self.assertNotIn(
                "artifact_publication_metadata_repair",
                state.resume_context,
            )
            self.assertEqual(
                state.last_recovery_route["outcome"],
                "plan_metadata_repaired",
            )
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[1].status, "pending")
            parent = next(
                item for item in state.tasks if item.task_id == task.task_id
            )
            self.assertEqual(parent.evidence_preflight, {})

    def test_artifact_contract_without_sibling_uses_task_owned_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            producer_ref = "tests/test_storage_smoke.py::test_publishes_receipt"
            artifact_ref = ".tmp-tests/storage/runs/*/receipt.json"
            task = TaskSpec(
                task_id="task-storage",
                title="Verify durable storage",
                description="Publish current-run storage evidence.",
                acceptance=["The storage receipt is portable."],
                verification_refs=[producer_ref],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-STORAGE",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": [artifact_ref],
                    }
                ],
            )

            refs = orchestrator._candidate_repair_refs(
                task,
                {
                    "failure_ids": [
                        "verification_contract:nonportable_ignored_evidence:"
                        f"{artifact_ref}"
                    ],
                    "comparable_failures": True,
                },
            )

            self.assertEqual(refs, [producer_ref])

    def test_recovery_signature_is_stable_across_review_reason_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            refs = [
                "tests/test_requirements_audit_state.py::A::test_a",
                "tests/test_requirements_audit_state.py::A::test_b",
            ]

            # The same failing refs must produce the same signature regardless of the
            # (volatile) review reason or ordering, so repeated failures accumulate rounds
            # and hit recovery max_rounds instead of spawning unbounded repair tasks.
            sig_first = orchestrator._recovery_signature(
                refs, "review flagged weak assertion around line 10"
            )
            sig_second = orchestrator._recovery_signature(
                list(reversed(refs)), "completely different wording citing line 42"
            )
            self.assertEqual(sig_first, sig_second)

            # A genuinely different failing ref set must change the signature.
            sig_other = orchestrator._recovery_signature(
                refs + ["tests/test_other.py::B::test_c"], "same wording"
            )
            self.assertNotEqual(sig_first, sig_other)

    def test_changed_failure_after_metadata_repair_opens_bounded_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            failure_ref = "tests/test_dialog.py::test_matches_prototype"
            parent = TaskSpec(
                task_id="task-dialog",
                title="Match the dialog prototype",
                description="Keep dialog geometry aligned with its prototype.",
                acceptance=["The dialog proof passes."],
                status="blocked",
                task_origin="planned",
                recovery_round=2,
                verification_refs=[failure_ref],
            )
            completed_repair = TaskSpec(
                task_id="repair-task-dialog-r1-1",
                title="Publish dialog evidence",
                description="Publish the prior evidence set.",
                acceptance=["Prior evidence is published."],
                status="done",
                task_origin="evidence_repair",
                parent_task_id=parent.task_id,
                recovery_round=2,
            )
            state = load_run_state(project_root)
            state.tasks = [completed_repair, parent]
            state.last_recovery_route = {
                "task_id": parent.task_id,
                "task_origin": "planned",
                "lineage_id": parent.task_id,
                "epoch": 0,
                "round": 2,
                "outcome": "plan_metadata_repaired",
                "failure_signature": "prior-publication-signature",
            }

            scheduled = orchestrator._schedule_repair_tasks_for_failure(
                state,
                state.tasks,
                parent,
                {
                    "reason": "1 new verification failure vs task baseline",
                    "failure_ids": [failure_ref],
                    "comparable_failures": True,
                },
            )

            self.assertTrue(scheduled)
            self.assertEqual(parent.recovery_epoch, 1)
            self.assertEqual(parent.recovery_round, 1)
            self.assertEqual(completed_repair.status, "done")
            self.assertEqual(completed_repair.recovery_round, 2)
            self.assertEqual(
                [
                    entry["result"]
                    for entry in parent.recovery_history
                    if entry.get("result") == "epoch_reopened"
                ],
                ["epoch_reopened"],
            )
            self.assertEqual(
                state.last_recovery_route["outcome"],
                "repair_tasks_scheduled",
            )
            self.assertEqual(state.last_recovery_route["epoch"], 1)

    def test_newly_resolved_terminal_vitest_failure_opens_bounded_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            owned_ref = "workbench/src/e2e/dialog.test.ts::matches_prototype"
            orchestrator.config.gates.steps.append(
                VerificationStep(
                    kind="test",
                    runner="vitest",
                    targets=["workbench/src/e2e/dialog.test.ts"],
                )
            )
            task = TaskSpec(
                task_id="task-dialog",
                title="Match the dialog prototype",
                description="Keep dialog geometry aligned with its prototype.",
                acceptance=["The dialog proof passes."],
                status="blocked",
                recovery_round=2,
                verification_refs=[owned_ref],
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            state.last_recovery_route = {
                "task_id": task.task_id,
                "lineage_id": task.task_id,
                "epoch": 0,
                "round": 2,
                "outcome": "not_recoverable",
                "failure_signature": "",
            }

            scheduled = orchestrator._schedule_repair_tasks_for_failure(
                state,
                state.tasks,
                task,
                {
                    "reason": "1 new verification failure vs task baseline",
                    "failure_ids": [
                        "src/e2e/dialog.test.ts > dialog > matches_prototype"
                    ],
                    "comparable_failures": True,
                },
            )

            self.assertTrue(scheduled)
            self.assertEqual(task.recovery_epoch, 1)
            self.assertEqual(task.recovery_round, 1)
            self.assertEqual(task.recovery_history[0]["trigger"], "not_recoverable")
            repair = next(
                item for item in state.tasks if item.task_origin == "evidence_repair"
            )
            self.assertEqual(repair.verification_refs, [owned_ref])

    def test_metadata_repair_signature_epoch_has_global_churn_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            failure_ref = "tests/test_dialog.py::test_mobile_matches_prototype"
            parent = TaskSpec(
                task_id="task-dialog",
                title="Match the dialog prototype",
                description="Keep dialog geometry aligned with its prototype.",
                acceptance=["The dialog proof passes."],
                status="blocked",
                task_origin="planned",
                recovery_epoch=1,
                recovery_round=2,
                verification_refs=[failure_ref],
                recovery_history=[
                    {
                        "epoch": 1,
                        "round": 0,
                        "result": "epoch_reopened",
                        "signature": "second-signature",
                        "previous_signature": "first-signature",
                        "trigger": "plan_metadata_repaired",
                    }
                ],
            )
            state = load_run_state(project_root)
            state.tasks = [parent]
            state.last_recovery_route = {
                "task_id": parent.task_id,
                "task_origin": "planned",
                "lineage_id": parent.task_id,
                "epoch": 1,
                "round": 2,
                "outcome": "plan_metadata_repaired",
                "failure_signature": "second-signature",
            }

            scheduled = orchestrator._schedule_repair_tasks_for_failure(
                state,
                state.tasks,
                parent,
                {
                    "reason": "1 new verification failure vs task baseline",
                    "failure_ids": [failure_ref],
                    "comparable_failures": True,
                },
            )

            self.assertFalse(scheduled)
            self.assertEqual(parent.recovery_epoch, 1)
            self.assertEqual(parent.recovery_round, 2)
            self.assertEqual(parent.recovery_history[-1]["result"], "exhausted")
            self.assertEqual(state.last_recovery_route["outcome"], "exhausted")

    def test_task_verify_commands_follow_owned_proof_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Schema contract",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": [
                            "tests/test_public_api.py::test_contract",
                            "app/service.py::build_payload",
                        ],
                    }
                ],
            )

            commands = orchestrator._build_task_verify_commands(task)

            self.assertEqual(len(commands), 1)
            self.assertIn("tests/test_public_api.py::test_contract", commands[0])
            self.assertNotIn("app/service.py::build_payload", commands[0])

    def test_task_verify_commands_accept_command_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Frontend fidelity",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": [
                            "cmd:npm --prefix workbench test -- src/e2e/home.test.ts -t desktop",
                            ".tmp-tests/frontend-prototype/home-desktop-1440x900.png",
                            "specs/frondend_prototype/home.html",
                        ],
                    }
                ],
            )

            self.assertEqual(
                orchestrator._build_task_verify_commands(task),
                ["npm --prefix workbench test -- src/e2e/home.test.ts -t desktop"],
            )

    def test_task_verify_prefers_owned_commands_over_global_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests"]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Owned gate",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    }
                ],
            )

            def fail_global(*args, **kwargs):
                raise AssertionError("global gate should not run for owned task verification")

            def pass_owned(commands, *, collect_all, context):
                self.assertTrue(collect_all)
                self.assertIn("tests/test_public_api.py::test_contract", commands[0])
                return (
                    GateResult(
                        ok=True,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=True,
                                returncode=0,
                                stdout="",
                                stderr="",
                            )
                        ],
                        summary="all commands passed",
                    ),
                    "",
                )

            with patch.object(orchestrator, "_run_gate_commands", side_effect=fail_global):
                with patch.object(
                    orchestrator,
                    "_run_gate_commands_for_commands",
                    side_effect=pass_owned,
                ):
                    with patch.object(orchestrator, "_quick_verify_failure", return_value=""):
                        result = orchestrator._run_task_verify(task)

            self.assertTrue(result["ok"], msg=str(result))

    def test_task_verify_prefers_command_evidence_over_global_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests"]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Owned frontend gate",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": [
                            "cmd:npm --prefix workbench test -- src/e2e/home.test.ts -t desktop",
                        ],
                    }
                ],
            )

            def fail_global(*args, **kwargs):
                raise AssertionError("global gate should not run for command evidence")

            def pass_owned(commands, *, collect_all, context):
                self.assertTrue(collect_all)
                self.assertEqual(
                    commands,
                    ["npm --prefix workbench test -- src/e2e/home.test.ts -t desktop"],
                )
                return (
                    GateResult(
                        ok=True,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=True,
                                returncode=0,
                                stdout="",
                                stderr="",
                            )
                        ],
                        summary="all commands passed",
                    ),
                    "",
                )

            with patch.object(orchestrator, "_run_gate_commands", side_effect=fail_global):
                with patch.object(
                    orchestrator,
                    "_run_gate_commands_for_commands",
                    side_effect=pass_owned,
                ):
                    with patch.object(orchestrator, "_quick_verify_failure", return_value=""):
                        result = orchestrator._run_task_verify(task)

            self.assertTrue(result["ok"], msg=str(result))

    def test_task_verify_marks_cross_domain_failure_as_scope_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Contract mismatch",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": ["app/stage_backends/text.py::PlanningBackend._schema"],
                    }
                ],
            )

            failed_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command="conda run -p ./.conda python -m pytest -q tests",
                        ok=False,
                        returncode=1,
                        stdout=(
                            "FAILED tests/test_real_voice_adapter_api.py::"
                            "RealVoiceAdapterApiTests::test_compose_resubmission\n"
                        ),
                        stderr="",
                    )
                ],
                summary="FAILED tests/test_real_voice_adapter_api.py::RealVoiceAdapterApiTests::test_compose_resubmission",
            )

            with patch.object(orchestrator, "_run_gate_commands", return_value=(failed_gate, "")):
                with patch(
                    "auto_agents.orchestrator.extract_failure_info",
                    return_value=FailureExtraction(
                        failure_ids=[
                            "tests/test_real_voice_adapter_api.py::RealVoiceAdapterApiTests::test_compose_resubmission"
                        ],
                        comparable=True,
                        non_comparable_ids=[],
                    ),
                ):
                    result = orchestrator._run_task_verify(task)

            self.assertFalse(result["ok"])
            self.assertTrue(result["contract_scope_issue"])
            self.assertIn("verification scope mismatch", str(result["reason"]))

    def test_task_verify_baseline_uses_owned_commands_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Owned baseline",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    }
                ],
            )

            captured = {}

            def fake_owned(commands, *, collect_all, context):
                captured["commands"] = list(commands)
                return (
                    GateResult(
                        ok=False,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=False,
                                returncode=1,
                                stdout="FAILED tests/test_public_api.py::test_contract",
                                stderr="",
                            )
                        ],
                        summary="FAILED tests/test_public_api.py::test_contract",
                    ),
                    "",
                )

            with patch.object(orchestrator, "_run_gate_commands_for_commands", side_effect=fake_owned):
                changed = orchestrator._ensure_task_verify_baseline(task)

            self.assertTrue(changed)
            self.assertEqual(
                captured["commands"],
                [orchestrator._build_task_proof_evidence_command(["tests/test_public_api.py::test_contract"])],
            )
            self.assertEqual(
                task.verify_baseline_failures,
                ["tests/test_public_api.py::test_contract"],
            )


class OutOfScopePlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "plan":
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Plan slice",
                            "description": "A valid task plan entry.",
                            "acceptance": ["Plan remains valid."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            leaked = self.project_root / "tests" / "test_stage_leak.py"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            write_text(leaked, "def test_stage_leak():\n    assert True\n")
            summary = "plan with out-of-scope mutation\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class PlanWithDiagnosticLogAdapter:
    def __init__(self, project_root: Path, *, write_requirements_audit: bool = False) -> None:
        self.project_root = project_root
        self.write_requirements_audit = write_requirements_audit

    def run(self, request):
        if request.stage == "plan":
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Plan slice",
                            "description": "A valid task plan entry.",
                            "acceptance": ["Plan remains valid."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            diagnostic = (
                self.project_root
                / ".auto-agents"
                / "failed-verification-logs"
                / "verify-stage-test.log"
            )
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            write_text(diagnostic, "FAILED tests/test_demo.py::test_contract\n")
            if self.write_requirements_audit:
                write_text(
                    self.project_root / ".auto-agents" / "docs" / "requirements_audit.md",
                    "# Requirements Audit\n\nResult: fail\n",
                )
            summary = "plan with diagnostic log\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class OutOfScopeProviderResearchAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "provider_research":
            reference_path = self.project_root / ".auto-agents" / "docs" / "provider_references" / "provider.md"
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(reference_path, "# Provider reference\n")
            write_json(
                provider_references_lock_path(self.project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": ".auto-agents/docs/provider_references/provider.md",
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )
            leaked = self.project_root / "tests" / "test_provider_stage_leak.py"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            write_text(leaked, "def test_provider_stage_leak():\n    assert True\n")
            summary = "provider research with out-of-scope mutation\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class OutOfScopeReviewAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "review":
            write_text(self.project_root / "notes.txt", "review should be read-only\n")
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReviewTsBuildInfoAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            write_text(self.project_root / "artifact.txt", "good\n")
            summary = "implemented good\n"
        elif request.stage == "review":
            self.review_calls += 1
            workbench = self.project_root / "workbench"
            workbench.mkdir(parents=True, exist_ok=True)
            write_text(workbench / "tsconfig.tsbuildinfo", '{"version":"incremental-2"}\n')
            summary = "DECISION: pass\nreview passed despite tooling cache churn\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReviewBuildLibAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            write_text(self.project_root / "artifact.txt", "good\n")
            summary = "implemented good\n"
        elif request.stage == "review":
            self.review_calls += 1
            write_text(
                self.project_root / "build" / "lib" / "app" / "__init__.py",
                "# generated build output\n",
            )
            summary = "DECISION: pass\nreview passed despite python build output churn\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class OutOfScopeImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(task_plan_path(self.project_root), "{\"tasks\": []}\n")
            summary = "implemented with forbidden auto-agents mutation\n"
        elif request.stage == "review":
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RecoveringOutOfScopeImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "good\n")
            if self.implement_calls == 1:
                write_text(task_plan_path(self.project_root), "{\"tasks\": []}\n")
                summary = "implemented with first-attempt auto-agents mutation\n"
            else:
                summary = "implemented clean retry\n"
        elif request.stage == "review":
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RecoveringConfigMutationImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "good\n")
            if self.implement_calls == 1:
                write_text(self.project_root / ".auto-agents" / "config.json", "{\"mutated\": true}\n")
                summary = "implemented with first-attempt config mutation\n"
            else:
                summary = "implemented clean retry\n"
        elif request.stage == "review":
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RecoveringProtectedInputMutationImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_input_before_review = ""

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "good\n")
            if self.implement_calls == 1:
                write_text(self.project_root / ".auto-agents" / "docs" / "review.md", "mutated review\n")
                write_text(self.project_root / "specs" / "2026-05-07-iter-01.md", "mutated spec\n")
                summary = "implemented with first-attempt protected input mutation\n"
            else:
                summary = "implemented clean retry\n"
        elif request.stage == "review":
            self.review_input_before_review = (
                self.project_root / ".auto-agents" / "docs" / "review.md"
            ).read_text(encoding="utf-8")
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RecoveringStagedPublicSpecMutationImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.second_attempt_spec_status = ""

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "good\n")
            if self.implement_calls == 1:
                write_text(
                    self.project_root / "spec.md",
                    "# Unauthorized staged public spec change\n",
                )
                subprocess.run(
                    ["git", "add", "--", "spec.md"],
                    cwd=str(self.project_root),
                    check=True,
                    text=True,
                    capture_output=True,
                )
                summary = "implemented with a staged protected spec mutation\n"
            else:
                self.second_attempt_spec_status = subprocess.run(
                    ["git", "status", "--short", "--", "spec.md"],
                    cwd=str(self.project_root),
                    check=True,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                ).stdout
                summary = "implemented clean retry\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class PublicSpecImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.implement_prompt = ""

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            self.implement_prompt = request.prompt
            write_text(self.project_root / "artifact.txt", "good\n")
            write_text(self.project_root / "spec.md", "# Updated public product spec\n")
            summary = "updated the declared public product spec\n"
        elif request.stage == "review":
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RecoveringHistoryMutationImplementAdapter:
    def __init__(self, project_root: Path, archive_path: Path) -> None:
        self.project_root = project_root
        self.archive_path = archive_path
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "good\n")
            if self.implement_calls == 1:
                write_text(self.archive_path, "{\"tasks\": []}\n")
                summary = "implemented with first-attempt history mutation\n"
            else:
                summary = "implemented clean retry\n"
        elif request.stage == "review":
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReadmeProposalMutationAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.calls = 0

    def run(self, request):
        if request.stage == "readme":
            self.calls += 1
            write_text(self.project_root / "README.md", "# premature write\n")
            summary = "proposal mutated readme\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RetryingVerificationCommandAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0

    def run(self, request):
        if request.stage == "plan":
            self.plan_calls += 1
            target = "tests/test_missing.py" if self.plan_calls == 1 else "tests/test_ok.py"
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_commands": [f"conda run -p ./.conda python -m pytest -q {target}"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Add CLI entrypoint",
                            "description": "Add a runnable command line entrypoint.",
                            "acceptance": ["`python -m demo --help` exits successfully."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            write_text(request.output_path, "verification plan\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class RetryingImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            value = "bad" if self.implement_calls == 1 else "good"
            write_text(self.project_root / "artifact.txt", value + "\n")
            write_text(request.output_path, f"implemented {value}\n")
            summary = f"implemented {value}"
        elif request.stage == "review":
            current = (self.project_root / "artifact.txt").read_text(encoding="utf-8").strip()
            decision = "pass" if current == "good" else "fail"
            summary = f"DECISION: {decision}\nartifact is {current}\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ResumeReviewAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            raise AssertionError("implement should not be called when resuming an interrupted task")
        if request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nresume review passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class BlockedRetryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "fixed\n")
            summary = "implemented fixed\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nblocked task recovered\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ProviderReferenceRepairAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            reference_path = (
                self.project_root
                / ".auto-agents"
                / "docs"
                / "provider_references"
                / "apiyi_gpt_image_2.md"
            )
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(
                reference_path,
                "# APIYI GPT-Image-2 Provider Reference\n\n"
                "gpt-image-2-vip uses POST /v1/images/generations and "
                "POST /v1/images/edits.\n",
            )
            summary = "updated provider reference\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class TaskPlanRepairAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            payload = load_task_plan(self.project_root)
            for item in payload.get("tasks", []):
                if not isinstance(item, dict) or item.get("task_id") != "task-001":
                    continue
                proofs = item.setdefault("requirement_proofs", [])
                if not proofs:
                    proofs.append(
                        {
                            "requirement_id": "REQ-001",
                            "oracle_index": 1,
                            "status": "verified",
                            "evidence_refs": [],
                        }
                    )
                proofs[0]["evidence_refs"] = [
                    ".auto-agents/docs/provider_references/apiyi_gpt_image_2.md"
                ]
            write_json(task_plan_path(self.project_root), payload)
            summary = "updated task plan proof refs\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class SequentialArtifactAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(
                self.project_root / f"artifact-{self.implement_calls}.txt",
                f"attempt-{self.implement_calls}\n",
            )
            summary = f"implemented attempt {self.implement_calls}\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nsequential task passed review\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class VerifyBeforeReviewAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "bad\n")
            summary = "implemented bad\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview should not run before verify passes\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class CachedReviewResumeAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            raise AssertionError("implement should not run when resuming cached review state")
        if request.stage == "review":
            self.review_calls += 1
            raise AssertionError("review should be reused from cache when worktree is unchanged")
        summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReviewEffortAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_efforts = []

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            raise AssertionError("implement should not run when resuming for review effort checks")
        if request.stage == "review":
            self.review_efforts.append(request.effort)
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RetryFeedbackAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_prompts = []
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_prompts.append(request.prompt)
            write_text(self.project_root / "artifact.txt", "bad\n")
            summary = "implemented bad\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class SplitPlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "plan":
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-child-a",
                            "title": "First child",
                            "description": "First split child.",
                            "acceptance": ["child a done"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-child-b",
                            "title": "Second child",
                            "description": "Second split child.",
                            "acceptance": ["child b done"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            summary = "plan split legacy task\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class StalePlanAuditRecoveryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts = []
        self.review_prompts = []

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            if self.implement_calls == 2:
                write_text(
                    self.project_root / "tests" / "test_plan_contract.py",
                    "EXPECTED_TASK = 'task-child-a'\n",
                )
            write_text(self.project_root / "artifact.txt", f"attempt-{self.implement_calls}\n")
            summary = f"implemented attempt {self.implement_calls}\n"
        elif request.stage == "review":
            self.review_calls += 1
            self.review_prompts.append(request.prompt)
            summary = "DECISION: pass\nreview passed after stale test migration\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class StaleTaskStatusAuditRecoveryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts = []
        self.review_prompts = []

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            if self.implement_calls == 2:
                write_text(
                    self.project_root / "tests" / "test_status_contract.py",
                    (
                        "EXPECTED = {\n"
                        "    'task-080': {\n"
                        "        'status': 'done',\n"
                        "    },\n"
                        "}\n"
                    ),
                )
            write_text(self.project_root / "artifact.txt", f"attempt-{self.implement_calls}\n")
            summary = f"implemented attempt {self.implement_calls}\n"
        elif request.stage == "review":
            self.review_calls += 1
            self.review_prompts.append(request.prompt)
            summary = "DECISION: pass\nreview passed after stale task-status migration\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class SequencedVerifyFailureAdapter:
    def __init__(self, project_root: Path, values):
        self.project_root = project_root
        self.values = list(values)
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            index = min(self.implement_calls, len(self.values) - 1)
            value = self.values[index]
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", value + "\n")
            summary = f"implemented {value}\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class MissingCondaFastFailAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "hello\n")
            summary = "implemented hello\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class MissingPytestTargetFastFailAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "hello\n")
            summary = "implemented hello\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class PermanentReviewFailureAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            summary = "implemented bad\n"
            write_text(self.project_root / "artifact.txt", "bad\n")
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: fail\nCore issue: health endpoint is not actually exercised.\n- Missing request test.\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RepairReviewRecoveryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts: list[str] = []

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            write_text(
                self.project_root / "artifact.txt",
                f"implementation round {self.implement_calls}\n",
            )
            summary = f"implemented round {self.implement_calls}\n"
        elif request.stage == "review":
            self.review_calls += 1
            if self.review_calls == 1:
                summary = (
                    "DECISION: fail\n"
                    "Acceptance proof is tautological; add two qualified candidates.\n"
                )
            else:
                summary = "DECISION: pass\nreview passed\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class AuditRecoveryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0
        self.implement_calls = 0
        self.provider_research_calls = 0
        self.review_calls = 0
        self.stage_calls: list[str] = []

    def run(self, request):
        self.stage_calls.append(request.stage)
        if request.stage == "plan":
            self.plan_calls += 1
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": [],
                            "status": "done",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Cover requirement",
                            "description": "Cover the missing mandatory requirement.",
                            "acceptance": ["coverage is explicit"],
                            "requirement_ids": ["REQ-001"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )
            summary = "plan updated\n"
            write_text(request.output_path, summary)
        elif request.stage == "provider_research":
            self.provider_research_calls += 1
            reference_path = self.project_root / ".auto-agents" / "docs" / "provider_references" / "provider.md"
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            reference_lines = ["# Provider reference"]
            for heading in PROVIDER_REFERENCE_V2_HEADINGS:
                reference_lines.extend(
                    ["", f"## {heading}", "", "Not applicable: recovery fixture."]
                )
            write_text(reference_path, "\n".join(reference_lines) + "\n")
            write_json(
                provider_references_lock_path(self.project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": ".auto-agents/docs/provider_references/provider.md",
                            "status": "verified",
                            "contract_version": PROVIDER_REFERENCE_CONTRACT_VERSION,
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )
            summary = "provider research updated\n"
            write_text(request.output_path, summary)
        elif request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "modern_backend\n")
            service_path = self.project_root / "app" / "service.py"
            if service_path.exists():
                write_text(service_path, "modern_backend = True\n")
            summary = "implemented audit recovery\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\naudit recovery review passed\n"
            write_text(request.output_path, summary)
        elif request.stage == "readme":
            if "Do NOT write the README yet. Only outline the planned sections." in request.prompt:
                summary = "- Overview\n- Architecture\n- Usage\n"
            else:
                write_text(
                    self.project_root / "README.md",
                    "# Demo\n## Overview\nRecovered project.\n## Architecture\nSimple test layout.\n## Usage\n```bash\npython -m demo\n```\n## Development\nRun tests.\n",
                )
                summary = "readme updated\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RetryFlowTests(unittest.TestCase):
    def test_pending_replan_commits_current_contract_with_unrelated_product_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            commit_all(project_root, "test: old planning baseline")
            old_head = head_ref(project_root)

            current_trace = {
                "contract_identity_schema_version": 1,
                "requirements": [
                    {
                        "id": "REQ-132",
                        "status": "active",
                        "supersedes": ["REQ-102"],
                    },
                    {
                        "id": "REQ-102",
                        "status": "superseded",
                        "superseded_by": ["REQ-132"],
                    },
                ],
            }
            write_text(
                project_root / ".auto-agents" / "docs" / "project_brief.md",
                "# Current iteration brief\n",
            )
            write_text(
                project_root / ".auto-agents" / "docs" / "architecture.md",
                "# Current iteration architecture\n",
            )
            write_json(requirements_trace_path(project_root), current_trace)
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-251",
                            "title": "Implement current contract",
                            "description": "Cover the replacement requirement.",
                            "acceptance": ["REQ-132 is implemented"],
                            "requirement_ids": ["REQ-132"],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            write_text(project_root / "product.py", "PARTIAL = True\n")
            subprocess.run(
                ["git", "add", "product.py"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )

            orchestrator = Orchestrator(project_root)
            pending_tasks = orchestrator._load_tasks_from_plan()
            self.assertTrue(all(task.status == "pending" for task in pending_tasks))

            orchestrator._commit_planning_baseline_if_needed(pending_tasks)

            current_head = head_ref(project_root)
            self.assertNotEqual(current_head, old_head)
            self.assertIn("product.py", changed_paths(project_root))
            committed_paths = subprocess.run(
                ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
            self.assertNotIn("product.py", committed_paths)
            self.assertTrue(hard_reset_clean(project_root, current_head))
            self.assertFalse((project_root / "product.py").exists())
            self.assertEqual(
                json.loads(requirements_trace_path(project_root).read_text(encoding="utf-8")),
                current_trace,
            )
            self.assertIn(
                "Current iteration brief",
                (project_root / ".auto-agents" / "docs" / "project_brief.md").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                "Current iteration architecture",
                (project_root / ".auto-agents" / "docs" / "architecture.md").read_text(
                    encoding="utf-8"
                ),
            )

    def test_pending_task_does_not_resume_from_orchestrator_only_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            commit_all(project_root, "test: baseline")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            task = TaskSpec(
                task_id="task-251",
                title="Replanned task",
                description="Must implement again after rewind.",
                acceptance=["implementation runs"],
                status="pending",
            )
            state.agent_attempts["implement-task-251"] = 1
            write_text(
                project_root / ".auto-agents/state/resume-marker.txt",
                "orchestrator state only\n",
            )

            self.assertTrue(changed_files(project_root))
            self.assertFalse(orchestrator._should_resume_task(state, task))

            write_text(project_root / "product.py", "VALUE = 1\n")
            self.assertTrue(orchestrator._should_resume_task(state, task))

    def test_upstream_rewind_and_replan_clear_stale_implement_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.agent_attempts = {
                "implement-task-251": 2,
                "implement-task-252": 1,
                "plan": 3,
            }

            orchestrator._clear_stale_implementation_resume_markers(
                state, task_ids={"task-251"}
            )
            self.assertNotIn("implement-task-251", state.agent_attempts)
            self.assertIn("implement-task-252", state.agent_attempts)
            self.assertEqual(state.agent_attempts["plan"], 3)

            orchestrator._rewind_state_from_stage(state, "plan")
            self.assertNotIn("implement-task-252", state.agent_attempts)
            self.assertEqual(state.agent_attempts["plan"], 3)

    def test_recovery_loop_uses_failure_and_requirement_scope_not_review_wording_or_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            first = TaskSpec(
                task_id="task-242",
                title="First owner",
                description="Fix contract.",
                acceptance=["contract passes"],
                requirement_ids=["REQ-102"],
            )
            replacement = TaskSpec(
                task_id="task-250",
                title="Replacement owner",
                description="Fix contract.",
                acceptance=["contract passes"],
                requirement_ids=["REQ-102"],
            )

            detected_first = orchestrator._record_recovery_loop_event(
                state,
                task=first,
                target_stage="clarify",
                review_text="first wording",
                failure_ids=["tests/test_contract.py::test_req_102"],
            )
            detected_second = orchestrator._record_recovery_loop_event(
                state,
                task=replacement,
                target_stage="clarify",
                review_text="completely different wording",
                failure_ids=["tests/test_contract.py::test_req_102"],
            )

            self.assertFalse(detected_first)
            self.assertTrue(detected_second)
            self.assertEqual(state.recovery_loop_events[-1]["requirement_ids"], ["REQ-102"])

    def test_rewind_incident_is_persisted_before_workspace_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            task = TaskSpec(
                task_id="task-incident",
                title="Preserve failure evidence",
                description="Record rewind context.",
                acceptance=["incident exists"],
                requirement_ids=["REQ-102"],
            )
            write_text(project_root / "attempt.txt", "failed attempt\n")

            relative = orchestrator._persist_rewind_incident(
                state,
                task=task,
                target_stage="clarify",
                rewind_ref="HEAD",
                gate_result={
                    "reason": "contract drift",
                    "review": "owner is clarify",
                    "failure_ids": ["tests/test_contract.py::test_req_102"],
                },
            )

            incident = json.loads((project_root / relative).read_text(encoding="utf-8"))
            self.assertEqual(incident["target_stage"], "clarify")
            self.assertEqual(incident["requirement_ids"], ["REQ-102"])
            self.assertIn("attempt.txt", incident["changed_paths"])

    @staticmethod
    def _configure_git_identity(project_root: Path) -> None:
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(project_root),
            check=True,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(project_root),
            check=True,
            text=True,
            capture_output=True,
        )

    def _seed_verify_ready_state(self, project_root: Path, orchestrator: Orchestrator) -> None:
        state = load_run_state(project_root)
        state.status = "pending"
        state.current_stage = "implement"
        state.stage_summaries = {
            "clarify": "done",
            "design": "done",
            "plan": "done",
            "provider_research": "done",
            "implement": "done",
        }
        state.tasks = orchestrator._load_tasks_from_plan()
        save_run_state(project_root, state)

    def _disable_gates_and_approvals(self, project_root: Path) -> None:
        orchestrator = Orchestrator(project_root)
        config = orchestrator.config
        config.gates.commands = []
        config.approvals.enabled = []
        config.gates.require_clean_git_before_task = False
        save_project_config(project_root, config)
        (project_root / ".conda" / "conda-meta").mkdir(parents=True, exist_ok=True)

    def test_plan_stage_retries_on_invalid_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingPlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            state = orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertEqual(orchestrator.adapter.plan_calls, 2)
            self.assertEqual(state.agent_attempts["plan"], 2)
            self.assertEqual(state.tasks[0].task_id, "task-001")

    def test_plan_stage_applies_generated_verification_commands_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = VerificationPlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            orchestrator._run_agent_stage("plan", state, spec_file)

            config = load_project_config(project_root)
            self.assertEqual(config.gates.commands, ["conda run -p ./.conda python -m pytest -q tests"])

    def test_plan_stage_expands_pytest_directory_steps_to_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(tests_dir / "test_alpha.py", "def test_alpha():\n    assert True\n")
            write_text(tests_dir / "test_beta.py", "def test_beta():\n    assert True\n")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = VerificationPlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            orchestrator._run_agent_stage("plan", state, spec_file)

            config = load_project_config(project_root)
            self.assertEqual(
                config.gates.commands,
                [
                    "conda run -p ./.conda python -m pytest -q tests/test_alpha.py",
                    "conda run -p ./.conda python -m pytest -q tests/test_beta.py",
                ],
            )
            self.assertEqual(
                [step.targets for step in config.gates.steps],
                [["tests/test_alpha.py"], ["tests/test_beta.py"]],
            )

    def test_gate_run_syncs_drifted_config_commands_from_task_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "shell",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Keep verification synced",
                            "description": "Exercise drift recovery.",
                            "acceptance": ["Gate config is synced before verify."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            config = load_project_config(project_root)
            config.gates.commands = ["false"]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            gate, mutation_error = orchestrator._run_gate_commands(
                collect_all=False,
                context="test gate commands",
            )

            self.assertFalse(mutation_error)
            self.assertTrue(gate.ok, msg=gate.summary)
            self.assertEqual([item.command for item in gate.commands], ["true"])
            self.assertEqual(load_project_config(project_root).gates.commands, ["true"])

    def test_gate_run_ignores_orchestrator_failed_verification_log_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            diagnostic_path = (
                ".auto-agents/failed-verification-logs/task-verify-default.log"
            )
            config = load_project_config(project_root)
            config.gates.commands = [
                "python -c \"from pathlib import Path; "
                f"p = Path('{diagnostic_path}'); "
                "p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text('failure details', encoding='utf-8')\""
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            gate, mutation_error = orchestrator._run_gate_commands(
                collect_all=False,
                context="verify stage commands",
            )

            self.assertTrue(gate.ok, msg=gate.summary)
            self.assertEqual(mutation_error, "")
            self.assertTrue((project_root / diagnostic_path).exists())

    def test_explicit_gate_run_filters_only_orchestrator_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            diagnostic_path = (
                ".auto-agents/failed-verification-logs/task-verify-explicit.log"
            )
            command = (
                "python -c \"from pathlib import Path; "
                f"p = Path('{diagnostic_path}'); "
                "p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text('failure details', encoding='utf-8'); "
                "Path('.auto-agents/verify-leak.txt').write_text('x', encoding='utf-8')\""
            )

            gate, mutation_error = orchestrator._run_gate_commands_for_commands(
                [command],
                collect_all=True,
                context="task verification commands",
            )

            self.assertTrue(gate.ok, msg=gate.summary)
            self.assertIn(".auto-agents/verify-leak.txt", mutation_error)
            self.assertNotIn(diagnostic_path, mutation_error)

    def test_validation_warns_when_gate_commands_drift_from_task_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "shell",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Keep verification synced",
                            "description": "Exercise drift warning.",
                            "acceptance": ["Validation surfaces gate drift."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            config = load_project_config(project_root)
            config.gates.commands = ["false"]
            save_project_config(project_root, config)

            report = validation_report(project_root)

            self.assertTrue(report["ok"], msg=str(report))
            self.assertTrue(
                any(
                    "gates.commands differ from task plan verification_commands" in warning
                    for warning in report["warnings"]
                ),
                msg=str(report["warnings"]),
            )

    def test_plan_stage_retries_when_verification_commands_reference_missing_pytest_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(tests_dir / "test_ok.py", "def test_ok():\n    assert True\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingVerificationCommandAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            orchestrator._run_agent_stage("plan", state, spec_file)

            config = load_project_config(project_root)
            self.assertEqual(orchestrator.adapter.plan_calls, 2)
            self.assertEqual(config.gates.commands, ["conda run -p ./.conda python -m pytest -q tests/test_ok.py"])

    def test_plan_stage_rejects_out_of_scope_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopePlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertIn("stage plan modified files outside its ownership", str(ctx.exception))
            self.assertIn("tests/test_stage_leak.py", str(ctx.exception))

    def test_plan_stage_ignores_orchestrator_failed_verification_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = PlanWithDiagnosticLogAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)

            state = orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertEqual(state.tasks[0].task_id, "task-001")
            self.assertTrue(
                (
                    project_root
                    / ".auto-agents"
                    / "failed-verification-logs"
                    / "verify-stage-test.log"
                ).exists()
            )

    def test_plan_stage_ignores_orchestrator_requirements_audit_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = PlanWithDiagnosticLogAdapter(
                project_root,
                write_requirements_audit=True,
            )

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)

            state = orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertEqual(state.tasks[0].task_id, "task-001")
            self.assertTrue(
                (project_root / ".auto-agents" / "docs" / "requirements_audit.md").exists()
            )

    def test_provider_research_rejects_out_of_scope_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Use official provider docs.",
                            "source": "spec.md",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["provider reference is verified"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": True,
                            "provider_reference": ".auto-agents/docs/provider_references/provider.md",
                            "notes": "",
                        }
                    ],
                },
            )
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopeProviderResearchAdapter(project_root)
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_provider_research(state, project_root / "spec.md")

            self.assertIn("stage provider_research modified files outside its ownership", str(ctx.exception))
            self.assertIn("tests/test_provider_stage_leak.py", str(ctx.exception))

    def test_review_stage_rejects_out_of_scope_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopeReviewAdapter(project_root)
            state = load_run_state(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_task_review(state.run_id, task)

            self.assertIn("stage review modified files outside its ownership", str(ctx.exception))
            self.assertIn("notes.txt", str(ctx.exception))

    def test_readme_proposal_stage_rejects_repository_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReadmeProposalMutationAdapter(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_readme(state, spec_file)

            self.assertIn("stage readme modified files outside its ownership during readme-propose", str(ctx.exception))
            self.assertIn("README.md", str(ctx.exception))

    def test_implement_stage_routes_auto_agents_mutation_to_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopeImplementAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            recovered = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(recovered.current_stage, "plan")
            self.assertEqual(recovered.rejected_stage, "plan")
            self.assertIn(".auto-agents/state/task_plan.json", recovered.rejection_reason)
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertIn(
                "\"task_id\": \"task-001\"",
                task_plan_path(project_root).read_text(encoding="utf-8"),
            )

    def test_implement_stage_policy_treats_input_specs_as_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = project_root / "custom-spec.md"
            state = load_run_state(project_root)

            allowed_scope, is_allowed = orchestrator._stage_mutation_policy(
                stage="implement",
                stage_key="implement-task-001",
                run_id=state.run_id,
            )

            self.assertTrue(is_allowed("src/app.py"))
            self.assertFalse(is_allowed("spec.md"))
            self.assertFalse(is_allowed("specs/2026-07-05-iter-01.md"))
            self.assertFalse(is_allowed("custom-spec.md"))
            self.assertIn("except input specs", "; ".join(allowed_scope))

            _repair_scope, repair_is_allowed = orchestrator._stage_mutation_policy(
                stage="implement",
                stage_key="implement-arbitrary-child-id",
                run_id=state.run_id,
                task_origin="evidence_repair",
            )
            self.assertTrue(repair_is_allowed(".auto-agents/state/task_plan.json"))
            self.assertFalse(
                repair_is_allowed(
                    ".auto-agents/docs/provider_references/provider.md"
                )
            )

    def test_implement_stage_allows_declared_public_spec_when_iteration_spec_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            state = load_run_state(project_root)

            _scope, is_allowed = orchestrator._stage_mutation_policy(
                stage="implement",
                stage_key="implement-task-001",
                run_id=state.run_id,
                mutable_artifacts=["spec.md"],
            )

            self.assertTrue(is_allowed("spec.md"))
            self.assertFalse(is_allowed("specs/iteration.md"))

    def test_task_mutable_artifacts_cannot_override_active_or_iteration_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            active_spec = project_root / "custom-spec.md"
            write_text(active_spec, "# Active input\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = active_spec.resolve()
            task = TaskSpec(
                task_id="task-001",
                title="Invalid ownership",
                description="Attempt to mutate protected inputs.",
                acceptance=["Protected inputs remain immutable."],
                mutable_artifacts=["custom-spec.md", "specs/history.md", "DESIGN.md"],
            )

            errors = orchestrator._task_mutable_artifact_errors(task)

            self.assertTrue(any("active input spec" in error for error in errors))
            self.assertTrue(any("immutable iteration spec" in error for error in errors))
            self.assertTrue(any("DESIGN.md" in error for error in errors))

    def test_declared_public_spec_survives_implementation_ownership_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            orchestrator.adapter = PublicSpecImplementAdapter(project_root)
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Update public product spec",
                            "description": "Synchronize the public product contract.",
                            "acceptance": ["spec.md contains the current contract"],
                            "mutable_artifacts": ["spec.md"],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            state = load_run_state(project_root)
            state.resume_context["spec_file"] = str(iteration_spec)
            state.tasks = orchestrator._load_tasks_from_plan()
            save_run_state(project_root, state)

            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(
                (project_root / "spec.md").read_text(encoding="utf-8"),
                "# Updated public product spec\n",
            )
            self.assertIn("Immutable run input spec: specs/iteration.md", orchestrator.adapter.implement_prompt)
            self.assertIn("Current task explicitly owns", orchestrator.adapter.implement_prompt)

    def test_legacy_blocked_doc_task_recovers_public_spec_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            task = TaskSpec(
                task_id="task-docs",
                title="Synchronize public docs",
                description="Keep public duration docs current.",
                acceptance=["spec.md and README.md define the public contract"],
                status="blocked",
                review_summary="AssertionError: spec.md misses requested_duration_sec",
                requirement_proofs=[{"evidence_refs": ["spec.md", "README.md"]}],
            )

            self.assertEqual(orchestrator._effective_task_mutable_artifacts(task), ["spec.md"])

    def test_stage_recovery_inherits_only_previously_authorized_implicated_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            write_text(project_root / "spec.md", "# Public contract\n")
            write_text(project_root / "tests" / "test_docs.py", "def test_docs(): pass\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            owner = TaskSpec(
                task_id="task-docs",
                title="Synchronize public docs",
                description="Keep public duration docs current.",
                acceptance=["spec.md defines the public contract"],
                status="done",
                review_summary="AssertionError: spec.md misses requested_duration_sec",
                requirement_proofs=[{"evidence_refs": ["spec.md"]}],
                verification_refs=["tests/test_docs.py::test_docs"],
            )
            recovery = TaskSpec(
                task_id="fix-rejection-1",
                title="Fix full verification failure",
                description="Full verification failed in tests/test_docs.py.",
                acceptance=["Tests pass"],
                status="blocked",
                task_origin="stage_recovery",
                verification_refs=[
                    "cmd:./.conda/bin/python -m pytest -q tests/test_docs.py"
                ],
                recovery_history=[
                    {"review": "AssertionError: spec.md misses requested_duration_sec"}
                ],
            )

            repaired = orchestrator._backfill_mutable_artifact_ownership(
                [owner, recovery]
            )

            self.assertEqual(owner.mutable_artifacts, ["spec.md"])
            self.assertEqual(recovery.mutable_artifacts, ["spec.md"])
            self.assertEqual(repaired, ["task-docs", "fix-rejection-1"])

    def test_recovery_feedback_cannot_grant_unowned_or_immutable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            write_text(project_root / "spec.md", "# Public contract\n")
            write_text(project_root / "tests" / "test_other.py", "def test_other(): pass\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            owner = TaskSpec(
                task_id="task-docs",
                title="Synchronize public docs",
                description="Synchronize the public contract.",
                acceptance=["Public contract is current."],
                mutable_artifacts=["spec.md"],
                verification_refs=["tests/test_docs.py::test_docs"],
            )

            inherited = orchestrator._recovery_mutable_artifacts(
                [owner],
                feedback=(
                    "specs/iteration.md misses a clause; DESIGN.md and secrets.md "
                    "also need edits"
                ),
                verification_refs=["tests/test_other.py::test_other"],
            )

            self.assertEqual(inherited, [])

    def test_split_children_inherit_parent_mutable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            parent = TaskSpec(
                task_id="task-docs",
                title="Synchronize public docs",
                description="Synchronize the public contract.",
                acceptance=["Public contract is current."],
                mutable_artifacts=["spec.md"],
            )
            child = TaskSpec(
                task_id="task-docs-a",
                title="Synchronize the public spec",
                description="Implement the split documentation slice.",
                acceptance=["Public contract is current."],
                parent_task_id="task-docs",
                task_origin="scope_split",
            )

            repaired = orchestrator._inherit_plan_replacement_mutable_artifacts(
                [parent], [child]
            )

            self.assertEqual(repaired, ["task-docs-a"])
            self.assertEqual(child.mutable_artifacts, ["spec.md"])

    def test_persisted_stage_recovery_can_update_inherited_public_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            write_text(project_root / "tests" / "test_docs.py", "def test_docs(): pass\n")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            orchestrator.adapter = PublicSpecImplementAdapter(project_root)
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-docs",
                            "title": "Synchronize public docs",
                            "description": "Keep public duration docs current.",
                            "acceptance": ["spec.md defines the public contract"],
                            "status": "done",
                            "commit_message": "",
                            "requirement_proofs": [{"evidence_refs": ["spec.md"]}],
                            "verification_refs": ["tests/test_docs.py::test_docs"],
                            "review_summary": "spec.md misses requested_duration_sec",
                        },
                        {
                            "task_id": "fix-rejection-1",
                            "title": "Fix full verification failure",
                            "description": "Full verification reports spec.md misses the contract.",
                            "acceptance": ["Tests pass"],
                            "status": "pending",
                            "commit_message": "",
                            "task_origin": "stage_recovery",
                            "verification_refs": [],
                            "mutable_artifacts": [],
                        },
                    ]
                },
            )
            commit_all(project_root, "test: seed recovery ownership baseline")
            state = load_run_state(project_root)
            state.resume_context["spec_file"] = str(iteration_spec)
            state.tasks = orchestrator._load_tasks_from_plan()
            save_run_state(project_root, state)

            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            recovery = next(
                task for task in result.tasks if task.task_id == "fix-rejection-1"
            )
            self.assertEqual(recovery.status, "done")
            self.assertEqual(recovery.mutable_artifacts, ["spec.md"])
            self.assertEqual(
                (project_root / "spec.md").read_text(encoding="utf-8"),
                "# Updated public product spec\n",
            )
            self.assertIn(
                "Current task explicitly owns these otherwise-protected public artifacts: spec.md",
                orchestrator.adapter.implement_prompt,
            )
            persisted = json.loads(
                task_plan_path(project_root).read_text(encoding="utf-8")
            )
            persisted_recovery = next(
                task
                for task in persisted["tasks"]
                if task["task_id"] == "fix-rejection-1"
            )
            self.assertEqual(persisted_recovery["mutable_artifacts"], ["spec.md"])

    def test_new_stage_recovery_persists_inherited_artifacts_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-docs",
                            "title": "Synchronize public docs",
                            "description": "Synchronize the public contract.",
                            "acceptance": ["Public contract is current."],
                            "status": "done",
                            "commit_message": "",
                            "mutable_artifacts": ["spec.md"],
                        }
                    ]
                },
            )
            state = load_run_state(project_root)
            state.resume_context["spec_file"] = str(iteration_spec)
            state.status = "paused"
            state.rejected_stage = "implement"
            state.rejection_reason = (
                "Failure type: full_verification\n"
                "AssertionError: spec.md misses requested_duration_sec"
            )

            with patch.object(orchestrator, "_commit_planning_baseline_if_needed"):
                result = orchestrator._run_implementation_loop(state, max_tasks=0)

            recovery = result.tasks[-1]
            self.assertEqual(recovery.task_origin, "stage_recovery")
            self.assertEqual(recovery.mutable_artifacts, ["spec.md"])
            persisted = json.loads(
                task_plan_path(project_root).read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["tasks"][-1]["mutable_artifacts"], ["spec.md"])

    def test_self_requeue_history_does_not_reclassify_stage_recovery_as_repair(self) -> None:
        stage_recovery = TaskSpec(
            task_id="fix-stage-recovery",
            title="Fix full verification failure",
            description=(
                "Full verification failed after all planned tasks were implemented.\n\n"
                "Feedback:\nThe public contract is stale."
            ),
            acceptance=["Tests pass"],
            task_origin="evidence_repair",
            recovery_history=[
                {
                    "result": "requeued",
                    "repair_task_ids": ["fix-stage-recovery", "repair-child"],
                }
            ],
        )
        repair = TaskSpec(
            task_id="repair-child",
            title="Repair proof evidence",
            description="Repair the failing proof.",
            acceptance=["Proof passes"],
            parent_task_id="fix-stage-recovery",
        )

        changed = Orchestrator._normalize_task_origins(
            [stage_recovery, repair]
        )

        self.assertTrue(changed)
        self.assertEqual(stage_recovery.task_origin, "stage_recovery")
        self.assertEqual(repair.task_origin, "evidence_repair")

    def test_legacy_recovery_cursor_migration_keeps_epoch_round_coherent(self) -> None:
        task = TaskSpec.from_dict(
            {
                "task_id": "legacy-recovery",
                "title": "Recover observable evidence",
                "description": "Repair the current evidence lifecycle.",
                "acceptance": ["The current proof passes."],
                "status": "pending",
                "commit_message": "",
                "task_origin": "scope_split",
                "recovery_history": [
                    {
                        "epoch": 0,
                        "round": 2,
                        "result": "requeued",
                    },
                    {
                        "epoch": 3,
                        "round": 1,
                        "result": "requeued",
                    },
                    {
                        "epoch": 3,
                        "round": 3,
                        "result": "exhausted",
                    },
                ],
            }
        )

        changed = Orchestrator._normalize_task_origins([task])

        self.assertTrue(changed)
        self.assertEqual((task.recovery_epoch, task.recovery_round), (3, 1))
        self.assertNotIn("_recovery_cursor_metadata_present", task.to_dict())
        self.assertFalse(Orchestrator._normalize_task_origins([task]))

    def test_explicit_recovery_cursor_is_not_rederived_from_history(self) -> None:
        task = TaskSpec.from_dict(
            {
                "task_id": "current-recovery",
                "title": "Recover observable evidence",
                "description": "Repair the current evidence lifecycle.",
                "acceptance": ["The current proof passes."],
                "status": "pending",
                "commit_message": "",
                "task_origin": "scope_split",
                "recovery_epoch": 3,
                "recovery_round": 0,
                "recovery_history": [
                    {
                        "epoch": 0,
                        "round": 2,
                        "result": "requeued",
                    },
                    {
                        "epoch": 3,
                        "round": 1,
                        "result": "requeued",
                    },
                ],
            }
        )

        changed = Orchestrator._normalize_task_origins([task])

        self.assertFalse(changed)
        self.assertEqual((task.recovery_epoch, task.recovery_round), (3, 0))

    def test_verify_implicated_paths_include_public_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(project_root / "spec.md", "# Public product spec\n")
            orchestrator = Orchestrator(project_root)

            paths = orchestrator._extract_verify_implicated_paths(
                "AssertionError: spec.md misses requested_duration_sec\n"
                "FAILED tests/test_docs.py::test_public_contract"
            )

            self.assertIn("spec.md", paths)

    def test_implement_stage_routes_task_plan_mutation_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RecoveringOutOfScopeImplementAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.tasks[0].status, "pending")
            self.assertEqual(result.current_stage, "plan")
            self.assertEqual(result.rejected_stage, "plan")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertIn(
                "\"task_id\": \"task-001\"",
                task_plan_path(project_root).read_text(encoding="utf-8"),
            )

    def test_implement_stage_restores_config_mutation_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            original_config = (project_root / ".auto-agents" / "config.json").read_text(encoding="utf-8")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RecoveringConfigMutationImplementAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(
                (project_root / ".auto-agents" / "config.json").read_text(encoding="utf-8"),
                original_config,
            )

    def test_implement_stage_routes_protected_spec_mutation_to_clarify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(project_root / "specs" / "2026-05-07-iter-01.md", "original spec\n")
            write_text(project_root / ".auto-agents" / "docs" / "review.md", "original review\n")
            original_review = (project_root / ".auto-agents" / "docs" / "review.md").read_text(encoding="utf-8")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = (
                project_root / "specs" / "2026-05-07-iter-01.md"
            ).resolve()
            orchestrator.adapter = RecoveringProtectedInputMutationImplementAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.tasks[0].status, "pending")
            self.assertEqual(result.current_stage, "clarify")
            self.assertEqual(result.rejected_stage, "clarify")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_input_before_review, "")
            self.assertEqual(
                (project_root / ".auto-agents" / "docs" / "review.md").read_text(encoding="utf-8"),
                original_review,
            )
            self.assertEqual(
                (project_root / "specs" / "2026-05-07-iter-01.md").read_text(encoding="utf-8"),
                "original spec\n",
            )

    def test_implement_retry_restores_staged_public_spec_index_and_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(project_root / "spec.md", "# Public product spec\n")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            commit_all(project_root, "chore: seed staged restore test")

            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            adapter = RecoveringStagedPublicSpecMutationImplementAdapter(
                project_root
            )
            orchestrator.adapter = adapter
            state = load_run_state(project_root)

            result = orchestrator._run_agent_with_retries(
                state,
                "implement",
                "implement-task-001",
                "Update repository code and tests only.",
                run_id=state.run_id,
            )

            self.assertTrue(result.ok)
            self.assertEqual(adapter.implement_calls, 2)
            self.assertEqual(adapter.second_attempt_spec_status, "")
            self.assertEqual(
                (project_root / "spec.md").read_text(encoding="utf-8"),
                "# Public product spec\n",
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--short", "--", "spec.md"],
                    cwd=str(project_root),
                    check=True,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                ).stdout,
                "",
            )
            self.assertFalse(
                orchestrator._attempt_recovery_checkpoint_root(
                    state.run_id,
                    "implement-task-001",
                ).exists()
            )

    def test_self_repair_reconciles_durable_attempt_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(project_root / "spec.md", "# Public product spec\n")
            commit_all(project_root, "chore: seed checkpoint reconciliation")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            before = orchestrator._worktree_change_snapshot()
            checkpoint = orchestrator._attempt_recovery_checkpoint_root(
                state.run_id,
                "implement-task-001",
            )
            checkpoint.mkdir(parents=True, exist_ok=True)
            orchestrator._capture_auto_agents_restore_point(checkpoint)
            orchestrator._write_attempt_recovery_manifest(
                checkpoint,
                run_id=state.run_id,
                stage="implement",
                stage_key="implement-task-001",
                before_snapshot=before,
                offending_paths=["spec.md"],
            )
            write_text(project_root / "spec.md", "# Poisoned spec\n")
            subprocess.run(
                ["git", "add", "--", "spec.md"],
                cwd=str(project_root),
                check=True,
            )

            reconciled = (
                orchestrator._reconcile_self_repair_attempt_checkpoints(state)
            )

            self.assertEqual(reconciled, [str(checkpoint)])
            self.assertFalse(checkpoint.exists())
            self.assertEqual(
                (project_root / "spec.md").read_text(encoding="utf-8"),
                "# Public product spec\n",
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--short", "--", "spec.md"],
                    cwd=str(project_root),
                    check=True,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                ).stdout,
                "",
            )

    def test_implement_stage_restores_archived_task_plan_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            archive_path = archived_task_plan_path(project_root, "oldrun123")
            original_archive = {"tasks": [{"task_id": "archived-task", "status": "done"}]}
            write_json(archive_path, original_archive)

            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RecoveringHistoryMutationImplementAdapter(project_root, archive_path)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(load_task_plan(project_root)["tasks"][0]["status"], "done")
            self.assertEqual(json.loads(archive_path.read_text(encoding="utf-8")), original_archive)

    def test_task_verify_rejects_dirty_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = [
                "python -c \"from pathlib import Path; Path('verify-leak.txt').write_text('x', encoding='utf-8')\""
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            task = orchestrator._load_tasks_from_plan()[0]
            with patch.object(orchestrator, "_build_task_verify_commands", return_value=[]):
                result = orchestrator._run_task_verify(task)

            self.assertFalse(result["ok"])
            self.assertIn("task verification commands modified tracked or unignored files", str(result["reason"]))
            self.assertIn("verify-leak.txt", str(result["reason"]))

    def test_task_verify_runs_owned_proof_evidence_even_with_baseline_only_gate_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = ["python -c \"print('ERROR: test_legacy (tests.test_demo.LegacyTests.test_legacy)'); raise SystemExit(1)\""]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            from auto_agents.models import TaskSpec as _TaskSpec

            task = _TaskSpec(
                task_id="task-001",
                title="Verify proof evidence",
                description="Make sure proof evidence still runs after baseline-only verify.",
                acceptance=["proof evidence passes"],
                verify_baseline_failures=["test_legacy (tests.test_demo.LegacyTests.test_legacy)"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    }
                ],
            )

            proof_calls = []
            orchestrator._run_task_proof_evidence = lambda current_task: (
                proof_calls.append(current_task.task_id),
                {
                    "ok": False,
                    "reason": "owned proof evidence failed: tests/test_public_api.py::test_contract",
                    "summary": "Owned proof evidence failed (1 refs): tests/test_public_api.py::test_contract",
                    "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    "passed_refs": [],
                    "failed_refs": ["tests/test_public_api.py::test_contract"],
                    "failure_ids": ["tests/test_public_api.py::test_contract"],
                    "command": "conda run -p ./.conda python -m pytest -q tests/test_public_api.py::test_contract",
                    "raw_output": "FAILED tests/test_public_api.py::test_contract",
                },
            )[1]

            with patch.object(orchestrator, "_build_task_verify_commands", return_value=[]):
                result = orchestrator._run_task_verify(task)

            self.assertEqual(proof_calls, ["task-001"])
            self.assertFalse(result["ok"])
            self.assertIn("owned proof evidence failed", str(result["reason"]))
            self.assertEqual(
                result["failure_ids"],
                ["tests/test_public_api.py::test_contract"],
            )

    def test_task_verify_runs_identity_diagnostic_for_killed_pytest_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            verify_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command="conda run -p ./.conda python -m pytest -q tests",
                        ok=False,
                        returncode=137,
                        stdout="...................F...\n",
                        stderr="Killed\n",
                    )
                ],
                summary="command failed: killed pytest suite",
            )

            orchestrator._run_gate_commands = lambda **_kwargs: (verify_gate, None)
            task = TaskSpec(
                task_id="task-001",
                title="diagnose verify identity",
                description="",
                acceptance=[],
            )

            diagnostic_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=(
                            "conda run -p ./.conda python -m pytest -vv -rA --tb=short "
                            "-o console_output_style=classic tests"
                        ),
                        ok=False,
                        returncode=1,
                        stdout="tests/test_demo.py::test_example FAILED                         [100%]\n",
                        stderr="",
                    )
                ],
                summary="command failed: diagnostic found identity",
            )

            import auto_agents.orchestrator as orch_mod
            original_collect = orch_mod.run_commands_collect_all
            try:
                captured = {}

                def _fake_collect(commands, cwd, **_kwargs):
                    captured["commands"] = list(commands)
                    captured["cwd"] = cwd
                    return diagnostic_gate

                orch_mod.run_commands_collect_all = _fake_collect
                result = orchestrator._run_task_verify(task)
            finally:
                orch_mod.run_commands_collect_all = original_collect

            self.assertFalse(result["ok"])
            self.assertTrue(result["comparable_failures"])
            self.assertEqual(result["failure_ids"], ["tests/test_demo.py::test_example"])
            self.assertIn("identity diagnostic captured", str(result["reason"]))
            self.assertEqual(
                captured["commands"],
                [
                    "conda run -p ./.conda python -m pytest -vv -rA --tb=short -o console_output_style=classic tests"
                ],
            )

    def test_task_verify_does_not_call_stable_id_new_against_command_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            verify_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command="npm test",
                        ok=False,
                        returncode=1,
                        stdout=(
                            "FAIL src/e2e/create-modal.test.ts > "
                            "create_modal_close_and_submit_contract\n"
                        ),
                    )
                ],
                summary="command failed",
            )
            orchestrator._run_gate_commands = lambda **_kwargs: (verify_gate, "")
            task = TaskSpec(
                task_id="task-001",
                title="verify identity transition",
                description="",
                acceptance=[],
                verify_baseline_failures=["cmd:npm test"],
            )

            result = orchestrator._run_task_verify(task)

            self.assertFalse(result["ok"])
            self.assertTrue(result["comparable_failures"])
            self.assertFalse(result["baseline_comparison_comparable"])
            self.assertTrue(result["baseline_identity_transition"])
            self.assertEqual(result["new_failure_ids"], [])
            self.assertNotIn("new verification failure", result["reason"])
            self.assertIn("baseline comparison is non-comparable", result["reason"])

    def test_task_proof_evidence_supports_mixed_pytest_and_vitest_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            workbench_root = project_root / "workbench"
            (workbench_root / "src" / "components").mkdir(parents=True)
            write_text(
                workbench_root / "package.json",
                json.dumps(
                    {
                        "name": "demo-workbench",
                        "private": True,
                        "scripts": {"test": "vitest run"},
                        "devDependencies": {"vitest": "3.1.1"},
                    }
                ),
            )
            write_text(workbench_root / "vitest.config.ts", "export default {}\n")
            write_text(
                workbench_root / "src" / "components" / "project-detail-workbench.test.tsx",
                "export {};\n",
            )
            (project_root / "tests").mkdir()
            write_text(project_root / "tests" / "test_public_api.py", "def test_contract():\n    assert True\n")

            vitest_ref = (
                "workbench/src/components/project-detail-workbench.test.tsx::"
                "ProjectDetailWorkbench > 生成失败展示用户可理解原因和下一步动作"
            )
            task = TaskSpec(
                task_id="task-001",
                title="Verify mixed proof evidence",
                description="Make sure pytest and vitest proof refs both run.",
                acceptance=["proof evidence passes"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": [
                            "tests/test_public_api.py::test_contract",
                            vitest_ref,
                        ],
                    }
                ],
            )

            captured_commands = []
            captured_parallel_groups = []

            def fake_run(commands, parallel_groups, cwd, *, collect_all, **_kwargs):
                all_commands = list(commands)
                captured_parallel_groups.extend(parallel_groups)
                for group in parallel_groups:
                    all_commands.extend(group.commands)
                captured_commands.extend(all_commands)
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=command,
                            ok=True,
                            returncode=0,
                            stdout="",
                            stderr="",
                        )
                        for command in all_commands
                    ],
                    summary="all commands passed",
                )

            with patch("auto_agents.orchestrator.run_gate_plan", side_effect=fake_run):
                result = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(result["ok"])
            self.assertEqual(captured_parallel_groups, [])
            self.assertEqual(
                result["passed_refs"],
                ["tests/test_public_api.py::test_contract", vitest_ref],
            )
            self.assertEqual(len(captured_commands), 2)
            self.assertIn("pytest -q tests/test_public_api.py::test_contract", captured_commands[0])
            self.assertIn("npm --prefix workbench test --", captured_commands[1])
            self.assertIn("src/components/project-detail-workbench.test.tsx", captured_commands[1])
            self.assertIn("-t", captured_commands[1])
            self.assertIn("ProjectDetailWorkbench > 生成失败展示用户可理解原因和下一步动作", captured_commands[1])

    def test_task_proof_evidence_keeps_unittest_node_ids_and_skips_source_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Verify source-backed proof evidence",
                description="Run executable tests and keep source symbols as supporting evidence.",
                acceptance=["proof evidence passes"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": [
                            "tests/test_openai_strict_schema_contract.py::OpenAIStrictSchemaContractTests::test_planning_schema_passes_openai_strict_contract",
                            "app/stage_backends/text.py::OpenAICompatiblePlanningBackend._planning_schema",
                            "app/application/openai_strict_schema.py::ensure_openai_strict_json_schema",
                            ".auto-agents/docs/architecture.md",
                        ],
                    }
                ],
            )

            captured_commands = []

            def fake_run(commands, parallel_groups, cwd, *, collect_all, **_kwargs):
                all_commands = list(commands)
                for group in parallel_groups:
                    all_commands.extend(group.commands)
                captured_commands.extend(all_commands)
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=command,
                            ok=True,
                            returncode=0,
                            stdout="",
                            stderr="",
                        )
                        for command in all_commands
                    ],
                    summary="all commands passed",
                )

            with patch("auto_agents.orchestrator.run_gate_plan", side_effect=fake_run):
                result = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(result["ok"])
            self.assertEqual(len(captured_commands), 1)
            self.assertIn(
                "tests/test_openai_strict_schema_contract.py::OpenAIStrictSchemaContractTests::"
                "test_planning_schema_passes_openai_strict_contract",
                captured_commands[0],
            )
            self.assertNotIn("app/stage_backends/text.py", captured_commands[0])
            self.assertEqual(
                result["supporting_refs"],
                [
                    "app/stage_backends/text.py::OpenAICompatiblePlanningBackend._planning_schema",
                    "app/application/openai_strict_schema.py::ensure_openai_strict_json_schema",
                    ".auto-agents/docs/architecture.md",
                ],
            )
            self.assertEqual(result["failed_refs"], [])

    def test_task_proof_evidence_runs_command_refs_and_keeps_frontend_assets_supporting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            command_ref = "cmd:npm --prefix workbench test -- src/e2e/home.test.ts -t desktop"
            screenshot_ref = ".tmp-tests/frontend-prototype/home-desktop-1440x900.png"
            prototype_ref = "specs/frondend_prototype/home.html"
            css_ref = "workbench/app/globals.css"
            task = TaskSpec(
                task_id="task-001",
                title="Verify frontend proof evidence",
                description="Run owned visual proof command and keep assets as evidence.",
                acceptance=["proof evidence passes"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": [
                            command_ref,
                            screenshot_ref,
                            prototype_ref,
                            css_ref,
                        ],
                    }
                ],
            )

            captured_commands = []

            def fake_run(commands, parallel_groups, cwd, *, collect_all, **_kwargs):
                all_commands = list(commands)
                for group in parallel_groups:
                    all_commands.extend(group.commands)
                captured_commands.extend(all_commands)
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=command,
                            ok=True,
                            returncode=0,
                            stdout="",
                            stderr="",
                        )
                        for command in all_commands
                    ],
                    summary="all commands passed",
                )

            with patch("auto_agents.orchestrator.run_gate_plan", side_effect=fake_run):
                result = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(result["ok"], msg=str(result))
            self.assertEqual(
                captured_commands,
                ["npm --prefix workbench test -- src/e2e/home.test.ts -t desktop"],
            )
            self.assertEqual(
                result["supporting_refs"],
                [screenshot_ref, prototype_ref, css_ref],
            )
            self.assertEqual(result["failed_refs"], [])

    def test_vitest_selector_ref_reuses_configured_artifact_producer_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.steps.append(
                VerificationStep(
                    kind="test",
                    runner="vitest",
                    targets=["workbench/src/e2e/browser-verification.test.ts"],
                    args=["-t", "publishes_current_run"],
                    artifact_globs=[".tmp-tests/runs/*/*.json"],
                )
            )

            command = orchestrator._build_task_proof_evidence_command_for_ref(
                "workbench/src/e2e/browser-verification.test.ts::"
                "publishes_current_run"
            )

            self.assertEqual(
                command,
                "npm exec -- vitest run -t publishes_current_run "
                "workbench/src/e2e/browser-verification.test.ts",
            )

    def test_vitest_selector_ref_falls_back_to_configured_full_file_producer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.steps.append(
                VerificationStep(
                    kind="test",
                    runner="vitest",
                    targets=["workbench/src/e2e/browser-verification.test.ts"],
                    artifact_globs=[".tmp-tests/runs/*/*.json"],
                )
            )

            command = orchestrator._build_task_proof_evidence_command_for_ref(
                "workbench/src/e2e/browser-verification.test.ts::"
                "another_current_run_contract"
            )

            self.assertEqual(
                command,
                "npm exec -- vitest run "
                "workbench/src/e2e/browser-verification.test.ts",
            )

    def test_ignored_exact_proof_ref_requires_current_isolated_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            (project_root / ".gitignore").write_text(".tmp-tests/\n", encoding="utf-8")
            evidence = project_root / ".tmp-tests" / "runs" / "old" / "receipt.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text('{"run_id":"old"}\n', encoding="utf-8")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.isolation.enabled = True
            task = TaskSpec(
                task_id="task-portability",
                title="Portable evidence",
                description="Require current isolated evidence.",
                acceptance=["proof is portable"],
                requirement_proofs=[
                    {
                        "status": "verified",
                        "evidence_refs": [
                            ".tmp-tests/runs/old/receipt.json",
                        ],
                    }
                ],
            )

            failure = (
                orchestrator._ignored_supporting_evidence_portability_failure(
                    task,
                    GateResult(ok=True, commands=[]),
                )
            )

            self.assertIsNotNone(failure)
            self.assertIn("nonportable_ignored_evidence", failure["failure_ids"][0])

    def test_ignored_wildcard_proof_ref_accepts_current_isolated_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            (project_root / ".gitignore").write_text(".tmp-tests/\n", encoding="utf-8")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.isolation.enabled = True
            task = TaskSpec(
                task_id="task-portability",
                title="Portable evidence",
                description="Require current isolated evidence.",
                acceptance=["proof is portable"],
                requirement_proofs=[
                    {
                        "status": "verified",
                        "evidence_refs": [
                            ".tmp-tests/runs/*/receipt.json",
                        ],
                    }
                ],
            )
            gate = GateResult(
                ok=True,
                commands=[
                    CommandResult(
                        command="npm test",
                        ok=True,
                        returncode=0,
                        artifacts={
                            ".tmp-tests/runs/current/receipt.json": "abc123",
                        },
                    )
                ],
            )

            failure = (
                orchestrator._ignored_supporting_evidence_portability_failure(
                    task,
                    gate,
                )
            )

            self.assertIsNone(failure)

    def test_ignored_proof_portability_is_backward_compatible_without_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            (project_root / ".gitignore").write_text(".tmp-tests/\n", encoding="utf-8")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.isolation.enabled = False
            task = TaskSpec(
                task_id="task-portability",
                title="Portable evidence",
                description="Shared worktree compatibility.",
                acceptance=["proof exists"],
                requirement_proofs=[
                    {
                        "status": "verified",
                        "evidence_refs": [".tmp-tests/runs/old/receipt.json"],
                    }
                ],
            )

            failure = (
                orchestrator._ignored_supporting_evidence_portability_failure(
                    task,
                    GateResult(ok=True, commands=[]),
                )
            )

            self.assertIsNone(failure)

    def test_task_proof_evidence_cache_key_changes_when_refs_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Verify cache invalidation",
                description="Do not reuse proof evidence after refs are updated.",
                acceptance=["proof evidence passes"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": ["tests/test_public_api.py::test_first"],
                    }
                ],
            )

            captured_commands = []

            def fake_run(commands, parallel_groups, cwd, *, collect_all, **_kwargs):
                all_commands = list(commands)
                for group in parallel_groups:
                    all_commands.extend(group.commands)
                captured_commands.extend(all_commands)
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=command,
                            ok=True,
                            returncode=0,
                            stdout="",
                            stderr="",
                        )
                        for command in all_commands
                    ],
                    summary="all commands passed",
                )

            with patch("auto_agents.orchestrator.run_gate_plan", side_effect=fake_run):
                first = orchestrator._run_task_proof_evidence(task)
                task.requirement_proofs[0]["evidence_refs"] = ["tests/test_public_api.py::test_second"]
                second = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(len(captured_commands), 2)
            self.assertIn("tests/test_public_api.py::test_first", captured_commands[0])
            self.assertIn("tests/test_public_api.py::test_second", captured_commands[1])

    def test_visual_judge_top_level_stage_records_task_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-visual",
                title="Visual task",
                description="Task with task-level visual judge evidence.",
                acceptance=["visual judge is recorded"],
                status="done",
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": [
                            ".auto-agents/runs/run-1/visual_judge/task-visual/report.json",
                        ],
                    }
                ],
            )
            state = RunState(run_id="run-1", tasks=[task], last_error="Unsupported stage: visual_judge")

            updated = orchestrator._run_visual_judge_stage(state)

            self.assertEqual(updated.current_stage, "visual_judge")
            self.assertEqual(updated.last_error, "")
            self.assertIn("reports=1", updated.stage_summaries["visual_judge"])

    def test_persisted_tasks_keep_generated_verification_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ],
                },
            )

            tasks = orchestrator._load_tasks_from_plan()
            tasks[0].status = "in_progress"
            orchestrator._persist_tasks(tasks)

            payload = task_plan_path(project_root).read_text(encoding="utf-8")
            self.assertIn('"test_strategy": "python-pytest"', payload)
            self.assertIn('"verification_steps": [', payload)

    def test_implement_stage_retries_after_review_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingImplementAdapter(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingImplementAdapter(project_root)

            state = load_run_state(project_root)
            state.tasks = []
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "good")
            reloaded_state = load_run_state(project_root)
            self.assertEqual(reloaded_state.tasks[0].status, "done")

    def test_resume_in_progress_task_skips_reimplementation_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ResumeReviewAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("hello\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertTrue(state.tasks[0].commit_sha)

    def test_implementation_loop_prefers_task_plan_when_run_state_tasks_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ResumeReviewAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("hello\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec.from_dict(
                    {
                        "task_id": "task-001",
                        "title": "Write artifact",
                        "description": "Write the artifact file.",
                        "acceptance": ["artifact.txt contains hello"],
                        "status": "pending",
                        "commit_message": "",
                        "test_generated": True,
                    }
                )
            ]
            state.agent_attempts["implement-task-001"] = 1

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            reloaded_state = load_run_state(project_root)
            self.assertEqual(reloaded_state.tasks[0].status, "done")

    def test_review_stage_cleans_ephemeral_tsbuildinfo_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.gates.require_clean_git_before_task = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewTsBuildInfoAdapter(project_root)

            workbench = project_root / "workbench"
            workbench.mkdir(exist_ok=True)
            tsbuildinfo_path = workbench / "tsconfig.tsbuildinfo"
            write_text(tsbuildinfo_path, '{"version":"incremental-1"}\n')
            commit_all(project_root, "chore: seed tsbuildinfo")

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(tsbuildinfo_path.read_text(encoding="utf-8").strip(), '{"version":"incremental-1"}')
            status = subprocess.run(
                ["git", "status", "--short", "--", "workbench/tsconfig.tsbuildinfo"],
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_review_stage_cleans_untracked_python_build_lib_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.gates.require_clean_git_before_task = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewBuildLibAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertFalse((project_root / "build" / "lib" / "app" / "__init__.py").exists())
            status = subprocess.run(
                ["git", "status", "--short", "--", "build"],
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_blocked_task_can_retry_with_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = BlockedRetryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "blocked",
                            "commit_message": "",
                            "review_summary": "previous review failure",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("bad\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "fixed")

    def test_interrupted_in_progress_task_preserves_dirty_tree_and_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Continue artifact",
                            "description": "Resume an interrupted implementation.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text(
                "partial implementation\n",
                encoding="utf-8",
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.current_stage = "implement"
            state.status = "blocked"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "synthetic_task_lifecycle",
                "status": "blocked",
            }
            orchestrator._set_implementation_ready_marker(
                state,
                state.tasks[0],
                False,
            )
            save_run_state(project_root, state)

            state = orchestrator.mark_self_repair_applied("repair-commit")
            resumed = Orchestrator(project_root)
            resumed.adapter = BlockedRetryAdapter(project_root)
            self.assertTrue(resumed._resume_blocked_run(state))
            self.assertNotIn(
                "parallel_sequential_retry_tasks",
                state.resume_context,
            )

            state = resumed._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(resumed.adapter.implement_calls, 1)
            self.assertEqual(resumed.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(
                (project_root / "artifact.txt").read_text(encoding="utf-8").strip(),
                "fixed",
            )
            self.assertNotIn(
                "task-001",
                state.resume_context.get("implementation_ready_tasks", {}),
            )

    def test_orchestrator_requeued_task_uses_dirty_sequential_retry_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = BlockedRetryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Continue artifact",
                            "description": (
                                "Continue the orchestrator-owned implementation."
                            ),
                            "acceptance": ["artifact.txt contains fixed"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text(
                "partial implementation\n",
                encoding="utf-8",
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.resume_context["parallel_sequential_retry_tasks"] = ["task-001"]

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(
                (project_root / "artifact.txt").read_text(encoding="utf-8").strip(),
                "fixed",
            )
            self.assertNotIn(
                "parallel_sequential_retry_tasks",
                state.resume_context,
            )

    def test_review_requeue_owns_dirty_tree_before_next_pending_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            config.execution.recovery.max_rounds = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "next-task",
                            "title": "Start unrelated work",
                            "description": "This task requires a clean worktree.",
                            "acceptance": ["Unrelated work is complete."],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        },
                        {
                            "task_id": "reviewed-task",
                            "title": "Continue reviewed work",
                            "description": "Address the latest review feedback.",
                            "acceptance": ["The reviewed artifact is corrected."],
                            "depends_on": [],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        },
                    ]
                },
            )
            commit_all(project_root, "test: seed review recovery")
            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            reviewed = state.tasks[1]
            baseline_ref = head_ref(project_root)
            write_text(project_root / "artifact.txt", "retained candidate\n")

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "CONTINUE",
                    "reason": "One bounded correction remains.",
                    "actionable_items": ["Correct the reviewed artifact."],
                    "split_axis": [],
                    "source": "provider",
                },
            ):
                requeued = orchestrator._recover_review_rejected_task(
                    state,
                    state.tasks,
                    reviewed,
                    {
                        "reason": "review rejected the task",
                        "review": "The reviewed artifact still needs correction.",
                    },
                )

            self.assertTrue(requeued)
            self.assertEqual(
                state.resume_context["parallel_sequential_retry_tasks"],
                [reviewed.task_id],
            )

            # Simulate a persisted requeue written by an older orchestrator,
            # before retry ownership was recorded separately from the route.
            state.resume_context.pop("parallel_sequential_retry_tasks")
            save_run_state(project_root, state)

            resumed = Orchestrator(project_root)
            resumed.adapter = BlockedRetryAdapter(project_root)
            result = resumed._run_implementation_loop(
                load_run_state(project_root),
                max_tasks=1,
            )

            by_id = {task.task_id: task for task in result.tasks}
            self.assertEqual(by_id["reviewed-task"].status, "done")
            self.assertEqual(by_id["next-task"].status, "pending")
            self.assertEqual(resumed.adapter.implement_calls, 1)
            commit_count = subprocess.run(
                ["git", "rev-list", "--count", f"{baseline_ref}..HEAD"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(commit_count, "1")
            self.assertNotIn(
                "parallel_sequential_retry_tasks",
                result.resume_context,
            )

    def test_pending_task_reports_changed_paths_when_clean_tree_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "notes.txt").write_text("dirty\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            message = str(ctx.exception)
            self.assertIn("task task-001", message)
            self.assertIn("notes.txt", message)
            self.assertIn("--allow-dirty-tree", message)

    def test_pending_task_can_run_with_allow_dirty_tree_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = BlockedRetryAdapter(project_root)
            orchestrator._allow_dirty_tree = True

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "notes.txt").write_text("dirty\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "fixed")

    def test_pending_repair_task_can_run_with_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = BlockedRetryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "repair-task-001-r1-1",
                            "title": "Repair proof evidence",
                            "description": "Repair failed verification evidence.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-001",
                            "task_origin": "evidence_repair",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "notes.txt").write_text("dirty parent-task context\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "fixed")

    def test_repair_task_routes_provider_reference_mutation_to_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ProviderReferenceRepairAdapter(project_root)

            task = TaskSpec(
                task_id="repair-task-001-r1-1",
                title="Repair provider reference proof evidence",
                description="Update the canonical provider reference evidence.",
                acceptance=["provider reference records gpt-image-2-vip"],
                status="pending",
                parent_task_id="task-001",
                task_origin="evidence_repair",
                commit_message="",
            )
            state = load_run_state(project_root)
            state.tasks = [task]

            result = orchestrator._execute_task_with_retries(state, task)

            reference_path = (
                project_root
                / ".auto-agents"
                / "docs"
                / "provider_references"
                / "apiyi_gpt_image_2.md"
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["rewind_to_stage"], "provider_research")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 0)
            self.assertFalse(reference_path.exists())

    def test_repair_task_can_update_task_plan_proof_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = TaskPlanRepairAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Parent task",
                            "description": "Already implemented parent.",
                            "acceptance": ["proof refs are canonical"],
                            "status": "done",
                            "commit_message": "",
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "status": "verified",
                                    "evidence_refs": ["specs/provider_references/old.md"],
                                }
                            ],
                        },
                        {
                            "task_id": "repair-task-001-r1-1",
                            "title": "Repair proof evidence",
                            "description": "Update parent task proof evidence refs.",
                            "acceptance": ["task plan proof refs are canonical"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-001",
                            "task_origin": "evidence_repair",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            payload = load_task_plan(project_root)
            parent = next(
                item for item in payload["tasks"] if item["task_id"] == "task-001"
            )
            repair = next(
                item
                for item in payload["tasks"]
                if item["task_id"] == "repair-task-001-r1-1"
            )
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(repair["status"], "done")
            self.assertEqual(
                parent["requirement_proofs"][0]["evidence_refs"],
                [".auto-agents/docs/provider_references/apiyi_gpt_image_2.md"],
            )

    def test_verify_failure_skips_review_and_retries_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; artifact = Path('artifact.txt'); "
                    "raise SystemExit(0 if artifact.exists() and artifact.read_text().strip() == 'good' else "
                    "(1 if artifact.exists() else 0))\""
                )
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = VerifyBeforeReviewAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_resume_reuses_cached_pass_review_for_unchanged_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = CachedReviewResumeAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("hello\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state.task_review_cache["task-001"] = {
                "fingerprint": worktree_fingerprint(project_root),
                "decision": "pass",
                "summary": "cached review passed",
            }
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 0)
            self.assertEqual(state.tasks[0].status, "done")

    def test_small_test_only_review_uses_balanced_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.efforts["review"] = "balanced"
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewEffortAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Update tests",
                            "description": "Adjust coverage.",
                            "acceptance": ["tests updated"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_efforts, ["balanced"])
            self.assertEqual(state.tasks[0].status, "done")

    def test_code_change_without_tests_escalates_review_to_deep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.efforts["review"] = "balanced"
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewEffortAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Update app",
                            "description": "Adjust behavior.",
                            "acceptance": ["app updated"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            src_dir = project_root / "src"
            src_dir.mkdir(exist_ok=True)
            (src_dir / "app.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_efforts, ["deep"])
            self.assertEqual(state.tasks[0].status, "done")

    def test_retry_feedback_uses_structured_failure_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; "
                    "raise SystemExit(1 if Path('artifact.txt').exists() else 0)\""
                )
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryFeedbackAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(len(orchestrator.adapter.implement_prompts), 2)
            self.assertIn("Failure type: local_verification", orchestrator.adapter.implement_prompts[1])
            self.assertIn("Verification triage:", orchestrator.adapter.implement_prompts[1])
            self.assertIn("Do not dismiss tightly coupled regressions", orchestrator.adapter.implement_prompts[1])
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_task_verify_baseline_ignores_preexisting_failure_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"print('ERROR: test_legacy (tests.test_demo.LegacyTests.test_legacy)'); "
                    "raise SystemExit(1)\""
                )
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = BlockedRetryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[0].verify_baseline_failures, [
                "test_legacy (tests.test_demo.LegacyTests.test_legacy)"
            ])
            self.assertIn("task baseline only: 1 pre-existing failure(s) remain", stream.getvalue())

    def test_repair_verification_refs_are_not_accepted_as_baseline_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            ref = "tests/test_contract.py::ContractTests::test_provider_reference"
            (project_root / "tests").mkdir()
            write_text(
                project_root / "tests" / "test_contract.py",
                "import unittest\n\n"
                "class ContractTests(unittest.TestCase):\n"
                "    def test_provider_reference(self):\n"
                "        self.assertTrue(False)\n",
            )
            task = TaskSpec(
                task_id="repair-task-001-r1-1",
                title="Repair proof evidence",
                description="Fix the proof evidence assertion.",
                acceptance=["The proof evidence ref passes."],
                task_origin="evidence_repair",
                verification_refs=[ref],
                verify_baseline_failures=[ref],
            )

            def fake_failing_owned_ref(commands, *, collect_all, context):
                return (
                    GateResult(
                        ok=False,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=False,
                                returncode=1,
                                stdout=f"FAILED {ref} - AssertionError\n",
                                stderr="",
                            )
                        ],
                        summary=f"command failed: {commands[0]}",
                    ),
                    "",
                )

            with patch.object(
                orchestrator,
                "_run_gate_commands_for_commands",
                side_effect=fake_failing_owned_ref,
            ):
                result = orchestrator._run_task_verify(task)

            self.assertFalse(result["ok"])
            self.assertEqual(result["failure_ids"], [ref])
            self.assertNotIn("task baseline only", str(result["reason"]))

    def test_task_verify_rewind_is_propagated_before_retrying_implement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-102",
                title="Document provider terminology",
                description="Clarify the public request contract.",
                acceptance=["The requirements audit contains explicit contract wording."],
                status="in_progress",
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            verify_result = {
                "ok": False,
                "reason": "derived audit evidence failed",
                "failure_ids": ["REQ-102"],
                "comparable_failures": True,
                "rewind_to_stage": "clarify",
                "rewind_reason": "requirements trace owns the missing wording",
            }

            with patch.object(
                orchestrator,
                "_run_task_verify",
                return_value=verify_result,
            ):
                result = orchestrator._execute_task_with_retries(
                    state,
                    task,
                    resume_existing=True,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["rewind_to_stage"], "clarify")
            self.assertEqual(
                result["rewind_reason"],
                "requirements trace owns the missing wording",
            )
            self.assertEqual(len(task.verify_history), 1)

    def test_repeated_task_audit_failure_escalates_from_implement_to_clarify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-134",
                title="Align request evidence",
                description="Make persisted evidence match the outbound request.",
                acceptance=["The requirements audit passes."],
                requirement_ids=["REQ-134"],
                status="in_progress",
                verify_history=[
                    {
                        "attempt": 1,
                        "decision": "fail",
                        "summary": "requirements audit still fails for REQ-134",
                        "failure_ids": ["REQ-134"],
                        "comparable_failures": True,
                    }
                ],
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            verify_result = {
                "ok": False,
                "reason": "requirements audit still fails for REQ-134",
                "failure_ids": ["REQ-134"],
                "comparable_failures": True,
                "requirements_audit_failure": True,
                "audit_no_progress_rewind_stage": "clarify",
                "audit_no_progress_rewind_reason": "Refine the forbidden pattern.",
            }

            with patch.object(
                orchestrator,
                "_run_task_verify",
                return_value=verify_result,
            ):
                result = orchestrator._execute_task_with_retries(
                    state,
                    task,
                    resume_existing=True,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["rewind_to_stage"], "clarify")
            self.assertEqual(result["expected_owner_stage"], "clarify")
            self.assertIn(
                "Repeated implementation attempts made no progress",
                result["rewind_reason"],
            )
            self.assertIn("Refine the forbidden pattern", result["rewind_reason"])
            self.assertEqual(len(task.verify_history), 2)

    def test_task_verify_baseline_does_not_absorb_failures_from_prior_done_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"import json; from pathlib import Path; "
                    "tasks = json.loads(Path('.auto-agents/state/task_plan.json').read_text(encoding='utf-8')).get('tasks', []); "
                    "done = any(task.get('task_id') == 'task-001' and task.get('status') == 'done' for task in tasks); "
                    "print('FAILED tests/test_plan_state.py::test_task_001_stays_pending') if done else None; "
                    "raise SystemExit(1 if done else 0)\""
                )
            ]
            config.execution.recovery.enabled = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequentialArtifactAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Start the next slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            try:
                orchestrator._run_implementation_loop(state, max_tasks=2)
            except RuntimeError as error:
                failure_message = str(error)
            else:
                self.fail(stream.getvalue())

            self.assertIn(
                "new verification failure(s) vs task baseline: tests/test_plan_state.py::test_task_001_stays_pending",
                failure_message,
            )
            self.assertEqual(state.implement_verify_baseline_failures, [])
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[1].status, "blocked")
            self.assertEqual(state.tasks[1].verify_baseline_failures, [])
            self.assertNotIn("task baseline only", stream.getvalue())

    def test_commit_warms_next_clean_head_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = ["python -c \"print('ok')\""]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequentialArtifactAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            contexts = []
            original_run_gate_commands = orchestrator._run_gate_commands

            def tracking_run_gate_commands(*, collect_all, context):
                contexts.append(context)
                return original_run_gate_commands(collect_all=collect_all, context=context)

            orchestrator._run_gate_commands = tracking_run_gate_commands
            state = orchestrator._run_implementation_loop(state, max_tasks=2)

            baseline_runs = [
                item for item in contexts if item == "implement verify baseline commands"
            ]
            self.assertEqual(len(baseline_runs), 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[1].status, "done")

    def test_implement_baseline_uses_persistent_cache_across_orchestrators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            config = load_project_config(project_root)
            config.gates.commands = ["python -c \"print('ok')\""]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            self.assertTrue(orchestrator._ensure_implement_verify_baseline(state, state.tasks))
            self.assertTrue(gate_baseline_cache_path(project_root).exists())

            second = Orchestrator(project_root)

            def fail_run_gate_commands(*, collect_all, context):
                raise AssertionError(f"gate commands should be reused from cache during {context}")

            second._run_gate_commands = fail_run_gate_commands
            fresh_state = load_run_state(project_root)
            fresh_state.tasks = second._load_tasks_from_plan()
            self.assertTrue(second._ensure_implement_verify_baseline(fresh_state, fresh_state.tasks))
            self.assertEqual(fresh_state.implement_verify_baseline_failures, [])

    def test_run_gate_commands_uses_parallel_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            config = load_project_config(project_root)
            config.gates.commands = ["python3 -c \"print('before')\""]
            config.gates.parallel_groups = [
                GateParallelGroup(
                    name="checks",
                    commands=[
                        "python3 -c \"print('peer-a')\"",
                        "python3 -c \"print('peer-b')\"",
                    ],
                )
            ]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            gate, mutation_error = orchestrator._run_gate_commands(
                collect_all=True,
                context="parallel test commands",
            )

            self.assertEqual(mutation_error, "")
            self.assertTrue(gate.ok)
            self.assertEqual(
                [item.stdout for item in gate.commands],
                ["before", "peer-a", "peer-b"],
            )

    def test_plan_stage_records_split_task_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = SplitPlanAdapter(project_root)

            from auto_agents.models import TaskSpec

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec(
                    task_id="task-legacy",
                    title="Legacy task",
                    description="Old task before split.",
                    acceptance=["legacy done"],
                    status="pending",
                )
            ]

            state = orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertEqual(
                state.plan_task_replacements,
                {"task-legacy": ["task-child-a", "task-child-b"]},
            )

    def test_stale_plan_audit_retries_text_and_skips_binary_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = StalePlanAuditRecoveryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-child-a",
                            "title": "First child",
                            "description": "Migrate stale test references.",
                            "acceptance": ["stale tests migrate to child ids"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-child-b",
                            "title": "Second child",
                            "description": "Sibling split child.",
                            "acceptance": ["sibling remains available"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(tests_dir / "test_plan_contract.py", "EXPECTED_TASK = 'task-legacy'\n")
            fixtures_dir = tests_dir / "fixtures"
            fixtures_dir.mkdir()
            binary_fixture = fixtures_dir / "plan_snapshot.bin"
            binary_fixture.write_bytes(b"\x00PNG\xc6 task-legacy")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.plan_task_replacements = {"task-legacy": ["task-child-a", "task-child-b"]}

            self.assertIn(
                "tests/fixtures/plan_snapshot.bin",
                orchestrator._repository_test_paths(),
            )

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[1].status, "pending")
            self.assertIn("PLAN MIGRATION CONTEXT", orchestrator.adapter.implement_prompts[0])
            self.assertIn("`task-legacy` was replaced by: task-child-a, task-child-b", orchestrator.adapter.implement_prompts[0])
            self.assertIn(
                "Stale plan-coupled tests still reference retired task IDs",
                orchestrator.adapter.implement_prompts[1],
            )
            self.assertIn("tests/test_plan_contract.py", orchestrator.adapter.implement_prompts[1])
            self.assertIn("PLAN MIGRATION CONTEXT", orchestrator.adapter.review_prompts[0])
            self.assertNotIn(
                "task-legacy",
                (project_root / "tests" / "test_plan_contract.py").read_text(encoding="utf-8"),
            )

    def test_stale_plan_coupled_test_audit_ignores_split_child_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-078a",
                            "title": "First child",
                            "description": "Split child A.",
                            "acceptance": ["uses child ids only"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-078",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-078b",
                            "title": "Second child",
                            "description": "Split child B.",
                            "acceptance": ["uses child ids only"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-078",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_plan_contract.py",
                "EXPECTED_TASKS = ['task-078a', 'task-078b']\n",
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.plan_task_replacements = {"task-078": ["task-078a", "task-078b"]}

            audit = orchestrator._run_stale_plan_coupled_test_audit(state.tasks[0], state=state)

            self.assertIsNone(audit)

    def test_stale_plan_coupled_test_audit_ignores_parent_task_id_expectations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-079a",
                            "title": "First child",
                            "description": "Split child A.",
                            "acceptance": ["uses parent metadata"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-079",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-079b",
                            "title": "Second child",
                            "description": "Split child B.",
                            "acceptance": ["uses parent metadata"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-079",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_plan_contract.py",
                (
                    "EXPECTED = {\n"
                    "    'task-079a': {'parent_task_id': 'task-079'},\n"
                    "    'task-079b': {'parent_task_id': 'task-079'},\n"
                    "}\n"
                ),
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.plan_task_replacements = {"task-079": ["task-079a", "task-079b"]}

            audit = orchestrator._run_stale_plan_coupled_test_audit(state.tasks[0], state=state)

            self.assertIsNone(audit)

    def test_task_status_coupled_test_audit_flags_stale_done_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-080",
                            "title": "Continuity acceptance",
                            "description": "Finish the acceptance slice.",
                            "acceptance": ["status reaches done"],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_status_contract.py",
                (
                    "EXPECTED = {\n"
                    "    'task-080': {\n"
                    "        'status': 'in_progress',\n"
                    "    },\n"
                    "}\n"
                ),
            )

            task = orchestrator._load_tasks_from_plan()[0]
            audit = orchestrator._run_task_status_coupled_test_audit(task, expected_status="done")

            self.assertIsNotNone(audit)
            assert audit is not None
            self.assertIn("task `task-080`", str(audit["reason"]))
            self.assertIn("`done`", str(audit["reason"]))
            self.assertIn("`in_progress`", str(audit["reason"]))
            self.assertIn("tests/test_status_contract.py", str(audit["reason"]))

    def test_status_coupled_tests_retry_implement_until_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = StaleTaskStatusAuditRecoveryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-080",
                            "title": "Continuity acceptance",
                            "description": "Finish the acceptance slice.",
                            "acceptance": ["status reaches done"],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_status_contract.py",
                (
                    "EXPECTED = {\n"
                    "    'task-080': {\n"
                    "        'status': 'in_progress',\n"
                    "    },\n"
                    "}\n"
                ),
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertIn(
                "Plan-coupled repository tests still expect task `task-080` to have a stale status.",
                orchestrator.adapter.implement_prompts[1],
            )
            self.assertIn("tests/test_status_contract.py", orchestrator.adapter.implement_prompts[1])
            self.assertIn("`done`", orchestrator.adapter.implement_prompts[1])
            self.assertIn(
                "'status': 'done'",
                (project_root / "tests" / "test_status_contract.py").read_text(encoding="utf-8"),
            )

    def test_verify_failure_logs_repeat_statistics_for_same_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; artifact = Path('artifact.txt'); "
                    "print('FAILED tests/test_demo.py::test_same') if artifact.exists() else None; "
                    "raise SystemExit(1 if artifact.exists() else 0)\""
                )
            ]
            config.retries.per_stage["implement"] = 4
            config.execution.recovery.enabled = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequencedVerifyFailureAdapter(project_root, ["bad", "bad"])

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains bad"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertIn(
                "[task:task-001] verify decision=fail compare=first-failure-set failure_ids=1",
                rendered,
            )
            self.assertIn(
                "[task:task-001] verify decision=fail compare=same-failure-set-as-attempt-1 repeat=2 failure_ids=1 action=stop-unchanged-set",
                rendered,
            )
            self.assertIn(
                "unchanged verify failure set repeated from attempt-1 (repeat=2); stopping retries early",
                rendered,
            )

    def test_missing_owned_pytest_evidence_ref_continues_repair_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.retries.per_stage["implement"] = 3
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequencedVerifyFailureAdapter(
                project_root,
                ["attempt-1", "attempt-2", "attempt-3"],
            )
            ref = "tests/test_contract.py::ContractTests::test_missing_contract"
            task = TaskSpec(
                task_id="repair-task-001-r1-1",
                title="Repair proof evidence",
                description="Add the missing proof evidence test.",
                acceptance=["The proof evidence ref exists and passes."],
                task_origin="evidence_repair",
                verification_refs=[ref],
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            missing_ref = project_root / "tests" / "test_contract.py"
            write_text(
                missing_ref,
                "import unittest\n\nclass ContractTests(unittest.TestCase):\n    pass\n",
            )
            missing_node_id = f"{missing_ref}::ContractTests::test_missing_contract"

            def fake_missing_owned_ref(commands, *, collect_all, context):
                return (
                    GateResult(
                        ok=False,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=False,
                                returncode=4,
                                stdout=(
                                    f"ERROR: not found: {missing_node_id}\n"
                                    "(no match in any of [<UnitTestCase ContractTests>])\n"
                                ),
                                stderr="",
                            )
                        ],
                        summary=f"command failed: {commands[0]}",
                    ),
                    "",
                )

            with patch.object(
                orchestrator,
                "_run_gate_commands_for_commands",
                side_effect=fake_missing_owned_ref,
            ):
                result = orchestrator._execute_task_with_retries(state, task)

            rendered = stream.getvalue()
            self.assertFalse(result["ok"])
            self.assertEqual(orchestrator.adapter.implement_calls, 3)
            self.assertIn("compare=changed-failure-set", rendered)
            self.assertNotIn("stopping retries early", rendered)
            self.assertIn(
                "not found: " + missing_node_id,
                str(result["reason"]),
            )

    def test_repair_owned_pytest_evidence_failure_continues_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.retries.per_stage["implement"] = 3
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequencedVerifyFailureAdapter(
                project_root,
                ["attempt-1", "attempt-2", "attempt-3"],
            )
            ref = "tests/test_contract.py::ContractTests::test_provider_reference"
            task = TaskSpec(
                task_id="repair-task-001-r1-1",
                title="Repair proof evidence",
                description="Fix the proof evidence assertion.",
                acceptance=["The proof evidence ref passes."],
                task_origin="evidence_repair",
                verification_refs=[ref],
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            write_text(
                project_root / "tests" / "test_contract.py",
                (
                    "import unittest\n\n"
                    "class ContractTests(unittest.TestCase):\n"
                    "    def test_provider_reference(self):\n"
                    "        self.assertTrue(True)\n"
                ),
            )

            def fake_failing_owned_ref(commands, *, collect_all, context):
                return (
                    GateResult(
                        ok=False,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=False,
                                returncode=1,
                                stdout=f"FAILED {ref} - AssertionError\n",
                                stderr="",
                            )
                        ],
                        summary=f"command failed: {commands[0]}",
                    ),
                    "",
                )

            with patch.object(
                orchestrator,
                "_run_gate_commands_for_commands",
                side_effect=fake_failing_owned_ref,
            ):
                result = orchestrator._execute_task_with_retries(state, task)

            rendered = stream.getvalue()
            self.assertFalse(result["ok"])
            self.assertEqual(orchestrator.adapter.implement_calls, 3)
            self.assertIn("compare=changed-failure-set", rendered)
            self.assertNotIn("stopping retries early", rendered)
            self.assertIn(ref, str(result["reason"]))

    def test_verify_failure_logs_changed_and_regression_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; value = Path('artifact.txt').read_text().strip(); "
                    "print('FAILED tests/test_demo.py::test_alpha' if value == 'alpha' else "
                    "('FAILED tests/test_demo.py::test_beta' if value == 'beta' else "
                    "'FAILED tests/test_demo.py::test_alpha')); raise SystemExit(1)\""
                )
            ]
            config.retries.per_stage["implement"] = 3
            config.execution.recovery.enabled = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequencedVerifyFailureAdapter(project_root, ["alpha", "beta", "alpha"])

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt changes"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertIn(
                "[task:task-001] verify decision=fail compare=changed-failure-set-vs-attempt-1 failure_ids=1 new=1 resolved=1",
                rendered,
            )
            self.assertIn(
                "[task:task-001] verify decision=fail compare=regression failure-set-from-attempt-1 previous=attempt-2 repeat=2 failure_ids=1",
                rendered,
            )

    def test_missing_conda_fast_fail_skips_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests"]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = MissingCondaFastFailAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as raised:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertIn(".conda/conda-meta", str(raised.exception))
            # Baseline preflight rejects an unusable test runtime before an
            # implementation provider call can consume the retry budget.
            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_task_specific_vitest_verification_skips_global_missing_conda_fast_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                project_root / "workbench" / "package.json",
                {
                    "scripts": {"test": "vitest run"},
                    "devDependencies": {"vitest": "^3.0.0"},
                },
            )
            write_text(
                project_root / "workbench" / "src" / "components" / "video-card-delete.test.tsx",
                "test('renders', () => {})\n",
            )

            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests"]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-frontend",
                title="Frontend",
                description="",
                acceptance=[],
                verification_refs=[
                    "workbench/src/components/video-card-delete.test.tsx::renders"
                ],
            )
            captured = {}

            def fake_task_commands(commands, *, collect_all, context):
                captured["commands"] = list(commands)
                return (
                    GateResult(
                        ok=True,
                        commands=[
                            CommandResult(
                                command=list(commands)[0],
                                ok=True,
                                returncode=0,
                                stdout="",
                                stderr="",
                            )
                        ],
                        summary="all commands passed",
                    ),
                    "",
                )

            with patch("auto_agents.orchestrator.shutil.which", return_value="/usr/bin/npm"):
                with patch.object(
                    orchestrator,
                    "_run_gate_commands_for_commands",
                    side_effect=fake_task_commands,
                ):
                    result = orchestrator._run_task_verify(task)

            self.assertTrue(result["ok"], msg=str(result))
            self.assertIn("npm --prefix workbench test --", captured["commands"][0])
            self.assertNotIn(".conda", captured["commands"][0])

    def test_missing_pytest_target_fails_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            runtime = project_root / ".conda"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "conda-meta").mkdir()
            (runtime / "bin" / "python").symlink_to(Path(sys.executable))
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = [
                "./.conda/bin/python -m pytest -q tests/test_missing.py"
            ]
            config.gates.require_clean_git_before_task = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = MissingPytestTargetFastFailAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as raised:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertIn("missing pytest target", str(raised.exception))
            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_review_rejection_is_included_in_final_error_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 1
            config.execution.recovery.enabled = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = PermanentReviewFailureAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as raised:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            error_text = str(raised.exception)
            self.assertIn("Task task-001 failed gates: review rejected the task", error_text)
            self.assertIn("Review: Core issue: health endpoint is not actually exercised.", error_text)

    def test_review_failure_is_emitted_before_task_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 1
            config.execution.recovery.enabled = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = PermanentReviewFailureAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertIn("[task:task-001] review decision=fail", rendered)
            self.assertIn("Core issue: health endpoint is not actually exercised.", rendered)
            self.assertIn("[task:task-001] blocked reason=review rejected the task", rendered)

    def test_blocked_retry_omits_stale_review_when_current_proof_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            class PromptCaptureAdapter:
                def __init__(self, root: Path) -> None:
                    self.root = root
                    self.implement_prompts = []

                def run(self, request):
                    if request.stage == "implement":
                        self.implement_prompts.append(request.prompt)
                        write_text(self.root / "artifact.txt", "fixed\n")
                        summary = "implemented\n"
                    elif request.stage == "review":
                        summary = "DECISION: pass\nreview passed\n"
                    else:
                        summary = f"{request.stage}\n"
                    write_text(request.output_path, summary)
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary=summary.strip(),
                        returncode=0,
                    )

            config = orchestrator.config
            config.gates.commands = ["python -c \"print('ok')\""]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            adapter = PromptCaptureAdapter(project_root)
            orchestrator.adapter = adapter

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Recover blocked proof task",
                            "description": "Resume after stale review.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "blocked",
                            "commit_message": "",
                            "review_summary": (
                                "Still failing: tests/test_public_api.py::test_contract"
                            ),
                            "review_history": [
                                {"attempt": 1, "summary": "Still failing: tests/test_public_api.py::test_contract"}
                            ],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "status": "verified",
                                    "evidence_refs": ["tests/test_public_api.py::test_contract"],
                                }
                            ],
                        }
                    ]
                },
            )

            orchestrator._run_task_proof_evidence = lambda task: {
                "ok": True,
                "reason": "",
                "summary": "Owned proof evidence passed (1 refs): tests/test_public_api.py::test_contract",
                "evidence_refs": ["tests/test_public_api.py::test_contract"],
                "passed_refs": ["tests/test_public_api.py::test_contract"],
                "failed_refs": [],
                "failure_ids": [],
                "command": "conda run -p ./.conda python -m pytest -q tests/test_public_api.py::test_contract",
                "raw_output": "",
            }
            orchestrator._build_task_verify_commands = lambda task: []

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(len(adapter.implement_prompts), 1)
            self.assertIn("cited evidence_refs now pass", adapter.implement_prompts[0])
            self.assertIn("Current proof evidence:", adapter.implement_prompts[0])
            retry_feedback = adapter.implement_prompts[0].split("Previous attempt issues:\n", 1)[1]
            self.assertNotIn("Still failing: tests/test_public_api.py::test_contract", retry_feedback)

    def test_blocked_proof_failure_schedules_repair_task_before_retrying_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Parent proof task",
                            "description": "Parent task failed owned proof evidence.",
                            "acceptance": ["parent proof passes"],
                            "status": "blocked",
                            "commit_message": "",
                            "review_summary": (
                                "owned proof evidence failed: "
                                "tests/test_public_api.py::test_contract"
                            ),
                            "verify_history": [
                                {
                                    "attempt": 4,
                                    "decision": "fail",
                                    "summary": (
                                        "owned proof evidence failed: "
                                        "tests/test_public_api.py::test_contract"
                                    ),
                                    "failure_ids": ["tests/test_public_api.py::test_contract"],
                                    "comparable_failures": True,
                                }
                            ],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "status": "verified",
                                    "evidence_refs": ["tests/test_public_api.py::test_contract"],
                                }
                            ],
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.current_stage, "implement")
            self.assertEqual([task.task_id for task in result.tasks], ["repair-task-001-r1-1", "task-001"])
            repair, parent = result.tasks
            self.assertEqual(repair.parent_task_id, "task-001")
            self.assertEqual(repair.verification_refs, ["tests/test_public_api.py::test_contract"])
            self.assertEqual(repair.requirement_ids, ["REQ-001"])
            self.assertEqual(parent.status, "pending")
            self.assertIn("repair-task-001-r1-1", parent.depends_on)
            self.assertEqual(parent.recovery_history[-1]["result"], "scheduled")
            self.assertIn("[recovery] scheduled parent=task-001", stream.getvalue())

    def test_strict_evidence_repair_completes_after_verify_and_review_without_local_proofs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = []
            config.execution.parallel_tasks.enabled = False
            config.execution.evidence_preflight.mode = "off"
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            requirement = _strict_requirement()
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [requirement]},
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            proof_ref = "tests/test_public_api.py::test_contract"
            write_text(
                tests_dir / "test_public_api.py",
                "def test_contract():\n    assert True\n",
            )
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 2,
                    "verification_policy_version": 2,
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Parent proof task",
                            "description": "Keep the public contract verified.",
                            "acceptance": ["The public contract passes."],
                            "status": "blocked",
                            "commit_message": "",
                            "review_summary": f"owned proof evidence failed: {proof_ref}",
                            "verify_history": [
                                {
                                    "attempt": 1,
                                    "decision": "fail",
                                    "summary": f"owned proof evidence failed: {proof_ref}",
                                    "failure_ids": [proof_ref],
                                    "comparable_failures": True,
                                }
                            ],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                _strict_requirement_proof(
                                    requirement,
                                    proof_ref,
                                    status="verified",
                                )
                            ],
                        }
                    ],
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            scheduled = orchestrator._run_implementation_loop(state, max_tasks=1)
            repair, parent = scheduled.tasks
            self.assertEqual(repair.task_origin, "evidence_repair")
            self.assertEqual(repair.requirement_ids, ["REQ-001"])
            self.assertEqual(repair.requirement_proofs, [])
            self.assertEqual(repair.verification_refs, [proof_ref])
            self.assertEqual(parent.status, "pending")

            adapter = BlockedRetryAdapter(project_root)
            orchestrator.adapter = adapter
            completed = orchestrator._run_implementation_loop(
                scheduled,
                max_tasks=1,
            )

            completed_repair = completed.tasks[0]
            self.assertEqual(completed_repair.status, "done")
            self.assertEqual(adapter.implement_calls, 1)
            self.assertEqual(adapter.review_calls, 1)
            self.assertEqual(
                [entry["decision"] for entry in completed_repair.verify_history],
                ["pass"],
            )

    def test_evidence_repair_with_explicit_requirement_proof_remains_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            requirement = _strict_requirement()
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [requirement]},
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 2, "tasks": []},
            )
            repair = TaskSpec(
                task_id="repair-task-001-r1-1",
                title="Repair proof evidence",
                description="Repair the public contract proof.",
                acceptance=["The public contract passes."],
                task_origin="evidence_repair",
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    _strict_requirement_proof(
                        requirement,
                        "tests/test_public_api.py::test_contract",
                        status="planned",
                    )
                ],
            )

            findings = orchestrator._task_completion_proof_findings(repair)

            self.assertTrue(findings)
            self.assertTrue(
                any("proof is not verified" in item["message"] for item in findings),
                findings,
            )
            planned = TaskSpec(
                task_id="task-001",
                title="Implement the public contract",
                description="Keep the public contract verified.",
                acceptance=["The public contract passes."],
                requirement_ids=["REQ-001"],
            )

            planned_findings = orchestrator._task_completion_proof_findings(planned)

            self.assertTrue(
                any(item["kind"] == "oracle_proof_missing" for item in planned_findings),
                planned_findings,
            )

    def test_legacy_evidence_repair_inherits_parent_requirement_lineage(self) -> None:
        parent = TaskSpec(
            task_id="task-boundary",
            title="Prove the external boundary",
            description="Exercise the authorized external boundary.",
            acceptance=["The real boundary passes."],
            requirement_ids=["REQ-BOUNDARY"],
        )
        repair = TaskSpec(
            task_id="legacy-repair",
            title="Repair boundary evidence",
            description="Repair the failed boundary proof.",
            acceptance=["The proof passes."],
            parent_task_id=parent.task_id,
            task_origin="planned",
            evidence_preflight={
                "decision": "READY",
                "fingerprint": "legacy-context-free-result",
            },
        )

        changed = Orchestrator._normalize_task_origins([repair, parent])

        self.assertTrue(changed)
        self.assertEqual(repair.task_origin, "evidence_repair")
        self.assertEqual(repair.requirement_ids, ["REQ-BOUNDARY"])
        self.assertEqual(repair.evidence_preflight, {})

    def test_review_rejected_repair_is_requeued_with_feedback_before_parent_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 1
            config.execution.parallel_tasks.enabled = False
            config.execution.recovery.max_rounds = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = RepairReviewRecoveryAdapter(project_root)

            repair_id = "repair-task-001-r1-1"
            signature = orchestrator._recovery_signature([])
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": repair_id,
                            "title": "Repair weak acceptance proof",
                            "description": "Replace the weak proof with observable behavior.",
                            "acceptance": ["The public API proves the selection rule."],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-001",
                        },
                        {
                            "task_id": "task-001",
                            "title": "Parent task",
                            "description": "Implement candidate selection.",
                            "acceptance": ["The highest qualified candidate is selected."],
                            "depends_on": [repair_id],
                            "status": "pending",
                            "commit_message": "",
                            "recovery_history": [
                                {
                                    "signature": signature,
                                    "round": 1,
                                    "result": "scheduled",
                                    "reason": "review rejected the task",
                                    "failure_ids": [],
                                    "repair_task_ids": [repair_id],
                                }
                            ],
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            first = orchestrator._run_implementation_loop(state, max_tasks=1)

            repair, parent = first.tasks
            self.assertEqual(repair.status, "pending")
            self.assertEqual(repair.recovery_history[-1]["result"], "requeued")
            self.assertEqual(repair.recovery_history[-1]["round"], 2)
            self.assertEqual(parent.recovery_history[-1]["result"], "requeued")
            self.assertEqual(parent.recovery_history[-1]["round"], 2)
            self.assertNotIn(f"implement-{repair_id}", first.agent_attempts)
            self.assertNotIn(
                repair_id,
                first.resume_context.get("implementation_ready_tasks", {}),
            )

            second = orchestrator._run_implementation_loop(first, max_tasks=1)

            self.assertEqual(second.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 2)
            self.assertIn("Previous attempt issues:", orchestrator.adapter.implement_prompts[1])
            self.assertIn(
                "Acceptance proof is tautological; add two qualified candidates.",
                orchestrator.adapter.implement_prompts[1],
            )
            self.assertIn(
                "[recovery] requeued repair=repair-task-001-r1-1 parent=task-001 round=2",
                stream.getvalue(),
            )

    def test_recovery_scheduling_resolves_stale_task_instance_by_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            failure_id = "tests/test_contract.py::test_observable_contract"
            current_task = TaskSpec(
                task_id="task-349",
                title="Current task",
                description="Verify the provider reference.",
                acceptance=["The observable contract passes."],
                status="pending",
                verification_refs=[failure_id],
            )
            stale_task = TaskSpec(
                task_id="task-349",
                title="Current task",
                description="Verify the provider reference.",
                acceptance=["The observable contract passes."],
                status="in_progress",
                depends_on=["completed-old-repair"],
                verification_refs=[failure_id],
            )
            state = load_run_state(project_root)
            state.tasks = [current_task]

            scheduled = orchestrator._schedule_repair_tasks_for_failure(
                state,
                state.tasks,
                stale_task,
                {
                    "reason": "1 new verification failure vs task baseline",
                    "review": "The observable contract still fails.",
                    "failure_ids": [failure_id],
                    "comparable_failures": True,
                },
            )

            self.assertTrue(scheduled)
            self.assertEqual(
                [task.task_id for task in state.tasks],
                ["repair-task-349-r1-1", "task-349"],
            )
            canonical = state.tasks[1]
            self.assertIs(canonical, current_task)
            self.assertEqual(canonical.status, "pending")
            self.assertEqual(canonical.depends_on, ["repair-task-349-r1-1"])
            self.assertEqual(canonical.recovery_round, 1)
            self.assertEqual(canonical.verify_retry_epoch, 1)
            self.assertEqual(stale_task.status, "in_progress")

    def test_verification_failed_evidence_repair_reenters_judged_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2

            failure_id = "tests/test_contract.py::test_observable_contract"
            prior_feedback = "The observable contract needs one focused correction."
            task = TaskSpec(
                task_id="evidence-contract",
                title="Repair observable evidence",
                description="Keep the executable evidence aligned with the contract.",
                acceptance=["The observable evidence passes."],
                status="in_progress",
                task_origin="evidence_repair",
                recovery_round=1,
                verification_refs=[failure_id],
                recovery_history=[
                    {
                        "signature": "prior-review-signature",
                        "failure_signature": "prior-review-signature",
                        "round": 1,
                        "epoch": 0,
                        "result": "requeued",
                        "reason": "review rejected the task",
                        "review": prior_feedback,
                        "failure_ids": [failure_id],
                        "repair_task_ids": ["evidence-contract"],
                        "judge_decision": "CONTINUE",
                        "judge_source": "provider",
                    }
                ],
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            state.agent_attempts["implement-evidence-contract"] = 1
            state.resume_context["implementation_ready_tasks"] = {
                "evidence-contract": True,
            }
            verification_feedback = (
                "- Failure type: local_verification\n"
                f"- Failing checks: {failure_id}"
            )

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "CONTINUE",
                    "reason": "The failing evidence has a bounded corrective action.",
                    "actionable_items": ["Correct the owned evidence contract."],
                    "split_axis": [],
                    "source": "provider",
                },
            ) as judge:
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "1 new verification failure vs task baseline",
                        "review": verification_feedback,
                        "failure_ids": [failure_id],
                        "comparable_failures": True,
                    },
                )

            self.assertTrue(scheduled)
            judge.assert_called_once_with(
                state,
                task,
                task,
                verification_feedback,
                2,
            )
            self.assertEqual(state.tasks, [task])
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.recovery_round, 2)
            self.assertEqual(task.verify_retry_epoch, 1)
            self.assertEqual(task.review_summary, verification_feedback)
            self.assertNotIn("implement-evidence-contract", state.agent_attempts)
            self.assertNotIn(
                "evidence-contract",
                state.resume_context.get("implementation_ready_tasks", {}),
            )
            self.assertEqual(task.recovery_history[-1]["result"], "requeued")
            self.assertEqual(task.recovery_history[-1]["failure_ids"], [failure_id])
            self.assertEqual(state.last_recovery_route["outcome"], "requeued")
            self.assertEqual(
                state.last_recovery_route["failure_kind"],
                "verification_failed",
            )
            self.assertEqual(state.last_recovery_route["judge_decision"], "CONTINUE")
            self.assertEqual(state.last_recovery_route["round"], 2)

    def test_verification_failed_evidence_repair_obeys_recovery_round_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            failure_id = "tests/test_contract.py::test_observable_contract"
            task = TaskSpec(
                task_id="evidence-contract",
                title="Repair observable evidence",
                description="Keep the executable evidence aligned with the contract.",
                acceptance=["The observable evidence passes."],
                status="in_progress",
                task_origin="evidence_repair",
                recovery_round=2,
                verification_refs=[failure_id],
            )
            state = load_run_state(project_root)
            state.tasks = [task]

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                side_effect=AssertionError("the hard cap must precede the judge"),
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "verification still fails",
                        "review": "The owned evidence still violates its contract.",
                        "failure_ids": [failure_id],
                    },
                )

            self.assertFalse(scheduled)
            self.assertEqual(task.status, "in_progress")
            self.assertEqual(task.recovery_history[-1]["result"], "exhausted")
            self.assertEqual(task.recovery_history[-1]["round"], 3)
            self.assertEqual(state.last_recovery_route["outcome"], "exhausted")
            self.assertEqual(
                state.last_recovery_route["failure_kind"],
                "verification_failed",
            )
            self.assertEqual(state.last_recovery_route["round"], 3)

    def test_requeued_task_does_not_reuse_prior_round_verify_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 2
            config.execution.recovery.max_rounds = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequencedVerifyFailureAdapter(
                project_root,
                ["first recovery attempt", "second recovery attempt"],
            )

            failure_id = "tests/test_contract.py::test_observable_contract"
            task = TaskSpec(
                task_id="contract-slice",
                title="Repair the observable contract",
                description="Address the latest review feedback.",
                acceptance=["The observable contract passes."],
                status="in_progress",
                task_origin="scope_split",
                review_summary="The observable contract still needs one focused fix.",
                verify_history=[
                    {
                        "attempt": 2,
                        "decision": "fail",
                        "summary": "verification failed before the review recovery",
                        "failure_ids": [failure_id],
                        "comparable_failures": True,
                    }
                ],
            )
            state = load_run_state(project_root)
            state.tasks = [task]

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "CONTINUE",
                    "reason": "The review identifies one remaining implementation fix.",
                    "actionable_items": ["Apply the focused contract fix."],
                    "split_axis": [],
                    "source": "provider",
                },
            ):
                requeued = orchestrator._recover_review_rejected_task(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": task.review_summary,
                        "failure_ids": [failure_id],
                    },
                )

            self.assertTrue(requeued)
            self.assertEqual(task.recovery_round, 1)
            self.assertEqual(task.verify_retry_epoch, 1)

            with patch.object(
                orchestrator,
                "_run_task_verify",
                side_effect=[
                    {
                        "ok": False,
                        "reason": "the observable contract still fails",
                        "failure_ids": [failure_id],
                        "current_failure_ids": [failure_id],
                        "comparable_failures": True,
                    },
                    {
                        "ok": True,
                        "reason": "all commands passed",
                        "current_failure_ids": [],
                    },
                ],
            ):
                result = orchestrator._execute_task_with_retries(state, task)

            self.assertTrue(result["ok"])
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(
                [entry["decision"] for entry in task.verify_history],
                ["fail", "fail", "pass"],
            )
            self.assertNotIn("recovery_round", task.verify_history[0])
            self.assertEqual(task.verify_history[1]["recovery_round"], 1)
            self.assertEqual(task.verify_history[2]["recovery_round"], 1)
            self.assertEqual(task.verify_history[1]["verify_retry_epoch"], 1)
            self.assertEqual(task.verify_history[2]["verify_retry_epoch"], 1)
            self.assertNotIn("stopping retries early", stream.getvalue())

    def test_review_rejected_scope_split_task_is_requeued_without_id_heuristics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 1
            config.execution.parallel_tasks.enabled = False
            config.execution.recovery.max_rounds = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepairReviewRecoveryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-271b",
                            "title": "Select the highest qualified candidate",
                            "description": "Implement the replanned candidate-selection slice.",
                            "acceptance": ["The public API selects the highest score."],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-271",
                            "split_depth": 1,
                            "task_origin": "scope_split",
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            first = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(first.tasks[0].status, "pending")
            self.assertEqual(first.tasks[0].task_origin, "scope_split")
            self.assertEqual(first.tasks[0].recovery_round, 1)
            self.assertEqual(first.last_recovery_route["outcome"], "requeued")
            self.assertEqual(first.last_recovery_route["lineage_id"], "task-271b")

            second = orchestrator._run_implementation_loop(first, max_tasks=1)

            self.assertEqual(second.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertIn(
                "Acceptance proof is tautological; add two qualified candidates.",
                orchestrator.adapter.implement_prompts[1],
            )

    def test_recovery_judge_parser_requires_structured_actionable_decisions(self) -> None:
        cont = Orchestrator._parse_recovery_judge_decision(
            'RECOVERY_DECISION: {"decision":"CONTINUE","reason":"fixable",'
            '"actionable_items":["add two candidates"],"split_axis":[]}'
        )
        replan = Orchestrator._parse_recovery_judge_decision(
            '{"decision":"REPLAN","reason":"too broad","actionable_items":[],'
            '"split_axis":["selection", "proof"]}'
        )
        legacy_stop = Orchestrator._parse_recovery_judge_decision(
            '{"decision":"STOP","reason":"external input required",'
            '"actionable_items":[],"split_axis":[]}'
        )
        stop = Orchestrator._parse_recovery_judge_decision(
            '{"decision":"STOP","reason":"external contract required",'
            '"actionable_items":[],"split_axis":[],'
            '"owner":"verification_contract",'
            '"prerequisite_keys":["acceptance.contract"],'
            '"evidence_refs":["latest-review"]}'
        )
        invalid = Orchestrator._parse_recovery_judge_decision(
            '{"decision":"CONTINUE","reason":"fixable","actionable_items":[]}'
        )

        self.assertEqual(cont["decision"], "CONTINUE")
        self.assertEqual(replan["decision"], "REPLAN")
        self.assertEqual(legacy_stop["decision"], "")
        self.assertEqual(stop["decision"], "STOP")
        self.assertEqual(stop["owner"], "verification_contract")
        self.assertEqual(invalid["decision"], "")

    def test_recovery_stop_owner_is_derived_from_typed_incident(self) -> None:
        for diagnosed_owner, expected_owner in (
            ("target_project", "target_project"),
            ("verification_contract", "verification_contract"),
            ("verification_infrastructure", "verification_infrastructure"),
            ("execution_environment", "verification_infrastructure"),
            ("requirements", "verification_contract"),
        ):
            with self.subTest(owner=diagnosed_owner):
                self.assertEqual(
                    Orchestrator._recovery_stop_owner_from_incident(
                        {
                            "source": "gate",
                            "status": "needs_human",
                            "diagnosis": {
                                "source": "deterministic",
                                "owner": diagnosed_owner,
                            },
                        }
                    ),
                    expected_owner,
                )

        self.assertEqual(
            Orchestrator._recovery_stop_owner_from_incident(
                {
                    "source": "provider",
                    "status": "needs_human",
                    "diagnosis": {
                        "source": "provider",
                        "owner": "target_project",
                    },
                }
            ),
            "external_provider",
        )
        self.assertEqual(
            Orchestrator._recovery_stop_owner_from_incident(
                {
                    "source": "gate",
                    "status": "needs_human",
                    "diagnosis": {
                        "source": "provider",
                        "owner": "external_provider",
                    },
                }
            ),
            "",
        )

    def test_unbound_recovery_judge_stop_is_rejected_at_last_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            task = TaskSpec(
                task_id="ordinary-child",
                title="Ordinary child",
                description="Implement a split slice.",
                acceptance=["Observable proof passes."],
                status="in_progress",
                task_origin="scope_split",
                recovery_round=1,
            )
            state = load_run_state(project_root)
            state.tasks = [task]

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "STOP",
                    "reason": "The review requires external clarification.",
                    "actionable_items": [],
                    "split_axis": [],
                    "owner": "verification_contract",
                    "prerequisite_keys": ["acceptance.contract"],
                    "evidence_refs": ["latest-review"],
                    "source": "provider",
                },
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": "Acceptance cannot be proven from the current contract.",
                    },
                )

            self.assertTrue(scheduled)
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.recovery_round, 2)
            self.assertEqual(task.recovery_history[-1]["result"], "requeued")
            self.assertEqual(
                task.recovery_history[-1]["rejected_stop"]["prerequisite_keys"],
                ["acceptance.contract"],
            )
            self.assertIn(
                "not bound to exactly one current typed recovery record",
                task.recovery_history[-1]["rejected_stop"]["reason"],
            )
            self.assertEqual(state.last_recovery_route["outcome"], "requeued")
            self.assertEqual(
                state.last_recovery_route["judge_source"],
                "reconciled_fallback",
            )
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})

    def test_typed_current_external_prerequisite_allows_provider_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="provider-boundary",
                title="Exercise the provider boundary",
                description="Run the bounded external proof.",
                acceptance=["The external proof completes."],
                status="in_progress",
                task_origin="scope_split",
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            state.execution_incident_budget_epoch = 3
            state.active_execution_incident_id = "provider-incident"
            state.execution_incidents = [
                {
                    "incident_id": "provider-incident",
                    "source": "provider",
                    "kind": "provider_protocol_error",
                    "task_id": task.task_id,
                    "status": "needs_human",
                    "budget_epoch": 3,
                    "incident_fingerprint": "provider-identity",
                    "evidence_fingerprint": "provider-evidence",
                }
            ]
            review = "The external execution lane remains unavailable."
            evidence = orchestrator._recovery_judge_evidence(
                state,
                task,
                task,
                review,
                1,
            )
            prerequisite = evidence["recovery_prerequisites"][0]
            prerequisite_refs = [
                prerequisite["evidence_ref"],
                *prerequisite["evidence_refs"],
            ]

            self.assertEqual(prerequisite["status"], "unsatisfied")
            self.assertEqual(prerequisite["owner"], "external_provider")
            self.assertEqual(
                prerequisite["current_fingerprint"],
                evidence["current_fingerprint"],
            )

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "STOP",
                    "reason": "The typed provider incident requires external action.",
                    "actionable_items": [],
                    "split_axis": [],
                    "owner": "external_provider",
                    "prerequisite_keys": [prerequisite["key"]],
                    "evidence_refs": prerequisite_refs,
                    "source": "provider",
                },
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": review,
                    },
                )

            self.assertTrue(scheduled)
            self.assertEqual(task.recovery_history[-1]["result"], "judge_stopped")
            self.assertEqual(state.last_recovery_route["outcome"], "judge_stopped")
            self.assertEqual(task.recovery_round, 1)
            self.assertEqual(state.last_recovery_route["round"], 1)
            self.assertEqual(
                state.last_recovery_route["failure_signature"],
                task.recovery_history[-1]["failure_signature"],
            )
            self.assertEqual(state.last_recovery_route["judge_decision"], "STOP")
            self.assertEqual(state.last_recovery_route["judge_source"], "provider")
            self.assertTrue(
                state.last_recovery_route["prerequisite_fingerprint"]
            )
            self.assertEqual(
                state.last_recovery_route["prerequisite_fingerprint"],
                task.recovery_history[-1]["prerequisite_fingerprint"],
            )
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.active_blocker["owner"], "external_provider")
            self.assertEqual(
                state.active_blocker["category"],
                "recovery_provider_action_required",
            )

    def test_repeated_provider_recovery_stop_is_idempotent_and_resume_needs_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            repair = TaskSpec(
                task_id="provider-proof-repair",
                title="Repair provider proof evidence",
                description="Run the bounded provider proof.",
                acceptance=["The provider proof completes."],
                status="in_progress",
                task_origin="evidence_repair",
                parent_task_id="provider-contract",
                recovery_epoch=3,
                recovery_round=1,
            )
            parent = TaskSpec(
                task_id="provider-contract",
                title="Exercise the provider contract",
                description="Keep the external contract observable.",
                acceptance=["The external contract is proven."],
                status="pending",
                recovery_epoch=3,
                recovery_round=1,
            )
            state = load_run_state(project_root)
            state.current_stage = "implement"
            state.tasks = [repair, parent]
            state.execution_incident_budget_epoch = 4
            state.active_execution_incident_id = "provider-incident"
            state.execution_incidents = [
                {
                    "incident_id": "provider-incident",
                    "source": "provider",
                    "kind": "provider_protocol_error",
                    "task_id": repair.task_id,
                    "status": "needs_human",
                    "budget_epoch": 4,
                    "incident_fingerprint": "provider-identity",
                    "evidence_fingerprint": "provider-evidence-v1",
                }
            ]
            review = "The external execution lane remains unavailable."
            evidence = orchestrator._recovery_judge_evidence(
                state,
                repair,
                parent,
                review,
                2,
            )
            prerequisite = evidence["recovery_prerequisites"][0]
            judgment = {
                "decision": "STOP",
                "reason": "The typed provider prerequisite needs external action.",
                "actionable_items": [],
                "split_axis": [],
                "owner": "external_provider",
                "prerequisite_keys": [prerequisite["key"]],
                "evidence_refs": [
                    prerequisite["evidence_ref"],
                    *prerequisite["evidence_refs"],
                ],
                "source": "provider",
            }
            failure = {
                "reason": "verification failed at the provider boundary",
                "review": review,
                "failure_ids": ["tests/test_provider.py::test_live_boundary"],
            }

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value=judgment,
            ):
                first = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    repair,
                    failure,
                )

            self.assertTrue(first)
            self.assertEqual(repair.recovery_round, 2)
            self.assertEqual(parent.recovery_round, 2)
            self.assertEqual(state.last_recovery_route["round"], 2)
            self.assertEqual(state.last_recovery_route["judge_decision"], "STOP")
            self.assertEqual(state.last_recovery_route["judge_source"], "provider")
            first_route = json.loads(json.dumps(state.last_recovery_route))
            repair_history_size = len(repair.recovery_history)
            parent_history_size = len(parent.recovery_history)

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                side_effect=AssertionError(
                    "unchanged terminal evidence must not invoke the judge again"
                ),
            ):
                repeated = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    repair,
                    failure,
                )

            self.assertTrue(repeated)
            self.assertEqual(state.last_recovery_route, first_route)
            self.assertEqual(len(repair.recovery_history), repair_history_size)
            self.assertEqual(len(parent.recovery_history), parent_history_size)
            self.assertEqual(
                state.active_blocker["category"],
                "recovery_provider_action_required",
            )

            unchanged_epoch = parent.recovery_epoch
            self.assertFalse(orchestrator._resume_blocked_run(state))
            self.assertEqual(state.status, "blocked")
            self.assertEqual(parent.recovery_epoch, unchanged_epoch)
            self.assertEqual(repair.recovery_round, 2)
            self.assertEqual(parent.recovery_round, 2)
            self.assertEqual(state.last_recovery_route, first_route)
            self.assertIn("resume_rejection", state.active_blocker)

            state.execution_incidents[0][
                "evidence_fingerprint"
            ] = "provider-evidence-v2"

            self.assertTrue(orchestrator._resume_blocked_run(state))
            self.assertEqual(state.status, "pending")
            self.assertEqual(parent.recovery_epoch, unchanged_epoch + 1)
            self.assertEqual(repair.recovery_epoch, unchanged_epoch + 1)
            self.assertEqual(repair.recovery_round, 0)
            self.assertEqual(parent.recovery_round, 0)
            self.assertEqual(state.last_recovery_route, {})
            self.assertTrue(
                state.active_blocker["recovery_resume_evidence"][
                    "prerequisite_changed"
                ]
            )

    def test_typed_prerequisite_rejects_owner_mismatch_and_stale_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="provider-boundary",
                title="Exercise the provider boundary",
                description="Run the bounded external proof.",
                acceptance=["The external proof completes."],
                status="in_progress",
                task_origin="scope_split",
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            state.active_execution_incident_id = "provider-incident"
            state.execution_incidents = [
                {
                    "incident_id": "provider-incident",
                    "source": "provider",
                    "kind": "provider_protocol_error",
                    "task_id": task.task_id,
                    "status": "needs_human",
                    "budget_epoch": 0,
                    "incident_fingerprint": "provider-identity",
                    "evidence_fingerprint": "provider-evidence",
                }
            ]
            review = "The external execution lane remains unavailable."
            evidence = orchestrator._recovery_judge_evidence(
                state,
                task,
                task,
                review,
                1,
            )
            prerequisite = evidence["recovery_prerequisites"][0]
            validation = orchestrator._validate_recovery_stop(
                state,
                task,
                task,
                review,
                1,
                {
                    "owner": "target_project",
                    "prerequisite_keys": [prerequisite["key"]],
                    "evidence_refs": [
                        prerequisite["evidence_ref"],
                        *prerequisite["evidence_refs"],
                    ],
                },
            )

            self.assertFalse(validation["valid"])
            self.assertIn(
                "owner does not match the typed prerequisite owner",
                validation["reason"],
            )

            stale_evidence = json.loads(json.dumps(evidence))
            stale_evidence["evidence_catalog"][
                prerequisite["evidence_ref"]
            ]["entry"]["current_fingerprint"] = "stale-fingerprint"
            with patch.object(
                orchestrator,
                "_recovery_judge_evidence",
                return_value=stale_evidence,
            ):
                stale_validation = orchestrator._validate_recovery_stop(
                    state,
                    task,
                    task,
                    review,
                    1,
                    {
                        "owner": "external_provider",
                        "prerequisite_keys": [prerequisite["key"]],
                        "evidence_refs": [
                            prerequisite["evidence_ref"],
                            *prerequisite["evidence_refs"],
                        ],
                    },
                )

            self.assertFalse(stale_validation["valid"])
            self.assertIn(
                "not a current unsatisfied engine-generated recovery record",
                stale_validation["reason"],
            )

    def test_recovery_judge_replan_routes_scope_split_task_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="ordinary-child",
                title="Ordinary child",
                description="Implement a split slice.",
                acceptance=["Observable proof passes."],
                status="in_progress",
                task_origin="scope_split",
                split_depth=1,
            )
            state = load_run_state(project_root)
            state.tasks = [task]

            with (
                patch.object(
                    orchestrator,
                    "_run_recovery_judge",
                    return_value={
                        "decision": "REPLAN",
                        "reason": "The slice still combines two contracts.",
                        "actionable_items": [],
                        "split_axis": ["selection", "proof"],
                        "source": "provider",
                    },
                ),
                patch.object(
                    orchestrator,
                    "_handle_scope_overflow_rewind",
                    return_value=state,
                ) as rewind,
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": "The task remains too broad.",
                    },
                )

            self.assertTrue(scheduled)
            rewind.assert_called_once()
            self.assertEqual(state.last_recovery_route["outcome"], "replanned")
            self.assertEqual(state.last_recovery_route["judge_decision"], "REPLAN")

    def test_self_repair_reconciles_newer_terminal_recovery_history_without_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            repair = TaskSpec(
                task_id="external-proof-repair",
                title="Repair external proof evidence",
                description="Run the bounded external proof.",
                acceptance=["The external proof completes."],
                status="pending",
                task_origin="evidence_repair",
                parent_task_id="external-contract",
                recovery_epoch=3,
                recovery_round=1,
                verify_retry_epoch=5,
            )
            parent = TaskSpec(
                task_id="external-contract",
                title="Exercise the external contract",
                description="Keep the provider contract observable.",
                acceptance=["The provider contract is proven."],
                status="pending",
                recovery_epoch=3,
                recovery_round=1,
            )
            state = load_run_state(project_root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.tasks = [repair, parent]
            prerequisite_keys = ["external.anonymous_availability"]
            evidence_refs = [
                f"verification:{repair.task_id}:0",
                f"lineage-contract:{parent.task_id}",
            ]
            evidence_fingerprint = orchestrator._recovery_evidence_fingerprint(
                parent,
                state=state,
                tasks=state.tasks,
            )
            prerequisite_fingerprint = (
                orchestrator._recovery_prerequisite_fingerprint(
                    state,
                    state.tasks,
                    repair,
                    parent,
                    prerequisite_keys=prerequisite_keys,
                    evidence_refs=evidence_refs,
                )
            )
            stopped_entry = {
                "epoch": 3,
                "round": 2,
                "max_rounds": 2,
                "result": "judge_stopped",
                "failure_kind": "verification_failed",
                "signature": "signed-terminal-failure",
                "failure_signature": "signed-terminal-failure",
                "evidence_fingerprint": evidence_fingerprint,
                "judge_decision": "STOP",
                "judge_reason": "The external prerequisite remains unavailable.",
                "judge_source": "provider",
                "stop_owner": "external_provider",
                "stop_category": "recovery_provider_action_required",
                "prerequisite_keys": prerequisite_keys,
                "evidence_refs": evidence_refs,
                "prerequisite_fingerprint": prerequisite_fingerprint,
                "repair_task_ids": [repair.task_id],
            }
            repair.recovery_history = [dict(stopped_entry)]
            parent.recovery_history = [dict(stopped_entry)]
            state.last_recovery_route = {
                "task_id": repair.task_id,
                "task_origin": repair.task_origin,
                "lineage_id": parent.task_id,
                "epoch": 3,
                "round": 1,
                "max_rounds": 2,
                "failure_kind": "verification_failed",
                "failure_signature": "",
                "evidence_fingerprint": evidence_fingerprint,
                "judge_decision": "",
                "judge_source": "",
                "outcome": "judge_stopped",
                "reason": "terminal recovery evidence is unchanged",
                "repair_task_ids": [],
                "engine_invariant": "",
                "stop_owner": "external_provider",
                "stop_category": "recovery_provider_action_required",
                "prerequisite_keys": prerequisite_keys,
                "evidence_refs": evidence_refs,
            }
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "terminal_recovery_lifecycle_not_idempotent",
                "reason": "terminal route provenance is inconsistent",
                "status": "blocked",
            }
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project_root, state)

            marked = orchestrator.mark_self_repair_applied("repair123")
            resumed = Orchestrator(project_root)
            with patch.object(
                resumed,
                "_begin_fresh_verify_retry_lifecycle",
                side_effect=AssertionError(
                    "terminal reconciliation must not open a verification retry"
                ),
            ):
                changed = resumed._resume_blocked_run(marked)

            repaired_task = next(
                task for task in marked.tasks if task.task_id == repair.task_id
            )
            repaired_parent = next(
                task for task in marked.tasks if task.task_id == parent.task_id
            )
            self.assertFalse(changed)
            self.assertEqual(marked.status, "blocked")
            self.assertEqual(repaired_task.recovery_round, 2)
            self.assertEqual(repaired_parent.recovery_round, 2)
            self.assertEqual(repaired_task.verify_retry_epoch, 5)
            self.assertEqual(len(repaired_task.recovery_history), 1)
            self.assertEqual(len(repaired_parent.recovery_history), 1)
            self.assertEqual(marked.last_recovery_route["round"], 2)
            self.assertEqual(
                marked.last_recovery_route["failure_signature"],
                "signed-terminal-failure",
            )
            self.assertEqual(marked.last_recovery_route["judge_decision"], "STOP")
            self.assertEqual(marked.last_recovery_route["judge_source"], "provider")
            self.assertEqual(
                marked.last_recovery_route["reason"],
                "The external prerequisite remains unavailable.",
            )
            self.assertEqual(marked.active_blocker["owner"], "external_provider")
            self.assertEqual(
                marked.active_blocker["category"],
                "recovery_provider_action_required",
            )

    def test_self_repair_restores_skipped_current_epoch_recovery_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            failure_id = "tests/test_contract.py::test_observable_contract"
            repair_id = "current-proof-repair"
            parent_id = "observable-contract"
            history = [
                {
                    "epoch": 0,
                    "round": 2,
                    "result": "requeued",
                    "signature": "older-epoch",
                    "failure_signature": "older-epoch",
                    "repair_task_ids": [repair_id],
                },
                {
                    "epoch": 3,
                    "round": 1,
                    "result": "requeued",
                    "signature": "current-round-one",
                    "failure_signature": "current-round-one",
                    "evidence_fingerprint": "current-round-one-evidence",
                    "repair_task_ids": [repair_id],
                },
                {
                    "epoch": 3,
                    "round": 3,
                    "result": "exhausted",
                    "signature": "invalid-terminal",
                    "failure_signature": "invalid-terminal",
                    "evidence_fingerprint": "invalid-terminal-evidence",
                    "repair_task_ids": [repair_id],
                },
            ]
            repair = TaskSpec(
                task_id=repair_id,
                title="Repair current proof evidence",
                description="Repair the observable proof.",
                acceptance=["The observable proof passes."],
                status="blocked",
                task_origin="evidence_repair",
                parent_task_id=parent_id,
                recovery_epoch=3,
                recovery_round=2,
                recovery_history=[dict(entry) for entry in history],
                verification_refs=[failure_id],
            )
            parent = TaskSpec(
                task_id=parent_id,
                title="Implement the observable contract",
                description="Keep the public contract observable.",
                acceptance=["The observable contract passes."],
                status="pending",
                recovery_epoch=3,
                recovery_round=2,
                recovery_history=[dict(entry) for entry in history],
                verification_refs=[failure_id],
            )
            state = load_run_state(project_root)
            state.current_stage = "implement"
            state.status = "blocked"
            state.tasks = [repair, parent]
            state.last_recovery_route = {
                "task_id": repair_id,
                "lineage_id": parent_id,
                "epoch": 3,
                "round": 3,
                "max_rounds": 2,
                "outcome": "exhausted",
            }
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "recovery_cursor_invariant",
                "status": "blocked",
            }
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project_root, state)

            marked = orchestrator.mark_self_repair_applied("repair123")
            resumed = Orchestrator(project_root)

            self.assertTrue(resumed._resume_blocked_run(marked))
            repaired_task = next(
                task for task in marked.tasks if task.task_id == repair_id
            )
            repaired_parent = next(
                task for task in marked.tasks if task.task_id == parent_id
            )
            self.assertEqual(repaired_task.status, "pending")
            self.assertEqual(repaired_task.recovery_round, 1)
            self.assertEqual(repaired_parent.recovery_round, 1)
            self.assertEqual(marked.last_recovery_route, {})
            self.assertEqual(
                marked.active_blocker["recovery_cursor_reconciliations"][0][
                    "last_consumed_round"
                ],
                1,
            )
            terminal = next(
                entry
                for entry in repaired_task.recovery_history
                if entry.get("result") == "exhausted"
            )
            self.assertEqual(
                terminal["recovery_cursor_reconciliation"]["outcome"],
                "ignored_noncontiguous_exhaustion",
            )

            with patch.object(
                resumed,
                "_run_recovery_judge",
                return_value={
                    "decision": "CONTINUE",
                    "reason": "One bounded current-epoch correction remains.",
                    "actionable_items": ["Correct the current proof."],
                    "split_axis": [],
                    "source": "provider",
                },
            ):
                scheduled = resumed._schedule_repair_tasks_for_failure(
                    marked,
                    marked.tasks,
                    repaired_task,
                    {
                        "reason": "verification failed after self-repair",
                        "review": "The current proof still needs one correction.",
                        "failure_ids": [failure_id],
                    },
                )

            self.assertTrue(scheduled)
            self.assertEqual(repaired_task.recovery_round, 2)
            self.assertEqual(repaired_parent.recovery_round, 2)
            self.assertEqual(marked.last_recovery_route["outcome"], "requeued")
            self.assertEqual(marked.last_recovery_route["round"], 2)

    def test_self_repair_keeps_contiguous_recovery_exhaustion_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="bounded-recovery",
                title="Bounded recovery",
                description="Use the configured recovery budget.",
                acceptance=["The bounded proof passes."],
                status="blocked",
                task_origin="scope_split",
                recovery_epoch=2,
                recovery_round=2,
                recovery_history=[
                    {"epoch": 2, "round": 1, "result": "requeued"},
                    {"epoch": 2, "round": 2, "result": "requeued"},
                    {"epoch": 2, "round": 3, "result": "exhausted"},
                ],
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            state.last_recovery_route = {
                "task_id": task.task_id,
                "lineage_id": task.task_id,
                "epoch": 2,
                "round": 3,
                "outcome": "exhausted",
            }

            reconciled = orchestrator._reconcile_noncontiguous_recovery_exhaustion(
                state,
                state.tasks,
            )

            self.assertEqual(reconciled, [])
            self.assertEqual(task.recovery_round, 2)
            self.assertEqual(state.last_recovery_route["outcome"], "exhausted")
            self.assertNotIn(
                "recovery_cursor_reconciliation",
                task.recovery_history[-1],
            )

    def test_review_recovery_hard_cap_applies_to_ordinary_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2
            task = TaskSpec(
                task_id="task-271b",
                title="Ordinary child",
                description="Implement a split slice.",
                acceptance=["Observable proof passes."],
                status="in_progress",
                task_origin="scope_split",
                recovery_round=2,
            )
            state = load_run_state(project_root)
            state.tasks = [task]

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                side_effect=AssertionError("hard cap must run before the provider judge"),
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": "A third implementation cycle would exceed the hard cap.",
                    },
                )

            self.assertFalse(scheduled)
            self.assertEqual(task.recovery_history[-1]["result"], "exhausted")
            self.assertEqual(task.recovery_history[-1]["round"], 3)
            self.assertEqual(state.last_recovery_route["outcome"], "exhausted")

    def test_changed_evidence_reopens_terminal_recovery_in_a_new_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-271b",
                title="Ordinary child",
                description="Implement a split slice.",
                acceptance=["Observable proof passes."],
                status="in_progress",
                task_origin="scope_split",
                recovery_round=2,
            )
            terminal_fingerprint = orchestrator._recovery_evidence_fingerprint(task)
            task.recovery_history.append(
                {
                    "epoch": 0,
                    "round": 3,
                    "result": "exhausted",
                    "signature": "old-signature",
                    "failure_signature": "old-signature",
                    "evidence_fingerprint": terminal_fingerprint,
                }
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            state.last_recovery_route = {
                "task_id": "unrelated-task",
                "lineage_id": "unrelated-task",
                "epoch": 0,
                "outcome": "requeued",
            }
            write_text(project_root / "new-evidence.txt", "changed evidence\n")

            with patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "CONTINUE",
                    "reason": "New evidence makes another cycle useful.",
                    "actionable_items": ["Use the new evidence."],
                    "split_axis": [],
                    "source": "provider",
                },
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": "Re-evaluate the implementation with the new evidence.",
                    },
                )

            self.assertTrue(scheduled)
            self.assertEqual(task.recovery_epoch, 1)
            self.assertEqual(task.recovery_round, 1)
            self.assertEqual(state.last_recovery_route["epoch"], 1)
            self.assertEqual(state.last_recovery_route["outcome"], "requeued")

    def test_scope_policy_version_change_reopens_terminal_recovery_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-docs",
                title="Synchronize public docs",
                description="Update spec.md.",
                acceptance=["Public docs pass their contract tests."],
                status="blocked",
                recovery_round=2,
            )
            with patch("auto_agents.orchestrator.IMPLEMENTATION_SCOPE_POLICY_VERSION", 1):
                old_fingerprint = orchestrator._recovery_evidence_fingerprint(task)
            task.recovery_history.append(
                {
                    "epoch": 0,
                    "round": 3,
                    "result": "exhausted",
                    "evidence_fingerprint": old_fingerprint,
                }
            )
            state = load_run_state(project_root)

            reopened = orchestrator._reopen_recovery_epoch_if_evidence_changed(
                state, [task], task
            )

            self.assertTrue(reopened)
            self.assertEqual(task.recovery_epoch, 1)
            self.assertEqual(task.recovery_round, 0)

    def test_recovered_artifact_ownership_reopens_terminal_recovery_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            iteration_spec = project_root / "specs" / "iteration.md"
            write_text(iteration_spec, "# Immutable iteration input\n")
            write_text(project_root / "spec.md", "# Public contract\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = iteration_spec.resolve()
            owner = TaskSpec(
                task_id="task-docs",
                title="Synchronize public docs",
                description="Synchronize the public contract.",
                acceptance=["Public contract is current."],
                status="done",
                mutable_artifacts=["spec.md"],
            )
            recovery = TaskSpec(
                task_id="fix-rejection-1",
                title="Fix full verification failure",
                description="Full verification reports spec.md misses the contract.",
                acceptance=["Tests pass"],
                status="blocked",
                task_origin="stage_recovery",
                recovery_round=2,
            )
            old_fingerprint = orchestrator._recovery_evidence_fingerprint(recovery)
            recovery.recovery_history.append(
                {
                    "epoch": 0,
                    "round": 3,
                    "result": "exhausted",
                    "evidence_fingerprint": old_fingerprint,
                }
            )
            state = load_run_state(project_root)

            repaired = orchestrator._backfill_mutable_artifact_ownership(
                [owner, recovery]
            )
            reopened = orchestrator._reopen_recovery_epoch_if_evidence_changed(
                state, [owner, recovery], recovery
            )

            self.assertEqual(repaired, ["fix-rejection-1"])
            self.assertEqual(recovery.mutable_artifacts, ["spec.md"])
            self.assertTrue(reopened)
            self.assertEqual(recovery.recovery_epoch, 1)
            self.assertEqual(recovery.recovery_round, 0)

    def test_review_rejected_repair_stops_after_configured_recovery_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.execution.recovery.max_rounds = 2

            repair_id = "repair-task-001-r1-1"
            signature = orchestrator._recovery_signature([])
            round_two = {
                "signature": signature,
                "round": 2,
                "result": "requeued",
                "reason": "review rejected the task",
                "failure_ids": [],
                "repair_task_ids": [repair_id],
            }
            repair = TaskSpec(
                task_id=repair_id,
                title="Repair weak acceptance proof",
                description="Replace the weak proof.",
                acceptance=["Proof is observable."],
                status="in_progress",
                parent_task_id="task-001",
                recovery_history=[dict(round_two)],
            )
            parent = TaskSpec(
                task_id="task-001",
                title="Parent task",
                description="Implement candidate selection.",
                acceptance=["Highest qualified candidate is selected."],
                status="pending",
                recovery_history=[dict(round_two)],
            )
            state = load_run_state(project_root)
            state.tasks = [repair, parent]

            scheduled = orchestrator._schedule_repair_tasks_for_failure(
                state,
                state.tasks,
                repair,
                {
                    "reason": "review rejected the task",
                    "review": "A new actionable proof blocker remains.",
                },
            )

            self.assertFalse(scheduled)
            self.assertEqual(repair.status, "in_progress")
            self.assertEqual(repair.recovery_history[-1]["result"], "exhausted")
            self.assertEqual(repair.recovery_history[-1]["round"], 3)
            self.assertEqual(parent.recovery_history[-1]["result"], "exhausted")
            self.assertEqual(parent.recovery_history[-1]["round"], 3)

    def test_terminal_in_progress_repair_is_requeued_before_resume_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = PermanentReviewFailureAdapter(project_root)

            repair_id = "repair-task-001-r1-1"
            signature = orchestrator._recovery_signature([])
            round_one = {
                "signature": signature,
                "round": 1,
                "result": "scheduled",
                "reason": "review rejected the task",
                "failure_ids": [],
                "repair_task_ids": [repair_id],
            }
            review = "Acceptance proof is tautological; add two qualified candidates."
            repair = TaskSpec(
                task_id=repair_id,
                title="Repair weak acceptance proof",
                description="Replace the weak proof.",
                acceptance=["Proof is observable."],
                status="in_progress",
                parent_task_id="task-001",
                review_summary=review,
            )
            parent = TaskSpec(
                task_id="task-001",
                title="Parent task",
                description="Implement candidate selection.",
                acceptance=["Highest qualified candidate is selected."],
                status="pending",
                depends_on=[repair_id],
                recovery_history=[round_one],
            )
            state = load_run_state(project_root)
            state.status = "failed"
            state.last_error = (
                f"Task {repair_id} failed gates: review rejected the task. "
                f"Review: {review}"
            )
            state.agent_attempts[f"implement-{repair_id}"] = 1
            state.resume_context["implementation_ready_tasks"] = {repair_id: True}
            state.tasks = [repair, parent]

            result = orchestrator._execute_task_in_main_worktree(
                state,
                state.tasks,
                repair,
            )

            self.assertIs(result, state)
            self.assertEqual(repair.status, "pending")
            self.assertEqual(repair.recovery_history[-1]["result"], "requeued")
            self.assertEqual(repair.recovery_history[-1]["round"], 2)
            self.assertEqual(state.status, "pending")
            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 0)
            self.assertNotIn(f"implement-{repair_id}", state.agent_attempts)
            self.assertNotIn(
                repair_id,
                state.resume_context.get("implementation_ready_tasks", {}),
            )

    def test_run_logger_writes_to_current_run_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            state = load_run_state(project_root)

            orchestrator._attach_run_logger(state.run_id)
            orchestrator._emit_stage_start("implement")

            log_path = project_root / ".auto-agents" / "runs" / state.run_id / "run.log"
            self.assertTrue(log_path.exists())
            self.assertIn("[stage:implement] start", log_path.read_text(encoding="utf-8"))
            self.assertIn("[stage:implement] start", stream.getvalue())


    def test_reject_resets_stage_and_injects_feedback(self):
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Idea\nWe need a mock project.", encoding="utf-8")
            
            state = orchestrator.run(spec_file=spec_file)
            self.assertEqual(state.status, "paused")
            self.assertEqual(state.pending_approval, "requirements")
            
            state = orchestrator.reject("requirements", "Please add a database.")
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.pending_approval, "")
            self.assertEqual(state.rejection_reason, "Please add a database.")
            self.assertEqual(state.rejected_stage, "clarify")
            
            from unittest.mock import patch
            with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
                from auto_agents.models import AgentResult
                mock_run.return_value = AgentResult(
                    ok=True,
                    command=[],
                    output_path=Path("."),
                    summary="READY_TO_GENERATE",
                    stdout=""
                )
                state = orchestrator.run(spec_file=spec_file)
                
                found = False
                for call in mock_run.call_args_list:
                    if "clarify" in call.kwargs.get("stage", ""):
                        prompt = call.kwargs.get("prompt", "")
                        if "Please add a database." in prompt:
                            found = True
                
                self.assertTrue(found, "Rejection reason should be injected into clarify prompt")

    def test_requirements_audit_forbidden_pattern_routes_by_flagged_path_owner(self) -> None:
        cases = {
            ".auto-agents/docs/project_brief.md": "clarify",
            ".auto-agents/state/requirements_trace.json": "clarify",
            ".auto-agents/docs/architecture.md": "design",
            ".auto-agents/state/task_plan.json": "plan",
            ".auto-agents/docs/provider_references/provider.md": "provider_research",
            "app/service.py": "implement",
        }

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            for path, expected_stage in cases.items():
                with self.subTest(path=path):
                    stage, hard_failure = orchestrator._audit_issue_route(
                        {
                            "kind": "forbidden_pattern",
                            "message": f"forbidden pattern found in {path}",
                            "path": path,
                        }
                    )
                    self.assertEqual(stage, expected_stage)
                    self.assertEqual(hard_failure, "")

    def test_requirements_audit_forbidden_pattern_on_diagnostics_is_not_recoverable(self) -> None:
        paths = [
            ".auto-agents/docs/requirements_audit.md",
            ".auto-agents/docs/review.md",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            for path in paths:
                with self.subTest(path=path):
                    blocker = {
                        "kind": "forbidden_pattern",
                        "message": f"forbidden pattern found in {path}",
                        "path": path,
                    }

                    stage, hard_failure = orchestrator._audit_issue_route(blocker)

                    self.assertIsNone(stage)
                    self.assertIn("automatic recovery is unsafe", hard_failure)
                    self.assertNotIn("owned by implement", orchestrator._audit_blocker_feedback(blocker))

    def test_requirements_audit_forbidden_pattern_on_immutable_spec_routes_to_clarify(self) -> None:
        paths = [
            "spec.md",
            "specs/2026-07-05-iter-01.md",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            for path in paths:
                with self.subTest(path=path):
                    blocker = {
                        "kind": "forbidden_pattern",
                        "message": f"forbidden pattern found in {path}",
                        "path": path,
                    }

                    stage, hard_failure = orchestrator._audit_issue_route(blocker)

                    self.assertEqual(stage, "clarify")
                    self.assertEqual(hard_failure, "")
                    self.assertIn("immutable input spec", orchestrator._audit_blocker_feedback(blocker))
                    self.assertNotIn("owned by implement", orchestrator._audit_blocker_feedback(blocker))

    def test_requirements_audit_routes_non_active_public_spec_to_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            active_spec = project_root / "specs" / "iteration.md"
            write_text(active_spec, "# Iteration input\n")
            orchestrator = Orchestrator(project_root)
            orchestrator._active_spec_file = active_spec.resolve()
            blocker = {
                "kind": "forbidden_pattern",
                "message": "forbidden pattern found in spec.md",
                "path": "spec.md",
            }

            stage, hard_failure = orchestrator._audit_issue_route(blocker)

            self.assertEqual(stage, "implement")
            self.assertEqual(hard_failure, "")
            self.assertIn("owned by implement", orchestrator._audit_blocker_feedback(blocker))

    def test_requirements_audit_pattern_definition_failures_route_to_clarify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            for kind in ("forbidden_pattern_safety", "forbidden_pattern_timeout"):
                with self.subTest(kind=kind):
                    blocker = {
                        "kind": kind,
                        "message": "unsafe literal must not be repeated",
                        "reason": "DOTALL combined with an unbounded wildcard is unsafe",
                        "path": ".auto-agents/state/requirements_trace.json",
                    }

                    stage, hard_failure = orchestrator._audit_issue_route(blocker)
                    feedback = orchestrator._audit_blocker_feedback(blocker)

                    self.assertEqual(stage, "clarify")
                    self.assertEqual(hard_failure, "")
                    self.assertIn("owned by clarify", feedback)
                    self.assertNotIn("unsafe literal must not be repeated", feedback)

    def test_requirements_audit_route_ignores_non_authoritative_immutable_spec_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            audit_result = {
                "issues": [
                    {
                        "requirement_id": "REQ-001",
                        "result": "fail",
                        "blockers": [
                            {
                                "kind": "forbidden_pattern",
                                "message": "forbidden pattern found in specs/2026-01-01-old.md",
                                "path": "specs/2026-01-01-old.md",
                                "authoritative": False,
                            },
                            {
                                "kind": "forbidden_pattern",
                                "message": "forbidden pattern found in app/service.py",
                                "path": "app/service.py",
                                "authoritative": True,
                            },
                        ],
                    }
                ]
            }

            target_stage, hard_failures = orchestrator._requirements_audit_route(audit_result)

            self.assertEqual(target_stage, "implement")
            self.assertEqual(hard_failures, [])

    def test_requirements_audit_route_ignores_advisory_blockers_on_failed_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            audit_result = {
                "issues": [
                    {
                        "requirement_id": "REQ-001",
                        "result": "fail",
                        "blockers": [
                            {
                                "kind": "task_coverage",
                                "message": "not covered by any done task",
                            },
                            {
                                "kind": "forbidden_pattern",
                                "message": "forbidden pattern found in specs/old.md",
                                "path": "specs/old.md",
                                "advisory": True,
                            },
                        ],
                    }
                ]
            }

            target_stage, hard_failures = orchestrator._requirements_audit_route(audit_result)

            self.assertEqual(target_stage, "plan")
            self.assertEqual(hard_failures, [])

    def test_requirements_audit_failure_on_immutable_spec_rewinds_to_clarify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.current_stage = "verify"
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
                "implement": "done",
                "verify": "done",
            }
            audit_result = {
                "path": str(project_root / ".auto-agents" / "docs" / "requirements_audit.md"),
                "issues": [
                    {
                        "requirement_id": "REQ-001",
                        "result": "fail",
                        "blockers": [
                            {
                                "kind": "forbidden_pattern",
                                "message": "forbidden pattern found in specs/current.md",
                                "pattern": "legacy_detail_page",
                                "path": "specs/current.md",
                                "authoritative": True,
                            }
                        ],
                    }
                ],
            }

            recovered = orchestrator._handle_requirements_audit_failure(state, audit_result)

            self.assertTrue(recovered)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.current_stage, "clarify")
            self.assertEqual(state.rejected_stage, "clarify")
            self.assertEqual(state.last_error, "")
            self.assertNotIn("verify", state.stage_summaries)
            self.assertIn("Recovery route: rerun from clarify", state.rejection_reason)
            self.assertIn("immutable input spec", state.rejection_reason)
            self.assertIn("requirements_trace.json", state.rejection_reason)
            self.assertIn("Do not edit input specs", state.rejection_reason)
            self.assertNotIn("Automatic recovery is unsafe", state.rejection_reason)

    def test_review_feedback_rewinds_to_design_for_architecture_owned_artifact(self) -> None:
        summary = (
            "DECISION: fail\n"
            "`.auto-agents/docs/architecture.md:146` still contradicts REQ-087 "
            "and must be updated before this task can pass."
        )

        self.assertEqual(Orchestrator._review_feedback_rewind_stage(summary), "design")

    def test_review_feedback_normalizes_absolute_provider_reference_links(self) -> None:
        summary = (
            "DECISION: fail\n"
            "[provider reference](/home/example/demo/.auto-agents/docs/"
            "provider_references/image.md:42) lacks rule-level provenance."
        )

        self.assertEqual(
            Orchestrator._review_feedback_rewind_stage(summary),
            "provider_research",
        )

    def test_review_feedback_does_not_guess_clarify_from_requirements_audit_wording(self) -> None:
        summary = (
            "DECISION: fail\n"
            "验收标准 1 未满足：REQ-102 审计段依然缺少明确契约表述。"
        )

        self.assertEqual(Orchestrator._review_feedback_rewind_stage(summary), "")

    def test_misrouted_project_brief_audit_recovery_rewinds_to_clarify_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            write_text(project_root / "spec.md", "# Spec\n")
            write_text(project_root / ".auto-agents" / "docs" / "project_brief.md", "legacy_gateway\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep legacy wording in the project brief.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["project brief uses current wording"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        },
                        {
                            "task_id": "fix-rejection-123",
                            "title": "Fix issues after release rejection",
                            "description": (
                                "The release was rejected with the following feedback:\n"
                                "The requirements audit failed. Use "
                                f"{project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'} "
                                "as the source of truth.\n\nPlease fix these issues."
                            ),
                            "acceptance": ["Feedback is fully addressed", "Tests pass"],
                            "requirement_ids": [],
                            "status": "blocked",
                            "commit_message": "",
                        },
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.status = "failed"
            state.current_stage = "implement"
            state.last_error = "Task fix-rejection-123 failed gates: review rejected the task"
            state.tasks = orchestrator._load_tasks_from_plan()

            changed = orchestrator._normalize_blocked_requirements_audit_recovery_resume(state)

            self.assertTrue(changed)
            self.assertEqual(state.current_stage, "clarify")
            self.assertEqual(state.rejected_stage, "clarify")
            self.assertIn("owned by clarify", state.rejection_reason)
            task_ids = [task.task_id for task in state.tasks]
            self.assertEqual(task_ids, ["task-001"])
            persisted_task_ids = [
                item["task_id"]
                for item in json.loads(task_plan_path(project_root).read_text(encoding="utf-8"))["tasks"]
            ]
            self.assertEqual(persisted_task_ids, ["task-001"])

    def test_blocked_requirements_audit_recovery_with_immutable_spec_rewinds_to_clarify(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            specs = project_root / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            spec_file = specs / "current.md"
            write_text(spec_file, "This current spec still asks for a legacy_detail_page.\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy detail page contract.",
                            "source": "specs/current.md",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_detail_page"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        },
                        {
                            "task_id": "fix-rejection-123",
                            "title": "Fix issues after release rejection",
                            "description": (
                                "The release was rejected with the following feedback:\n"
                                "The requirements audit failed. Use "
                                f"{project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'} "
                                "as the source of truth.\n\nPlease fix these issues."
                            ),
                            "acceptance": ["Feedback is fully addressed", "Tests pass"],
                            "requirement_ids": [],
                            "status": "blocked",
                            "commit_message": "",
                        },
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.status = "failed"
            state.current_stage = "implement"
            state.last_error = "Task fix-rejection-123 failed gates: review rejected the task"
            state.resume_context["spec_file"] = str(spec_file)
            state.tasks = orchestrator._load_tasks_from_plan()

            changed = orchestrator._normalize_blocked_requirements_audit_recovery_resume(state)

            self.assertTrue(changed)
            self.assertEqual(state.current_stage, "clarify")
            self.assertEqual(state.rejected_stage, "clarify")
            self.assertIn("immutable input spec", state.rejection_reason)
            task_ids = [task.task_id for task in state.tasks]
            self.assertEqual(task_ids, ["task-001"])
            persisted_task_ids = [
                item["task_id"]
                for item in json.loads(task_plan_path(project_root).read_text(encoding="utf-8"))["tasks"]
            ]
            self.assertEqual(persisted_task_ids, ["task-001"])

    def test_requirements_audit_recovery_task_verify_fails_before_review_when_audit_still_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        },
                        {
                            "task_id": "fix-rejection-123",
                            "title": "Fix issues after release rejection",
                            "description": (
                                "The release was rejected with the following feedback:\n"
                                "The requirements audit failed. Use "
                                f"{project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'} "
                                "as the source of truth.\n\nPlease fix these issues."
                            ),
                            "acceptance": ["Feedback is fully addressed", "Tests pass"],
                            "requirement_ids": [],
                            "status": "in_progress",
                            "commit_message": "",
                        },
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            recovery_task = state.tasks[1]

            result = orchestrator._run_task_verify(recovery_task, state=state)

            self.assertFalse(result["ok"])
            self.assertIn("requirements audit still failed", str(result["reason"]))
            self.assertIn("REQ-001", result["failure_ids"])

    def test_task_verify_regenerates_requirements_audit_before_running_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-102",
                            "text": "Provider terminology is documented.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["contract wording is explicit"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            (project_root / "tests").mkdir()
            required_wording = (
                "referenceImages and referenceBlobs are provider-internal error wording, "
                "not public /v1/images/edits request fields"
            )
            write_text(
                project_root / "tests" / "test_requirements_audit_state.py",
                "from pathlib import Path\n\n"
                "def test_req_102_contract_wording():\n"
                "    report = Path('.auto-agents/docs/requirements_audit.md').read_text(encoding='utf-8')\n"
                f"    assert {required_wording!r} in report\n",
            )
            audit_path = project_root / ".auto-agents" / "docs" / "requirements_audit.md"
            write_text(audit_path, f"# Requirements Audit\n\n{required_wording}\n")
            ref = "tests/test_requirements_audit_state.py::test_req_102_contract_wording"
            task = TaskSpec(
                task_id="task-102",
                title="Document provider terminology",
                description="Clarify the public request contract.",
                acceptance=["The requirements audit contains explicit contract wording."],
                requirement_ids=["REQ-102"],
                verification_refs=[ref],
                status="in_progress",
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            orchestrator = Orchestrator(project_root)

            def fake_contract_check(commands, *, collect_all, context):
                self.assertNotIn(required_wording, audit_path.read_text(encoding="utf-8"))
                return (
                    GateResult(
                        ok=False,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=False,
                                returncode=1,
                                stdout=f"FAILED {ref} - AssertionError\n",
                                stderr="",
                            )
                        ],
                        summary=f"command failed: {commands[0]}",
                    ),
                    "",
                )

            with patch.object(
                orchestrator,
                "_run_gate_commands_for_commands",
                side_effect=fake_contract_check,
            ):
                result = orchestrator._run_task_verify(task, state=state)

            self.assertFalse(result["ok"])
            self.assertNotIn("rewind_to_stage", result, result)
            self.assertIn(ref, result["failure_ids"])
            regenerated = audit_path.read_text(encoding="utf-8")
            self.assertIn("Provider terminology is documented.", regenerated)
            self.assertNotIn(required_wording, regenerated)

    def test_requirements_audit_content_comparison_ignores_generation_time(self) -> None:
        first = (
            "# Requirements Audit\n\nGenerated at: 2026-07-10T01:00:00Z\n\n"
            "## REQ-102: pass\n"
        )
        second = (
            "# Requirements Audit\n\nGenerated at: 2026-07-10T02:00:00Z\n\n"
            "## REQ-102: pass\n"
        )

        self.assertEqual(
            Orchestrator._requirements_audit_stable_content(first),
            Orchestrator._requirements_audit_stable_content(second),
        )

    def test_task_requirements_audit_proof_failure_rewinds_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            task = TaskSpec(
                task_id="task-236",
                title="Bind requirement proofs",
                description="Complete the REQ-102 proof contract.",
                acceptance=["REQ-102 proof metadata is complete."],
                requirement_ids=["REQ-102"],
                verification_refs=[".auto-agents/docs/requirements_audit.md"],
                status="in_progress",
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            audit_result = {
                "ok": False,
                "path": str(
                    project_root / ".auto-agents" / "docs" / "requirements_audit.md"
                ),
                "report": (
                    "# Requirements Audit\n\n## REQ-102: fail\n\nFindings:\n"
                    "- REQ-102 acceptance oracle #3 has no valid verified proof\n"
                ),
                "issues": [
                    {
                        "requirement_id": "REQ-102",
                        "result": "fail",
                        "blockers": [
                            {
                                "kind": "oracle_proof_invalid",
                                "message": (
                                    "REQ-102 acceptance oracle #3 has no valid verified proof"
                                ),
                            }
                        ],
                    },
                    {
                        "requirement_id": "REQ-999",
                        "result": "fail",
                        "blockers": [
                            {
                                "kind": "forbidden_pattern",
                                "message": "unrelated implementation blocker",
                                "path": "app/unrelated.py",
                                "authoritative": True,
                            }
                        ],
                    },
                ],
            }
            orchestrator = Orchestrator(project_root)

            with patch(
                "auto_agents.orchestrator.run_requirements_audit",
                return_value=audit_result,
            ):
                result = orchestrator._run_task_verify(task, state=state)

            self.assertFalse(result["ok"])
            self.assertEqual(result["failure_ids"], ["REQ-102"])
            self.assertEqual(result["rewind_to_stage"], "plan")
            self.assertIn("Recovery route: rerun from plan", result["rewind_reason"])
            self.assertIn("no valid verified proof", result["rewind_reason"])
            self.assertNotIn("unrelated implementation blocker", result["rewind_reason"])

    def test_task_requirements_audit_propagates_implement_no_progress_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            task = TaskSpec(
                task_id="task-134",
                title="Align request evidence",
                description="Make persisted evidence match the outbound request.",
                acceptance=["The requirements audit passes."],
                requirement_ids=["REQ-134"],
                verification_refs=[".auto-agents/docs/requirements_audit.md"],
                status="in_progress",
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            audit_result = {
                "ok": False,
                "path": str(
                    project_root / ".auto-agents" / "docs" / "requirements_audit.md"
                ),
                "report": "# Requirements Audit\n\n## REQ-134: fail\n",
                "issues": [
                    {
                        "requirement_id": "REQ-134",
                        "result": "fail",
                        "blockers": [
                            {
                                "kind": "forbidden_pattern",
                                "message": "forbidden pattern found in product code",
                                "path": "app/application/public_image.py",
                                "authoritative": True,
                            }
                        ],
                    }
                ],
            }
            orchestrator = Orchestrator(project_root)

            with patch(
                "auto_agents.orchestrator.run_requirements_audit",
                return_value=audit_result,
            ):
                result = orchestrator._run_task_verify(task, state=state)

            self.assertFalse(result["ok"])
            self.assertTrue(result["requirements_audit_failure"])
            self.assertEqual(result["audit_no_progress_rewind_stage"], "clarify")
            self.assertIn(
                "Recovery route: rerun from clarify",
                result["audit_no_progress_rewind_reason"],
            )
            self.assertNotIn("rewind_to_stage", result)

    def test_requirements_audit_forbidden_pattern_routes_back_to_implement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertIn("requirements_audit", state.stage_summaries)
            self.assertEqual(
                (project_root / "app" / "service.py").read_text(encoding="utf-8").strip(),
                "modern_backend = True",
            )
            task_plan_text = task_plan_path(project_root).read_text(encoding="utf-8")
            run_state_text = (project_root / ".auto-agents" / "state" / "run_state.json").read_text(encoding="utf-8")
            self.assertNotIn("legacy_gateway still exists", task_plan_text)
            self.assertNotIn("legacy_gateway still exists", run_state_text)

    def test_requirements_audit_historical_spec_corroboration_does_not_block_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            specs = project_root / "specs"
            specs.mkdir()
            spec_file = specs / "2026-07-02-current.md"
            spec_file.write_text("# Current spec\n", encoding="utf-8")
            write_text(specs / "2026-01-01-old.md", "Old iteration required legacy_gateway.\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "specs/2026-07-02-current.md",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(
                (project_root / "app" / "service.py").read_text(encoding="utf-8").strip(),
                "modern_backend = True",
            )

    def test_requirements_audit_recovery_emits_verify_failure_before_rewind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")

            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            rendered = stream.getvalue()
            self.assertIn("[stage:verify] decision=fail route=implement", rendered)
            self.assertIn("requirements audit failed:", rendered)
            self.assertLess(
                rendered.index("[stage:verify] decision=fail route=implement"),
                rendered.index("[stage:implement] start"),
            )

    def test_requirements_audit_missing_coverage_routes_back_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Cover the requirement in at least one done task.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["task coverage exists"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": [],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.plan_calls, 1)
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual([task.status for task in state.tasks], ["done", "done"])

    def test_requirements_audit_missing_provider_reference_routes_back_to_provider_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Use verified provider documentation.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["provider reference is verified"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": True,
                            "provider_reference": ".auto-agents/docs/provider_references/provider.md",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.provider_research_calls, 1)
            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertIn("requirements_audit", state.stage_summaries)

    def test_pending_stages_reruns_explicitly_failed_verify(self) -> None:
        from auto_agents.models import RunState, TaskSpec

        state = RunState(run_id="run-123", status="failed", current_stage="verify")
        state.tasks = [
            TaskSpec(
                task_id="task-001",
                title="Done task",
                description="Already finished.",
                acceptance=["done"],
                status="done",
            )
        ]
        state.stage_summaries = {
            "clarify": "done",
            "design": "done",
            "plan": "done",
            "provider_research": "done",
            "implement": "done",
            "verify": "# Verify\n\nResult: fail\n\n- `pytest` -> failed",
        }

        pending = Orchestrator._pending_stages(object.__new__(Orchestrator), state)

        self.assertEqual(pending, ["visual_judge", "verify", "readme"])

    def test_legacy_requirements_audit_failure_state_is_rewound_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            state = load_run_state(project_root)
            state.status = "failed"
            state.current_stage = "verify"
            state.last_error = f"requirements audit failed: {project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'}"
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
                "implement": "done",
                "verify": "done",
                "requirements_audit": "Result: pass",
            }
            state.tasks = orchestrator._load_tasks_from_plan()
            save_run_state(project_root, state)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertIn("requirements_audit", state.stage_summaries)
            self.assertNotIn("readme", state.rejected_stage)

    def test_exhausted_requirements_audit_recovery_state_is_rewound_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.status = "failed"
            state.current_stage = ""
            state.last_error = (
                "requirements audit failed after 4 automatic recovery attempt(s): "
                f"{project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'}"
            )
            state.agent_attempts["requirements_audit_recovery"] = 4

            changed = orchestrator._normalize_legacy_requirements_audit_resume(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.current_stage, "verify")
            self.assertEqual(state.last_error, "")
            self.assertNotIn("requirements_audit_recovery", state.agent_attempts)

    def test_requirements_audit_blocked_provider_reference_still_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Use verified provider documentation.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["provider reference is verified"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": True,
                            "provider_reference": ".auto-agents/docs/provider_references/provider.md",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": ".auto-agents/docs/provider_references/provider.md",
                            "status": "blocked",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_verify(state)

            self.assertIn("Automatic recovery is unsafe", str(ctx.exception))

    def test_parallel_tasks_fall_back_to_sequential_without_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            config = orchestrator.config
            config.gates.commands = ["python3 -c \"print('ok')\""]
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequentialArtifactAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertEqual([task.status for task in state.tasks], ["done", "done"])
            self.assertIn("fallback to sequential", stream.getvalue())

    def test_sequential_tasks_wait_for_dependencies_even_when_planned_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            dependent = TaskSpec(
                task_id="dependent",
                title="Dependent",
                description="Consume the prerequisite.",
                acceptance=["dependency is consumed"],
                status="pending",
                depends_on=["prerequisite"],
            )
            prerequisite = TaskSpec(
                task_id="prerequisite",
                title="Prerequisite",
                description="Produce the prerequisite.",
                acceptance=["dependency exists"],
                status="pending",
            )
            tasks = [dependent, prerequisite]
            state.tasks = tasks
            execution_order = []

            def complete_task(_state, _tasks, task):
                execution_order.append(task.task_id)
                task.status = "done"
                return None

            with patch.object(
                orchestrator,
                "_execute_task_in_main_worktree",
                side_effect=complete_task,
            ):
                result = orchestrator._run_sequential_implementation_loop(
                    state,
                    tasks,
                    max_tasks=None,
                )

            self.assertEqual(execution_order, ["prerequisite", "dependent"])
            self.assertTrue(all(task.status == "done" for task in result.tasks))

    def test_parallel_tasks_strict_mode_requires_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.strict = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertIn("depends_on", str(ctx.exception))

    def test_parallel_tasks_integrate_ready_batch_in_task_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            integrated = []

            def fake_run_task_in_worktree(state_snapshot, tasks_snapshot, task_id):
                task = next(item for item in tasks_snapshot if item.task_id == task_id)
                task.status = "done"
                task.review_summary = f"review for {task_id}"
                return {
                    "ok": True,
                    "task": task.to_dict(),
                    "reason": "",
                    "review": task.review_summary,
                    "commit_sha": f"worker-{task_id}",
                    "verify_current_failure_ids": [],
                }

            def fake_integrate(task, tasks, worker_commit_sha):
                integrated.append((task.task_id, worker_commit_sha))
                return f"main-{task.task_id}"

            with patch.object(orchestrator, "_run_task_in_worktree", side_effect=fake_run_task_in_worktree):
                with patch.object(orchestrator, "_integrate_parallel_task_result", side_effect=fake_integrate):
                    result = orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertEqual(
                integrated,
                [("task-001", "worker-task-001"), ("task-002", "worker-task-002")],
            )
            self.assertEqual([task.status for task in result.tasks], ["done", "done"])
            self.assertEqual(
                [task.commit_sha for task in result.tasks],
                ["main-task-001", "main-task-002"],
            )

    def test_parallel_tasks_defer_overlapping_worker_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            integrated = []
            sequential = []

            def fake_run_task_in_worktree(state_snapshot, tasks_snapshot, task_id):
                task = next(item for item in tasks_snapshot if item.task_id == task_id)
                task.status = "done"
                task.review_summary = f"review for {task_id}"
                return {
                    "ok": True,
                    "task": task.to_dict(),
                    "reason": "",
                    "review": task.review_summary,
                    "commit_sha": f"worker-{task_id}",
                    "changed_paths": ["shared.txt"],
                    "verify_current_failure_ids": [],
                }

            def fake_integrate(task, tasks, worker_commit_sha):
                integrated.append((task.task_id, worker_commit_sha))
                return f"main-{task.task_id}"

            def fake_execute_sequential(state_arg, tasks_arg, task):
                sequential.append(task.task_id)
                task.status = "done"
                task.commit_sha = f"main-{task.task_id}"
                return None

            with patch.object(orchestrator, "_run_task_in_worktree", side_effect=fake_run_task_in_worktree):
                with patch.object(orchestrator, "_integrate_parallel_task_result", side_effect=fake_integrate):
                    with patch.object(
                        orchestrator,
                        "_execute_task_in_main_worktree",
                        side_effect=fake_execute_sequential,
                    ):
                        result = orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertEqual(integrated, [("task-001", "worker-task-001")])
            self.assertEqual(sequential, ["task-002"])
            self.assertEqual([task.status for task in result.tasks], ["done", "done"])
            self.assertIn("defer integration task=task-002", stream.getvalue())

    def test_parallel_tasks_aggregate_failed_workers_and_copy_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            config.execution.recovery.enabled = False
            save_project_config(project_root, config)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            def fake_run_task_in_worktree(state_snapshot, tasks_snapshot, task_id):
                task = next(item for item in tasks_snapshot if item.task_id == task_id)
                task.review_summary = f"review for {task_id}"
                task.verify_history.append({
                    "attempt": 1,
                    "decision": "fail",
                    "summary": f"failed {task_id}",
                    "failure_ids": [f"reason:{task_id}"],
                })
                task.requirement_proofs = [{"requirement_id": "REQ-001", "status": "planned"}]
                return {
                    "ok": False,
                    "task": task.to_dict(),
                    "reason": f"failed {task_id}",
                    "review": task.review_summary,
                    "failure_ids": [f"reason:{task_id}"],
                    "comparable_failures": True,
                }

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with patch.object(orchestrator, "_run_task_in_worktree", side_effect=fake_run_task_in_worktree):
                with self.assertRaises(RuntimeError) as ctx:
                    orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertIn("task-001: failed task-001", str(ctx.exception))
            self.assertIn("task-002: failed task-002", str(ctx.exception))
            reloaded = orchestrator._load_tasks_from_plan()
            self.assertEqual([task.status for task in reloaded], ["blocked", "blocked"])
            self.assertEqual(reloaded[0].review_summary, "review for task-001")
            self.assertEqual(reloaded[0].requirement_proofs[0]["requirement_id"], "REQ-001")
            self.assertEqual(reloaded[1].verify_history[-1]["failure_ids"], ["reason:task-002"])

    def test_parallel_tasks_honor_structured_plan_rewind_before_repair_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-route",
                            "title": "Route planner-owned gap",
                            "description": "Verify structured recovery routing.",
                            "acceptance": ["the owning stage is rerun"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-peer",
                            "title": "Complete independent peer",
                            "description": "Produce an independent artifact.",
                            "acceptance": ["peer artifact exists"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            def fake_run_task_in_worktree(state_snapshot, tasks_snapshot, task_id):
                task = next(item for item in tasks_snapshot if item.task_id == task_id)
                if task_id == "task-route":
                    task.status = "blocked"
                    task.review_summary = "The task plan owns the missing proof mapping."
                    return orchestrator._parallel_task_failure_result(
                        task,
                        {
                            "reason": "a planner-owned proof mapping is missing",
                            "review": task.review_summary,
                            "failure_ids": ["REQ-generic"],
                            "rewind_to_stage": "plan",
                            "expected_owner_stage": "plan",
                            "rewind_reason": "rerun the owning planning stage",
                        },
                    )
                task.status = "done"
                return {
                    "ok": True,
                    "task": task.to_dict(),
                    "reason": "",
                    "review": "peer passed",
                    "commit_sha": "peer-worker-commit",
                    "changed_paths": ["peer.txt"],
                    "verify_current_failure_ids": [],
                }

            def fake_integrate(task, tasks, worker_commit_sha):
                write_text(project_root / "peer.txt", "integrated peer\n")
                subprocess.run(
                    ["git", "add", "peer.txt"],
                    cwd=str(project_root),
                    check=True,
                    text=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "test: integrate peer"],
                    cwd=str(project_root),
                    check=True,
                    text=True,
                    capture_output=True,
                )
                return head_ref(project_root)

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with patch.object(
                orchestrator,
                "_run_task_in_worktree",
                side_effect=fake_run_task_in_worktree,
            ), patch.object(
                orchestrator,
                "_integrate_parallel_task_result",
                side_effect=fake_integrate,
            ):
                result = orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertEqual(result.current_stage, "plan")
            self.assertEqual(result.rejected_stage, "plan")
            self.assertIn("rerun the owning planning stage", result.rejection_reason)
            self.assertEqual(result.last_recovery_route, {})
            self.assertTrue((project_root / "peer.txt").exists())
            self.assertEqual(
                {task.task_id: task.status for task in result.tasks},
                {"task-route": "pending", "task-peer": "done"},
            )

    def test_parallel_tasks_auto_workers_adapt_to_success_and_provider_pressure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = "auto"
            config.execution.parallel_tasks.max_auto_workers = 3
            config.execution.parallel_tasks.adaptive = True
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            self.assertEqual(orchestrator._parallel_worker_count(), 3)
            self.assertEqual(orchestrator._record_parallel_pressure(3), 1)
            self.assertEqual(orchestrator._parallel_worker_count(), 1)
            self.assertEqual(orchestrator._record_parallel_success(1), 2)
            self.assertEqual(orchestrator._parallel_worker_count(), 2)
            self.assertEqual(orchestrator._record_parallel_success(2), 3)
            self.assertEqual(orchestrator._parallel_worker_count(), 3)

    def test_parallel_provider_pressure_ignores_owned_proof_rate_limited_test_names(self) -> None:
        result = {
            "ok": False,
            "reason": (
                "owned proof evidence failed: tests/test_asset_consistency_runtime_api.py::"
                "AssetConsistencyRuntimeApiTests::"
                "test_rate_limited_asset_task_consistency_payload_preserves_retry_evidence"
            ),
            "review": "",
            "failure_ids": [
                "tests/test_asset_consistency_runtime_api.py::"
                "AssetConsistencyRuntimeApiTests::"
                "test_rate_limited_asset_task_consistency_payload_preserves_retry_evidence"
            ],
            "proof_evidence": {
                "ok": False,
                "failed_refs": [
                    "tests/test_asset_consistency_runtime_api.py::"
                    "AssetConsistencyRuntimeApiTests::"
                    "test_rate_limited_asset_task_consistency_payload_preserves_retry_evidence"
                ],
            },
        }

        self.assertFalse(Orchestrator._parallel_result_is_provider_pressure(result))

    def test_parallel_provider_pressure_detects_agent_provider_errors(self) -> None:
        pressure_reasons = [
            "All providers exhausted. Tried: codex. Last error: 429 rate limit exceeded",
            "parallel worktree execution failed: provider availability error",
            "implementation failed: stalled (no output) after 7200s",
        ]

        for reason in pressure_reasons:
            with self.subTest(reason=reason):
                self.assertTrue(
                    Orchestrator._parallel_result_is_provider_pressure(
                        {"ok": False, "reason": reason, "review": ""}
                    )
                )

    def test_parallel_tasks_auto_workers_support_copilot_pro_plus_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.active_provider = "copilot-cli"
            config.providers["copilot-cli"].subscription_tier = "pro+"
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = "auto"
            config.execution.parallel_tasks.max_auto_workers = 8
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            self.assertEqual(orchestrator._parallel_worker_count(), 2)
            self.assertEqual(orchestrator._record_parallel_success(2), 3)
            self.assertEqual(orchestrator._record_parallel_success(3), 4)
            self.assertEqual(orchestrator._parallel_worker_count(), 4)

    def test_parallel_tasks_fixed_workers_do_not_adapt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            config.execution.parallel_tasks.max_auto_workers = 8
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            self.assertEqual(orchestrator._parallel_worker_count(), 2)
            self.assertEqual(orchestrator._record_parallel_pressure(2), 2)
            self.assertEqual(orchestrator._parallel_worker_count(), 2)

    def test_parallel_tasks_logs_auto_resolution_and_single_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            config = orchestrator.config
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = "auto"
            config.execution.parallel_tasks.max_auto_workers = 3
            config.gates.require_clean_git_before_task = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Only ready task",
                            "description": "Run one ready task.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with patch.object(orchestrator, "_execute_task_in_main_worktree", return_value=None):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertIn("auto mode resolved workers=3", rendered)
            self.assertIn("ready=1 batch=1; executing sequentially task=task-001", rendered)

    def test_run_stops_after_implement_when_max_task_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec = project_root / "SPEC.md"
            spec.write_text("# demo\n", encoding="utf-8")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.approvals.enabled = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.status = "pending"
            state.current_stage = "implement"
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
            }
            save_run_state(project_root, state)

            def exhaust_budget(run_state, max_tasks=None):
                orchestrator._task_budget_exhausted = True
                run_state.stage_summaries["implement"] = "Processed 1 task(s)."
                return run_state

            with patch.object(orchestrator, "_run_implementation_loop", side_effect=exhaust_budget):
                with patch.object(orchestrator, "_run_verify") as verify_mock:
                    result = orchestrator.run(spec, auto_approve=True, max_tasks=1, skip_validate=True)

            self.assertEqual(result.status, "pending")
            self.assertEqual(result.current_stage, "implement")
            self.assertIn("implement", result.stage_summaries)
            verify_mock.assert_not_called()


class IterationAdapter:
    """Adapter that tracks stage calls for iteration testing.

    On the plan stage it writes only the new active iteration tasks.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.stage_calls: list[str] = []

    def run(self, request):
        self.stage_calls.append(request.stage)
        if request.stage == "clarify":
            write_text(request.output_path, "Clarified iteration scope.\nREADY_TO_GENERATE\n")
        elif request.stage == "plan":
            tp = task_plan_path(self.project_root)
            new_task = {
                "task_id": "task-002",
                "title": "New iteration task",
                "description": "Task added in iteration.",
                "acceptance": ["new feature works"],
                "status": "pending",
                "commit_message": "",
                "test_generated": True,
            }
            write_json(tp, {
                "test_strategy": "python-pytest",
                "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                "tasks": [new_task],
            })
            write_text(request.output_path, "iteration plan\n")
        elif request.stage == "implement":
            write_text(self.project_root / "iter_artifact.txt", "done\n")
            write_text(request.output_path, "implemented iteration task\n")
        elif request.stage == "review":
            summary = "DECISION: pass\niteration review passed\n"
            write_text(request.output_path, summary)
            return AgentResult(
                ok=True, command=["fake"], output_path=request.output_path,
                summary=summary.strip(), returncode=0,
            )
        elif request.stage == "readme":
            if "Do NOT write the README yet. Only outline the planned sections." in request.prompt:
                write_text(request.output_path, "- Overview\n- Architecture\n- Usage\n")
            else:
                readme_content = (
                    "# Demo\n## Overview\nA demo project.\n"
                    "## Architecture\nSimple layout.\n"
                    "## Usage\n```bash\npython main.py\n```\n"
                    "## Development\nRun tests.\n"
                )
                write_text(self.project_root / "README.md", readme_content)
                write_text(request.output_path, "readme updated\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True, command=["fake"], output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class IterationFlowTests(unittest.TestCase):
    """Tests for starting a new iteration from a completed project."""

    def _make_completed_project(self, tmp):
        """Create a project with status=completed and one done task."""
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")

        # Disable approval gates so run completes without pausing
        config = load_project_config(project_root)
        config.approvals.enabled = []
        config.gates.commands = []
        config.gates.require_clean_git_before_task = False
        config.gates.allow_agent_updates = False
        save_project_config(project_root, config)

        # Seed a completed run state with one done task
        from auto_agents.config import save_run_state as _save
        from auto_agents.models import RunState, TaskSpec
        state = load_run_state(project_root)
        state.status = "completed"
        state.current_stage = "readme"
        state.stage_summaries = {
            "clarify": "done", "design": "done", "plan": "done",
            "implement": "done", "verify": "done", "readme": "done",
        }
        state.approved_gates = ["requirements", "architecture", "release"]
        state.agent_attempts = {"clarify": 1, "design": 1, "plan": 1}
        state.task_review_cache = {"task-001": {"decision": "pass"}}
        state.tasks = [
            TaskSpec(
                task_id="task-001", title="Phase 1 task",
                description="Already done.", acceptance=["done"],
                status="done", commit_message="feat: phase1",
            )
        ]
        _save(project_root, state)

        # Persist the done task into task_plan.json too
        write_json(task_plan_path(project_root), {
            "tasks": [state.tasks[0].to_dict()]
        })

        spec_file = project_root / "spec.md"
        spec_file.write_text("# Spec\nPhase 2 features.", encoding="utf-8")

        # Create a fake conda env so verification fast-fail check passes
        (project_root / ".conda" / "conda-meta").mkdir(parents=True, exist_ok=True)

        return project_root, spec_file

    def test_readme_auto_approve_never_prompts_for_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root, spec_file = self._make_completed_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = IterationAdapter(project_root)
            orchestrator._user_input_fn = lambda _prompt: (_ for _ in ()).throw(
                AssertionError("--auto-approve must not read README feedback from stdin")
            )

            state = orchestrator._run_readme(
                load_run_state(project_root),
                spec_file,
                auto_approve=True,
            )

            self.assertIn("readme", state.stage_summaries)
            self.assertTrue((project_root / "README.md").exists())

    def test_iteration_resets_state_fields(self):
        """approved_gates, agent_attempts and task_review_cache must be
        cleared when a new iteration starts."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, spec_file = self._make_completed_project(tmp)

            # Add a distinctive old agent_attempts key that won't recur
            from auto_agents.config import save_run_state as _save
            state = load_run_state(project_root)
            state.agent_attempts["implement-task-001"] = 3
            _save(project_root, state)

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = IterationAdapter(project_root)

            old_run_id = state.run_id

            # Simulate user answering "y" to the iteration prompt
            orchestrator._user_input_fn = lambda _prompt: "y"
            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertNotEqual(state.run_id, old_run_id, "New run_id should be generated")
            self.assertEqual(state.status, "completed")
            task_archive = archived_task_plan_path(project_root, old_run_id)
            state_archive = archived_run_state_path(project_root, old_run_id)
            self.assertTrue(task_archive.exists())
            self.assertTrue(state_archive.exists())
            archived_plan = json.loads(task_archive.read_text(encoding="utf-8"))
            self.assertEqual(archived_plan["tasks"][0]["task_id"], "task-001")
            self.assertEqual(archived_plan["tasks"][0]["status"], "done")
            self.assertEqual(state.resume_context["previous_run_id"], old_run_id)
            self.assertEqual(state.resume_context["previous_task_plan_archive"], str(task_archive))
            # Old implement-task-001 attempt count should be gone
            self.assertNotIn("implement-task-001", state.agent_attempts,
                             "Old agent_attempts should have been cleared at iteration start")
            # Old task_review_cache should be gone
            self.assertNotIn("task-001", state.task_review_cache,
                             "Old task_review_cache should have been cleared")

    def test_restart_blocked_archives_nonempty_plan_and_legacy_pending_run(self):
        for run_status in ("blocked", "pending"):
            with self.subTest(run_status=run_status), tempfile.TemporaryDirectory() as tmp:
                project_root = Path(tmp) / "demo"
                Orchestrator.init_project(project_root, "demo", "mock")
                commit_all(project_root, "chore: bootstrap test project")
                orchestrator = Orchestrator(project_root)
                state = load_run_state(project_root)
                old_run_id = state.run_id
                blocked_task = TaskSpec(
                    task_id="repair-task-001",
                    title="Blocked proof repair",
                    description="External proof is unavailable.",
                    acceptance=["The proof is available."],
                    status="blocked",
                    task_origin="evidence_repair",
                )
                state.status = run_status
                state.current_stage = "implement"
                state.tasks = [blocked_task]
                state.last_error = "review rejected the task"
                save_run_state(project_root, state)
                write_json(
                    task_plan_path(project_root),
                    {"tasks": [blocked_task.to_dict()]},
                )

                with (
                    patch.object(orchestrator, "_pending_stages", return_value=[]),
                    patch.object(orchestrator, "_commit_if_dirty"),
                ):
                    restarted = orchestrator.run(
                        spec_file=project_root / "spec.md",
                        restart_blocked=True,
                        skip_validate=True,
                    )

                self.assertNotEqual(restarted.run_id, old_run_id)
                self.assertEqual(restarted.status, "completed")
                self.assertEqual(
                    restarted.resume_context["restarted_blocked_run_id"],
                    old_run_id,
                )
                self.assertEqual(load_task_plan(project_root)["tasks"], [])
                archived_plan = json.loads(
                    archived_task_plan_path(
                        project_root,
                        old_run_id,
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    archived_plan["tasks"][0]["task_id"],
                    "repair-task-001",
                )
                self.assertEqual(
                    archived_plan["tasks"][0]["status"],
                    "blocked",
                )

    def test_restart_blocked_rejects_active_run_without_blocked_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            commit_all(project_root, "chore: bootstrap test project")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            pending_task = TaskSpec(
                task_id="task-001",
                title="Pending work",
                description="Work has not started.",
                acceptance=["The work completes."],
                status="pending",
            )
            state.status = "pending"
            state.tasks = [pending_task]
            save_run_state(project_root, state)
            write_json(
                task_plan_path(project_root),
                {"tasks": [pending_task.to_dict()]},
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "--restart-blocked requires the active run to be blocked",
            ):
                orchestrator.restart_blocked_run()

            self.assertEqual(load_run_state(project_root).status, "pending")
            self.assertEqual(
                load_task_plan(project_root)["tasks"][0]["status"],
                "pending",
            )

    def test_resume_prunes_historically_covered_pending_tasks_and_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Keep the old capability working.",
                            "source": "history",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["The old API response still works."],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        },
                        {
                            "id": "REQ-002",
                            "text": "Implement the new behavior.",
                            "source": "new scope",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["The new API response is strict."],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        },
                    ],
                },
            )
            old_run_id = "oldrun123"
            archive_path = archived_task_plan_path(project_root, old_run_id)
            write_json(
                archive_path,
                {
                    "oracle_proof_schema_version": 1,
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Old capability",
                            "description": "Previously delivered requirement.",
                            "acceptance": ["The old API response still works."],
                            "status": "done",
                            "commit_message": "",
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "proof_type": "integration_test",
                                    "oracle_strength": "behavioral",
                                    "evidence_boundary": "system_boundary",
                                    "evidence_refs": ["tests/test_old_api.py::test_contract"],
                                    "forbidden_proxy_oracles": [],
                                    "proxy_oracles": [],
                                    "status": "verified",
                                }
                            ],
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "tasks": [
                        {
                            "task_id": "task-stale",
                            "title": "Lock old capability",
                            "description": "Stale historical coverage task.",
                            "acceptance": ["The old API response still works."],
                            "status": "pending",
                            "commit_message": "",
                            "requirement_ids": ["REQ-001"],
                            "depends_on": [],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "proof_type": "integration_test",
                                    "oracle_strength": "behavioral",
                                    "evidence_boundary": "system_boundary",
                                    "evidence_refs": ["tests/test_old_api.py::test_contract"],
                                    "forbidden_proxy_oracles": [],
                                    "proxy_oracles": [],
                                    "status": "planned",
                                }
                            ],
                        },
                        {
                            "task_id": "repair-task-stale-r1-1",
                            "title": "Repair task-stale proof evidence",
                            "description": "Stale repair task.",
                            "acceptance": ["proof passes"],
                            "status": "pending",
                            "commit_message": "",
                            "requirement_ids": [],
                            "depends_on": [],
                            "parent_task_id": "task-stale",
                            "task_origin": "evidence_repair",
                            "verification_refs": ["tests/test_old_api.py::test_contract"],
                        },
                        {
                            "task_id": "task-new",
                            "title": "Implement new behavior",
                            "description": "Current iteration work.",
                            "acceptance": ["The new API response is strict."],
                            "status": "pending",
                            "commit_message": "",
                            "requirement_ids": ["REQ-002"],
                            "depends_on": ["repair-task-stale-r1-1"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-002",
                                    "oracle_index": 1,
                                    "proof_type": "integration_test",
                                    "oracle_strength": "behavioral",
                                    "evidence_boundary": "system_boundary",
                                    "evidence_refs": ["tests/test_new_api.py::test_contract"],
                                    "forbidden_proxy_oracles": [],
                                    "proxy_oracles": [],
                                    "status": "planned",
                                }
                            ],
                        },
                    ],
                },
            )

            state = load_run_state(project_root)
            state.current_stage = "implement"
            state.tasks = orchestrator._load_tasks_from_plan()
            state.resume_context = {
                "previous_run_id": old_run_id,
                "previous_task_plan_archive": str(archive_path),
            }
            state.rejected_stage = "implement"
            state.rejection_reason = "stale requirement coverage"

            changed = orchestrator._normalize_historically_covered_iteration_resume(state)

            self.assertTrue(changed)
            self.assertEqual([task.task_id for task in state.tasks], ["task-new"])
            self.assertEqual(state.tasks[0].depends_on, [])
            self.assertEqual(state.rejected_stage, "")
            reloaded = orchestrator._load_tasks_from_plan()
            self.assertEqual([task.task_id for task in reloaded], ["task-new"])

    def test_iteration_runs_implement_for_new_tasks(self):
        """After plan appends new pending tasks during iteration, the
        implement stage must execute them (dynamic pending-stages loop)."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, spec_file = self._make_completed_project(tmp)
            orchestrator = Orchestrator(project_root)
            adapter = IterationAdapter(project_root)
            orchestrator.adapter = adapter

            orchestrator._user_input_fn = lambda _prompt: "y"
            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual([task.task_id for task in state.tasks], ["task-002"])
            self.assertEqual(state.tasks[0].title, "New iteration task")
            self.assertEqual(state.tasks[0].status, "done")
            # implement must have been called
            self.assertIn("implement", adapter.stage_calls,
                          "Implement stage should run for new pending tasks")

    def test_iteration_without_auto_approve_pauses_at_gate(self):
        """Without --auto-approve the iteration should pause at the first
        approval gate (requirements) after clarify."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, spec_file = self._make_completed_project(tmp)

            # Re-enable the requirements gate
            config = load_project_config(project_root)
            config.approvals.enabled = ["requirements"]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = IterationAdapter(project_root)

            # First call returns "y" for iteration prompt; subsequent
            # calls return default (empty) which the interactive clarify
            # path interprets as "nothing to add, proceed".
            call_count = [0]
            def mock_input(prompt):
                call_count[0] += 1
                if call_count[0] == 1:
                    return "y"
                return ""
            orchestrator._user_input_fn = mock_input

            state = orchestrator.run(spec_file=spec_file, auto_approve=False)

            self.assertEqual(state.status, "paused")
            self.assertEqual(state.pending_approval, "requirements")
            # approved_gates should be empty (cleared at iteration start)
            self.assertEqual(state.approved_gates, [])

    def test_auto_approve_still_runs_interactive_clarify(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\nPhase 1 features.\n", encoding="utf-8")

            config = load_project_config(project_root)
            config.approvals.enabled = ["requirements"]
            save_project_config(project_root, config)

            state = load_run_state(project_root)
            state.stage_summaries = {
                "design": "done",
                "plan": "done",
                "provider_research": "done",
                "implement": "done",
                "verify": "done",
                "readme": "done",
            }
            from auto_agents.models import TaskSpec

            state.tasks = [
                TaskSpec(
                    task_id="task-001",
                    title="Existing task",
                    description="Already complete.",
                    acceptance=["done"],
                    status="done",
                    commit_message="feat: done",
                )
            ]
            save_run_state(project_root, state)

            orchestrator = Orchestrator(project_root)
            interactive_calls: list[str] = []

            def fake_interactive(state, clarify_spec_file):
                interactive_calls.append(str(clarify_spec_file))
                state.current_stage = "clarify"
                state.stage_summaries["clarify"] = "clarified"
                state.last_error = ""
                return state

            orchestrator._run_interactive_clarify = fake_interactive

            state = orchestrator.run(spec_file=spec_file, auto_approve=True, skip_validate=True)

            self.assertEqual(interactive_calls, [str(spec_file)])
            self.assertEqual(state.status, "completed")
            self.assertEqual(state.pending_approval, "")
            self.assertIn("requirements", state.approved_gates)

    def test_reject_architecture_clears_downstream_state(self):
        """Rejecting architecture should clear design+ downstream summaries
        and remove architecture/release approvals."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, _spec_file = self._make_completed_project(tmp)

            orchestrator = Orchestrator(project_root)
            state = orchestrator.reject("architecture", "Need to redesign iteration scope")

            self.assertEqual(state.status, "pending")
            self.assertEqual(state.rejected_stage, "design")
            self.assertEqual(state.rejection_reason, "Need to redesign iteration scope")

            # clarify should remain; design and downstream must be removed.
            self.assertIn("clarify", state.stage_summaries)
            self.assertNotIn("design", state.stage_summaries)
            self.assertNotIn("plan", state.stage_summaries)
            self.assertNotIn("implement", state.stage_summaries)
            self.assertNotIn("verify", state.stage_summaries)
            self.assertNotIn("readme", state.stage_summaries)

            # requirements can remain approved; architecture/release must reset.
            self.assertIn("requirements", state.approved_gates)
            self.assertNotIn("architecture", state.approved_gates)
            self.assertNotIn("release", state.approved_gates)


class RepeatReviewBlockerAdapter:
    """Implement touches code on every attempt; review always returns the same blockers.

    Used to trigger the scope-overflow fingerprint signal.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            (self.project_root / f"artifact-{self.implement_calls}.txt").write_text(
                f"attempt-{self.implement_calls}\n", encoding="utf-8"
            )
            summary = f"implement attempt {self.implement_calls}\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = (
                "DECISION: fail\n"
                "Core issue: task bundles backend, API, and UI.\n"
                "- Split backend lifecycle from API surface.\n"
                "- Split workbench UI from server changes.\n"
            )
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ScopeOverflowTests(unittest.TestCase):
    def test_review_fingerprint_normalizes_and_matches(self) -> None:
        a = Orchestrator._review_fingerprint(
            "DECISION: fail\nCore issue: scope too large.\n- Split backend from UI.\n"
        )
        b = Orchestrator._review_fingerprint(
            "  decision: fail\n  core issue: scope too large.  \n  - split backend from ui.  \n"
        )
        c = Orchestrator._review_fingerprint("DECISION: pass\n")
        self.assertTrue(a)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(Orchestrator._review_fingerprint("   "), "")

    def test_repeated_review_blockers_trigger_plan_rewind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.implement = 4
            save_project_config(project_root, config)
            subprocess.run(["git", "config", "user.name", "test"], cwd=str(project_root), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(project_root), check=True)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepeatReviewBlockerAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-big",
                            "title": "Cross-cutting task",
                            "description": "Bundles too many concerns.",
                            "acceptance": ["all layers updated"],
                            "status": "pending",
                            "commit_message": "",
                            "split_depth": 0,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            # Rewind: rejected_stage set to plan, plan summary cleared.
            self.assertEqual(result.rejected_stage, "plan")
            self.assertIn("SPLIT_TASK:", result.rejection_reason)
            self.assertIn("task-big", result.rejection_reason)
            self.assertNotIn("plan", result.stage_summaries)
            # Task reset to pending (not blocked) so plan can split it.
            self.assertEqual(result.tasks[0].status, "pending")
            self.assertEqual(changed_paths(project_root), [])
            self.assertFalse((project_root / "artifact-1.txt").exists())
            self.assertFalse((project_root / "artifact-2.txt").exists())
            # Two review failures are enough to trigger the signal (attempt 1 records
            # fingerprint, attempt 2 matches it).
            self.assertGreaterEqual(orchestrator.adapter.review_calls, 2)

    def test_verify_failure_does_not_count_as_review_failure_for_arbiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 4
            save_project_config(project_root, config)
            subprocess.run(["git", "config", "user.name", "test"], cwd=str(project_root), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(project_root), check=True)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepeatReviewBlockerAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-big",
                            "title": "Cross-cutting task",
                            "description": "Bundles too many concerns.",
                            "acceptance": ["all layers updated"],
                            "status": "pending",
                            "commit_message": "",
                            "split_depth": 0,
                        }
                    ]
                },
            )
            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            verify_failure = {
                "ok": False,
                "reason": "one new verification failure",
                "failure_ids": ["tests/test_demo.py::test_regression"],
                "current_failure_ids": ["tests/test_demo.py::test_regression"],
                "baseline_failure_ids": [],
                "new_failure_ids": ["tests/test_demo.py::test_regression"],
                "raw_output": "FAILED tests/test_demo.py::test_regression",
                "comparable_failures": True,
            }
            verify_pass = {
                "ok": True,
                "reason": "all commands passed",
                "current_failure_ids": [],
                "proof_evidence": {},
            }

            with patch.object(
                orchestrator,
                "_run_task_verify",
                side_effect=[verify_failure, verify_pass, verify_pass],
            ), patch.object(
                orchestrator,
                "_run_scope_arbiter",
                return_value={
                    "decision": "SPLIT",
                    "rationale": "should require two real review failures",
                    "split_axis": ["backend", "frontend"],
                },
            ) as arbiter:
                result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 3)
            self.assertEqual(orchestrator.adapter.review_calls, 2)
            arbiter.assert_not_called()
            self.assertEqual(result.rejected_stage, "plan")

    def test_scope_overflow_rewind_failure_stops_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.implement = 4
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepeatReviewBlockerAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-big",
                            "title": "Cross-cutting task",
                            "description": "Bundles too many concerns.",
                            "acceptance": ["all layers updated"],
                            "status": "pending",
                            "commit_message": "",
                            "split_depth": 0,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            import auto_agents.orchestrator as orch_mod

            original_reset = orch_mod.hard_reset_clean
            try:
                orch_mod.hard_reset_clean = lambda *_args, **_kwargs: False
                with self.assertRaises(RuntimeError) as ctx:
                    orchestrator._run_implementation_loop(state, max_tasks=1)
            finally:
                orch_mod.hard_reset_clean = original_reset

            self.assertIn("scope-overflow rewind failed to restore the baseline", str(ctx.exception))

    def test_split_depth_cap_blocks_instead_of_rewinding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.implement = 4
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepeatReviewBlockerAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-child",
                            "title": "Already-split child",
                            "description": "Split lineage has reached the cap.",
                            "acceptance": ["criterion"],
                            "status": "pending",
                            "commit_message": "",
                            "split_depth": Orchestrator.MAX_SPLIT_DEPTH,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            reloaded_tasks = orchestrator._load_tasks_from_plan()
            self.assertEqual(reloaded_tasks[0].status, "blocked")
            reloaded_state = load_run_state(project_root)
            self.assertNotEqual(reloaded_state.rejected_stage, "plan")

    def test_expected_test_migrations_excluded_from_new_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            from auto_agents.models import TaskSpec as _TaskSpec
            task = _TaskSpec(
                task_id="t",
                title="t",
                description="",
                acceptance=[],
                verify_baseline_failures=["old:legacy_case"],
                expected_test_migrations=["new:migrated_case"],
            )
            # Monkeypatch gate runner to return a fixed failure set that includes
            # one pre-existing failure (baseline) and one expected migration.
            class _Gate:
                ok = False
                summary = "new:migrated_case FAILED\nold:legacy_case FAILED"
                stdout = summary
                stderr = ""
                returncode = 1
                commands = []

            import auto_agents.orchestrator as orch_mod
            original_extract = orch_mod.extract_failure_ids
            try:
                orch_mod.extract_failure_ids = lambda gate: ["new:migrated_case", "old:legacy_case"]
                config.gates.commands = ["echo run"]
                orchestrator.config = config
                orchestrator._run_gate_commands_for_commands = (
                    lambda *args, **kwargs: (_Gate(), "")
                )
                result = orchestrator._run_task_verify(task)
            finally:
                orch_mod.extract_failure_ids = original_extract

            # Migration is excluded; baseline failure is also excluded → verify passes.
            self.assertTrue(result["ok"], msg=str(result))

    def test_full_verify_failure_routes_to_implement_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec(
                    task_id="task-001",
                    title="Existing task",
                    description="Already implemented.",
                    acceptance=["current contract is implemented"],
                    status="done",
                )
            ]
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "implement": "Completed 1 tasks.",
            }
            save_run_state(project_root, state)
            gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command="fake test",
                        ok=False,
                        returncode=1,
                        stdout="FAILED tests/test_api.py::test_old_contract - AssertionError: old field",
                    )
                ],
                summary="FAILED tests/test_api.py::test_old_contract",
            )

            with patch.object(orchestrator, "_run_gate_commands", return_value=(gate, "")):
                updated = orchestrator._run_verify(state)

            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.current_stage, "implement")
            self.assertEqual(updated.rejected_stage, "implement")
            self.assertEqual(updated.agent_attempts["verify_recovery"], 1)
            self.assertEqual(updated.verify_recovery_refs, ["cmd:fake test"])
            self.assertNotIn("verify", updated.stage_summaries)
            self.assertIn("Failure type: full_verification", updated.rejection_reason)
            self.assertIn("update repository tests only when they are stale", updated.rejection_reason)
            self.assertIn("[stage:verify] decision=fail route=implement", stream.getvalue())

    def test_full_verify_recovery_exhaustion_routes_to_clarify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec(
                    task_id="task-001",
                    title="Existing task",
                    description="Already implemented.",
                    acceptance=["current contract is implemented"],
                    status="done",
                )
            ]
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "implement": "Completed 1 tasks.",
            }
            state.agent_attempts["verify_recovery"] = orchestrator._verify_gate_recovery_limit()
            gate = GateResult(
                ok=False,
                commands=[CommandResult(command="fake test", ok=False, returncode=1)],
                summary="FAILED tests/test_api.py::test_still_fails",
            )

            with patch.object(orchestrator, "_run_gate_commands", return_value=(gate, "")):
                updated = orchestrator._run_verify(state)

            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.current_stage, "clarify")
            self.assertEqual(updated.rejected_stage, "clarify")
            self.assertNotIn("verify_recovery", updated.agent_attempts)
            self.assertIn("Automatic full verification recovery was exhausted", updated.rejection_reason)
            self.assertIn("Use the clarify conversation", updated.rejection_reason)
            self.assertIn("[stage:verify] decision=fail route=clarify", stream.getvalue())


class VaryingReviewArbiterAdapter:
    """Implement touches code; review always fails with VARYING wording so the
    static fingerprint signal never matches; arbiter returns a configurable
    decision."""

    def __init__(self, project_root: Path, arbiter_decision: str = "SPLIT", arbiter_text: Optional[str] = None) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.arbiter_calls = 0
        self.arbiter_decision = arbiter_decision
        self.arbiter_text = arbiter_text

    def run(self, request):
        from auto_agents.adapters.base import AgentResult as _AR
        if request.stage == "implement":
            self.implement_calls += 1
            (self.project_root / f"artifact-{self.implement_calls}.txt").write_text(
                f"attempt-{self.implement_calls}\n", encoding="utf-8"
            )
            summary = f"implement attempt {self.implement_calls}\n"
        elif request.stage == "review":
            self.review_calls += 1
            summary = (
                "DECISION: fail\n"
                f"This is review #{self.review_calls} with unique wording {self.review_calls}.\n"
                f"Acceptance criterion {self.review_calls} is not satisfied.\n"
            )
        elif request.stage == "arbiter":
            self.arbiter_calls += 1
            if self.arbiter_text is not None:
                summary = self.arbiter_text
            elif self.arbiter_decision == "SPLIT":
                summary = (
                    "DECISION: SPLIT\n"
                    "RATIONALE: task spans backend and UI which keep alternating as blockers.\n"
                    "SPLIT_AXIS:\n"
                    "- backend: extract data layer change\n"
                    "- UI: extract surface change\n"
                )
            else:
                summary = (
                    "DECISION: CONTINUE\n"
                    "RATIONALE: implementer is close; one more sharp attempt should converge.\n"
                )
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return _AR(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ScopeArbiterTests(unittest.TestCase):
    def _make_project(self, tmp: str, with_review_history: int = 0) -> Tuple[Path, Orchestrator]:
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        orchestrator = Orchestrator(project_root)
        config = orchestrator.config
        config.gates.commands = []
        config.retries.implement = 4
        save_project_config(project_root, config)
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(project_root), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(project_root), check=True)
        commit_all(project_root, "baseline")
        orchestrator = Orchestrator(project_root)
        history = []
        for i in range(with_review_history):
            history.append({
                "attempt": i + 1,
                "summary": f"DECISION: fail\nprior review {i+1}",
            })
        write_json(
            task_plan_path(project_root),
            {
                "tasks": [
                    {
                        "task_id": "task-arb",
                        "title": "Cross-cutting task",
                        "description": "Bundles several layers.",
                        "acceptance": ["all layers updated"],
                        "status": "blocked" if with_review_history else "pending",
                        "commit_message": "",
                        "split_depth": 0,
                        "review_history": history,
                    }
                ]
            },
        )
        if with_review_history:
            commit_all(project_root, "test: persist blocked task baseline")
        return project_root, orchestrator

    def test_arbiter_parses_split_and_continue(self) -> None:
        split = Orchestrator._parse_arbiter_decision(
            "DECISION: SPLIT\nRATIONALE: too coupled.\nSPLIT_AXIS:\n- a\n- b\n"
        )
        self.assertEqual(split["decision"], "SPLIT")
        self.assertEqual(split["rationale"], "too coupled.")
        self.assertEqual(split["split_axis"], ["a", "b"])

        cont = Orchestrator._parse_arbiter_decision("DECISION: CONTINUE\nRATIONALE: close.\n")
        self.assertEqual(cont["decision"], "CONTINUE")
        self.assertEqual(cont["split_axis"], [])

        bad = Orchestrator._parse_arbiter_decision("garbage output")
        self.assertEqual(bad["decision"], "")

    def test_arbiter_split_triggers_rewind_when_fingerprints_vary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp)
            orchestrator.adapter = VaryingReviewArbiterAdapter(project_root, arbiter_decision="SPLIT")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.rejected_stage, "plan")
            self.assertIn("SPLIT_TASK:", result.rejection_reason)
            self.assertIn("Scope arbiter verdict: SPLIT", result.rejection_reason)
            self.assertIn("backend", result.rejection_reason)
            self.assertGreaterEqual(orchestrator.adapter.arbiter_calls, 1)
            self.assertEqual(result.tasks[0].status, "pending")
            self.assertTrue(result.tasks[0].arbitration_history)
            self.assertEqual(result.tasks[0].arbitration_history[-1]["decision"], "SPLIT")

    def test_arbiter_continue_lets_loop_exhaust_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp)
            orchestrator.config.execution.recovery.enabled = False
            orchestrator.adapter = VaryingReviewArbiterAdapter(project_root, arbiter_decision="CONTINUE")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            reloaded_tasks = orchestrator._load_tasks_from_plan()
            self.assertEqual(reloaded_tasks[0].status, "blocked")
            reloaded_state = load_run_state(project_root)
            self.assertNotEqual(reloaded_state.rejected_stage, "plan")
            self.assertEqual(orchestrator.adapter.review_calls, 4)
            self.assertGreaterEqual(orchestrator.adapter.arbiter_calls, 3)

    def test_arbiter_consulted_on_first_fail_when_history_already_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp, with_review_history=2)
            orchestrator.adapter = VaryingReviewArbiterAdapter(project_root, arbiter_decision="SPLIT")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.rejected_stage, "plan")
            self.assertIn("Scope arbiter verdict: SPLIT", result.rejection_reason)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(orchestrator.adapter.arbiter_calls, 1)

    def test_arbiter_unparseable_output_falls_back_to_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp)
            orchestrator.config.execution.recovery.enabled = False
            orchestrator.adapter = VaryingReviewArbiterAdapter(
                project_root, arbiter_text="this is not parseable at all"
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            reloaded_tasks = orchestrator._load_tasks_from_plan()
            self.assertEqual(reloaded_tasks[0].status, "blocked")
            self.assertEqual(orchestrator.adapter.review_calls, 4)


if __name__ == "__main__":
    unittest.main()
