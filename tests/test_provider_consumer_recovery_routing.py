from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_ENGINE_SOURCE_ROOT = Path(
    os.environ.get(
        "AUTO_AGENTS_TEST_ENGINE_SOURCE_ROOT",
        Path(__file__).resolve().parents[1] / "src",
    )
).resolve()
sys.path.insert(0, str(_ENGINE_SOURCE_ROOT))

from auto_agents.config import load_run_state, save_run_state
from auto_agents.git_ops import commit_all
from auto_agents.io_utils import read_json, write_json, write_text
from auto_agents.models import CommandResult, GateResult, RunState, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.repair_cases import RepairCase, RepairCaseStore


class _ProviderRoutingFixture:
    failure_ref = "tests/test_contract.py::test_current_provider_contract"
    verified_reference = (
        ".auto-agents/docs/provider_references/verified-provider.md"
    )
    incomplete_reference = (
        ".auto-agents/docs/provider_references/incomplete-provider.md"
    )
    unrelated_reference = (
        ".auto-agents/docs/provider_references/unrelated-provider.md"
    )
    provider_lock = ".auto-agents/state/provider_references.lock.json"

    def project(self, root: Path) -> Orchestrator:
        Orchestrator.init_project(root, "demo", "mock")
        orchestrator = Orchestrator(root)
        write_text(root / self.verified_reference, "status: verified\n")
        write_text(root / self.incomplete_reference, "status: pending\n")
        write_text(root / self.unrelated_reference, "status: pending\n")
        write_json(
            root / ".auto-agents/state/requirements_trace.json",
            {
                "requirements": [
                    {
                        "id": "REQ-212",
                        "status": "active",
                        "external_docs_required": True,
                        "provider_references": [
                            self.verified_reference,
                            self.incomplete_reference,
                            self.unrelated_reference,
                        ],
                    }
                ]
            },
        )
        write_json(
            root / self.provider_lock,
            {
                "version": 1,
                "references": {
                    "verified": {
                        "path": self.verified_reference,
                        "status": "verified",
                    },
                    "incomplete": {
                        "path": self.incomplete_reference,
                        "status": "pending",
                    },
                },
            },
        )
        return orchestrator

    def lineage(
        self,
        *,
        include_implementation_source: bool = False,
    ) -> tuple[TaskSpec, TaskSpec]:
        evidence_refs = [
            self.failure_ref,
            self.verified_reference,
            self.incomplete_reference,
            self.provider_lock,
            ".auto-agents/state/requirements_trace.json",
        ]
        if include_implementation_source:
            evidence_refs.append("src/provider_consumer.py")
        parent = TaskSpec(
            task_id="task-provider-contract",
            title="Verify the provider contract",
            description="",
            acceptance=[],
            requirement_ids=["REQ-212"],
            task_origin="scope_split",
            split_depth=Orchestrator.MAX_SPLIT_DEPTH,
            verification_refs=[self.failure_ref],
            requirement_proofs=[
                {
                    "requirement_id": "REQ-212",
                    "oracle_index": 1,
                    "proof_type": "mixed",
                    "oracle_strength": "semantic",
                    "evidence_boundary": "system_boundary",
                    "evidence_refs": evidence_refs,
                    "status": "planned",
                },
                {
                    "requirement_id": "REQ-999",
                    "oracle_index": 2,
                    "proof_type": "mixed",
                    "oracle_strength": "semantic",
                    "evidence_boundary": "system_boundary",
                    "evidence_refs": [
                        self.failure_ref,
                        self.unrelated_reference,
                    ],
                    "status": "planned",
                },
            ],
            evidence_preflight={
                "decision": "BLOCK",
                "target_stage": "provider_research",
                "required_mutations": [
                    {
                        "path": self.incomplete_reference,
                        "owner": "provider_research",
                    },
                    {
                        "path": self.provider_lock,
                        "owner": "provider_research",
                    },
                    {
                        "path": self.unrelated_reference,
                        "owner": "provider_research",
                    },
                ],
            },
        )
        repair = TaskSpec(
            task_id="repair-provider-contract-r1-1",
            title="Repair provider contract evidence",
            description="",
            acceptance=[],
            requirement_ids=["REQ-212"],
            parent_task_id=parent.task_id,
            task_origin="evidence_repair",
            split_depth=Orchestrator.MAX_SPLIT_DEPTH + 1,
            verification_refs=[self.failure_ref],
            verify_baseline_failures=[self.failure_ref],
        )
        return parent, repair

    def verify_result(self, *, reason: str = "") -> dict[str, object]:
        return {
            "ok": False,
            "reason": reason or f"command failed: {self.failure_ref}",
            "failure_ids": [self.failure_ref],
            "current_failure_ids": [self.failure_ref],
            "baseline_failure_ids": [self.failure_ref],
            "new_failure_ids": [self.failure_ref],
            "comparable_failures": True,
            "baseline_comparison_comparable": True,
            "proof_evidence": {
                "ok": False,
                "failed_refs": [self.failure_ref],
                "passed_refs": [self.verified_reference],
            },
        }

    def run_absolute_baseline_verify(
        self,
        orchestrator: Orchestrator,
        repair: TaskSpec,
    ) -> dict[str, object]:
        command = f"python -m pytest -q {self.failure_ref}"
        failed_gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command=command,
                    ok=False,
                    returncode=1,
                    stdout=(
                        f"FAILED {self.failure_ref} - AssertionError\n"
                    ),
                )
            ],
            summary=f"command failed: {self.failure_ref}",
        )
        with (
            patch.object(
                orchestrator,
                "_build_task_verify_commands",
                return_value=[command],
            ),
            patch.object(
                orchestrator,
                "_quick_verify_failure",
                return_value=None,
            ),
            patch.object(
                orchestrator,
                "_run_task_gate_commands_for_commands",
                return_value=(failed_gate, ""),
            ),
        ):
            result = orchestrator._run_task_verify(repair)
        result["proof_evidence"] = {
            "ok": False,
            "failed_refs": [self.failure_ref],
            "passed_refs": [self.verified_reference],
        }
        return result


