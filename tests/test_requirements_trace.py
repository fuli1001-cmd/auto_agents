import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    load_run_state,
    provider_references_lock_path,
    requirements_trace_path,
    save_run_state,
    task_plan_path,
)
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.requirements import (
    audit_requirements,
    load_requirements_trace,
    normalize_generated_task_plan_statuses,
    preserve_task_plan_negative_oracle_clauses,
    validate_done_task_requirement_proofs,
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

    def test_plan_validation_counts_archived_verified_done_proofs(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(),
                _requirement(
                    id="REQ-002",
                    text="Implement strict schema validation.",
                    acceptance_oracles=["The Responses payload uses a strict json_schema."],
                ),
            ],
        }
        current_plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-002",
                    "title": "Strict schema",
                    "description": "Validate the Responses payload schema.",
                    "acceptance": ["The Responses payload uses a strict json_schema."],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-002"],
                    "requirement_proofs": [
                        _proof(
                            requirement_id="REQ-002",
                            acceptance_oracle="The Responses payload uses a strict json_schema.",
                            status="planned",
                        )
                    ],
                }
            ],
        }
        archived_tasks = [
            {
                "task_id": "task-001",
                "title": "Provider output",
                "description": "Return normalized provider output.",
                "acceptance": ["The public API returns normalized provider output."],
                "status": "done",
                "commit_message": "",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [_proof(status="verified")],
            }
        ]

        errors = validate_task_plan_with_requirements(
            current_plan,
            trace,
            historical_tasks=archived_tasks,
        )

        self.assertEqual(errors, [])

    def test_plan_validation_treats_archived_only_missing_oracle_as_historical_debt(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "The public API returns normalized provider output.",
                        "Historical audit notes are cleaned up.",
                    ],
                ),
                _requirement(
                    id="REQ-002",
                    text="Implement strict schema validation.",
                    acceptance_oracles=["The Responses payload uses a strict json_schema."],
                ),
            ],
        }
        current_plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-002",
                    "title": "Strict schema",
                    "description": "Validate the Responses payload schema.",
                    "acceptance": ["The Responses payload uses a strict json_schema."],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-002"],
                    "requirement_proofs": [
                        _proof(
                            requirement_id="REQ-002",
                            acceptance_oracle="The Responses payload uses a strict json_schema.",
                            status="planned",
                        )
                    ],
                }
            ],
        }
        archived_tasks = [
            {
                "task_id": "task-001",
                "title": "Provider output",
                "description": "Return normalized provider output.",
                "acceptance": ["The public API returns normalized provider output."],
                "status": "done",
                "commit_message": "",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [_proof(status="verified")],
            }
        ]

        errors = validate_task_plan_with_requirements(
            current_plan,
            trace,
            historical_tasks=archived_tasks,
        )

        self.assertEqual(errors, [])

    def test_plan_validation_still_requires_current_task_to_cover_all_owned_oracles(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "The public API returns normalized provider output.",
                        "Historical audit notes are cleaned up.",
                    ],
                )
            ],
        }
        current_plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-002",
                    "title": "Current provider output",
                    "description": "Re-own provider output.",
                    "acceptance": ["The public API returns normalized provider output."],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [_proof(status="planned")],
                }
            ],
        }
        archived_tasks = [
            {
                "task_id": "task-001",
                "title": "Provider output",
                "description": "Return normalized provider output.",
                "acceptance": ["The public API returns normalized provider output."],
                "status": "done",
                "commit_message": "",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [_proof(status="verified")],
            }
        ]

        errors = validate_task_plan_with_requirements(
            current_plan,
            trace,
            historical_tasks=archived_tasks,
        )

        self.assertTrue(any("REQ-001 acceptance oracle #2" in item for item in errors), errors)

    def test_plan_validation_still_requires_new_unarchived_requirements(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(),
                _requirement(
                    id="REQ-002",
                    text="Implement strict schema validation.",
                    acceptance_oracles=["The Responses payload uses a strict json_schema."],
                ),
            ],
        }
        current_plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-999",
                    "title": "Unrelated",
                    "description": "Do unrelated work.",
                    "acceptance": ["works"],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": [],
                    "requirement_proofs": [],
                }
            ],
        }
        archived_tasks = [
            {
                "task_id": "task-001",
                "title": "Provider output",
                "description": "Return normalized provider output.",
                "acceptance": ["The public API returns normalized provider output."],
                "status": "done",
                "commit_message": "",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [_proof(status="verified")],
            }
        ]

        errors = validate_task_plan_with_requirements(
            current_plan,
            trace,
            historical_tasks=archived_tasks,
        )

        self.assertTrue(any("REQ-002" in item for item in errors), errors)
        self.assertFalse(any("REQ-001" in item for item in errors), errors)

    def test_plan_validation_rejects_weakened_negative_contract_atom(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "GET /api/v1/projects/{project_id} default response must not contain complete `tasks[].result`, `retry_trace`, or `retry_attempts`."
                    ],
                    forbidden_proxy_oracles=[
                        "Only removing retry_trace while still returning tasks[].result"
                    ],
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
                    "title": "Lightweight project detail",
                    "description": "Trim oversized retry evidence from project detail responses.",
                    "acceptance": [
                        "Project detail task result omits `retry_trace` and `retry_attempts`."
                    ],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [
                        _proof(
                            oracle_index=1,
                            acceptance_oracle="",
                            forbidden_proxy_oracles=[
                                "Only removing retry_trace while still returning tasks[].result"
                            ],
                            status="planned",
                        )
                    ],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertTrue(any("tasks[].result" in item for item in errors), errors)
        self.assertTrue(any("weakens a negative requirement clause" in item for item in errors))

    def test_plan_validation_accepts_preserved_negative_contract_atom(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "GET /api/v1/projects/{project_id} default response must not contain complete `tasks[].result`, `retry_trace`, or `retry_attempts`."
                    ],
                    forbidden_proxy_oracles=[
                        "Only removing retry_trace while still returning tasks[].result"
                    ],
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
                    "title": "Lightweight project detail",
                    "description": "Split project detail task summaries from task diagnostics.",
                    "acceptance": [
                        "GET /api/v1/projects/{project_id} default response omits `tasks[].result`, `retry_trace`, and `retry_attempts`.",
                        "GET /api/v1/tasks/{task_id} keeps complete diagnostics available on demand.",
                    ],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [
                        _proof(
                            oracle_index=1,
                            acceptance_oracle="",
                            forbidden_proxy_oracles=[
                                "Only removing retry_trace while still returning tasks[].result"
                            ],
                            status="planned",
                        )
                    ],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertEqual(errors, [])

    def test_plan_oracle_preservation_repair_copies_missing_negative_tokens(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    id="REQ-001",
                    acceptance_oracles=[
                        "默认审核测试可用 fake/fixture 触发 `pass/review/block`，无需新增外部审核 API 文档。"
                    ],
                ),
                _requirement(
                    id="REQ-002",
                    acceptance_oracles=[
                        "已通过资产在后续局部纠偏中的下一步动作记录为 `reuse_asset`，不会被整批重生覆盖。"
                    ],
                ),
            ],
        }
        plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Moderation boundary",
                    "description": "Keep the default fake moderation flow.",
                    "acceptance": [
                        "默认 `fake` / `fixture` 审核 `decision=pass` 自动流转。"
                    ],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [
                        _proof(
                            requirement_id="REQ-001",
                            oracle_index=1,
                            acceptance_oracle="",
                            status="planned",
                        )
                    ],
                },
                {
                    "task_id": "task-002",
                    "title": "Local asset retry",
                    "description": "Keep passed assets out of local regeneration.",
                    "acceptance": [
                        "已通过资产在后续局部纠偏中保持复用，不会被整批重生覆盖。"
                    ],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-002"],
                    "requirement_proofs": [
                        _proof(
                            requirement_id="REQ-002",
                            oracle_index=1,
                            acceptance_oracle="",
                            status="planned",
                        )
                    ],
                },
            ],
        }

        original_errors = validate_task_plan_with_requirements(plan, trace)
        self.assertTrue(any("/fixture" in item for item in original_errors), original_errors)
        self.assertTrue(any("reuse_asset" in item for item in original_errors), original_errors)

        repaired, updates = preserve_task_plan_negative_oracle_clauses(plan, trace)

        self.assertEqual(len(updates), 2)
        self.assertNotIn("fake/fixture", plan["tasks"][0]["acceptance"][0])
        self.assertIn("fake/fixture", repaired["tasks"][0]["acceptance"][0])
        self.assertIn("reuse_asset", repaired["tasks"][1]["acceptance"][0])
        self.assertEqual(validate_task_plan_with_requirements(repaired, trace), [])

    def test_plan_status_normalization_converts_stale_done_planned_proofs_to_pending(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        plan = {
            "oracle_proof_schema_version": 1,
            "test_strategy": "unit tests",
            "verification_commands": ["npm test"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Copied done task",
                    "description": "Planner copied a runtime status into a new plan.",
                    "acceptance": ["The public API returns normalized provider output."],
                    "status": "done",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [_proof(status="planned")],
                }
            ],
        }

        original_errors = validate_task_plan_with_requirements(plan, trace)
        self.assertTrue(any("proof is not verified" in item for item in original_errors), original_errors)

        repaired, updates = normalize_generated_task_plan_statuses(plan)

        self.assertEqual(repaired["tasks"][0]["status"], "pending")
        self.assertTrue(any("task-001" in item for item in updates), updates)
        self.assertEqual(validate_task_plan_with_requirements(repaired, trace), [])

    def test_plan_validation_rejects_done_task_with_unverified_oracle_proof(self) -> None:
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
                    "status": "done",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [_proof(status="planned")],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertTrue(any("proof is not verified" in item for item in errors))

    def test_done_task_proof_validation_reports_unverified_bound_proof(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        task = TaskSpec(
            task_id="task-001",
            title="Build",
            description="Build it.",
            acceptance=["works"],
            requirement_ids=["REQ-001"],
            requirement_proofs=[_proof(status="planned")],
            status="in_progress",
        )

        findings = validate_done_task_requirement_proofs(task, trace)

        self.assertTrue(any("proof is not verified" in str(item["message"]) for item in findings))

    def test_done_task_proof_validation_rejects_weakened_negative_contract_atom(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "Default project detail payload must not contain `tasks[].result`, `retry_trace`, or scorer raw evidence."
                    ],
                    forbidden_proxy_oracles=[
                        "Only hiding retry_trace in the frontend"
                    ],
                )
            ],
        }
        task = TaskSpec(
            task_id="task-001",
            title="Trim project detail evidence",
            description="Remove raw retry traces from the public response.",
            acceptance=["The public response omits `retry_trace`."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    oracle_index=1,
                    acceptance_oracle="",
                    forbidden_proxy_oracles=["Only hiding retry_trace in the frontend"],
                    status="verified",
                )
            ],
            status="in_progress",
        )

        findings = validate_done_task_requirement_proofs(task, trace)

        self.assertTrue(any("tasks[].result" in str(item["message"]) for item in findings), findings)

    def test_requirements_audit_fails_done_task_with_weakened_negative_contract_atom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            (project_root / ".auto-agents" / "state").mkdir(parents=True)
            (project_root / ".auto-agents" / "docs").mkdir(parents=True)
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            acceptance_oracles=[
                                "Default project detail payload must not contain `tasks[].result`, `retry_trace`, or `retry_attempts`."
                            ],
                            forbidden_proxy_oracles=[
                                "Only trimming retry_trace from a still-present result field"
                            ],
                        )
                    ],
                },
            )
            write_json(task_plan_path(project_root), {"oracle_proof_schema_version": 1, "tasks": []})
            task = TaskSpec(
                task_id="task-001",
                title="Trim project detail evidence",
                description="Remove retry trace from task result summaries.",
                acceptance=["Project detail result omits `retry_trace` and `retry_attempts`."],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    _proof(
                        oracle_index=1,
                        acceptance_oracle="",
                        forbidden_proxy_oracles=[
                            "Only trimming retry_trace from a still-present result field"
                        ],
                        status="verified",
                    )
                ],
                status="done",
            )

            ok, report = audit_requirements(project_root, [task])

        self.assertFalse(ok)
        self.assertIn("tasks[].result", report)

    def test_oracle_proof_audit_blockers_route_to_plan(self) -> None:
        for kind in ("oracle_proof_missing", "oracle_proof_invalid"):
            with self.subTest(kind=kind):
                route, hard_failure = Orchestrator._audit_issue_route(
                    {"kind": kind, "message": "proof blocker"}
                )

                self.assertEqual(route, "plan")
                self.assertEqual(hard_failure, "")

    def test_verify_failure_with_oracle_proof_text_is_auditable(self) -> None:
        self.assertTrue(
            Orchestrator._verify_failure_looks_like_oracle_proof_state(
                "AssertionError: 'planned' != 'verified' in requirement_proofs"
            )
        )
        self.assertFalse(
            Orchestrator._verify_failure_looks_like_oracle_proof_state(
                "AssertionError: expected 200 response"
            )
        )

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

    def test_requirements_audit_ignores_gate_baseline_cache_for_forbidden_patterns(self) -> None:
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
            write_json(
                project_root / ".auto-agents" / "state" / "gate_baseline_cache.json",
                {
                    "entries": [
                        {
                            "summary": (
                                "A previous verification failure mentioned legacy_gateway while "
                                "explaining stale output."
                            )
                        }
                    ]
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
                        requirement_proofs=[_proof()],
                        status="done",
                    )
                ],
            )

            self.assertTrue(ok, msg=report)
            self.assertNotIn("gate_baseline_cache.json", report)

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

    def test_requirements_audit_counts_archived_verified_done_proofs(self) -> None:
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
            archive_path = project_root / ".auto-agents" / "history" / "task_plans" / "oldrun123.json"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(
                archive_path,
                {
                    "oracle_proof_schema_version": 1,
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Provider output",
                            "description": "Return normalized provider output.",
                            "acceptance": ["The public API returns normalized provider output."],
                            "status": "done",
                            "commit_message": "",
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [_proof(status="verified")],
                        }
                    ],
                },
            )
            state = load_run_state(project_root)
            state.resume_context = {"previous_task_plan_archive": str(archive_path)}
            save_run_state(project_root, state)

            ok, report = audit_requirements(project_root, [])

            self.assertTrue(ok, msg=report)
            self.assertIn("Oracle proof audit: strict", report)
            self.assertIn("REQ-001: pass", report)

    def test_requirements_audit_marks_archived_only_missing_oracle_as_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            acceptance_oracles=[
                                "The public API returns normalized provider output.",
                                "Historical audit notes are cleaned up.",
                            ],
                        )
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "test_strategy": "unit tests",
                    "verification_commands": ["npm test"],
                    "tasks": [],
                },
            )
            archive_path = project_root / ".auto-agents" / "history" / "task_plans" / "oldrun123.json"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(
                archive_path,
                {
                    "oracle_proof_schema_version": 1,
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Provider output",
                            "description": "Return normalized provider output.",
                            "acceptance": ["The public API returns normalized provider output."],
                            "status": "done",
                            "commit_message": "",
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [_proof(status="verified")],
                        }
                    ],
                },
            )
            state = load_run_state(project_root)
            state.resume_context = {"previous_task_plan_archive": str(archive_path)}
            save_run_state(project_root, state)

            ok, report = audit_requirements(project_root, [])

            self.assertTrue(ok, msg=report)
            self.assertIn("REQ-001: advisory", report)
            self.assertIn("acceptance oracle #2 has no proof entry", report)

    def test_requirements_audit_counts_all_archived_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(),
                        _requirement(
                            id="REQ-002",
                            text="Implement strict schema validation.",
                            acceptance_oracles=["The Responses payload uses a strict json_schema."],
                        ),
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "test_strategy": "unit tests",
                    "verification_commands": ["npm test"],
                    "tasks": [],
                },
            )
            archives = [
                (
                    "oldrun001",
                    {
                        "task_id": "task-001",
                        "title": "Provider output",
                        "description": "Return normalized provider output.",
                        "acceptance": ["The public API returns normalized provider output."],
                        "status": "done",
                        "commit_message": "",
                        "requirement_ids": ["REQ-001"],
                        "requirement_proofs": [_proof(status="verified")],
                    },
                ),
                (
                    "oldrun002",
                    {
                        "task_id": "task-002",
                        "title": "Strict schema",
                        "description": "Validate the Responses payload schema.",
                        "acceptance": ["The Responses payload uses a strict json_schema."],
                        "status": "done",
                        "commit_message": "",
                        "requirement_ids": ["REQ-002"],
                        "requirement_proofs": [
                            _proof(
                                requirement_id="REQ-002",
                                acceptance_oracle="The Responses payload uses a strict json_schema.",
                                status="verified",
                            )
                        ],
                    },
                ),
            ]
            for run_id, task in archives:
                archive_path = project_root / ".auto-agents" / "history" / "task_plans" / f"{run_id}.json"
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(archive_path, {"oracle_proof_schema_version": 1, "tasks": [task]})

            ok, report = audit_requirements(project_root, [])

            self.assertTrue(ok, msg=report)
            self.assertIn("REQ-001: pass", report)
            self.assertIn("REQ-002: pass", report)

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

    def test_architecture_documentation_oracle_requires_test_and_doc_evidence(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "architecture.md must not describe process review as a third content review."
                    ],
                )
            ],
        }
        task = TaskSpec(
            task_id="task-001",
            title="Clean architecture docs",
            description="Update architecture.md.",
            acceptance=["architecture.md must not describe process review as a third content review."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    acceptance_oracle="architecture.md must not describe process review as a third content review.",
                    evidence_refs=["tests/test_contract_docs.py::test_architecture_contract"],
                )
            ],
            status="done",
        )

        findings = validate_done_task_requirement_proofs(task, trace)

        self.assertTrue(any("must cite .auto-agents/docs/architecture.md" in item["message"] for item in findings))

    def test_architecture_documentation_oracle_accepts_test_plus_doc_evidence(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "architecture.md must not describe process review as a third content review."
                    ],
                )
            ],
        }
        task = TaskSpec(
            task_id="task-001",
            title="Clean architecture docs",
            description="Update architecture.md.",
            acceptance=["architecture.md must not describe process review as a third content review."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    acceptance_oracle="architecture.md must not describe process review as a third content review.",
                    evidence_refs=[
                        "tests/test_contract_docs.py::test_architecture_contract",
                        ".auto-agents/docs/architecture.md",
                    ],
                )
            ],
            status="done",
        )

        findings = validate_done_task_requirement_proofs(task, trace)

        self.assertEqual(findings, [])

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
