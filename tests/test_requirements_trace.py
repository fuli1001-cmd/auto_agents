import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    load_run_state,
    provider_references_lock_path,
    requirements_trace_path,
    task_plan_path,
)
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.requirements import (
    audit_requirements,
    load_requirements_trace,
    validate_requirements_trace_payload,
)
from auto_agents.validation import validate_task_plan_with_requirements, validation_report


def _requirement(**overrides):
    payload = {
        "id": "REQ-001",
        "text": "Implement the direct integration.",
        "source": "user conversation",
        "status": "active",
        "priority": "mandatory",
        "acceptance_oracles": ["The public API returns normalized provider output."],
        "oracle_type": "integration_test",
        "oracle_strength": "behavioral",
        "evidence_boundary": "system_boundary",
        "forbidden_proxy_oracles": [],
        "forbidden_patterns": [],
        "external_docs_required": False,
        "provider_reference": "",
        "notes": "",
    }
    payload.update(overrides)
    return payload


def _legacy_requirement(**overrides):
    payload = _requirement()
    for field in ("oracle_type", "oracle_strength", "evidence_boundary", "forbidden_proxy_oracles"):
        payload.pop(field, None)
    payload.update(overrides)
    return payload


def _proof(**overrides):
    payload = {
        "requirement_id": "REQ-001",
        "oracle_index": 1,
        "acceptance_oracle": "The public API returns normalized provider output.",
        "proof_type": "integration_test",
        "oracle_strength": "behavioral",
        "evidence_boundary": "system_boundary",
        "evidence_refs": ["tests/test_public_api.py::test_normalized_provider_output"],
        "forbidden_proxy_oracles": [],
        "proxy_oracles": [],
        "status": "verified",
    }
    payload.update(overrides)
    return payload