def test_evidence_repair_baseline_provider_failure_routes_via_parent_proof(
) -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()

        verify_result = fixture.run_absolute_baseline_verify(
            orchestrator,
            repair,
        )

        assert not verify_result["ok"]
        assert verify_result["failure_ids"] == [fixture.failure_ref]
        assert verify_result["baseline_failure_ids"] == [fixture.failure_ref]
        assert verify_result["new_failure_ids"] == [fixture.failure_ref]

        leaf_stage, _leaf_feedback = (
            orchestrator._verification_failure_owner_route(
                repair,
                verify_result,
            )
        )
        assert leaf_stage == ""

        stage, feedback, route_evidence_refs = (
            orchestrator._verification_failure_owner_route_details(
                repair,
                verify_result,
                tasks=[parent, repair],
            )
        )

        assert stage == "provider_research"
        assert set(route_evidence_refs) == {
            fixture.verified_reference,
            fixture.incomplete_reference,
            fixture.provider_lock,
        }
        assert fixture.verified_reference in feedback
        assert fixture.incomplete_reference in feedback
        assert fixture.provider_lock in feedback
        assert fixture.unrelated_reference not in route_evidence_refs
        assert repair.requirement_proofs == []


def test_evidence_repair_mixed_runtime_failure_does_not_overroute() -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage(
            include_implementation_source=True,
        )

        stage, feedback = orchestrator._verification_failure_owner_route(
            repair,
            fixture.verify_result(),
            tasks=[parent, repair],
        )

        assert stage == ""
        assert feedback == ""


def test_evidence_repair_lineage_requires_requirement_identity() -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        parent.requirement_proofs[0]["requirement_id"] = ""

        stage, feedback, evidence_refs = (
            orchestrator._verification_failure_owner_route_details(
                repair,
                fixture.verify_result(),
                tasks=[parent, repair],
            )
        )

        assert stage == ""
        assert feedback == ""
        assert evidence_refs == []


def test_replan_split_depth_stop_preserves_provider_owner() -> None:
    """Exercise the pre-existing recovery API for a base-code differential."""

    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        state = RunState(
            run_id="run-provider-contract",
            current_stage="implement",
            tasks=[parent, repair],
        )
        judgment = {
            "decision": "REPLAN",
            "reason": "the upstream contract evidence must change",
            "split_axis": ["document status", "lock identity"],
            "source": "provider",
        }
        failure_with_unrelated_prose = fixture.verify_result(
            reason=(
                f"command failed: {fixture.failure_ref}; diagnostics mention "
                f"{fixture.unrelated_reference}"
            )
        )
        route_evidence_refs = {
            fixture.verified_reference,
            fixture.incomplete_reference,
            fixture.provider_lock,
        }

        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            return_value=judgment,
        ):
            handled = orchestrator._recover_evidence_repair_failure(
                state,
                [parent, repair],
                repair,
                failure_with_unrelated_prose,
            )

        assert handled, "split-depth recovery dropped the provider owner route"
        route = state.last_recovery_route
        assert route["outcome"] == "judge_stopped"
        assert route["stop_owner"] == "provider_research"
        assert route["failure_ids"] == [fixture.failure_ref]
        assert (
            route["stop_category"]
            == "recovery_provider_research_action_required"
        )
        assert set(route["evidence_refs"]) == route_evidence_refs
        assert fixture.unrelated_reference not in route["evidence_refs"]
        assert set(route["evidence_artifact_fingerprints"]) == set(
            route_evidence_refs
        )
        assert (
            fixture.unrelated_reference
            not in route["evidence_artifact_fingerprints"]
        )
        assert state.active_blocker["owner"] == "provider_research"

        persisted = load_run_state(root)
        assert (
            persisted.last_recovery_route["stop_owner"]
            == "provider_research"
        )
        assert set(
            persisted.last_recovery_route[
                "evidence_artifact_fingerprints"
            ]
        ) == set(route_evidence_refs)
        validation = orchestrator._validate_restored_recovery_stop(
            state,
            [parent, repair],
            repair,
            parent,
            route,
        )
        assert validation["valid"], validation["reason"]

        mismatched_route = dict(
            route,
            failure_ids=["tests/test_contract.py::test_runtime_behavior"],
        )
        mismatched_validation = (
            orchestrator._validate_restored_recovery_stop(
                state,
                [parent, repair],
                repair,
                parent,
                mismatched_route,
            )
        )
        assert not mismatched_validation["valid"]
        assert "matching failure lineage" in mismatched_validation["reason"]

        write_text(root / fixture.incomplete_reference, "status: verified\n")
        resume = orchestrator._terminal_recovery_resume_evidence(
            state,
            [parent, repair],
            repair,
            parent,
            route,
        )

        assert resume["resume_allowed"]
        assert resume["evidence_artifacts_changed"]


