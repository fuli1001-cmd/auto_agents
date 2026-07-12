import json
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
from auto_agents.models import AgentResult, TaskSpec
from auto_agents.frontend_fidelity import validate_frontend_fidelity_trace
from auto_agents.orchestrator import Orchestrator
from auto_agents.requirements import (
    audit_requirements,
    load_requirements_trace,
    migrate_legacy_provider_reference_consumer_hashes,
    normalize_generated_task_plan_statuses,
    preserve_task_plan_negative_oracle_clauses,
    run_requirements_audit,
    requirement_contract_sha256,
    stamp_requirement_contract_hashes,
    stamp_task_plan_contract_hashes,
    provider_reference_consumer_contract_sha256,
    provider_reference_effective_status,
    stamp_provider_reference_consumer_hashes,
    validate_done_task_requirement_proofs,
    validate_requirements_trace_payload,
    validate_requirement_contract_transitions,
)
from auto_agents.validation import validate_task_plan_with_requirements, validation_report
from auto_agents.visual_judge import parse_visual_judge_response, visual_evidence_pairs_for_task


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


def _visual_task() -> TaskSpec:
    return TaskSpec(
        task_id="task-visual",
        title="Home visual fidelity",
        description="Match the Home page prototype.",
        acceptance=["Home matches prototype screenshots."],
        requirement_ids=["REQ-001"],
        requirement_proofs=[
            _proof(
                proof_type="mixed",
                oracle_strength="semantic",
                evidence_boundary="system_boundary",
                evidence_refs=["tests/e2e/home.visual.spec.ts::captures"],
                status="verified",
                visual_evidence={
                    "surface": "Home",
                    "viewport": "desktop",
                    "prototype_image_ref": ".auto-agents/runs/run-1/screenshots/prototype-home.png",
                    "actual_image_ref": ".auto-agents/runs/run-1/screenshots/home.png",
                    "prototype_source_ref": "specs/frondend_prototype/home.html",
                },
            )
        ],
    )


class _VisualJudgeAdapter:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.requests = []

    def available(self) -> bool:
        return True

    def run(self, request) -> AgentResult:
        self.requests.append(request)
        write_text(request.output_path, self.summary + "\n")
        return AgentResult(
            ok=True,
            command=["visual-judge"],
            output_path=request.output_path,
            summary=self.summary,
            model="mock-vision",
            returncode=0,
        )


