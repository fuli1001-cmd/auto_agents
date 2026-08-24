from __future__ import annotations

import hashlib
import tempfile
import unittest
import sys
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gates import GateCommandBaselineIdentityError
from auto_agents.git_ops import (
    add_worktree,
    changed_files,
    changed_paths,
    commit_all,
    hard_reset_clean,
    head_ref,
    ref_exists,
    update_ref,
)
from auto_agents.io_utils import read_json, write_json, write_text
from auto_agents.models import CommandResult, GateResult, RunState, TaskSpec
from auto_agents.orchestrator import (
    VERIFY_BASELINE_SCHEMA_VERSION,
    Orchestrator,
)


class RecoveryResilienceTests(unittest.TestCase):
    def _project(self, root: Path) -> Orchestrator:
        Orchestrator.init_project(root, "demo", "mock")
        return Orchestrator(root)

    def test_non_comparable_test_baseline_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="baseline",
                description="",
                acceptance=[],
            )
            command = "python -m pytest -q tests/test_demo.py"
            failed = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stderr="pytest could not start",
                    )
                ],
                summary="command failed",
            )
            orchestrator._build_task_verify_commands = Mock(
                return_value=[command]
            )
            orchestrator._run_missing_baseline_commands = Mock(
                return_value=(failed, "")
            )
            orchestrator._run_verify_failure_identity_diagnostic = Mock(
                return_value=failed
            )
            orchestrator._gate_baseline_cache.put = Mock()

            with self.assertRaises(GateCommandBaselineIdentityError) as raised:
                orchestrator._ensure_task_verify_baseline(task)

            self.assertIsNotNone(raised.exception.result)
            self.assertFalse(raised.exception.result.infrastructure_error)
            self.assertFalse(raised.exception.result.infrastructure_failure_id)
            identity = raised.exception.result.process_snapshot[
                "baseline_failure_identity"
            ]
            self.assertEqual(identity["repair_scope"], "verification_contract")
            self.assertEqual(task.verify_baseline_ref, "")
            self.assertEqual(task.verify_baseline_failures, [])
            self.assertEqual(task.verify_baseline_schema_version, 0)
            orchestrator._gate_baseline_cache.put.assert_not_called()

    def test_bracketed_vitest_suite_baseline_has_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            test_file = "src/e2e/setup.test.ts"
            gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=f"npm exec -- vitest run {test_file}",
                        ok=False,
                        returncode=1,
                        stdout=(
                            f" FAIL  {test_file} [ {test_file} ]\n"
                            "Error: Hook timed out in 90000ms.\n"
                            " Test Files  1 failed (1)\n"
                            "      Tests  17 skipped (17)\n"
                        ),
                    )
                ],
                summary="one failed suite",
            )
            orchestrator._run_verify_failure_identity_diagnostic = Mock(
                side_effect=AssertionError(
                    "a stable suite identity must not require a diagnostic rerun"
                )
            )

            failures = orchestrator._validated_baseline_failures(
                gate,
                context="task baseline verification commands",
            )

            self.assertEqual(failures, [test_file])

    def test_stable_test_baseline_is_versioned_after_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="baseline",
                description="",
                acceptance=[],
            )
            command = "python -m pytest -q tests/test_demo.py"
            failed = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout="FAILED tests/test_demo.py::test_contract - assert 1 == 2",
                    )
                ],
                summary="one failed",
            )
            orchestrator._build_task_verify_commands = Mock(
                return_value=[command]
            )
            orchestrator._run_missing_baseline_commands = Mock(
                return_value=(failed, "")
            )
            orchestrator._gate_baseline_cache.get = Mock(
                side_effect=[None, ["tests/test_demo.py::test_contract"]]
            )
            orchestrator._gate_baseline_cache.put = Mock()

            changed = orchestrator._ensure_task_verify_baseline(task)

            self.assertTrue(changed)
            self.assertTrue(task.verify_baseline_ref)
            self.assertEqual(
                task.verify_baseline_failures,
                ["tests/test_demo.py::test_contract"],
            )
            self.assertEqual(
                task.verify_baseline_schema_version,
                VERIFY_BASELINE_SCHEMA_VERSION,
            )

    def test_legacy_command_test_baseline_requeues_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="baseline",
                description="",
                acceptance=[],
                status="blocked",
                verify_baseline_ref="deadbeef:dirty",
                verify_baseline_failures=[
                    "cmd:python -m pytest -q tests/test_demo.py"
                ],
            )
            state = RunState(
                run_id="run-001",
                status="blocked",
                current_stage="implement",
                tasks=[task],
                last_recovery_route={
                    "task_id": task.task_id,
                    "outcome": "exhausted",
                },
            )

            self.assertTrue(
                orchestrator._normalize_legacy_verify_baselines(state)
            )
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.verify_baseline_ref, "")
            self.assertEqual(task.verify_retry_epoch, 1)
            self.assertEqual(task.recovery_epoch, 1)
            self.assertEqual(
                task.verify_baseline_schema_version,
                VERIFY_BASELINE_SCHEMA_VERSION,
            )
            self.assertFalse(
                orchestrator._normalize_legacy_verify_baselines(state)
            )

    def test_legacy_provider_baseline_rewinds_to_owner_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="provider baseline",
                description="",
                acceptance=[],
                status="blocked",
                review_history=[
                    {
                        "attempt": 1,
                        "summary": (
                            "REQ-102 canonical provider reference lacks "
                            "the required size contract"
                        ),
                    }
                ],
                verify_baseline_ref="deadbeef:dirty",
                verify_baseline_failures=[
                    "cmd:python -m pytest -q tests/test_provider.py"
                ],
            )
            state = RunState(
                run_id="run-001",
                status="blocked",
                current_stage="implement",
                tasks=[task],
            )
            reference = (
                ".auto-agents/docs/provider_references/provider.md"
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value={reference}
            )
            orchestrator._mark_provider_references_needs_refresh = Mock(
                return_value=[reference]
            )

            self.assertTrue(
                orchestrator._normalize_legacy_verify_baselines(state)
            )

            self.assertEqual(state.current_stage, "provider_research")
            self.assertEqual(state.rejected_stage, "provider_research")
            self.assertIn("legacy invalid", state.rejection_reason)
            orchestrator._mark_provider_references_needs_refresh.assert_called_once()

    def test_same_failures_with_changed_candidate_do_not_stop_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="retry",
                description="",
                acceptance=[],
                verify_baseline_schema_version=VERIFY_BASELINE_SCHEMA_VERSION,
            )
            failure_id = "tests/test_demo.py::test_contract"
            orchestrator._record_verify_result(
                task,
                1,
                "fail",
                "failed",
                [failure_id],
            )
            write_text(root / "candidate.py", "changed = True\n")

            analysis = orchestrator._analyze_verify_failure(
                task,
                [failure_id],
            )

            self.assertFalse(analysis["stop_retry"])

    def test_provider_reference_verify_failure_routes_to_owner_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="provider contract",
                description="",
                acceptance=[],
                review_history=[
                    {
                        "attempt": 1,
                        "summary": "REQ-102 canonical document is incomplete",
                    }
                ],
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value={
                    ".auto-agents/docs/provider_references/provider.md"
                }
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": (
                        "tests/test_contract.py::"
                        "test_canonical_reference_records_sizes failed"
                    ),
                    "failure_ids": [
                        "tests/test_contract.py::"
                        "test_canonical_reference_records_sizes"
                    ],
                },
            )

            self.assertEqual(stage, "provider_research")
            self.assertIn("provider.md", feedback)

    def test_provider_baseline_noise_does_not_route_new_frontend_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-303",
                title="frontend browser evidence",
                description="",
                acceptance=[],
                review_history=[
                    {
                        "attempt": 1,
                        "summary": (
                            "REQ-102 canonical provider reference is incomplete"
                        ),
                    }
                ],
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value={
                    ".auto-agents/docs/provider_references/provider.md"
                }
            )
            frontend_failure = (
                "src/e2e/video-home-prototype-fidelity.test.ts > "
                "video home prototype fidelity > "
                "create_failure_and_validation_errors_remain_user_visible"
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": (
                        "1 new verification failure(s) vs task baseline: "
                        f"{frontend_failure}"
                    ),
                    "failure_ids": [frontend_failure],
                    "new_failure_ids": [frontend_failure],
                    "baseline_failure_ids": [
                        "tests/test_requirements_audit_state.py::"
                        "test_canonical_reference_records_sizes"
                    ],
                    "baseline_comparison_comparable": True,
                    "raw_output": (
                        "FAILED tests/test_requirements_audit_state.py::"
                        "test_canonical_reference_records_sizes\n"
                        ".auto-agents/docs/provider_references/provider.md"
                    ),
                },
            )

            self.assertEqual(stage, "")
            self.assertEqual(feedback, "")
            orchestrator._provider_reference_paths_from_review.assert_not_called()

    def test_non_comparable_failure_does_not_rewind_provider_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="provider contract",
                description="",
                acceptance=[],
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": "canonical provider reference command failed",
                    "failure_ids": ["cmd:pytest tests/test_provider.py"],
                    "new_failure_ids": ["cmd:pytest tests/test_provider.py"],
                    "baseline_comparison_comparable": False,
                    "raw_output": (
                        ".auto-agents/docs/provider_references/provider.md"
                    ),
                },
            )

            self.assertEqual(stage, "")
            self.assertEqual(feedback, "")

    def test_provider_route_selects_only_reference_named_by_new_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            apiyi_reference = (
                ".auto-agents/docs/provider_references/apiyi_gpt_image_2.md"
            )
            unrelated_reference = (
                ".auto-agents/docs/provider_references/doubao_tts_direct.md"
            )
            orchestrator._active_provider_reference_paths = Mock(
                return_value={apiyi_reference, unrelated_reference}
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value=set()
            )
            task = TaskSpec(
                task_id="task-provider",
                title="provider contract",
                description="",
                acceptance=[],
            )
            failure_id = (
                "tests/test_requirements_audit_state.py::"
                "test_apiyi_gpt_image_2_canonical_reference_records_sizes"
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": (
                        "1 new verification failure(s) vs task baseline: "
                        f"{failure_id}"
                    ),
                    "failure_ids": [failure_id],
                    "new_failure_ids": [failure_id],
                    "baseline_comparison_comparable": True,
                },
            )

            self.assertEqual(stage, "provider_research")
            self.assertIn(apiyi_reference, feedback)
            self.assertNotIn(unrelated_reference, feedback)

    def test_doc_only_provider_proof_routes_without_test_name_heuristics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            failure_id = "tests/test_contract.py::test_req_210_sources_are_separated"
            reference = ".auto-agents/docs/provider_references/image.md"
            task = TaskSpec(
                task_id="task-provider-doc",
                title="provider documentation contract",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-210",
                        "oracle_index": 1,
                        "evidence_refs": [failure_id, reference],
                    }
                ],
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": f"1 new verification failure: {failure_id}",
                    "failure_ids": [failure_id],
                    "new_failure_ids": [failure_id],
                    "baseline_comparison_comparable": True,
                    "proof_evidence": {
                        "ok": False,
                        "failed_refs": [failure_id],
                        "passed_refs": [reference],
                    },
                },
            )

            self.assertEqual(stage, "provider_research")
            self.assertIn(reference, feedback)

    def test_behavioral_provider_proof_with_application_source_stays_in_implement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            failure_id = "tests/test_provider.py::test_serialized_request"
            task = TaskSpec(
                task_id="task-provider-runtime",
                title="provider runtime",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-211",
                        "oracle_index": 1,
                        "evidence_refs": [
                            failure_id,
                            ".auto-agents/docs/provider_references/image.md",
                            "app/provider.py",
                        ],
                    }
                ],
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": f"1 new verification failure: {failure_id}",
                    "failure_ids": [failure_id],
                    "new_failure_ids": [failure_id],
                    "baseline_comparison_comparable": True,
                },
            )

            self.assertEqual(stage, "")
            self.assertEqual(feedback, "")

    def test_mixed_behavioral_proof_with_passing_provider_doc_stays_in_implement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            failure_id = (
                "tests/integration/test_initial_schema.py::"
                "test_startup_does_not_mutate_schema"
            )
            reference = (
                ".auto-agents/docs/provider_references/"
                "selected-cloud-and-infrastructure.md"
            )
            task = TaskSpec(
                task_id="task-002",
                title="initial schema",
                description="",
                acceptance=[],
                requirement_ids=["REQ-012"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-012",
                        "oracle_index": 5,
                        "proof_type": "mixed",
                        "oracle_strength": "behavioral",
                        "evidence_boundary": "system_boundary",
                        "evidence_refs": [
                            failure_id,
                            ".auto-agents/docs/architecture.md",
                            reference,
                        ],
                    }
                ],
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": f"1 new verification failure: {failure_id}",
                    "failure_ids": [failure_id],
                    "new_failure_ids": [failure_id],
                    "baseline_comparison_comparable": True,
                    "proof_evidence": {
                        "ok": False,
                        "failed_refs": [failure_id],
                        "passed_refs": [
                            ".auto-agents/docs/architecture.md",
                            reference,
                        ],
                    },
                },
            )

            self.assertEqual(stage, "")
            self.assertEqual(feedback, "")

    def test_mixed_behavioral_proof_without_saved_evidence_stays_in_implement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            failure_id = "tests/test_schema.py::test_startup_contract"
            reference = ".auto-agents/docs/provider_references/cloud.md"
            task = TaskSpec(
                task_id="task-legacy",
                title="legacy blocked run",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "proof_type": "mixed",
                        "oracle_strength": "behavioral",
                        "evidence_boundary": "system_boundary",
                        "evidence_refs": [failure_id, reference],
                    }
                ],
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": f"1 new verification failure: {failure_id}",
                    "failure_ids": [failure_id],
                    "new_failure_ids": [failure_id],
                    "baseline_comparison_comparable": True,
                },
            )

            self.assertEqual(stage, "")
            self.assertEqual(feedback, "")

    def test_semantic_failure_signature_distinguishes_same_pytest_node(self) -> None:
        failure_id = "tests/test_schema.py::test_startup_contract"

        first = Orchestrator._verification_failure_semantic_signature(
            [failure_id],
            raw_output=(
                "E   TypeError: traceback assignment failed\n"
                "tests/test_schema.py:269: TypeError"
            ),
        )
        second = Orchestrator._verification_failure_semantic_signature(
            [failure_id],
            raw_output=(
                "E   AssertionError: assert False\n"
                "tests/test_schema.py:388: AssertionError"
            ),
        )

        self.assertNotEqual(first, second)

    def test_recovery_loop_requires_same_semantic_failure_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            state = RunState(run_id="run-001")
            task = TaskSpec(
                task_id="task-002",
                title="candidate",
                description="",
                acceptance=[],
                requirement_ids=["REQ-012"],
            )
            failure_id = "tests/test_schema.py::test_startup_contract"

            first = orchestrator._record_recovery_loop_event(
                state,
                task=task,
                target_stage="provider_research",
                review_text="provider.md",
                failure_ids=[failure_id],
                failure_signature="traceback-type-error",
                artifact_fingerprints={"provider.md": "same"},
            )
            second = orchestrator._record_recovery_loop_event(
                state,
                task=task,
                target_stage="provider_research",
                review_text="provider.md",
                failure_ids=[failure_id],
                failure_signature="missing-cause-assertion",
                artifact_fingerprints={"provider.md": "same"},
            )
            third = orchestrator._record_recovery_loop_event(
                state,
                task=task,
                target_stage="provider_research",
                review_text="provider.md",
                failure_ids=[failure_id],
                failure_signature="missing-cause-assertion",
                artifact_fingerprints={"provider.md": "same"},
            )

            self.assertFalse(first)
            self.assertFalse(second)
            self.assertTrue(third)

    def test_review_rewind_uses_task_attempt_base_and_pre_reset_owner_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            reference = (
                ".auto-agents/docs/provider_references/provider.md"
            )
            reference_path = root / reference
            lock_path = root / ".auto-agents/state/provider_references.lock.json"
            write_text(reference_path, "old provider contract\n")
            write_json(
                lock_path,
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": reference,
                            "status": "verified",
                            "notes": "old",
                        }
                    },
                },
            )
            old_ref = commit_all(root, "old baseline")
            write_text(root / "completed-task.txt", "keep me\n")
            write_text(reference_path, "refreshed provider contract\n")
            write_json(
                lock_path,
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": reference,
                            "status": "verified",
                            "notes": "refreshed",
                        }
                    },
                },
            )
            attempt_base = commit_all(root, "provider refresh")
            expected_reference_hash = hashlib.sha256(
                reference_path.read_bytes()
            ).hexdigest()
            write_text(root / "failed-candidate.py", "broken = True\n")

            task = TaskSpec(
                task_id="task-002",
                title="candidate",
                description="",
                acceptance=[],
                requirement_ids=["REQ-012"],
                status="in_progress",
                verify_baseline_ref=f"{old_ref}:context",
            )
            state = RunState(
                run_id="run-001",
                current_stage="implement",
                tasks=[task],
            )
            orchestrator._set_task_attempt_base_ref(
                state,
                task,
                attempt_base,
            )

            result = orchestrator._handle_review_stage_rewind(
                state,
                task,
                [task],
                {
                    "reason": "provider contract failed",
                    "review": reference,
                    "failure_ids": ["tests/test_provider.py::test_contract"],
                    "provider_reference_paths": [reference],
                },
                "provider_research",
            )

            self.assertIs(result, state)
            self.assertEqual(head_ref(root), attempt_base)
            self.assertTrue((root / "completed-task.txt").is_file())
            self.assertFalse((root / "failed-candidate.py").exists())
            self.assertEqual(task.verify_baseline_ref, "")
            self.assertEqual(task.verify_baseline_failures, [])
            self.assertEqual(
                state.recovery_loop_events[-1]["artifact_fingerprints"][reference],
                expected_reference_hash,
            )

    def test_misrouted_provider_resume_restores_lock_and_returns_to_implement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            reference = (
                ".auto-agents/docs/provider_references/provider.md"
            )
            lock_path = (
                root
                / ".auto-agents"
                / "state"
                / "provider_references.lock.json"
            )
            baseline_entry = {
                "path": reference,
                "status": "verified",
                "retrieved_at": "2026-07-28T00:00:00Z",
                "source_urls": ["https://example.test/provider"],
                "notes": "verified contract",
            }
            write_json(
                lock_path,
                {
                    "version": 1,
                    "references": {"provider": baseline_entry},
                },
            )
            rewind_ref = commit_all(root, "provider baseline")
            task = TaskSpec(
                task_id="task-303",
                title="frontend browser evidence",
                description="",
                acceptance=[],
                status="pending",
            )
            state = RunState(
                run_id="run-001",
                status="blocked",
                current_stage="provider_research",
                tasks=[task],
                stage_summaries={
                    "clarify": "done",
                    "prototype": "done",
                    "design": "done",
                    "plan": "done",
                },
                last_error="run interrupted by SIGINT",
                active_blocker={
                    "owner": "target_project",
                    "category": "target_recovery_exhausted",
                    "status": "blocked",
                },
            )
            marker = (
                "Needs refresh: review rejected task task-303 and requested "
                "provider_research recovery"
            )
            write_json(
                lock_path,
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            **baseline_entry,
                            "status": "needs_refresh",
                            "notes": f"verified contract\n{marker}",
                        }
                    },
                },
            )
            incident_dir = (
                root
                / ".auto-agents"
                / "runs"
                / state.run_id
                / "recovery_incidents"
            )
            frontend_failure = (
                "src/e2e/video-home-prototype-fidelity.test.ts > "
                "create_failure_and_validation_errors_remain_user_visible"
            )
            task.requirement_proofs = [
                {
                    "requirement_id": "REQ-303",
                    "oracle_index": 1,
                    "proof_type": "mixed",
                    "oracle_strength": "behavioral",
                    "evidence_boundary": "system_boundary",
                    "evidence_refs": [frontend_failure, reference],
                }
            ]
            write_json(
                incident_dir / "incident-001.json",
                {
                    "schema_version": 1,
                    "incident_id": "incident-001",
                    "task_id": task.task_id,
                    "target_stage": "provider_research",
                    "rewind_ref": rewind_ref,
                    "failure_ids": [frontend_failure],
                    "reason": (
                        "1 new verification failure(s) vs task baseline: "
                        f"{frontend_failure}"
                    ),
                    "rewind_reason": (
                        "verification failure points to "
                        "provider_research-owned source-of-truth"
                    ),
                    "review": reference,
                },
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value={reference}
            )

            self.assertTrue(
                orchestrator._normalize_misrouted_provider_research_resume(
                    state
                )
            )

            restored = read_json(lock_path)
            self.assertEqual(
                restored["references"]["provider"],
                baseline_entry,
            )
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.current_stage, "implement")
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.verify_retry_epoch, 1)
            self.assertIn("plan", state.stage_summaries)
            self.assertIn("provider_research", state.stage_summaries)
            self.assertNotIn("implement", state.stage_summaries)
            self.assertEqual(state.last_error, "")
            self.assertEqual(state.active_blocker, {})
            self.assertIn(
                "incident-001",
                state.resume_context["review_route_reclassifications"],
            )
            self.assertEqual(
                state.last_recovery_route["outcome"],
                "route_reclassified",
            )
            self.assertFalse(
                orchestrator._normalize_misrouted_provider_research_resume(
                    state
                )
            )

    def test_provider_lock_restore_refuses_missing_recorded_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            reference = (
                ".auto-agents/docs/provider_references/provider.md"
            )
            lock_path = (
                root
                / ".auto-agents"
                / "state"
                / "provider_references.lock.json"
            )
            marked_entry = {
                "path": reference,
                "status": "needs_refresh",
                "retrieved_at": "",
                "source_urls": [],
                "notes": (
                    "Needs refresh: review rejected task task-303 and "
                    "requested provider_research recovery"
                ),
            }
            write_json(
                lock_path,
                {
                    "version": 1,
                    "references": {"provider": marked_entry},
                },
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value={reference}
            )

            restored, error = (
                orchestrator._restore_provider_reference_refresh_incident(
                    {
                        "task_id": "task-303",
                        "rewind_ref": "missing-ref",
                        "review": reference,
                    }
                )
            )

            self.assertFalse(restored)
            self.assertIn("cannot safely restore", error)
            self.assertEqual(
                read_json(lock_path)["references"]["provider"],
                marked_entry,
            )

    def test_provider_refresh_marker_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            reference = (
                ".auto-agents/docs/provider_references/provider.md"
            )
            lock_path = (
                root
                / ".auto-agents"
                / "state"
                / "provider_references.lock.json"
            )
            write_json(
                lock_path,
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": reference,
                            "status": "verified",
                            "retrieved_at": "",
                            "source_urls": [],
                            "notes": "verified",
                        }
                    },
                },
            )

            first = orchestrator._mark_provider_references_needs_refresh(
                [reference],
                reason="review rejected task task-303",
            )
            second = orchestrator._mark_provider_references_needs_refresh(
                [reference],
                reason="review rejected task task-303",
            )

            entry = read_json(lock_path)["references"]["provider"]
            self.assertEqual(first, [reference])
            self.assertEqual(second, [])
            self.assertEqual(
                entry["notes"].count(
                    "Needs refresh: review rejected task task-303"
                ),
                1,
            )

    def test_rewind_incident_records_causal_failures_and_lock_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            reference = (
                ".auto-agents/docs/provider_references/provider.md"
            )
            baseline_entry = {
                "path": reference,
                "status": "verified",
            }
            orchestrator._provider_reference_entries_at_ref = Mock(
                return_value={
                    reference: {
                        "lock_key": "provider",
                        "entry": baseline_entry,
                    }
                }
            )
            state = RunState(run_id="run-001")
            task = TaskSpec(
                task_id="task-provider",
                title="provider contract",
                description="",
                acceptance=[],
            )
            failure_id = (
                "tests/test_provider.py::"
                "test_provider_canonical_reference"
            )

            relative_path = orchestrator._persist_rewind_incident(
                state,
                task=task,
                target_stage="provider_research",
                rewind_ref="baseline-ref",
                gate_result={
                    "route_source": "verification_failure_owner",
                    "reason": "one new provider failure",
                    "review": reference,
                    "failure_ids": [failure_id],
                    "current_failure_ids": ["old-failure", failure_id],
                    "baseline_failure_ids": ["old-failure"],
                    "new_failure_ids": [failure_id],
                    "comparable_failures": True,
                    "baseline_comparison_comparable": True,
                    "provider_reference_paths": [reference],
                },
            )

            incident = read_json(root / relative_path)
            self.assertEqual(incident["schema_version"], 2)
            self.assertEqual(
                incident["route_source"],
                "verification_failure_owner",
            )
            self.assertEqual(incident["new_failure_ids"], [failure_id])
            self.assertEqual(
                incident["provider_reference_paths"],
                [reference],
            )
            self.assertEqual(
                incident["provider_lock_before"][reference]["entry"],
                baseline_entry,
            )

    def test_parallel_worktree_installs_dependency_links_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            (root / ".conda" / "conda-meta").mkdir(parents=True)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            task = TaskSpec(
                task_id="task-001",
                title="parallel",
                description="",
                acceptance=[],
            )
            state = RunState(run_id="run-001", tasks=[task])

            with patch(
                "auto_agents.orchestrator.install_dependency_links",
                side_effect=RuntimeError("dependency links installed first"),
            ) as install:
                result = orchestrator._run_task_in_worktree(
                    state,
                    [task],
                    task.task_id,
                )

            self.assertFalse(result["ok"])
            self.assertIn("dependency links installed first", result["reason"])
            install.assert_called_once()
            installed_root, links = install.call_args.args
            self.assertEqual(
                installed_root.name,
                task.task_id,
            )
            self.assertIn(".conda", links)

    def test_parallel_result_commit_excludes_installed_dependency_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            (root / ".conda" / "conda-meta").mkdir(parents=True)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            task = TaskSpec(
                task_id="task-001",
                title="parallel",
                description="",
                acceptance=[],
            )
            state = RunState(run_id="run-001", tasks=[task])

            def complete(
                worker: Orchestrator,
                worker_state: RunState,
                worker_task: TaskSpec,
                resume_existing: bool = False,
                gate_recheck_first: bool = False,
            ) -> dict:
                del worker_state, worker_task, resume_existing, gate_recheck_first
                write_text(worker.project_root / "candidate.py", "ready = True\n")
                return {
                    "ok": True,
                    "reason": "",
                    "review": "accepted",
                    "verify_current_failure_ids": [],
                }

            with patch.object(
                Orchestrator,
                "_ensure_task_verify_baseline",
                return_value=False,
            ), patch.object(
                Orchestrator,
                "_execute_task_with_retries",
                new=complete,
            ):
                result = orchestrator._run_task_in_worktree(
                    state,
                    [task],
                    task.task_id,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["changed_paths"], ["candidate.py"])
            dependency_entry = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "--name-only",
                    str(result["commit_sha"]),
                    "--",
                    ".conda",
                ],
                cwd=str(root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(dependency_entry.stdout.strip(), "")

    def test_failed_checkpoint_excludes_installed_dependency_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            (root / ".conda" / "conda-meta").mkdir(parents=True)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            task = TaskSpec(
                task_id="task-001",
                title="parallel failure",
                description="",
                acceptance=[],
            )
            state = RunState(run_id="run-001", tasks=[task])

            def fail(
                worker: Orchestrator,
                worker_state: RunState,
                worker_task: TaskSpec,
                resume_existing: bool = False,
                gate_recheck_first: bool = False,
            ) -> dict:
                del worker_state, worker_task, resume_existing, gate_recheck_first
                write_text(worker.project_root / "candidate.py", "ready = False\n")
                return {
                    "ok": False,
                    "reason": "verification failed",
                    "review": "candidate is incomplete",
                    "failure_ids": ["tests/test_demo.py::test_contract"],
                }

            with patch.object(
                Orchestrator,
                "_ensure_task_verify_baseline",
                return_value=False,
            ), patch.object(
                Orchestrator,
                "_execute_task_with_retries",
                new=fail,
            ):
                result = orchestrator._run_task_in_worktree(
                    state,
                    [task],
                    task.task_id,
                )

            self.assertFalse(result["ok"])
            checkpoint = result["failure_checkpoint"]
            self.assertEqual(checkpoint["changed_paths"], ["candidate.py"])
            dependency_entry = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "--name-only",
                    str(checkpoint["commit_sha"]),
                    "--",
                    ".conda",
                ],
                cwd=str(root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(dependency_entry.stdout.strip(), "")

    def test_legacy_checkpoint_restore_protects_local_dependency_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            commit_all(root, "chore: initialize test project")
            (root / ".conda" / "conda-meta").mkdir(parents=True)
            write_text(root / ".conda" / "marker.txt", "keep\n")

            checkpoint_worktree = Path(tmp) / "checkpoint"
            add_worktree(root, checkpoint_worktree, ref=head_ref(root))
            write_text(checkpoint_worktree / "candidate.py", "ready = True\n")
            (checkpoint_worktree / ".conda").symlink_to(
                root / ".conda",
                target_is_directory=True,
            )
            subprocess.run(
                ["git", "add", "candidate.py"],
                cwd=str(checkpoint_worktree),
                check=True,
            )
            subprocess.run(
                ["git", "add", "-f", ".conda"],
                cwd=str(checkpoint_worktree),
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "checkpoint: legacy candidate"],
                cwd=str(checkpoint_worktree),
                check=True,
                text=True,
                capture_output=True,
            )
            checkpoint_sha = head_ref(checkpoint_worktree)
            checkpoint_ref = (
                "refs/auto-agents/runs/run-001/failed-tasks/task-001/epoch-0"
            )
            update_ref(root, checkpoint_ref, checkpoint_sha)

            task = TaskSpec(
                task_id="task-001",
                title="restore",
                description="",
                acceptance=[],
            )
            state = RunState(run_id="run-001", tasks=[task])
            state.task_failure_checkpoints[task.task_id] = {
                "ref": checkpoint_ref,
                "has_candidate_changes": True,
                "changed_paths": [".conda", "candidate.py"],
            }

            restored = orchestrator._restore_task_failure_checkpoint(
                state,
                task,
                root,
            )

            self.assertEqual(restored, checkpoint_ref)
            self.assertEqual(
                (root / ".conda" / "marker.txt").read_text(encoding="utf-8"),
                "keep\n",
            )
            self.assertEqual(
                (root / "candidate.py").read_text(encoding="utf-8"),
                "ready = True\n",
            )
            checkpoint = state.task_failure_checkpoints[task.task_id]
            self.assertEqual(checkpoint["changed_paths"], ["candidate.py"])
            self.assertEqual(checkpoint["excluded_dependency_paths"], [".conda"])

    def test_failed_candidate_checkpoint_and_log_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            initial_ref = head_ref(root)
            write_text(root / "candidate.py", "candidate = True\n")
            log_dir = (
                root / ".auto-agents" / "failed-verification-logs"
            )
            log_dir.mkdir(parents=True)
            write_text(log_dir / "task-verify.log", "FAILED contract\n")
            task = TaskSpec(
                task_id="task-001",
                title="checkpoint",
                description="",
                acceptance=[],
                verify_retry_epoch=2,
            )
            state = RunState(run_id="run-001", tasks=[task])

            checkpoint = orchestrator._preserve_failed_task_checkpoint(
                state,
                task,
                root,
                {
                    "reason": "verification failed",
                    "review": "",
                    "failure_ids": [
                        "tests/test_demo.py::test_contract"
                    ],
                },
            )

            self.assertTrue(checkpoint["has_candidate_changes"])
            self.assertTrue(ref_exists(root, str(checkpoint["ref"])))
            self.assertEqual(
                checkpoint["changed_paths"],
                ["candidate.py"],
            )
            archived_logs = [
                root / str(path)
                for path in checkpoint["diagnostic_paths"]
                if str(path).endswith(".log")
            ]
            self.assertEqual(len(archived_logs), 1)
            self.assertTrue(archived_logs[0].is_file())
            self.assertEqual(changed_paths(root), [])

            self.assertTrue(hard_reset_clean(root, initial_ref))
            state.task_failure_checkpoints[task.task_id] = checkpoint
            restored = orchestrator._restore_task_failure_checkpoint(
                state,
                task,
                root,
            )
            self.assertEqual(restored, checkpoint["ref"])
            self.assertEqual(changed_paths(root), ["candidate.py"])


if __name__ == "__main__":
    unittest.main()