def test_repeated_provider_failure_stop_preserves_provider_owner() -> None:
    """Keep provider evidence when deterministic no-progress stops retries."""

    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        state = RunState(
            run_id="run-repeated-provider-failure",
            current_stage="implement",
            tasks=[parent, repair],
        )
        verify_result = fixture.verify_result()

        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            return_value={
                "decision": "CONTINUE",
                "reason": "retry once with the retained failure evidence",
                "actionable_items": ["recheck the provider contract"],
                "source": "provider",
            },
        ):
            first_handled = orchestrator._recover_evidence_repair_failure(
                state,
                [parent, repair],
                repair,
                verify_result,
            )

        assert first_handled
        assert state.last_recovery_route["outcome"] == "requeued"

        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            side_effect=AssertionError(
                "repeated unchanged failures stop before another judge call"
            ),
        ):
            repeated_handled = (
                orchestrator._recover_evidence_repair_failure(
                    state,
                    [parent, repair],
                    repair,
                    verify_result,
                )
            )

        assert repeated_handled
        route = state.last_recovery_route
        expected_evidence = {
            fixture.verified_reference,
            fixture.incomplete_reference,
            fixture.provider_lock,
        }
        assert route["outcome"] == "judge_stopped"
        assert route["engine_invariant"] == "repeated_failure_no_progress"
        assert route["stop_owner"] == "provider_research"
        assert (
            route["stop_category"]
            == "recovery_provider_research_action_required"
        )
        assert route["failure_ids"] == [fixture.failure_ref]
        assert set(route["evidence_refs"]) == expected_evidence
        assert set(route["evidence_artifact_fingerprints"]) == (
            expected_evidence
        )
        assert state.active_blocker["owner"] == "provider_research"

        validation = orchestrator._validate_restored_recovery_stop(
            state,
            [parent, repair],
            repair,
            parent,
            route,
        )
        assert validation["valid"], validation["reason"]

        legacy_route = dict(
            route,
            engine_invariant="",
            stop_owner="target_project",
            stop_category="recovery_evidence_change_required",
            evidence_refs=[],
        )
        legacy_route.pop("evidence_artifact_fingerprints", None)
        legacy_validation = orchestrator._validate_restored_recovery_stop(
            state,
            [parent, repair],
            repair,
            parent,
            legacy_route,
        )
        assert not legacy_validation["valid"]
        assert (
            legacy_validation["expected_stop_owner"]
            == "provider_research"
        )
        assert set(legacy_validation["evidence_refs"]) == expected_evidence

        write_text(root / fixture.incomplete_reference, "status: verified\n")
        resume = orchestrator._terminal_recovery_resume_evidence(
            state,
            [parent, repair],
            repair,
            parent,
            route,
        )

        assert resume["resume_allowed"]
        assert resume["evidence_artifacts_changed"]


def test_repeated_provider_failure_rejudges_after_bound_artifact_change() -> None:
    """Provider artifacts participate in no-progress identity before STOP."""

    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        state = RunState(
            run_id="run-provider-progress-before-stop",
            current_stage="implement",
            tasks=[parent, repair],
        )
        verify_result = fixture.verify_result()
        continue_judgment = {
            "decision": "CONTINUE",
            "reason": "retry with the current provider evidence",
            "actionable_items": ["recheck the provider contract"],
            "source": "provider",
        }

        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            return_value=continue_judgment,
        ) as first_judge:
            first_handled = orchestrator._recover_evidence_repair_failure(
                state,
                [parent, repair],
                repair,
                verify_result,
            )

        assert first_handled
        first_judge.assert_called_once()
        assert state.last_recovery_route["outcome"] == "requeued"

        write_text(
            root / fixture.incomplete_reference,
            "status: verified\nrevision: 2\n",
        )

        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            return_value=continue_judgment,
        ) as second_judge:
            repeated_handled = orchestrator._recover_evidence_repair_failure(
                state,
                [parent, repair],
                repair,
                verify_result,
            )

        assert repeated_handled
        second_judge.assert_called_once()
        assert state.last_recovery_route["outcome"] == "requeued"
        assert state.last_recovery_route["engine_invariant"] == ""

        requeued = [
            entry
            for entry in repair.recovery_history
            if isinstance(entry, dict) and entry.get("result") == "requeued"
        ]
        assert len(requeued) == 2
        first_entry, second_entry = requeued
        assert first_entry["evidence_fingerprint"] == second_entry[
            "evidence_fingerprint"
        ]
        assert first_entry["progress_fingerprint"] != second_entry[
            "progress_fingerprint"
        ]
        assert first_entry["evidence_artifact_fingerprints"][
            fixture.incomplete_reference
        ] != second_entry["evidence_artifact_fingerprints"][
            fixture.incomplete_reference
        ]
        assert state.last_recovery_route["progress_fingerprint"] == (
            second_entry["progress_fingerprint"]
        )