class RequirementsTraceTests(unittest.TestCase):
    def test_requirement_contract_hash_ignores_notes_and_lifecycle_but_not_oracles(self) -> None:
        original = _requirement(notes="old", status="active")
        lifecycle_update = _requirement(
            notes="new",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        changed_oracle = _requirement(acceptance_oracles=["A different behavior is required."])

        self.assertEqual(
            requirement_contract_sha256(original),
            requirement_contract_sha256(lifecycle_update),
        )
        self.assertNotEqual(
            requirement_contract_sha256(original),
            requirement_contract_sha256(changed_oracle),
        )

    def test_proven_requirement_contract_must_use_new_replacement_id(self) -> None:
        previous = {"version": 1, "requirements": [_requirement()]}
        mutated = {
            "version": 1,
            "requirements": [_requirement(text="Changed delivered behavior.")],
        }
        historical = [
            {
                "task_id": "task-old",
                "status": "done",
                "requirement_ids": ["REQ-001"],
            }
        ]

        errors = validate_requirement_contract_transitions(
            previous, mutated, historical_tasks=historical
        )

        self.assertTrue(any("contract drift" in error for error in errors))
        self.assertTrue(any("new unused REQ ID" in error for error in errors))

    def test_proof_only_historical_task_also_freezes_requirement_contract(self) -> None:
        previous = {"version": 1, "requirements": [_requirement()]}
        mutated = {
            "version": 1,
            "requirements": [_requirement(acceptance_oracles=["Changed oracle."])],
        }
        historical = [
            {
                "task_id": "task-old",
                "status": "done",
                "requirement_ids": [],
                "requirement_proofs": [_proof(status="verified")],
            }
        ]

        errors = validate_requirement_contract_transitions(
            previous, mutated, historical_tasks=historical
        )

        self.assertTrue(any("contract drift" in error for error in errors))

    def test_proven_requirement_can_be_superseded_with_reciprocal_replacement(self) -> None:
        previous = {"version": 1, "requirements": [_requirement()]}
        old = _requirement(status="superseded", superseded_by=["REQ-002"])
        new = _requirement(
            id="REQ-002",
            text="Changed delivered behavior.",
            supersedes=["REQ-001"],
        )
        current, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [old, new]}
        )
        historical = [
            {"task_id": "task-old", "status": "done", "requirement_ids": ["REQ-001"]}
        ]

        transition_errors = validate_requirement_contract_transitions(
            previous, current, historical_tasks=historical
        )
        schema_errors = validate_requirements_trace_payload(current)

        self.assertEqual(transition_errors, [])
        self.assertEqual(schema_errors, [])

    def test_new_plan_proofs_are_bound_to_requirement_contract_hash(self) -> None:
        trace, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [_requirement()]}
        )
        plan = {
            "oracle_proof_schema_version": 1,
            "tasks": [
                {
                    "task_id": "task-001",
                    "requirement_proofs": [_proof(status="planned")],
                }
            ],
        }

        stamped, updates = stamp_task_plan_contract_hashes(plan, trace)

        self.assertEqual(stamped["oracle_proof_schema_version"], 2)
        self.assertEqual(
            stamped["tasks"][0]["requirement_proofs"][0]["requirement_contract_sha256"],
            trace["requirements"][0]["contract_sha256"],
        )
        self.assertTrue(updates)

    def test_legacy_plan_with_empty_proof_lists_is_not_silently_upgraded(self) -> None:
        trace, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [_requirement()]}
        )
        legacy = {
            "tasks": [
                {
                    "task_id": "task-legacy",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [],
                }
            ]
        }

        stamped, updates = stamp_task_plan_contract_hashes(legacy, trace)

        self.assertEqual(stamped, legacy)
        self.assertEqual(updates, [])

    def test_provider_reference_verified_lock_is_invalidated_by_contract_change(self) -> None:
        reference = ".auto-agents/docs/provider_references/provider.md"
        trace, _ = stamp_requirement_contract_hashes(
            {
                "version": 1,
                "requirements": [
                    _requirement(
                        external_docs_required=True,
                        provider_reference=reference,
                    )
                ],
            }
        )
        lock, _ = stamp_provider_reference_consumer_hashes(
            {
                "version": 1,
                "references": {
                    "provider": {
                        "path": reference,
                        "status": "verified",
                    }
                },
            },
            trace,
        )
        self.assertEqual(
            provider_reference_effective_status(lock, trace, reference), "verified"
        )

        changed_trace, _ = stamp_requirement_contract_hashes(
            {
                "version": 1,
                "requirements": [
                    _requirement(
                        text="A changed provider contract.",
                        external_docs_required=True,
                        provider_reference=reference,
                    )
                ],
            }
        )

        self.assertNotEqual(
            provider_reference_consumer_contract_sha256(trace, reference),
            provider_reference_consumer_contract_sha256(changed_trace, reference),
        )
        self.assertEqual(
            provider_reference_effective_status(lock, changed_trace, reference),
            "needs_refresh",
        )

    def test_legacy_provider_lock_migration_backfills_only_unchanged_consumers(self) -> None:
        unchanged_ref = ".auto-agents/docs/provider_references/unchanged.md"
        changed_ref = ".auto-agents/docs/provider_references/changed.md"
        previous = {
            "version": 1,
            "requirements": [
                _requirement(
                    id="REQ-001",
                    external_docs_required=True,
                    provider_reference=unchanged_ref,
                ),
                _requirement(
                    id="REQ-002",
                    text="Old provider contract.",
                    external_docs_required=True,
                    provider_reference=changed_ref,
                ),
            ],
        }
        current, _ = stamp_requirement_contract_hashes(
            {
                "version": 1,
                "requirements": [
                    previous["requirements"][0],
                    _requirement(
                        id="REQ-002",
                        text="Changed provider contract.",
                        external_docs_required=True,
                        provider_reference=changed_ref,
                    ),
                ],
            }
        )
        legacy_lock = {
            "version": 1,
            "references": {
                "unchanged": {"path": unchanged_ref, "status": "verified"},
                "changed": {"path": changed_ref, "status": "verified"},
            },
        }

        migrated, updates = migrate_legacy_provider_reference_consumer_hashes(
            legacy_lock, previous, current
        )

        self.assertEqual(updates, ["unchanged"])
        self.assertEqual(
            provider_reference_effective_status(migrated, current, unchanged_ref),
            "verified",
        )
        self.assertEqual(
            provider_reference_effective_status(migrated, current, changed_ref),
            "needs_refresh",
        )

    def test_visual_judge_extracts_explicit_visual_evidence_pairs(self) -> None:
        task = TaskSpec(
            task_id="task-visual",
            title="Home visual fidelity",
            description="Match prototype.",
            acceptance=["Home matches prototype."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    proof_type="mixed",
                    evidence_refs=["tests/e2e/home.visual.spec.ts::captures"],
                    visual_evidence={
                        "surface": "Home",
                        "viewport": "desktop",
                        "prototype_image_ref": ".auto-agents/runs/run/screenshots/prototype-home.png",
                        "actual_image_ref": ".auto-agents/runs/run/screenshots/home.png",
                        "prototype_source_ref": "specs/frondend_prototype/home.html",
                    },
                )
            ],
        )
        trace = {
            "version": 1,
            "frontend_surfaces": [{"name": "Home", "prototype_refs": ["specs/frondend_prototype/home.html"]}],
            "requirements": [
                _requirement(
                    text="Frontend Home page matches the prototype visual surface.",
                    source="specs/frondend_prototype/home.html",
                    oracle_type="mixed",
                    frontend_surface=True,
                )
            ],
        }

        pairs = visual_evidence_pairs_for_task(task, trace, max_pairs=6)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].surface, "Home")
        self.assertEqual(pairs[0].viewport, "desktop")
        self.assertEqual(pairs[0].purpose, "prototype_fidelity")

    def test_visual_judge_skips_non_comparable_visual_evidence_entries(self) -> None:
        task = TaskSpec(
            task_id="task-visual",
            title="Home visual fidelity",
            description="Match prototype.",
            acceptance=["Home matches prototype."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    proof_type="mixed",
                    evidence_refs=[
                        "tests/e2e/home.visual.spec.ts::captures",
                        ".tmp-tests/stability-before.png",
                        ".tmp-tests/stability-after.png",
                    ],
                    visual_evidence=[
                        {
                            "surface": "Home",
                            "viewport": "desktop",
                            "purpose": "layout_stability",
                            "visual_judge": False,
                            "prototype_image_ref": "specs/frondend_prototype/home.html",
                            "actual_image_ref": ".tmp-tests/stability-home-desktop.png",
                            "prototype_source_ref": "specs/frondend_prototype/home.html",
                        },
                        {
                            "surface": "Home",
                            "viewport": "desktop",
                            "purpose": "prototype_fidelity",
                            "prototype_image_ref": ".tmp-tests/prototype-home-desktop.png",
                            "actual_image_ref": ".tmp-tests/home-desktop.png",
                            "prototype_source_ref": "specs/frondend_prototype/home.html",
                        },
                    ],
                )
            ],
        )
        trace = {
            "version": 1,
            "frontend_surfaces": [{"name": "Home", "prototype_refs": ["specs/frondend_prototype/home.html"]}],
            "requirements": [
                _requirement(
                    text="Frontend Home page matches the prototype visual surface.",
                    source="specs/frondend_prototype/home.html",
                    oracle_type="mixed",
                    frontend_surface=True,
                )
            ],
        }

        pairs = visual_evidence_pairs_for_task(task, trace, max_pairs=6)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].actual_image_ref, ".tmp-tests/home-desktop.png")
        self.assertEqual(pairs[0].purpose, "prototype_fidelity")

    def test_visual_judge_skips_legacy_state_transition_screenshot_pairs(self) -> None:
        task = TaskSpec(
            task_id="task-visual",
            title="Stage text layout stability",
            description="Verify dynamic state text does not shift layout.",
            acceptance=["Stage text is stable."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    proof_type="mixed",
                    exact_acceptance_oracle=(
                        "Desktop and mobile browser evidence proves stage text updates keep "
                        "card height stable with no text overflow, no element overlap, and no layout jump."
                    ),
                    evidence_refs=[
                        "tests/e2e/home.visual.spec.ts::stage_text_layout_is_stable",
                        ".tmp-tests/frontend-lightweight/stage-text-desktop-1440x900.png",
                    ],
                    visual_evidence={
                        "surface": "Home",
                        "viewport": "desktop",
                        "prototype_image_ref": "specs/frondend_prototype/home.html",
                        "actual_image_ref": ".tmp-tests/frontend-lightweight/stage-text-desktop-1440x900.png",
                        "prototype_source_ref": "specs/frondend_prototype/home.html",
                    },
                )
            ],
        )
        trace = {
            "version": 1,
            "frontend_surfaces": [{"name": "Home", "prototype_refs": ["specs/frondend_prototype/home.html"]}],
            "requirements": [
                _requirement(
                    text="Frontend Home page matches the prototype visual surface.",
                    source="specs/frondend_prototype/home.html",
                    oracle_type="mixed",
                    frontend_surface=True,
                )
            ],
        }

        pairs = visual_evidence_pairs_for_task(task, trace, max_pairs=6)

        self.assertEqual(pairs, [])

    def test_visual_judge_parse_fails_low_score(self) -> None:
        report = parse_visual_judge_response(
            '{"status":"passed","score":70,"findings":[],"summary":"close but not enough"}',
            threshold=85,
        )

        self.assertEqual(report.status, "failed")
        self.assertFalse(report.ok)

    def test_visual_judge_auto_skips_when_all_providers_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [{"name": "Home", "prototype_refs": ["specs/frondend_prototype/home.html"]}],
                    "requirements": [
                        _requirement(
                            text="Frontend Home page matches the prototype visual surface.",
                            source="specs/frondend_prototype/home.html",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "auto"
            for provider in orchestrator.config.providers.values():
                provider.vision = "disabled"
            (project_root / ".auto-agents" / "runs" / "run-1" / "screenshots").mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(project_root / ".auto-agents" / "runs" / "run-1" / "screenshots" / name, "fake image")
            state = load_run_state(project_root)
            task = _visual_task()

            result = orchestrator._run_task_visual_judge(state, task)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "skipped")
            self.assertIn("no configured provider", result["reason"])

    def test_visual_judge_pass_appends_report_to_matching_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [{"name": "Home", "prototype_refs": ["specs/frondend_prototype/home.html"]}],
                    "requirements": [
                        _requirement(
                            text="Frontend Home page matches the prototype visual surface.",
                            source="specs/frondend_prototype/home.html",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            (project_root / ".auto-agents" / "runs" / "run-1" / "screenshots").mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(project_root / ".auto-agents" / "runs" / "run-1" / "screenshots" / name, "fake image")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "required"
            orchestrator.config.providers[orchestrator.config.active_provider].vision = "enabled"
            orchestrator.adapter = _VisualJudgeAdapter(
                '{"status":"passed","score":96,"findings":[],"summary":"matches prototype"}'
            )
            state = load_run_state(project_root)
            task = _visual_task()

            result = orchestrator._run_task_visual_judge(state, task)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["proofs_updated"])
            self.assertTrue(
                any(ref.endswith("visual_judge/task-visual/report.json") for ref in task.requirement_proofs[0]["evidence_refs"])
            )

    def test_visual_judge_required_fails_on_low_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [{"name": "Home", "prototype_refs": ["specs/frondend_prototype/home.html"]}],
                    "requirements": [
                        _requirement(
                            text="Frontend Home page matches the prototype visual surface.",
                            source="specs/frondend_prototype/home.html",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            (project_root / ".auto-agents" / "runs" / "run-1" / "screenshots").mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(project_root / ".auto-agents" / "runs" / "run-1" / "screenshots" / name, "fake image")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "required"
            orchestrator.config.providers[orchestrator.config.active_provider].vision = "enabled"
            orchestrator.adapter = _VisualJudgeAdapter(
                '{"status":"failed","score":61,"findings":[{"severity":"blocker","surface":"Home","viewport":"desktop","message":"old workbench layout remains"}],"summary":"too different"}'
            )
            state = load_run_state(project_root)
            task = _visual_task()

            result = orchestrator._run_task_visual_judge(state, task)

            self.assertFalse(result["ok"])
            self.assertIn("old workbench layout remains", result["reason"])

    def test_frontend_prototype_spec_requires_surface_contract(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        spec = "Build a frontend page that must match specs/frondend_prototype/home.html prototype screenshots."

        errors = validate_frontend_fidelity_trace(trace, spec_text=spec)

        self.assertTrue(any("frontend prototype fidelity" in item for item in errors))
        self.assertTrue(any("frontend_surfaces" in item for item in errors))

    def test_frontend_surface_requires_active_visual_requirement(self) -> None:
        trace = {
            "version": 1,
            "frontend_surfaces": [
                {
                    "name": "Home",
                    "route": "/",
                    "prototype_refs": ["specs/frontend_prototype/home.html"],
                    "viewports": ["desktop"],
                }
            ],
            "requirements": [_requirement()],
        }

        errors = validate_frontend_fidelity_trace(trace)

        self.assertTrue(any("no active mandatory requirement" in item for item in errors))

    def test_plan_validation_rejects_payload_only_frontend_visual_proof(self) -> None:
        trace = {
            "version": 1,
            "frontend_surfaces": [
                {
                    "name": "Home",
                    "route": "/",
                    "prototype_refs": ["specs/frontend_prototype/home.html"],
                    "viewports": ["desktop"],
                    "requirement_ids": ["REQ-001"],
                }
            ],
            "requirements": [
                _requirement(
                    text="The frontend Home page must match the referenced prototype visual layout.",
                    source="specs/frontend_prototype/home.html",
                    acceptance_oracles=[
                        "Rendered Home page preserves prototype layout, copy, component hierarchy, and visual styling."
                    ],
                    oracle_type="mixed",
                    oracle_strength="semantic",
                    evidence_boundary="system_boundary",
                    forbidden_proxy_oracles=["payload-only tests", "route exists"],
                    notes="frontend_surface: Home",
                    frontend_surface=True,
                )
            ],
        }
        plan = {
            "test_strategy": "vitest",
            "verification_commands": ["npm test"],
            "oracle_proof_schema_version": 1,
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Build home",
                    "description": "Implement home data wiring.",
                    "acceptance": ["Home route returns provider payload."],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [
                        _proof(
                            proof_type="integration_test",
                            oracle_strength="semantic",
                            evidence_boundary="system_boundary",
                            evidence_refs=["tests/test_home_payload.ts::returns_payload"],
                            forbidden_proxy_oracles=["payload-only tests", "route exists"],
                            proxy_oracles=[],
                            status="planned",
                        )
                    ],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertTrue(any("page-level visual evidence" in item for item in errors))

    def test_frontend_fidelity_validation_uses_explicit_surface_requirement_scope(self) -> None:
        trace = {
            "version": 1,
            "frontend_surfaces": [
                {
                    "name": "Home",
                    "route": "/",
                    "prototype_refs": ["specs/frontend_prototype/home.html"],
                    "viewports": ["desktop"],
                    "requirement_ids": ["REQ-200"],
                }
            ],
            "requirements": [
                _requirement(
                    id="REQ-034",
                    text=(
                        "Asset generation uses real visual scoring, runtime snapshot evidence, "
                        "and visual_contract.retry_guidance for corrective retries."
                    ),
                    source="legacy asset generation spec",
                    acceptance_oracles=["Runtime snapshot records provider-visible retry guidance."],
                    oracle_type="integration_test",
                ),
                _requirement(
                    id="REQ-200",
                    text="The frontend Home page must match the referenced prototype visual layout.",
                    source="specs/frontend_prototype/home.html",
                    acceptance_oracles=["Rendered Home page matches prototype screenshot evidence."],
                    oracle_type="mixed",
                    oracle_strength="semantic",
                    evidence_boundary="system_boundary",
                    notes="frontend_surface: Home",
                ),
            ],
        }
        plan = {
            "test_strategy": "vitest plus playwright",
            "verification_commands": ["npm test"],
            "oracle_proof_schema_version": 1,
            "tasks": [
                {
                    "task_id": "task-legacy",
                    "title": "Keep visual scoring retry guidance",
                    "description": "Validate provider-visible retry guidance.",
                    "acceptance": ["Runtime snapshot records retry guidance."],
                    "status": "done",
                    "commit_message": "",
                    "requirement_ids": ["REQ-034"],
                    "requirement_proofs": [
                        _proof(
                            requirement_id="REQ-034",
                            acceptance_oracle="Runtime snapshot records provider-visible retry guidance.",
                            proof_type="integration_test",
                            evidence_refs=["tests/test_assets.py::test_retry_guidance_snapshot"],
                            status="verified",
                        )
                    ],
                },
                {
                    "task_id": "task-home",
                    "title": "Build home visual surface",
                    "description": "Implement Home against the prototype.",
                    "acceptance": ["Playwright captures Home screenshot evidence."],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-200"],
                    "requirement_proofs": [
                        _proof(
                            requirement_id="REQ-200",
                            acceptance_oracle="Rendered Home page matches prototype screenshot evidence.",
                            proof_type="mixed",
                            oracle_strength="semantic",
                            evidence_refs=["tests/e2e/home.visual.spec.ts::matches_prototype_screenshot"],
                            status="planned",
                        )
                    ],
                },
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertEqual(errors, [])

    def test_plan_validation_accepts_frontend_visual_evidence(self) -> None:
        trace = {
            "version": 1,
            "frontend_surfaces": [
                {
                    "name": "Home",
                    "route": "/",
                    "prototype_refs": ["specs/frontend_prototype/home.html"],
                    "viewports": ["desktop"],
                    "requirement_ids": ["REQ-001"],
                }
            ],
            "requirements": [
                _requirement(
                    text="The frontend Home page must match the referenced prototype visual layout.",
                    source="specs/frontend_prototype/home.html",
                    acceptance_oracles=[
                        "Rendered Home page preserves prototype layout, copy, component hierarchy, and visual styling."
                    ],
                    oracle_type="mixed",
                    oracle_strength="semantic",
                    evidence_boundary="system_boundary",
                    forbidden_proxy_oracles=["payload-only tests", "route exists"],
                    notes="frontend_surface: Home",
                    frontend_surface=True,
                )
            ],
        }
        plan = {
            "test_strategy": "vitest plus playwright",
            "verification_commands": ["npm test"],
            "oracle_proof_schema_version": 1,
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Build home visual surface",
                    "description": "Implement the full Home page visual surface against the prototype.",
                    "acceptance": [
                        "Playwright captures desktop screenshot evidence against the prototype.",
                        "DOM/CSS checks preserve the prototype copy and layout hierarchy.",
                    ],
                    "status": "pending",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [
                        _proof(
                            proof_type="mixed",
                            oracle_strength="semantic",
                            evidence_boundary="system_boundary",
                            evidence_refs=["tests/e2e/home.visual.spec.ts::matches_prototype_screenshot"],
                            forbidden_proxy_oracles=["payload-only tests", "route exists"],
                            proxy_oracles=[],
                            status="planned",
                        )
                    ],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertEqual(errors, [])

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

    def test_plan_validation_ignores_stale_done_task_requirement_ids(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    acceptance_oracles=[
                        "The public API returns normalized provider output.",
                        "The public API records durable provider evidence.",
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
                    "task_id": "task-old",
                    "title": "Old completed slice",
                    "description": "Historical task from a prior trace.",
                    "acceptance": ["old behavior worked"],
                    "status": "done",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001", "REQ-404"],
                    "requirement_proofs": [
                        _proof(),
                        _proof(requirement_id="REQ-404"),
                    ],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertFalse(
            any("unknown requirement" in item for item in errors),
            msg=str(errors),
        )
        self.assertEqual(errors, [])

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

    def test_plan_validation_accepts_done_task_missing_later_forbidden_proxy_records(self) -> None:
        trace = {
            "version": 1,
            "requirements": [
                _requirement(
                    forbidden_proxy_oracles=[
                        "Only checking the old model path",
                        "Only documenting runtime configurability",
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
                    "title": "Build feature",
                    "description": "Build it.",
                    "acceptance": ["works"],
                    "status": "done",
                    "commit_message": "",
                    "requirement_ids": ["REQ-001"],
                    "requirement_proofs": [
                        _proof(
                            status="verified",
                            forbidden_proxy_oracles=["Only checking the old model path"],
                        )
                    ],
                }
            ],
        }

        errors = validate_task_plan_with_requirements(plan, trace)

        self.assertFalse(any("does not record forbidden proxy" in item for item in errors), errors)
        self.assertEqual(errors, [])

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

    def test_requirements_audit_downgrades_out_of_scope_requirement_to_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            id="REQ-CUR",
                            source="issues.md; specs/2026-07-02-iter-01.md 课题4",
                        ),
                        _requirement(
                            id="REQ-OLD",
                            source="specs/2026-06-22-iter-01.md; conversation",
                        ),
                    ],
                },
            )

            result = run_requirements_audit(
                project_root,
                [],
                current_spec=Path("specs/2026-07-02-iter-01.md"),
            )

            issues = {issue["requirement_id"]: issue for issue in result["issues"]}
            # In-scope requirement (source references the current spec) still hard-fails.
            self.assertEqual(issues["REQ-CUR"]["result"], "fail")
            # Out-of-scope historical requirement is downgraded to advisory backlog.
            self.assertEqual(issues["REQ-OLD"]["result"], "advisory")
            self.assertTrue(issues["REQ-OLD"]["out_of_scope_backlog"])
            self.assertIn("Out-of-scope backlog", str(result["report"]))

    def test_requirements_audit_out_of_scope_requirement_alone_does_not_block_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            id="REQ-OLD",
                            source="specs/2026-06-22-iter-01.md; conversation",
                        )
                    ],
                },
            )

            result = run_requirements_audit(
                project_root,
                [],
                current_spec=Path("specs/2026-07-02-iter-01.md"),
            )

            self.assertTrue(result["ok"])

    def test_requirements_audit_without_current_spec_stays_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            id="REQ-OLD",
                            source="specs/2026-06-22-iter-01.md; conversation",
                        )
                    ],
                },
            )

            # The standalone/legacy audit (no current spec) enforces every requirement.
            result = run_requirements_audit(project_root, [])

            self.assertFalse(result["ok"])
            issues = {issue["requirement_id"]: issue for issue in result["issues"]}
            self.assertEqual(issues["REQ-OLD"]["result"], "fail")

    def test_requirements_audit_counts_assume_done_task_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [_requirement()]},
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": []},
            )
            task = TaskSpec(
                task_id="task-221",
                title="补齐首页列表公开边界证明",
                description="Fill the audit boundary proof.",
                acceptance=["The bound requirement passes the audit."],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="pending",
            )

            # While the owning task is still pending its proof does not count, so the
            # requirement fails: this is the deadlock that used to force test-gaming.
            pending = run_requirements_audit(project_root, [task])
            pending_issue = {i["requirement_id"]: i for i in pending["issues"]}["REQ-001"]
            self.assertEqual(pending_issue["result"], "fail")

            # Treating the task as done lets its proof count, so the audit passes honestly.
            assumed = run_requirements_audit(
                project_root, [task], assume_done_task_ids={"task-221"}
            )
            assumed_issue = {i["requirement_id"]: i for i in assumed["issues"]}["REQ-001"]
            self.assertEqual(assumed_issue["result"], "pass")
            self.assertTrue(assumed["ok"])
            self.assertNotEqual(
                pending["input_context_sha256"], assumed["input_context_sha256"]
            )
            self.assertIn(
                f"Input context: {assumed['input_context_sha256']}", assumed["report"]
            )

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

    def test_requirements_audit_ignores_review_report_for_forbidden_patterns(self) -> None:
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
            write_text(
                project_root / ".auto-agents" / "docs" / "review.md",
                "Review feedback mentions legacy_gateway while describing removed behavior.\n",
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
            self.assertNotIn("review.md", report)

    def test_requirements_audit_ignores_session_transcripts_for_forbidden_patterns(self) -> None:
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
            # Agent session transcripts routinely quote requirement/forbidden language while
            # doing their work; they are internal working memory, not product source-of-truth.
            session_dir = (
                project_root / ".auto-agents" / "state" / "sessions" / "abc123"
            )
            (session_dir / "outputs").mkdir(parents=True)
            write_json(
                session_dir / "session_state.json",
                {"conversation": [{"content": "discussed the legacy_gateway removal"}]},
            )
            write_text(
                session_dir / "outputs" / "collab-1.md",
                "We must remove the legacy_gateway entry from the product.\n",
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
            self.assertNotIn("legacy_gateway", report)
            self.assertNotIn("sessions/", report)

    def test_requirements_audit_ignores_forbidden_patterns_inside_state_proof_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            pattern = (
                r"params\.image_size[^\n]{0,120}"
                r"parameter_template\.size[^\n]{0,120}(不一致|可不同|无需一致)"
            )
            forbidden = "只修改 `params.image_size` 或只修改 `parameter_template.size` 导致两者不一致"
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            forbidden_patterns=[pattern],
                            forbidden_proxy_oracles=[forbidden],
                        )
                    ],
                },
            )
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["Runtime size fields remain aligned."],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    _proof(
                        status="verified",
                        forbidden_proxy_oracles=[forbidden],
                    )
                ],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "tasks": [task.to_dict()],
                },
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            save_run_state(project_root, state)

            ok, report = audit_requirements(project_root, [task])

            self.assertTrue(ok, msg=report)
            self.assertNotIn("forbidden pattern", report)

    def test_requirements_audit_state_only_forbidden_pattern_is_advisory_not_blocking(self) -> None:
        # Corroboration rule: a forbidden pattern that appears only in auto_agents-internal
        # orchestration state (here a task description) is advisory, not blocking, because it
        # has no corroborating authoritative product-file match. This prevents false positives
        # on correct task descriptions that merely discuss or instruct removing a forbidden
        # concept.
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            pattern = (
                r"params\.image_size[^\n]{0,120}"
                r"parameter_template\.size[^\n]{0,120}(不一致|可不同|无需一致)"
            )
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [_requirement(forbidden_patterns=[pattern])],
                },
            )
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Mentions params.image_size and parameter_template.size 可不同 in the plan.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "tasks": [task.to_dict()],
                },
            )

            ok, report = audit_requirements(project_root, [task])

            self.assertTrue(ok, msg=report)
            self.assertIn("advisory: no corroborating authoritative product-file match", report)

    def test_requirements_audit_state_forbidden_pattern_blocks_when_corroborated_by_product_file(self) -> None:
        # When the SAME forbidden pattern also appears in an authoritative product file, the
        # state finding is corroborated and the requirement hard-fails.
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            pattern = (
                r"params\.image_size[^\n]{0,120}"
                r"parameter_template\.size[^\n]{0,120}(不一致|可不同|无需一致)"
            )
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [_requirement(forbidden_patterns=[pattern])],
                },
            )
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Mentions params.image_size and parameter_template.size 可不同 in the plan.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "tasks": [task.to_dict()],
                },
            )
            # Authoritative product doc actually encodes the forbidden approach.
            write_text(
                project_root / ".auto-agents" / "docs" / "architecture.md",
                "The runtime keeps params.image_size and parameter_template.size 可不同.\n",
            )

            ok, report = audit_requirements(project_root, [task])

            self.assertFalse(ok)
            self.assertIn("architecture.md", report)

    def test_requirements_audit_still_hard_fails_forbidden_pattern_in_product_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [_requirement(forbidden_patterns=["legacy_gateway"])],
                },
            )
            write_text(
                project_root / "src" / "backend.py",
                "def call():\n    return 'legacy_gateway'\n",
            )
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )

            ok, report = audit_requirements(project_root, [task])

            self.assertFalse(ok)
            self.assertIn("src/backend.py", report)

    def test_requirements_audit_provider_reference_doc_forbidden_pattern_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [_requirement(forbidden_patterns=["model_selector_ui"])],
                },
            )
            # Provider reference docs describe external API capabilities, not the product's
            # user surface; a match there is corroboration-only.
            ref_dir = project_root / ".auto-agents" / "docs" / "provider_references"
            ref_dir.mkdir(parents=True, exist_ok=True)
            write_text(ref_dir / "some_provider.md", "The API exposes a model_selector_ui field.\n")
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )

            ok, report = audit_requirements(project_root, [task])

            self.assertTrue(ok, msg=report)
            self.assertIn("advisory: no corroborating authoritative product-file match", report)

    def test_requirements_audit_noncurrent_spec_forbidden_pattern_is_advisory(self) -> None:
        # A spec from a DIFFERENT iteration is a historical record; its forbidden-pattern hit
        # is corroboration-only and must not hard-fail or force rewriting history.
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [_requirement(
                    source="specs/2026-07-02-current.md",
                    forbidden_patterns=["legacy_detail_page"],
                )]},
            )
            specs = project_root / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            write_text(specs / "2026-01-01-old.md", "This old iteration required a legacy_detail_page.\n")
            write_text(specs / "2026-07-02-current.md", "Current iteration removes it.\n")
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )

            ok, report = audit_requirements(
                project_root, [task], current_spec=Path("specs/2026-07-02-current.md")
            )

            self.assertTrue(ok, msg=report)
            self.assertIn("advisory: no corroborating authoritative product-file match", report)
            self.assertIn("specs/2026-01-01-old.md", report)

    def test_orchestrator_audit_uses_resume_context_spec_for_historical_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [_requirement(
                    source="specs/2026-07-02-current.md",
                    forbidden_patterns=["legacy_detail_page"],
                )]},
            )
            specs = project_root / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            write_text(specs / "2026-01-01-old.md", "This old iteration required a legacy_detail_page.\n")
            write_text(specs / "2026-07-02-current.md", "Current iteration removes it.\n")
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )
            state = load_run_state(project_root)
            state.tasks = [task]
            state.resume_context["spec_file"] = str(specs / "2026-07-02-current.md")
            save_run_state(project_root, state)

            result = Orchestrator(project_root).audit_requirements()

            self.assertTrue(result["ok"], msg=result["summary"])
            self.assertIn("advisory: no corroborating authoritative product-file match", result["summary"])
            self.assertIn("specs/2026-01-01-old.md", result["summary"])

    def test_requirements_audit_current_spec_forbidden_pattern_hard_fails(self) -> None:
        # The CURRENT iteration spec is authoritative: a forbidden pattern there hard-fails.
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [_requirement(
                    source="specs/2026-07-02-current.md",
                    forbidden_patterns=["legacy_detail_page"],
                )]},
            )
            specs = project_root / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            write_text(specs / "2026-07-02-current.md", "This spec still asks for a legacy_detail_page.\n")
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )

            ok, report = audit_requirements(
                project_root, [task], current_spec=Path("specs/2026-07-02-current.md")
            )

            self.assertFalse(ok)
            self.assertIn("specs/2026-07-02-current.md", report)

    def test_requirements_audit_current_spec_negated_forbidden_pattern_is_not_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            pattern = r"(确认规划|确认资产|确认分镜)[^\n]{0,60}(按钮|入口|用户点击|人工确认|页面)"
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            source="specs/2026-07-02-current.md",
                            forbidden_patterns=[pattern],
                        )
                    ],
                },
            )
            specs = project_root / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            write_text(
                specs / "2026-07-02-current.md",
                "首页不出现确认规划、确认资产或确认分镜入口。\n",
            )
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )

            ok, report = audit_requirements(
                project_root, [task], current_spec=Path("specs/2026-07-02-current.md")
            )

            self.assertTrue(ok, msg=report)
            self.assertNotIn("forbidden pattern", report)

    def test_requirements_audit_test_file_forbidden_pattern_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [_requirement(forbidden_patterns=["legacy_filter"])]},
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            write_text(
                tests_dir / "test_homepage.py",
                "def test_no_legacy_filter():\n    assert 'legacy_filter' not in render_homepage()\n",
            )
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )

            ok, report = audit_requirements(project_root, [task])

            self.assertTrue(ok, msg=report)
            self.assertIn("advisory: no corroborating authoritative product-file match", report)
            self.assertIn("tests/test_homepage.py", report)

    def test_requirements_audit_without_current_spec_keeps_specs_authoritative(self) -> None:
        # With no current spec (e.g. standalone CLI audit) spec files stay strict/authoritative.
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {"version": 1, "requirements": [_requirement(forbidden_patterns=["legacy_detail_page"])]},
            )
            specs = project_root / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            write_text(specs / "2026-01-01-old.md", "This spec required a legacy_detail_page.\n")
            task = TaskSpec(
                task_id="task-001",
                title="Build",
                description="Build it.",
                acceptance=["works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[_proof(status="verified")],
                status="done",
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 1, "tasks": [task.to_dict()]},
            )

            ok, report = audit_requirements(project_root, [task])

            self.assertFalse(ok)
            self.assertIn("specs/2026-01-01-old.md", report)

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

    def test_requirements_audit_passes_verified_provider_references_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            first = ".auto-agents/docs/provider_references/first.md"
            second = ".auto-agents/docs/provider_references/second.md"
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            external_docs_required=True,
                            provider_reference="",
                            provider_references=[first, second],
                        )
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "first": {
                            "path": first,
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/first"],
                            "notes": "",
                        },
                        "second": {
                            "path": second,
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/second"],
                            "notes": "",
                        },
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

    def test_provider_research_validation_accepts_legacy_semicolon_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            first = ".auto-agents/docs/provider_references/first.md"
            second = ".auto-agents/docs/provider_references/second.md"
            write_text(project_root / first, "# First\n")
            write_text(project_root / second, "# Second\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            external_docs_required=True,
                            provider_reference=f"{first}; {second}",
                        )
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "first": {
                            "path": first,
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/first"],
                            "notes": "",
                        },
                        "second": {
                            "path": second,
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/second"],
                            "notes": "",
                        },
                    },
                },
            )

            feedback = Orchestrator(project_root)._provider_research_validation_feedback(
                AgentResult(ok=True, command=[], output_path=project_root / "out.md", summary="")
            )

            self.assertIsNone(feedback)

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

    def test_requirements_audit_ignores_archived_task_plan_text_for_forbidden_patterns(self) -> None:
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
                            "description": "Old review mentioned legacy_gateway while rejecting stale code.",
                            "acceptance": ["The public API returns normalized provider output."],
                            "status": "done",
                            "commit_message": "",
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [_proof(status="verified")],
                        }
                    ],
                },
            )

            ok, report = audit_requirements(project_root, [])

            self.assertTrue(ok, msg=report)
            self.assertIn("REQ-001: pass", report)
            self.assertNotIn(".auto-agents/history/task_plans", report)
            self.assertNotIn("forbidden pattern", report)

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

    def test_requirements_audit_marks_current_done_snapshot_missing_new_oracle_as_advisory(self) -> None:
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
                                "The public API records durable provider evidence.",
                            ],
                            forbidden_proxy_oracles=[
                                "Only checking a metadata-only response",
                            ],
                        )
                    ],
                },
            )

            ok, report = audit_requirements(
                project_root,
                [
                    TaskSpec(
                        task_id="task-001",
                        title="Historical done snapshot",
                        description="Completed before the trace gained a second oracle.",
                        acceptance=["works"],
                        requirement_ids=["REQ-001"],
                        requirement_proofs=[
                            _proof(
                                status="verified",
                                forbidden_proxy_oracles=[],
                            )
                        ],
                        status="done",
                    )
                ],
            )

            self.assertTrue(ok, msg=report)
            self.assertIn("REQ-001: advisory", report)
            self.assertIn("acceptance oracle #2 has no proof entry", report)
            self.assertIn("does not record forbidden proxy exclusion", report)

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

    def test_provider_research_ignores_unrelated_historical_provider_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            current_ref = ".auto-agents/docs/provider_references/current.md"
            historical_ref = ".auto-agents/docs/provider_references/historical_tts.md"
            trace, _ = stamp_requirement_contract_hashes(
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            id="REQ-CURRENT",
                            external_docs_required=True,
                            provider_reference=current_ref,
                        ),
                        _requirement(
                            id="REQ-HISTORICAL",
                            text="Unrelated historical TTS contract.",
                            external_docs_required=True,
                            provider_reference=historical_ref,
                        ),
                    ],
                }
            )
            write_json(requirements_trace_path(project_root), trace)
            write_text(project_root / current_ref, "# Current provider\n")
            write_text(project_root / historical_ref, "# Historical TTS provider\n")
            lock, _ = stamp_provider_reference_consumer_hashes(
                {
                    "version": 1,
                    "references": {
                        "current": {"path": current_ref, "status": "verified"},
                        "historical_tts": {
                            "path": historical_ref,
                            "status": "needs_user_input",
                        },
                    },
                },
                trace,
                reference_paths={current_ref},
            )
            write_json(provider_references_lock_path(project_root), lock)
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-current",
                            "title": "Current iteration provider work",
                            "description": "Current scope only.",
                            "acceptance": ["current provider remains verified"],
                            "requirement_ids": ["REQ-CURRENT"],
                            "status": "pending",
                        }
                    ]
                },
            )
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)

            state = orchestrator._run_provider_research(state, project_root / "spec.md")

            self.assertEqual(state.current_stage, "provider_research")
            self.assertIn("already verified", state.stage_summaries["provider_research"])
            blockers = orchestrator.provider_research_blockers(
                requirement_ids={"REQ-CURRENT"}
            )
            self.assertEqual(blockers, [])
            persisted_lock = json.loads(
                provider_references_lock_path(project_root).read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted_lock["references"]["historical_tts"]["status"],
                "needs_user_input",
            )

    def test_rejected_provider_research_refreshes_verified_lock(self) -> None:
        class RefreshAdapter:
            def __init__(self, project_root: Path, reference: str) -> None:
                self.project_root = project_root
                self.reference = reference
                self.calls = 0

            def run(self, request):
                self.calls += 1
                write_text(self.project_root / self.reference, "# Provider\n\nrefreshed\n")
                write_json(
                    provider_references_lock_path(self.project_root),
                    {
                        "version": 1,
                        "references": {
                            "provider": {
                                "path": self.reference,
                                "status": "verified",
                                "retrieved_at": "2026-04-11T00:00:00Z",
                                "source_urls": ["https://example.com/official"],
                                "notes": "refreshed after review",
                            }
                        },
                    },
                )
                summary = "provider reference refreshed\n"
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary.strip(),
                    returncode=0,
                )

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            reference = ".auto-agents/docs/provider_references/provider.md"
            write_text(project_root / reference, "# Provider\n\nstale\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(
                            id="REQ-102",
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
            adapter = RefreshAdapter(project_root, reference)
            orchestrator.adapter = adapter
            state = load_run_state(project_root)
            state.rejected_stage = "provider_research"
            state.rejection_reason = (
                "Review feedback: REQ-102 still lacks canonical provider reference details."
            )

            state = orchestrator._run_provider_research(state, project_root / "spec.md")

            self.assertEqual(adapter.calls, 1)
            self.assertEqual(state.current_stage, "provider_research")
            self.assertIn("refreshed", state.stage_summaries["provider_research"])
            lock = json.loads(provider_references_lock_path(project_root).read_text(encoding="utf-8"))
            self.assertEqual(lock["references"]["provider"]["status"], "verified")

    def test_rejected_provider_research_without_refresh_target_is_self_repair_error(self) -> None:
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
                            id="REQ-001",
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
            state.rejected_stage = "provider_research"
            state.rejection_reason = "Review feedback points to provider_research but names no reference."

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_provider_research(state, project_root / "spec.md")

            self.assertIn("recovery loop orchestration no-op", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