class RequirementsTraceTests(unittest.TestCase):
    def test_plan_validation_requires_mandatory_requirement_coverage(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        plan = {
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Build feature",
                    "description": "Build it.",
                    "acceptance": ["works"],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": [],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertTrue(any("mandatory active requirements" in item for item in errors))

    def test_plan_validation_rejects_unknown_requirement_ids(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        plan = {
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Build feature",
                    "description": "Build it.",
                    "acceptance": ["works"],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-404"],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertTrue(any("unknown requirement_ids" in item for item in errors))

    def test_plan_validation_requires_oracle_proofs_in_strict_mode(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Build feature",
                    "description": "Build it.",
                    "acceptance": ["works"],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertTrue(any("requirement_proofs" in item for item in errors))

    def test_plan_validation_accepts_oracle_proofs_in_strict_mode(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Build feature",
                    "description": "Build it.",
                    "acceptance": ["works"],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [_proof(status="planned")],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertEqual(errors, [])

    def test_plan_validation_rejects_weak_or_proxy_oracle_proof(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    oracle_strength="behavioral",
                    evidence_boundary="system_boundary",
                    forbidden_proxy_oracles=["metadata-only request evidence"],
                )
            ],
        }
        plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Build feature",
                    "description": "Build it.",
                    "acceptance": ["works"],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [
                        _proof(
                            oracle_strength="proxy",
                            evidence_boundary="internal_state",
                            forbidden_proxy_oracles=["metadata-only request evidence"],
                            proxy_oracles=["metadata-only request evidence"],
                            status="planned",
                        )
                    ],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertTrue(any("oracle_strength proxy is weaker than behavioral" in item for item in errors))
        self.assertTrue(any("uses forbidden proxy oracle" in item for item in errors))

    def test_project_validation_does_not_block_between_clarify_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(requirements_trace_path(project_root), {"version": 1, "requirements": [_requirement()]})

            report = validation_report(project_root)

            self.assertTrue(report["ok"])
            self.assertEqual(report["errors"], [])

    def test_project_validation_accepts_legacy_requirements_trace_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(requirements_trace_path(project_root), {"version": 1, "requirements": [_legacy_requirement()]})

            report = validation_report(project_root)

            self.assertTrue(report["ok"])
            self.assertEqual(report["errors"], [])

    def test_load_requirements_trace_backfills_legacy_quality_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(requirements_trace_path(project_root), {"version": 1, "requirements": [_legacy_requirement()]})

            trace = load_requirements_trace(project_root)
            requirement = trace["requirements"][0]

            self.assertEqual(requirement["oracle_type"], "mixed")
            self.assertEqual(requirement["oracle_strength"], "behavioral")
            self.assertEqual(requirement["evidence_boundary"], "system_boundary")
            self.assertEqual(requirement["forbidden_proxy_oracles"], [])

    def test_requirements_audit_fails_for_forbidden_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            forbidden_patterns=["legacy_gateway"],
                        )
                    ],
                },
            )
            write_text(project_root / "src" / "backend.py", "def call():\n    return 'legacy_gateway'\n")

            ok, report = audit_requirements(
                project_root,
                [
                    TaskSpec(
                        task_id="task-001",
                        title="Build",
                        description="Build it.",
                        acceptance=["works"],
                        requirement_ids=["REQ-001"],
                        status="done",
                    )
                ],
            )

            self.assertFalse(ok)
            self.assertIn("forbidden pattern 'legacy_gateway'", report)

    def test_requirements_audit_passes_verified_provider_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            reference = ".auto-agents/docs/provider_references/provider.md"
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            external_docs_required=True,
                            provider_reference=reference,
                        )
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": reference,
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )

            ok, report = audit_requirements(
                project_root,
                [
                    TaskSpec(
                        task_id="task-001",
                        title="Build",
                        description="Build it.",
                        acceptance=["works"],
                        requirement_ids=["REQ-001"],
                        status="done",
                    )
                ],
            )

            self.assertTrue(ok)
            self.assertIn("REQ-001: pass", report)

    def test_requirements_audit_fails_missing_oracle_proof_in_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(requirements_trace_path(project_root), {"version": 1, "requirements": [_requirement()]})
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "test_strategy": "unit tests",
                    "verification_commands": ["npm test"],
                    "tasks": [],
                },
            )

            ok, report = audit_requirements(
                project_root,
                [
                    TaskSpec(
                        task_id="task-001",
                        title="Build",
                        description="Build it.",
                        acceptance=["works"],
                        requirement_ids=["REQ-001"],
                        status="done",
                    )
                ],
            )

            self.assertFalse(ok)
            self.assertIn("oracle proof", report.lower())
            self.assertIn("has no done-task oracle proof entries", report)

    def test_requirements_audit_passes_verified_oracle_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(requirements_trace_path(project_root), {"version": 1, "requirements": [_requirement()]})

            ok, report = audit_requirements(
                project_root,
                [
                    TaskSpec(
                        task_id="task-001",
                        title="Build",
                        description="Build it.",
                        acceptance=["works"],
                        requirement_ids=["REQ-001"],
                        requirement_proofs=[_proof()],
                        status="done",
                    )
                ],
            )

            self.assertTrue(ok, msg=report)
            self.assertIn("Oracle proof audit: strict", report)

    def test_requirements_audit_fails_forbidden_proxy_oracle_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            forbidden_proxy_oracles=["metadata-only request evidence"],
                        )
                    ],
                },
            )

            ok, report = audit_requirements(
                project_root,
                [
                    TaskSpec(
                        task_id="task-001",
                        title="Build",
                        description="Build it.",
                        acceptance=["works"],
                        requirement_ids=["REQ-001"],
                        requirement_proofs=[
                            _proof(
                                forbidden_proxy_oracles=["metadata-only request evidence"],
                                proxy_oracles=["metadata-only request evidence"],
                            )
                        ],
                        status="done",
                    )
                ],
            )

            self.assertFalse(ok)
            self.assertIn("uses forbidden proxy oracle", report)

    def test_requirements_trace_rejects_invalid_quality_contract_values(self) -> None:
        trace = {"version": 1, "requirements": [_requirement(
            oracle_type="legacy_gateway",
            oracle_strength="weak",
            evidence_boundary="ui_only",
            forbidden_proxy_oracles="logs only",
        )]}

        errors = validate_requirements_trace_payload(trace)

        self.assertTrue(any("oracle_type" in item for item in errors))
        self.assertTrue(any("oracle_strength" in item for item in errors))
        self.assertTrue(any("evidence_boundary" in item for item in errors))
        self.assertTrue(any("forbidden_proxy_oracles" in item for item in errors))

    def test_provider_research_reuses_verified_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            reference = ".auto-agents/docs/provider_references/provider.md"
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            external_docs_required=True,
                            provider_reference=reference,
                        )
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": reference,
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)

            state = orchestrator._run_provider_research(state, project_root / "spec.md")

            self.assertEqual(state.current_stage, "provider_research")
            self.assertIn("already verified", state.stage_summaries["provider_research"])


if __name__ == "__main__":
    unittest.main()