def test_explicit_provider_failure_tracks_lock_change_after_reload() -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        state = RunState(
            run_id="run-explicit-provider-failure",
            current_stage="implement",
            tasks=[parent, repair],
        )
        verify_result = fixture.verify_result()
        verify_result["proof_evidence"] = {
            "ok": False,
            "failed_refs": [fixture.incomplete_reference],
            "passed_refs": [fixture.verified_reference],
        }

        stage, _feedback, evidence_refs = (
            orchestrator._verification_failure_owner_route_details(
                repair,
                verify_result,
                tasks=[parent, repair],
            )
        )

        expected_evidence = {
            fixture.incomplete_reference,
            fixture.provider_lock,
        }
        assert stage == "provider_research"
        assert set(evidence_refs) == expected_evidence
        assert orchestrator._provider_reference_document_paths(
            evidence_refs
        ) == {fixture.incomplete_reference}

        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            return_value={
                "decision": "REPLAN",
                "reason": "the provider identity evidence must change",
                "split_axis": ["document status", "lock identity"],
                "source": "provider",
            },
        ):
            handled = orchestrator._recover_evidence_repair_failure(
                state,
                [parent, repair],
                repair,
                verify_result,
            )

        assert handled
        assert set(state.last_recovery_route["evidence_refs"]) == (
            expected_evidence
        )
        assert set(
            state.last_recovery_route["evidence_artifact_fingerprints"]
        ) == expected_evidence
        artifact_binding = state.last_recovery_route[
            "provider_artifact_binding"
        ]
        assert artifact_binding["failure_ids"] == [fixture.failure_ref]
        assert artifact_binding["failed_refs"] == [
            fixture.incomplete_reference
        ]
        assert artifact_binding["passed_refs"] == [
            fixture.verified_reference
        ]
        assert set(artifact_binding["evidence_refs"]) == expected_evidence

        restarted = Orchestrator(root)
        persisted = load_run_state(root)
        persisted_by_id = {task.task_id: task for task in persisted.tasks}
        persisted_parent = persisted_by_id[parent.task_id]
        persisted_repair = persisted_by_id[repair.task_id]
        persisted_route = persisted.last_recovery_route
        validation = restarted._validate_restored_recovery_stop(
            persisted,
            persisted.tasks,
            persisted_repair,
            persisted_parent,
            persisted_route,
        )
        assert validation["valid"], validation["reason"]

        lock = read_json(root / fixture.provider_lock, default={})
        lock["revision"] = 2
        write_json(root / fixture.provider_lock, lock)
        resume = restarted._terminal_recovery_resume_evidence(
            persisted,
            persisted.tasks,
            persisted_repair,
            persisted_parent,
            persisted_route,
        )

        assert resume["resume_allowed"]
        assert resume["evidence_artifacts_changed"]
        assert not resume["evidence_changed"]

        resumed = restarted._resume_blocked_run(persisted)

        assert resumed
        assert persisted.status == "pending"
        assert persisted.last_recovery_route == {}
        assert "recovery_stop_reconciliations" not in persisted.active_blocker
        assert persisted.active_blocker["recovery_resume_evidence"][
            "evidence_artifacts_changed"
        ]


def test_restart_quarantines_provider_stop_with_omitted_bound_artifact() -> None:
    """A partial persisted binding cannot hide a changed provider lock."""

    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        state = RunState(
            run_id="run-incomplete-provider-binding",
            current_stage="implement",
            tasks=[parent, repair],
        )
        verify_result = fixture.verify_result()
        verify_result["proof_evidence"] = {
            "ok": False,
            "failed_refs": [fixture.incomplete_reference],
            "passed_refs": [fixture.verified_reference],
        }

        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            return_value={
                "decision": "REPLAN",
                "reason": "the provider identity evidence must change",
                "split_axis": ["document status", "lock identity"],
                "source": "provider",
            },
        ):
            handled = orchestrator._recover_evidence_repair_failure(
                state,
                [parent, repair],
                repair,
                verify_result,
            )

        assert handled
        assert set(state.last_recovery_route["evidence_refs"]) == {
            fixture.incomplete_reference,
            fixture.provider_lock,
        }

        persisted = load_run_state(root)
        persisted.last_recovery_route["evidence_refs"] = [
            fixture.incomplete_reference
        ]
        persisted.last_recovery_route[
            "evidence_artifact_fingerprints"
        ].pop(fixture.provider_lock)
        for task in persisted.tasks:
            for entry in task.recovery_history:
                if (
                    isinstance(entry, dict)
                    and entry.get("result") == "judge_stopped"
                ):
                    entry["evidence_refs"] = [fixture.incomplete_reference]
                    entry.get("evidence_artifact_fingerprints", {}).pop(
                        fixture.provider_lock,
                        None,
                    )
        save_run_state(root, persisted)

        lock = read_json(root / fixture.provider_lock, default={})
        lock["revision"] = 2
        write_json(root / fixture.provider_lock, lock)

        restarted = Orchestrator(root)
        restored = load_run_state(root)
        restored_by_id = {task.task_id: task for task in restored.tasks}
        restored_parent = restored_by_id[parent.task_id]
        restored_repair = restored_by_id[repair.task_id]
        validation = restarted._validate_restored_recovery_stop(
            restored,
            restored.tasks,
            restored_repair,
            restored_parent,
            restored.last_recovery_route,
            require_current_artifacts=False,
        )

        assert not validation["valid"]
        previous_epoch = restored_parent.recovery_epoch
        assert restarted._resume_blocked_run(restored)
        assert restored.status == "pending"
        assert restored_parent.recovery_epoch == previous_epoch + 1
        assert restored_repair.recovery_epoch == previous_epoch + 1
        assert restored.last_recovery_route == {}
        reconciliations = restored.active_blocker[
            "recovery_stop_reconciliations"
        ]
        assert reconciliations[-1]["outcome"] == (
            "ignored_malformed_recovery_stop"
        )


def test_restart_rejects_unsafe_provider_paths_before_fingerprinting() -> None:
    """Malformed persisted paths cannot forge provider-artifact progress."""

    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        commit_all(root, "provider baseline")
        state = RunState(
            run_id="run-unsafe-provider-stop",
            current_stage="implement",
            tasks=[parent, repair],
        )
        with patch.object(
            orchestrator,
            "_run_recovery_judge",
            return_value={
                "decision": "REPLAN",
                "reason": "the provider evidence must change",
                "split_axis": ["document status", "lock identity"],
                "source": "provider",
            },
        ):
            handled = orchestrator._recover_evidence_repair_failure(
                state,
                [parent, repair],
                repair,
                fixture.verify_result(),
            )

        assert handled
        assert state.status == "blocked"

        outside = Path(tmp) / "outside-provider.md"
        write_text(outside, "outside provider material\n")
        incomplete_path = root / fixture.incomplete_reference
        incomplete_path.unlink()
        incomplete_path.symlink_to(outside)
        traversal = (
            ".auto-agents/docs/provider_references/"
            "../../../../outside-provider.md"
        )
        assert (root / traversal).resolve() == outside.resolve()
        malformed_refs = [
            traversal,
            fixture.incomplete_reference,
            fixture.provider_lock,
        ]
        forged_fingerprints = {
            reference: "forged-prior-fingerprint"
            for reference in malformed_refs
        }

        persisted = load_run_state(root)
        persisted.last_recovery_route["evidence_refs"] = malformed_refs
        persisted.last_recovery_route[
            "evidence_artifact_fingerprints"
        ] = forged_fingerprints
        for task in persisted.tasks:
            for entry in task.recovery_history:
                if (
                    isinstance(entry, dict)
                    and entry.get("result") == "judge_stopped"
                ):
                    entry["evidence_refs"] = list(malformed_refs)
                    entry["evidence_artifact_fingerprints"] = dict(
                        forged_fingerprints
                    )
        save_run_state(root, persisted)

        restarted = Orchestrator(root)
        resumed_state = load_run_state(root)
        original_read_bytes = Path.read_bytes
        outside_reads: list[Path] = []

        def guarded_read_bytes(path: Path) -> bytes:
            if path.resolve() == outside.resolve():
                outside_reads.append(path)
                raise AssertionError("restart read an artifact outside the project")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", guarded_read_bytes):
            resumed = restarted._resume_blocked_run(resumed_state)

        assert resumed
        assert outside_reads == []
        assert resumed_state.status == "pending"
        assert resumed_state.last_recovery_route == {}
        assert "recovery_resume_evidence" not in resumed_state.active_blocker
        reconciliation = resumed_state.active_blocker[
            "recovery_stop_reconciliations"
        ][0]
        assert reconciliation["outcome"] == "ignored_malformed_recovery_stop"
        assert "invalid structured evidence" in reconciliation["reason"]


def test_restart_quarantines_legacy_provider_split_depth_stop() -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        baseline_ref = commit_all(root, "provider baseline")
        state = load_run_state(root)
        state.current_stage = "implement"
        state.status = "blocked"
        state.tasks = [parent, repair]
        orchestrator._set_task_attempt_base_ref(
            state,
            repair,
            baseline_ref,
        )
        evidence_fingerprint = orchestrator._recovery_evidence_fingerprint(
            parent,
            state=state,
            tasks=state.tasks,
        )
        failure_signature = "legacy-provider-contract-failure"
        stopped_entry = {
            "epoch": 0,
            "round": 2,
            "max_rounds": 2,
            "result": "judge_stopped",
            "failure_kind": "verification_failed",
            "signature": failure_signature,
            "failure_signature": failure_signature,
            "failure_ids": [fixture.failure_ref],
            "evidence_fingerprint": evidence_fingerprint,
            "judge_decision": "STOP",
            "judge_reason": (
                "replan requested at split depth limit: upstream evidence "
                "requires its owning stage"
            ),
            "judge_source": "deterministic",
            "stop_owner": "target_project",
            "stop_category": "recovery_evidence_change_required",
            "prerequisite_keys": [],
            "evidence_refs": [],
            "engine_invariant": "replan_split_depth_limit",
            "repair_task_ids": [repair.task_id],
        }
        parent.recovery_round = 2
        repair.recovery_round = 2
        parent.recovery_history = [dict(stopped_entry)]
        repair.recovery_history = [dict(stopped_entry)]
        state.last_recovery_route = {
            "task_id": repair.task_id,
            "task_origin": repair.task_origin,
            "lineage_id": parent.task_id,
            "epoch": 0,
            "round": 2,
            "max_rounds": 2,
            "failure_kind": "verification_failed",
            "failure_signature": failure_signature,
            "evidence_fingerprint": evidence_fingerprint,
            "judge_decision": "STOP",
            "judge_source": "deterministic",
            "outcome": "judge_stopped",
            "reason": stopped_entry["judge_reason"],
            "repair_task_ids": [repair.task_id],
            "engine_invariant": "replan_split_depth_limit",
            "stop_owner": "target_project",
            "stop_category": "recovery_evidence_change_required",
            "prerequisite_keys": [],
            "evidence_refs": [],
        }
        state.active_blocker = {
            "owner": "auto_agents",
            "category": "legacy_recovery_route_misclassified",
            "reason": "A persisted recovery route has contradictory ownership.",
            "status": "blocked",
        }
        repair_case = RepairCase(
            case_id="health-provider-recovery",
            run_id=state.run_id,
            source="health_watch",
            kind="self_repair_stagnation",
            severity="confirmed",
            symptom="the terminal recovery route made no durable progress",
        )
        RepairCaseStore(root, state.run_id).save(repair_case)
        state.active_repair_case_id = repair_case.case_id
        state.repair_phase = "self_repairing"
        orchestrator._persist_tasks(state.tasks)
        save_run_state(root, state)

        restarted = Orchestrator(root)
        resumed_state = restarted.mark_self_repair_applied(
            "provider-route-repair"
        )
        assert resumed_state.status == "pending"
        assert resumed_state.active_blocker["status"] == "retrying"
        assert resumed_state.active_blocker["self_repair_commit"] == (
            "provider-route-repair"
        )
        resumed = restarted._resume_blocked_run(resumed_state)

        assert resumed
        assert resumed_state.status == "pending"
        assert resumed_state.last_recovery_route == {}
        resumed_by_id = {
            task.task_id: task for task in resumed_state.tasks
        }
        resumed_parent = resumed_by_id[parent.task_id]
        resumed_repair = resumed_by_id[repair.task_id]
        assert resumed_parent.recovery_epoch == 1
        assert resumed_repair.recovery_epoch == 1
        expected_evidence = {
            fixture.verified_reference,
            fixture.incomplete_reference,
            fixture.provider_lock,
        }
        reconciliation = resumed_state.active_blocker[
            "recovery_stop_reconciliations"
        ][0]
        assert reconciliation["expected_stop_owner"] == "provider_research"
        assert set(reconciliation["evidence_refs"]) == expected_evidence
        assert reconciliation["evidence_artifact_fingerprints"] == (
            restarted._artifact_fingerprints(expected_evidence)
        )

        command = f"python -m pytest -q {fixture.failure_ref}"
        with (
            patch.object(
                restarted,
                "_build_task_verify_commands",
                return_value=[command],
            ),
            patch.object(
                restarted,
                "_quick_verify_failure_details",
                return_value=None,
            ),
            patch.object(
                restarted,
                "_run_task_verify",
                return_value=fixture.verify_result(),
            ),
        ):
            gate_result = restarted._execute_task_with_retries(
                resumed_state,
                resumed_repair,
                resume_existing=True,
            )

        assert gate_result["rewind_to_stage"] == "provider_research"
        assert set(gate_result["evidence_refs"]) == expected_evidence
        assert set(gate_result["provider_reference_paths"]) == {
            fixture.verified_reference,
            fixture.incomplete_reference,
        }
        rewind_state = restarted._handle_review_stage_rewind(
            resumed_state,
            resumed_repair,
            resumed_state.tasks,
            gate_result,
            "provider_research",
        )
        assert rewind_state is resumed_state
        assert resumed_state.current_stage == "provider_research"


def test_restart_does_not_infer_provider_owner_from_stop_reason() -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        behavioral_failure = "tests/test_contract.py::test_runtime_behavior"
        state = RunState(
            run_id="run-behavioral-recovery",
            status="blocked",
            current_stage="implement",
            tasks=[parent, repair],
        )
        route = {
            "task_id": repair.task_id,
            "task_origin": repair.task_origin,
            "lineage_id": parent.task_id,
            "epoch": 0,
            "round": 2,
            "max_rounds": 2,
            "failure_kind": "verification_failed",
            "failure_signature": "behavioral-failure",
            "failure_ids": [behavioral_failure],
            "evidence_fingerprint": (
                orchestrator._recovery_evidence_fingerprint(
                    parent,
                    state=state,
                    tasks=state.tasks,
                )
            ),
            "judge_decision": "STOP",
            "judge_source": "deterministic",
            "outcome": "judge_stopped",
            "reason": (
                "replan requested at split depth limit: diagnostics mention "
                f"{fixture.incomplete_reference}"
            ),
            "repair_task_ids": [repair.task_id],
            "engine_invariant": "replan_split_depth_limit",
            "stop_owner": "target_project",
            "stop_category": "recovery_evidence_change_required",
            "prerequisite_keys": [],
            "evidence_refs": [],
        }

        validation = orchestrator._validate_restored_recovery_stop(
            state,
            state.tasks,
            repair,
            parent,
            route,
        )

        assert validation == {
            "valid": True,
            "reason": "engine-generated deterministic recovery STOP",
        }


@pytest.mark.parametrize(
    ("engine_invariant", "stop_reason"),
    [
        (
            "replan_split_depth_limit",
            "replan requested at split depth limit: runtime behavior still fails",
        ),
        (
            "repeated_failure_no_progress",
            "deterministic no-progress: failure and owner artifacts are unchanged",
        ),
    ],
    ids=("split-depth", "repeated-failure"),
)
def test_history_only_behavioral_deterministic_stop_remains_terminal(
    engine_invariant: str,
    stop_reason: str,
) -> None:
    """History reconstruction must preserve an absent provider binding."""

    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        behavioral_failure = "tests/test_contract.py::test_runtime_behavior"
        commit_all(root, "behavioral recovery baseline")
        state = RunState(
            run_id=f"run-behavioral-{engine_invariant}",
            status="blocked",
            current_stage="implement",
            tasks=[parent, repair],
        )
        evidence_fingerprint = orchestrator._recovery_evidence_fingerprint(
            parent,
            state=state,
            tasks=state.tasks,
        )
        failure_signature = f"behavioral-{engine_invariant}"
        stopped_entry = {
            "epoch": 0,
            "round": 2,
            "max_rounds": 2,
            "result": "judge_stopped",
            "failure_kind": "verification_failed",
            "signature": failure_signature,
            "failure_signature": failure_signature,
            "failure_ids": [behavioral_failure],
            "evidence_fingerprint": evidence_fingerprint,
            "judge_decision": "STOP",
            "judge_reason": stop_reason,
            "judge_source": "deterministic",
            "stop_owner": "target_project",
            "stop_category": "recovery_evidence_change_required",
            "prerequisite_keys": [],
            "evidence_refs": [],
            "evidence_artifact_fingerprints": {},
            "engine_invariant": engine_invariant,
            "repair_task_ids": [repair.task_id],
        }
        assert "provider_artifact_binding" not in stopped_entry
        parent.recovery_round = 2
        repair.recovery_round = 2
        parent.recovery_history = [dict(stopped_entry)]
        repair.recovery_history = [dict(stopped_entry)]
        state.last_recovery_route = {}
        state.active_blocker = {
            "owner": "target_project",
            "category": "recovery_evidence_change_required",
            "reason": f"Recovery stopped for {parent.task_id}; {stop_reason}",
            "status": "blocked",
            "resume_attempts": 0,
        }
        orchestrator._persist_tasks(state.tasks)
        save_run_state(root, state)

        restarted = Orchestrator(root)
        resumed_state = load_run_state(root)
        resumed_by_id = {
            task.task_id: task for task in resumed_state.tasks
        }
        previous_epoch = resumed_by_id[parent.task_id].recovery_epoch

        resumed = restarted._resume_blocked_run(resumed_state)

        assert not resumed
        assert resumed_state.status == "blocked"
        restored_route = resumed_state.last_recovery_route
        assert restored_route["outcome"] == "judge_stopped"
        assert restored_route["engine_invariant"] == engine_invariant
        assert restored_route["stop_owner"] == "target_project"
        assert "provider_artifact_binding" not in restored_route
        assert "recovery_stop_reconciliations" not in resumed_state.active_blocker
        assert resumed_by_id[parent.task_id].recovery_epoch == previous_epoch
        assert resumed_by_id[repair.task_id].recovery_epoch == previous_epoch


def test_provider_rewind_rejects_traversal_alias_to_provider_lock() -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        lock_alias = (
            ".auto-agents/docs/provider_references/"
            "../../state/provider_references.lock.json"
        )
        parent.requirement_proofs[0]["evidence_refs"].append(lock_alias)
        verify_result = fixture.verify_result()
        verify_result["proof_evidence"] = {
            "ok": False,
            "failed_refs": [fixture.incomplete_reference, lock_alias],
            "passed_refs": [fixture.verified_reference],
        }
        state = RunState(
            run_id="run-provider-alias",
            status="pending",
            current_stage="implement",
            tasks=[parent, repair],
        )
        baseline_ref = commit_all(root, "provider baseline")
        orchestrator._set_task_attempt_base_ref(
            state,
            repair,
            baseline_ref,
        )

        assert (root / lock_alias).resolve() == (
            root / fixture.provider_lock
        ).resolve()
        command = f"python -m pytest -q {fixture.failure_ref}"
        with (
            patch.object(
                orchestrator,
                "_build_task_verify_commands",
                return_value=[command],
            ),
            patch.object(
                orchestrator,
                "_quick_verify_failure_details",
                return_value=None,
            ),
            patch.object(
                orchestrator,
                "_run_task_verify",
                return_value=verify_result,
            ),
        ):
            gate_result = orchestrator._execute_task_with_retries(
                state,
                repair,
                resume_existing=True,
            )

        expected_evidence = {
            fixture.incomplete_reference,
            fixture.provider_lock,
        }
        assert gate_result["rewind_to_stage"] == "provider_research"
        assert set(gate_result["evidence_refs"]) == expected_evidence
        assert set(gate_result["provider_reference_paths"]) == {
            fixture.incomplete_reference
        }
        assert lock_alias not in gate_result["evidence_refs"]
        assert lock_alias not in gate_result["provider_reference_paths"]

        rewind_state = orchestrator._handle_review_stage_rewind(
            state,
            repair,
            [parent, repair],
            gate_result,
            "provider_research",
        )
        assert rewind_state is state

        incident_paths = sorted(
            (
                root
                / ".auto-agents/runs"
                / state.run_id
                / "recovery_incidents"
            ).glob("*.json")
        )
        assert len(incident_paths) == 1
        incident = read_json(incident_paths[0], default={})
        assert set(incident["evidence_refs"]) == expected_evidence
        assert incident["provider_reference_paths"] == [
            fixture.incomplete_reference
        ]

        provider_dir = (
            root / ".auto-agents/docs/provider_references"
        ).resolve()
        for reference in incident["provider_reference_paths"]:
            reference_path = Path(reference)
            assert not reference_path.is_absolute()
            assert ".." not in reference_path.parts
            assert reference_path.suffix == ".md"
            (root / reference).resolve().relative_to(provider_dir)

        lock_path = (root / fixture.provider_lock).resolve()
        lock = read_json(lock_path, default={})
        lock_entries = lock["references"]
        assert all(
            (root / entry["path"]).resolve() != lock_path
            for entry in lock_entries.values()
            if isinstance(entry, dict) and entry.get("path")
        )


def test_provider_rewind_separates_route_evidence_from_refresh_documents(
) -> None:
    fixture = _ProviderRoutingFixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo"
        orchestrator = fixture.project(root)
        parent, repair = fixture.lineage()
        state = RunState(
            run_id="run-provider-resume",
            status="pending",
            current_stage="implement",
            tasks=[parent, repair],
        )
        baseline_ref = commit_all(root, "provider baseline")
        orchestrator._set_task_attempt_base_ref(
            state,
            repair,
            baseline_ref,
        )
        command = f"python -m pytest -q {fixture.failure_ref}"
        with (
            patch.object(
                orchestrator,
                "_build_task_verify_commands",
                return_value=[command],
            ),
            patch.object(
                orchestrator,
                "_quick_verify_failure_details",
                return_value=None,
            ),
            patch.object(
                orchestrator,
                "_run_task_verify",
                return_value=fixture.verify_result(),
            ),
        ):
            gate_result = orchestrator._execute_task_with_retries(
                state,
                repair,
                resume_existing=True,
            )

        route_evidence_refs = {
            fixture.verified_reference,
            fixture.incomplete_reference,
            fixture.provider_lock,
        }
        refresh_document_paths = {
            fixture.verified_reference,
            fixture.incomplete_reference,
        }
        assert gate_result["rewind_to_stage"] == "provider_research"
        assert set(gate_result["evidence_refs"]) == route_evidence_refs
        assert set(gate_result["provider_reference_paths"]) == (
            refresh_document_paths
        )
        assert fixture.unrelated_reference not in gate_result[
            "provider_reference_paths"
        ]

        rewind_state = orchestrator._handle_review_stage_rewind(
            state,
            repair,
            [parent, repair],
            gate_result,
            "provider_research",
        )
        assert rewind_state is state

        incident_paths = sorted(
            (
                root
                / ".auto-agents/runs"
                / state.run_id
                / "recovery_incidents"
            ).glob("*.json")
        )
        assert len(incident_paths) == 1
        incident = read_json(incident_paths[0], default={})
        assert set(incident["evidence_refs"]) == route_evidence_refs
        assert set(incident["provider_reference_paths"]) == (
            refresh_document_paths
        )
        assert set(incident["provider_lock_before"]) == (
            refresh_document_paths
        )

        lock = read_json(root / fixture.provider_lock, default={})
        lock_entries = lock["references"]
        assert {
            entry["path"]
            for entry in lock_entries.values()
            if isinstance(entry, dict)
        }.isdisjoint({fixture.provider_lock})
        assert {
            entry["path"]: entry["status"]
            for entry in lock_entries.values()
            if isinstance(entry, dict)
            and entry.get("path") in refresh_document_paths
        } == {
            path: "needs_refresh" for path in refresh_document_paths
        }

        restarted = Orchestrator(root)
        resumed_state = load_run_state(root)

        assert not restarted._normalize_misrouted_provider_research_resume(
            resumed_state
        )
        assert resumed_state.current_stage == "provider_research"
        assert "review_route_reclassifications" not in (
            resumed_state.resume_context
        )
