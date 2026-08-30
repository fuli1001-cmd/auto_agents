import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    load_run_state,
    provider_references_lock_path,
    requirements_trace_path,
    save_run_state,
    task_plan_path,
)
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, ProviderConfig, TaskSpec
from auto_agents.frontend_fidelity import (
    requirement_is_frontend_fidelity,
    validate_frontend_fidelity_task_plan,
    validate_frontend_fidelity_trace,
)
from auto_agents.frontend_design import validate_frontend_scope
from auto_agents.orchestrator import Orchestrator
from auto_agents.provider_contract import (
    PROVIDER_REFERENCE_CONTRACT_VERSION,
    PROVIDER_REFERENCE_V2_HEADINGS,
)
from auto_agents.requirements import (
    ambiguous_historical_requirement_contract_ids,
    audit_requirements,
    historical_requirement_contract_collision_ids,
    load_requirements_trace,
    migrate_legacy_provider_reference_consumer_hashes,
    normalize_generated_task_plan_statuses,
    next_unused_requirement_id,
    non_amendable_ambiguous_requirement_contract_ids,
    preserve_task_plan_negative_oracle_clauses,
    run_requirements_audit,
    requirement_contract_sha256,
    stamp_requirement_contract_hashes,
    stamp_task_plan_contract_hashes,
    provider_reference_consumer_contract_sha256,
    provider_reference_effective_status,
    stamp_provider_reference_consumer_hashes,
    unique_historical_requirement_contract_ids,
    validate_done_task_requirement_proofs,
    validate_requirements_trace_payload,
    validate_requirement_contract_transitions,
    validate_task_requirement_coverage,
    validate_provider_resolve_trace_transition,
)
from auto_agents.validation import validate_task_plan_with_requirements, validation_report
from auto_agents.visual_judge import (
    collect_visual_evidence_for_task,
    parse_visual_judge_response,
    visual_evidence_pairs_for_task,
)
import auto_agents.requirements as requirements_module


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

    def supports_image_attachments(self) -> bool:
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


class _SequencedVisualJudgeAdapter(_VisualJudgeAdapter):
    def __init__(self, summaries) -> None:
        super().__init__("")
        self.summaries = list(summaries)

    def run(self, request) -> AgentResult:
        self.summary = self.summaries.pop(0)
        return super().run(request)


class _UnsupportedVisualJudgeAdapter(_VisualJudgeAdapter):
    def supports_image_attachments(self) -> bool:
        return False

    def run(self, request) -> AgentResult:
        raise AssertionError("an adapter without image attachments must not be invoked")


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

    def test_provider_resolve_trace_transition_allows_only_notes_and_approved_defer(self) -> None:
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [_requirement()]}
        )
        notes_only = json.loads(json.dumps(previous))
        notes_only["requirements"][0]["notes"] = "User approved the conservative provider assumption."
        deferred = json.loads(json.dumps(notes_only))
        deferred["requirements"][0]["status"] = "deferred"

        self.assertEqual(
            validate_provider_resolve_trace_transition(previous, notes_only),
            [],
        )
        self.assertTrue(
            any(
                "without an explicit session-owned defer approval" in error
                for error in validate_provider_resolve_trace_transition(previous, deferred)
            )
        )
        self.assertEqual(
            validate_provider_resolve_trace_transition(
                previous,
                deferred,
                deferred_requirement_ids={"REQ-001"},
            ),
            [],
        )

    def test_provider_resolve_trace_transition_rejects_contract_and_shape_changes(self) -> None:
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [_requirement()]}
        )
        changed_source = json.loads(json.dumps(previous))
        changed_source["requirements"][0]["source"] += "; provider conversation"
        added_requirement = json.loads(json.dumps(previous))
        added_requirement["requirements"].append(_requirement(id="REQ-002"))

        source_errors = validate_provider_resolve_trace_transition(previous, changed_source)
        shape_errors = validate_provider_resolve_trace_transition(previous, added_requirement)

        self.assertTrue(any("contract-owned fields" in error for error in source_errors))
        self.assertTrue(any("proof-bearing contract" in error for error in source_errors))
        self.assertTrue(any("IDs or ordering" in error for error in shape_errors))

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

    def test_new_requirement_cannot_reuse_id_reserved_only_by_archived_proof(self) -> None:
        previous = {"version": 1, "requirements": [_requirement()]}
        current = json.loads(json.dumps(previous))
        current["requirements"].append(_requirement(id="REQ-032"))
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": [],
                "requirement_proofs": [
                    _proof(requirement_id="REQ-032")
                ],
            }
        ]
        errors = validate_requirement_contract_transitions(
            previous,
            current,
            historical_tasks=historical,
        )

        self.assertTrue(any("namespace collision for REQ-032" in error for error in errors))

    def test_next_requirement_id_includes_archived_ids_and_proofs(self) -> None:
        trace = {"version": 1, "requirements": [_requirement(id="REQ-004")]}
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-011"],
                "requirement_proofs": [
                    _proof(requirement_id="REQ-019")
                ],
            }
        ]

        self.assertEqual(
            next_unused_requirement_id(trace, historical_tasks=historical),
            "REQ-020",
        )

    def test_active_contract_collision_with_archived_proof_requires_supersession(self) -> None:
        archived_requirement = _requirement(
            id="REQ-032",
            text="Delivered historical behavior.",
        )
        active_requirement = _requirement(
            id="REQ-032",
            text="Different current behavior.",
        )
        current = {"version": 1, "requirements": [active_requirement]}
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-032"],
                "requirement_proofs": [
                    _proof(
                        requirement_id="REQ-032",
                        requirement_contract_sha256=requirement_contract_sha256(
                            archived_requirement
                        ),
                    )
                ],
            }
        ]

        self.assertEqual(
            historical_requirement_contract_collision_ids(current, historical),
            ["REQ-032"],
        )
        errors = validate_requirement_contract_transitions(
            current,
            json.loads(json.dumps(current)),
            historical_tasks=historical,
        )
        self.assertTrue(
            any(
                "current proof-bearing contract conflicts with archived delivered proof"
                in error
                for error in errors
            )
        )

    def test_corrupted_delivered_contract_recovers_over_two_clarify_transitions(self) -> None:
        archived_requirement = _requirement(text="Delivered historical behavior.")
        corrupted_requirement = _requirement(text="Drifted current behavior.")
        initial, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [corrupted_requirement]}
        )
        quarantined_old = _requirement(
            text="Drifted current behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        quarantined, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [quarantined_old, replacement]}
        )
        restored_old = _requirement(
            text="Delivered historical behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        restored, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [restored_old, replacement]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            archived_requirement
                        )
                    )
                ],
            }
        ]
        later_historical = [
            *historical,
            {
                "task_id": "task-replacement",
                "status": "done",
                "requirement_ids": ["REQ-002"],
            },
        ]

        self.assertEqual(
            validate_requirement_contract_transitions(
                initial,
                quarantined,
                historical_tasks=historical,
            ),
            [],
        )
        self.assertEqual(
            historical_requirement_contract_collision_ids(
                quarantined,
                historical,
            ),
            ["REQ-001"],
        )
        unchanged_errors = validate_requirement_contract_transitions(
            quarantined,
            json.loads(json.dumps(quarantined)),
            historical_tasks=later_historical,
        )
        self.assertTrue(
            any("archived-contract mismatch" in error for error in unchanged_errors)
        )
        self.assertEqual(
            validate_requirement_contract_transitions(
                quarantined,
                restored,
                historical_tasks=later_historical,
            ),
            [],
        )
        self.assertEqual(
            historical_requirement_contract_collision_ids(
                restored,
                later_historical,
            ),
            [],
        )

    def test_corrupted_contract_recovery_rejects_ambiguous_verified_hashes(self) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        corrupted_old = _requirement(
            text="Drifted current behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [corrupted_old, replacement]}
        )
        restored_old = _requirement(
            text="First archived behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        current, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [restored_old, replacement]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            }
        ]

        errors = validate_requirement_contract_transitions(
            previous,
            current,
            historical_tasks=historical,
        )

        self.assertTrue(any("contract drift" in error for error in errors))
        self.assertTrue(any("ambiguous" in error for error in errors))

    def test_ambiguous_contract_accepts_unchanged_terminal_quarantine(self) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        quarantined_old = _requirement(
            text="Current preserved behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        trace, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [quarantined_old, replacement]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            }
        ]

        self.assertEqual(
            ambiguous_historical_requirement_contract_ids(trace, historical),
            ["REQ-001"],
        )
        self.assertEqual(
            unique_historical_requirement_contract_ids(trace, historical),
            [],
        )
        self.assertEqual(
            validate_requirement_contract_transitions(
                trace,
                json.loads(json.dumps(trace)),
                historical_tasks=historical,
            ),
            [],
        )
        self.assertEqual(
            non_amendable_ambiguous_requirement_contract_ids(trace, historical),
            [],
        )

    def test_ambiguous_terminal_quarantine_allows_reciprocal_chain_to_active_descendant(
        self,
    ) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        quarantined_old = _requirement(
            text="Current preserved behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [quarantined_old, replacement]}
        )
        superseded_replacement = json.loads(
            json.dumps(previous["requirements"][1])
        )
        superseded_replacement.update(
            {"status": "superseded", "superseded_by": ["REQ-003"]}
        )
        active_descendant = _requirement(
            id="REQ-003",
            text="Iteration replacement behavior.",
            supersedes=["REQ-002"],
        )
        current, _ = stamp_requirement_contract_hashes(
            {
                "version": 1,
                "requirements": [
                    json.loads(json.dumps(previous["requirements"][0])),
                    superseded_replacement,
                    active_descendant,
                ],
            }
        )
        historical = [
            {
                "task_id": "task-ambiguous-archive",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            },
            {
                "task_id": "task-replacement-proof",
                "status": "done",
                "requirement_ids": ["REQ-002"],
                "requirement_proofs": [
                    _proof(
                        requirement_id="REQ-002",
                        requirement_contract_sha256=requirement_contract_sha256(
                            replacement
                        ),
                    )
                ],
            },
        ]

        self.assertEqual(validate_requirements_trace_payload(current), [])
        self.assertEqual(
            validate_requirement_contract_transitions(
                previous,
                current,
                historical_tasks=historical,
            ),
            [],
        )
        self.assertEqual(
            non_amendable_ambiguous_requirement_contract_ids(current, historical),
            [],
        )
        proof_transfer_errors = validate_requirement_contract_transitions(
            previous,
            current,
            historical_tasks=[
                *historical,
                {
                    "task_id": "task-reserved-descendant",
                    "status": "done",
                    "requirement_ids": ["REQ-003"],
                },
            ],
        )
        self.assertTrue(
            any("ambiguous" in error for error in proof_transfer_errors),
            proof_transfer_errors,
        )

    def test_ambiguous_terminal_quarantine_rejects_rewired_downstream_chain(
        self,
    ) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        quarantined_old = _requirement(
            text="Current preserved behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        superseded_replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            status="superseded",
            supersedes=["REQ-001"],
            superseded_by=["REQ-003"],
        )
        active_descendant = _requirement(
            id="REQ-003",
            text="Existing terminal behavior.",
            supersedes=["REQ-002"],
        )
        previous, _ = stamp_requirement_contract_hashes(
            {
                "version": 1,
                "requirements": [
                    quarantined_old,
                    superseded_replacement,
                    active_descendant,
                ],
            }
        )
        rewired = json.loads(json.dumps(previous))
        rewired["requirements"][1]["superseded_by"] = ["REQ-004"]
        rewired["requirements"][2]["supersedes"] = []
        rewired["requirements"].append(
            _requirement(
                id="REQ-004",
                text="Substituted terminal behavior.",
                supersedes=["REQ-002"],
            )
        )
        rewired, _ = stamp_requirement_contract_hashes(rewired)
        historical = [
            {
                "task_id": "task-ambiguous-archive",
                "status": "done",
                "requirement_ids": ["REQ-001", "REQ-002", "REQ-003"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            }
        ]

        self.assertEqual(
            validate_requirement_contract_transitions(
                previous,
                json.loads(json.dumps(previous)),
                historical_tasks=historical,
            ),
            [],
        )
        extended = json.loads(json.dumps(previous))
        extended["requirements"][2].update(
            {"status": "superseded", "superseded_by": ["REQ-004"]}
        )
        extended["requirements"].append(
            _requirement(
                id="REQ-004",
                text="Extended terminal behavior.",
                supersedes=["REQ-003"],
            )
        )
        extended, _ = stamp_requirement_contract_hashes(extended)
        self.assertEqual(validate_requirements_trace_payload(extended), [])
        self.assertEqual(
            validate_requirement_contract_transitions(
                previous,
                extended,
                historical_tasks=historical,
            ),
            [],
        )

        self.assertEqual(validate_requirements_trace_payload(rewired), [])
        errors = validate_requirement_contract_transitions(
            previous,
            rewired,
            historical_tasks=historical,
        )
        self.assertTrue(any("ambiguous" in error for error in errors), errors)

    def test_ambiguous_terminal_quarantine_rejects_unsafe_downstream_topologies(
        self,
    ) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        quarantined_old = _requirement(
            text="Current preserved behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [quarantined_old, replacement]}
        )
        historical = [
            {
                "task_id": "task-ambiguous-archive",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            }
        ]

        dead_end = json.loads(json.dumps(previous))
        dead_end["requirements"][1]["status"] = "superseded"

        missing_descendant = json.loads(json.dumps(dead_end))
        missing_descendant["requirements"][1]["superseded_by"] = ["REQ-003"]

        missing_reciprocal = json.loads(json.dumps(missing_descendant))
        missing_reciprocal["requirements"].append(
            _requirement(
                id="REQ-003",
                text="Nonreciprocal terminal behavior.",
            )
        )

        cycle = json.loads(json.dumps(previous))
        cycle["requirements"][1].update(
            {
                "status": "superseded",
                "supersedes": ["REQ-001", "REQ-003"],
                "superseded_by": ["REQ-003"],
            }
        )
        cycle["requirements"].append(
            _requirement(
                id="REQ-003",
                text="Cyclic replacement behavior.",
                status="superseded",
                supersedes=["REQ-002"],
                superseded_by=["REQ-002"],
            )
        )

        incomplete_branch = json.loads(json.dumps(previous))
        incomplete_branch["requirements"][1].update(
            {
                "status": "superseded",
                "superseded_by": ["REQ-003", "REQ-004"],
            }
        )
        incomplete_branch["requirements"].extend(
            [
                _requirement(
                    id="REQ-003",
                    text="Available branch behavior.",
                    supersedes=["REQ-002"],
                ),
                _requirement(
                    id="REQ-004",
                    text="Dead branch behavior.",
                    status="superseded",
                    supersedes=["REQ-002"],
                ),
            ]
        )

        candidates = {
            "superseded dead end": dead_end,
            "missing descendant": missing_descendant,
            "missing reciprocity": missing_reciprocal,
            "cycle": cycle,
            "incomplete branch": incomplete_branch,
        }
        for label, candidate in candidates.items():
            with self.subTest(label=label):
                candidate, _ = stamp_requirement_contract_hashes(candidate)
                errors = validate_requirement_contract_transitions(
                    previous,
                    candidate,
                    historical_tasks=historical,
                )
                self.assertTrue(
                    any("ambiguous" in error for error in errors),
                    errors,
                )

        cycle, _ = stamp_requirement_contract_hashes(cycle)
        self.assertEqual(validate_requirements_trace_payload(cycle), [])

    def test_ambiguous_contract_can_enter_terminal_quarantine(self) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        active = _requirement(text="Current preserved behavior.")
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [active]}
        )
        quarantined = json.loads(json.dumps(active))
        quarantined.update(
            {"status": "superseded", "superseded_by": ["REQ-002"]}
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        current, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [quarantined, replacement]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            }
        ]

        self.assertEqual(
            validate_requirement_contract_transitions(
                previous,
                current,
                historical_tasks=historical,
            ),
            [],
        )
        proof_transfer_errors = validate_requirement_contract_transitions(
            previous,
            current,
            historical_tasks=[
                *historical,
                {
                    "task_id": "task-reserved-replacement",
                    "status": "done",
                    "requirement_ids": ["REQ-002"],
                },
            ],
        )
        self.assertTrue(
            any("ambiguous" in error for error in proof_transfer_errors),
            proof_transfer_errors,
        )

    def test_ambiguous_terminal_quarantine_rejects_every_unsafe_escape(self) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        quarantined_old = _requirement(
            text="Current preserved behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [quarantined_old, replacement]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            }
        ]

        changed_contract = json.loads(json.dumps(previous))
        changed_contract["requirements"][0]["text"] = "Selected archived behavior."
        changed_contract, _ = stamp_requirement_contract_hashes(changed_contract)

        changed_hash = json.loads(json.dumps(previous))
        changed_hash["requirements"][0]["contract_sha256"] = "sha256:replacement"

        reactivated = json.loads(json.dumps(previous))
        reactivated["requirements"][0]["status"] = "active"

        removed_link = json.loads(json.dumps(previous))
        removed_link["requirements"][0]["superseded_by"] = []

        substituted_link = json.loads(json.dumps(previous))
        substituted_link["requirements"][0]["superseded_by"] = ["REQ-003"]
        substituted_link["requirements"].append(
            _requirement(
                id="REQ-003",
                text="Substituted replacement behavior.",
                supersedes=["REQ-001"],
            )
        )
        substituted_link, _ = stamp_requirement_contract_hashes(substituted_link)

        missing_reciprocal = json.loads(json.dumps(previous))
        missing_reciprocal["requirements"][1]["supersedes"] = []

        for label, candidate in (
            ("proof-bearing edit", changed_contract),
            ("contract hash edit", changed_hash),
            ("reactivation", reactivated),
            ("link removal", removed_link),
            ("link substitution", substituted_link),
            ("missing reciprocity", missing_reciprocal),
        ):
            with self.subTest(label=label):
                errors = validate_requirement_contract_transitions(
                    previous,
                    candidate,
                    historical_tasks=historical,
                )
                self.assertTrue(
                    any("ambiguous" in error for error in errors),
                    errors,
                )

    def test_malformed_ambiguous_quarantine_is_non_amendable(self) -> None:
        first_archived = _requirement(text="First archived behavior.")
        second_archived = _requirement(text="Second archived behavior.")
        quarantined_old = _requirement(
            text="Current preserved behavior.",
            status="superseded",
            superseded_by=[],
        )
        trace, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [quarantined_old]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            first_archived
                        )
                    ),
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            second_archived
                        )
                    ),
                ],
            }
        ]

        self.assertEqual(
            non_amendable_ambiguous_requirement_contract_ids(trace, historical),
            ["REQ-001"],
        )

    def test_unverified_archived_hash_cannot_authorize_contract_recovery(self) -> None:
        archived_requirement = _requirement(text="Unverified historical behavior.")
        corrupted_old = _requirement(
            text="Drifted current behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
            supersedes=["REQ-001"],
        )
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [corrupted_old, replacement]}
        )
        restored_old = _requirement(
            text="Unverified historical behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        current, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [restored_old, replacement]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            archived_requirement
                        ),
                        status="planned",
                    )
                ],
            }
        ]

        errors = validate_requirement_contract_transitions(
            previous,
            current,
            historical_tasks=historical,
        )

        self.assertTrue(any("contract drift" in error for error in errors))

    def test_corrupted_contract_recovery_requires_reciprocal_replacement(self) -> None:
        archived_requirement = _requirement(text="Delivered historical behavior.")
        corrupted_old = _requirement(
            text="Drifted current behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        replacement = _requirement(
            id="REQ-002",
            text="Current replacement behavior.",
        )
        previous, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [corrupted_old, replacement]}
        )
        restored_old = _requirement(
            text="Delivered historical behavior.",
            status="superseded",
            superseded_by=["REQ-002"],
        )
        current, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [restored_old, replacement]}
        )
        historical = [
            {
                "task_id": "task-archived",
                "status": "done",
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    _proof(
                        requirement_contract_sha256=requirement_contract_sha256(
                            archived_requirement
                        )
                    )
                ],
            }
        ]

        errors = validate_requirement_contract_transitions(
            previous,
            current,
            historical_tasks=historical,
        )

        self.assertTrue(any("contract drift" in error for error in errors))

    def test_legacy_oracle_drift_also_marks_historical_id_collision(self) -> None:
        current = {
            "version": 1,
            "requirements": [_requirement(id="REQ-035")],
        }
        historical = [
            {
                "task_id": "task-legacy",
                "status": "done",
                "requirement_ids": ["REQ-035"],
                "requirement_proofs": [
                    _proof(
                        requirement_id="REQ-035",
                        acceptance_oracle="A different historical oracle.",
                    )
                ],
            }
        ]

        self.assertEqual(
            historical_requirement_contract_collision_ids(current, historical),
            ["REQ-035"],
        )

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

    def test_forbidden_pattern_definition_validation_respects_lifecycle(self) -> None:
        unsafe = r"(?s)for\s+.*check.*(?:retry|attempt)"
        bounded = r"for\s+[\s\S]{0,500}?check[\s\S]{0,500}?(?:retry|attempt)"

        for status in ("active", "deferred"):
            with self.subTest(status=status):
                errors = validate_requirements_trace_payload(
                    {
                        "version": 1,
                        "requirements": [
                            _requirement(status=status, forbidden_patterns=[unsafe])
                        ],
                    }
                )
                self.assertTrue(any("definition is unsafe" in error for error in errors))

        bounded_errors = validate_requirements_trace_payload(
            {
                "version": 1,
                "requirements": [_requirement(forbidden_patterns=[bounded])],
            }
        )
        superseded_errors = validate_requirements_trace_payload(
            {
                "version": 1,
                "requirements": [
                    _requirement(status="superseded", forbidden_patterns=[unsafe])
                ],
            }
        )

        self.assertEqual(bounded_errors, [])
        self.assertEqual(superseded_errors, [])

    def test_proven_unsafe_pattern_can_be_archived_and_replaced_safely(self) -> None:
        unsafe = r"(?s)for\s+.*check.*(?:retry|attempt)"
        previous = {
            "version": 1,
            "requirements": [_requirement(forbidden_patterns=[unsafe])],
        }
        old = _requirement(
            status="superseded",
            forbidden_patterns=[unsafe],
            superseded_by=["REQ-002"],
        )
        new = _requirement(
            id="REQ-002",
            forbidden_patterns=[r"for\s+[\s\S]{0,500}?check"],
            supersedes=["REQ-001"],
        )
        current, _ = stamp_requirement_contract_hashes(
            {"version": 1, "requirements": [old, new]}
        )
        historical = [
            {"task_id": "task-old", "status": "done", "requirement_ids": ["REQ-001"]}
        ]

        self.assertEqual(
            validate_requirement_contract_transitions(
                previous,
                current,
                historical_tasks=historical,
            ),
            [],
        )
        self.assertEqual(validate_requirements_trace_payload(current), [])

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

    def test_visual_judge_never_infers_success_and_failure_screenshots_from_evidence_refs(self) -> None:
        task = TaskSpec(
            task_id="task-visual",
            title="Create flow behavior",
            description="Verify success and failure behavior.",
            acceptance=["Success returns home and failure stays in the dialog."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    exact_acceptance_oracle=(
                        "Creation success returns to the generation list and failure remains in the dialog."
                    ),
                    evidence_refs=[
                        ".tmp-tests/frontend-prototype/create-modal-success-home-desktop-1440x900.png",
                        ".tmp-tests/frontend-prototype/create-modal-failure-desktop-1440x900.png",
                    ],
                )
            ],
        )
        trace = {
            "version": 1,
            "frontend_surfaces": [{"name": "Create", "prototype_refs": ["create.html"]}],
            "requirements": [
                _requirement(
                    text="Create surface matches the prototype.",
                    oracle_type="mixed",
                    frontend_surface=True,
                )
            ],
        }

        self.assertEqual(visual_evidence_pairs_for_task(task, trace, max_pairs=6), [])

    def test_visual_judge_deduplicates_explicit_pairs_and_merges_proof_owners(self) -> None:
        visual_evidence = {
            "surface": "create_video_modal",
            "viewport": "desktop 1440x900 open modal",
            "purpose": "prototype_fidelity",
            "prototype_image_ref": ".tmp-tests/frontend-prototype/create-modal-prototype.png",
            "actual_image_ref": ".tmp-tests/frontend-prototype/create-modal.png",
        }
        task = TaskSpec(
            task_id="task-visual",
            title="Create modal fidelity",
            description="Match prototype.",
            acceptance=["Modal matches."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(oracle_index=index, visual_evidence=dict(visual_evidence))
                for index in (1, 2, 6)
            ],
        )
        trace = {
            "version": 1,
            "frontend_surfaces": [{"name": "Create", "prototype_refs": ["create.html"]}],
            "requirements": [
                _requirement(
                    text="Create surface matches the prototype.",
                    oracle_type="mixed",
                    frontend_surface=True,
                )
            ],
        }

        selection = collect_visual_evidence_for_task(task, trace, max_pairs=6)

        self.assertEqual(len(selection.pairs), 1)
        self.assertEqual(
            [owner["oracle_index"] for owner in selection.pairs[0].proof_owners],
            [1, 2, 6],
        )
        self.assertTrue(
            Orchestrator._append_visual_judge_report_to_proofs(
                task,
                selection.pairs,
                ".auto-agents/runs/run/visual_judge/task-visual/report.json",
            )
        )
        self.assertTrue(
            all(
                any("/visual_judge/" in ref for ref in proof["evidence_refs"])
                for proof in task.requirement_proofs
            )
        )

    def test_visual_judge_rejects_html_as_prototype_image(self) -> None:
        task = TaskSpec(
            task_id="task-visual",
            title="Home fidelity",
            description="Match prototype.",
            acceptance=["Home matches."],
            requirement_ids=["REQ-001"],
            requirement_proofs=[
                _proof(
                    visual_evidence={
                        "surface": "Home",
                        "viewport": "desktop",
                        "purpose": "prototype_fidelity",
                        "prototype_image_ref": "specs/frondend_prototype/home.html",
                        "actual_image_ref": ".tmp-tests/home.png",
                        "prototype_source_ref": "specs/frondend_prototype/home.html",
                    }
                )
            ],
        )
        trace = {
            "version": 1,
            "frontend_surfaces": [{"name": "Home", "prototype_refs": ["home.html"]}],
            "requirements": [
                _requirement(
                    text="Home matches the prototype.",
                    oracle_type="mixed",
                    frontend_surface=True,
                )
            ],
        }

        selection = collect_visual_evidence_for_task(task, trace, max_pairs=6)

        self.assertEqual(selection.pairs, [])
        self.assertTrue(any("prototype_image_ref must reference" in item for item in selection.diagnostics))

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

    def test_visual_judge_parse_preserves_inconclusive_for_auto_mode_routing(self) -> None:
        report = parse_visual_judge_response(
            '{"status":"inconclusive","score":0,"findings":[],"summary":"image unavailable"}',
            threshold=85,
            expected_pair_ids=["pair-1"],
        )

        self.assertEqual(report.status, "inconclusive")
        self.assertEqual(report.pair_results[0]["status"], "inconclusive")

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

    def test_visual_judge_excludes_provider_without_image_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [
                        {"name": "Home", "prototype_refs": ["home.html"]}
                    ],
                    "requirements": [
                        _requirement(
                            text="Frontend Home page matches the prototype visual surface.",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            screenshot_dir = project_root / ".auto-agents" / "runs" / "run-1" / "screenshots"
            screenshot_dir.mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(screenshot_dir / name, "fake image")

            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "auto"
            for provider in orchestrator.config.providers.values():
                provider.vision = "disabled"
            orchestrator.config.providers[orchestrator.config.active_provider].vision = "enabled"
            orchestrator.adapter = _UnsupportedVisualJudgeAdapter("unused")

            result = orchestrator._run_task_visual_judge(
                load_run_state(project_root),
                _visual_task(),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "skipped")
            report = json.loads(
                (project_root / result["report_path"]).read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(
                    "native image attachments are unsupported" in item
                    for item in report["diagnostics"]
                )
            )

    def test_visual_judge_uses_supported_fallback_after_unsupported_active_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [
                        {"name": "Home", "prototype_refs": ["home.html"]}
                    ],
                    "requirements": [
                        _requirement(
                            text="Frontend Home page matches the prototype visual surface.",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            screenshot_dir = project_root / ".auto-agents" / "runs" / "run-1" / "screenshots"
            screenshot_dir.mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(screenshot_dir / name, "fake image")

            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "required"
            for provider in orchestrator.config.providers.values():
                provider.vision = "disabled"
            orchestrator.config.providers[orchestrator.config.active_provider].vision = "enabled"
            orchestrator.config.providers["copilot-cli"] = ProviderConfig(
                kind="copilot-cli",
                binary="copilot",
                profile_map={},
                vision="enabled",
            )
            orchestrator.adapter = _UnsupportedVisualJudgeAdapter("unused")
            fallback = _VisualJudgeAdapter(
                '{"status":"passed","score":97,"findings":[],"summary":"matches"}'
            )

            with patch.object(
                orchestrator,
                "_build_adapter_for_provider",
                return_value=fallback,
            ):
                result = orchestrator._run_task_visual_judge(
                    load_run_state(project_root),
                    _visual_task(),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(fallback.requests), 1)
            self.assertEqual(len(fallback.requests[0].attachments), 2)

    def test_visual_judge_does_not_fallback_from_explicit_unsupported_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [
                        {"name": "Home", "prototype_refs": ["home.html"]}
                    ],
                    "requirements": [
                        _requirement(
                            text="Frontend Home page matches the prototype visual surface.",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            screenshot_dir = project_root / ".auto-agents" / "runs" / "run-1" / "screenshots"
            screenshot_dir.mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(screenshot_dir / name, "fake image")

            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "required"
            orchestrator.config.visual_judge.provider = orchestrator.config.active_provider
            orchestrator.config.providers[orchestrator.config.active_provider].vision = "enabled"
            orchestrator.adapter = _UnsupportedVisualJudgeAdapter("unused")

            with patch.object(orchestrator, "_build_adapter_for_provider") as build_adapter:
                result = orchestrator._run_task_visual_judge(
                    load_run_state(project_root),
                    _visual_task(),
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            build_adapter.assert_not_called()

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
            self.assertEqual(len(orchestrator.adapter.requests), 2)

    def test_visual_judge_batch_failure_is_overturned_by_isolated_pair_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [{"name": "Home", "prototype_refs": ["home.html"]}],
                    "requirements": [
                        _requirement(
                            text="Home matches the prototype.",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            screenshot_dir = project_root / ".auto-agents" / "runs" / "run-1" / "screenshots"
            screenshot_dir.mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(screenshot_dir / name, "fake image")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "required"
            orchestrator.config.providers[orchestrator.config.active_provider].vision = "enabled"
            orchestrator.adapter = _SequencedVisualJudgeAdapter(
                [
                    '{"status":"failed","score":62,"findings":[{"severity":"blocker","message":"wrong layout"}],"summary":"batch mismatch"}',
                    '{"status":"passed","score":96,"findings":[],"summary":"isolated pair matches"}',
                ]
            )

            state = load_run_state(project_root)
            result = orchestrator._run_task_visual_judge(state, _visual_task())

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(orchestrator.adapter.requests), 2)
            self.assertTrue(all(len(request.attachments) == 2 for request in orchestrator.adapter.requests))
            self.assertIn("prototype_attachment_index", orchestrator.adapter.requests[0].prompt)
            report = json.loads(
                (project_root / ".auto-agents" / "runs" / state.run_id / "visual_judge" / "task-visual" / "report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([item["phase"] for item in report["attempts"]], ["batch", "recheck"])

    def test_visual_judge_auto_skips_when_isolated_recheck_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_surfaces": [{"name": "Home", "prototype_refs": ["home.html"]}],
                    "requirements": [
                        _requirement(
                            text="Home matches the prototype.",
                            oracle_type="mixed",
                            frontend_surface=True,
                        )
                    ],
                },
            )
            screenshot_dir = project_root / ".auto-agents" / "runs" / "run-1" / "screenshots"
            screenshot_dir.mkdir(parents=True)
            for name in ("prototype-home.png", "home.png"):
                write_text(screenshot_dir / name, "fake image")
            orchestrator = Orchestrator(project_root)
            orchestrator.config.visual_judge.mode = "auto"
            orchestrator.config.providers[orchestrator.config.active_provider].vision = "enabled"
            response = (
                '{"status":"inconclusive","score":0,"findings":[],'
                '"summary":"could not inspect image"}'
            )
            orchestrator.adapter = _SequencedVisualJudgeAdapter([response, response])

            result = orchestrator._run_task_visual_judge(
                load_run_state(project_root),
                _visual_task(),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(len(orchestrator.adapter.requests), 2)

    def test_visual_gate_recheck_skips_implementation_on_first_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            task = TaskSpec(
                task_id="task-visual-resume",
                title="Resume visual gate",
                description="Recheck gates without implementation.",
                acceptance=["Gates pass."],
                status="in_progress",
            )
            verify_result = {
                "ok": True,
                "reason": "all commands passed",
                "current_failure_ids": [],
                "proof_evidence": {},
            }
            review_result = {"ok": True, "review": "DECISION: pass", "reason": "review passed"}
            with (
                patch.object(orchestrator, "_run_task_verify", return_value=verify_result),
                patch.object(orchestrator, "_cached_review_result", return_value=review_result),
                patch.object(orchestrator, "_run_agent_with_retries") as implement,
            ):
                result = orchestrator._execute_task_with_retries(
                    state,
                    task,
                    gate_recheck_first=True,
                )

            self.assertTrue(result["ok"])
            implement.assert_not_called()

    def test_frontend_prototype_spec_requires_surface_contract(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        spec = "Build a frontend page that must match specs/frondend_prototype/home.html prototype screenshots."

        errors = validate_frontend_fidelity_trace(trace, spec_text=spec)

        self.assertTrue(any("frontend prototype fidelity" in item for item in errors))
        self.assertTrue(any("frontend_surfaces" in item for item in errors))

    def test_non_frontend_screen_direction_and_typed_snapshot_spec_is_not_fidelity(self) -> None:
        trace = {"version": 1, "requirements": [_requirement()]}
        spec = (
            "Preserve storyboard screen direction and its typed state snapshot; "
            "this iteration changes backend contracts only."
        )

        errors = validate_frontend_fidelity_trace(trace, spec_text=spec)

        self.assertFalse(
            any("frontend prototype fidelity" in item for item in errors)
        )

    def test_preservation_only_spec_can_explicitly_decline_frontend_work(self) -> None:
        trace = {
            "version": 1,
            "frontend_scope": {
                "requested": False,
                "surfaces": [],
            },
            "requirements": [_requirement()],
        }
        spec = (
            "Do not modify the approved frontend prototype; "
            "this iteration changes backend contracts only."
        )

        errors = validate_frontend_fidelity_trace(trace, spec_text=spec)

        self.assertFalse(
            any("frontend prototype fidelity" in item for item in errors)
        )

    def test_frontend_scope_rejects_surfaces_when_work_is_not_requested(self) -> None:
        trace = {
            "version": 1,
            "frontend_scope": {
                "requested": False,
                "surfaces": [
                    {
                        "id": "home",
                        "name": "Home",
                        "priority": "primary",
                        "requirement_ids": ["REQ-001"],
                    }
                ],
            },
            "requirements": [_requirement()],
        }

        errors = validate_frontend_scope(trace)

        self.assertTrue(
            any("surfaces must be empty when requested=false" in error for error in errors)
        )

    def test_preservation_only_iteration_allows_superseding_frontend_lineage(self) -> None:
        previous_requirement = _requirement(
            id="REQ-009",
            text="Preserve the approved Workbench home prototype.",
            source="specs/previous-iteration.md",
            acceptance_oracles=["The rendered home remains visually unchanged."],
            oracle_type="mixed",
            oracle_strength="semantic",
            notes="frontend_surface: home",
            frontend_surface=True,
        )
        previous = {
            "version": 1,
            "frontend_scope": {"requested": False, "surfaces": []},
            "requirements": [previous_requirement],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["status"] = "superseded"
        current["requirements"][0]["superseded_by"] = ["REQ-020"]
        current["requirements"].append(
            _requirement(
                id="REQ-020",
                text="The Workbench home must preserve the approved prototype fidelity.",
                source="specs/storyboard-contract.md non-goals",
                acceptance_oracles=[
                    "Desktop and mobile screenshots remain visually unchanged."
                ],
                oracle_type="mixed",
                oracle_strength="semantic",
                notes="frontend_surface: home",
                frontend_surface=True,
                supersedes=["REQ-009"],
            )
        )

        errors = validate_frontend_fidelity_trace(
            current,
            spec_text="Do not modify the approved Workbench visual design.",
            previous_trace=previous,
        )

        self.assertFalse(
            any("requested=false forbids introducing" in error for error in errors)
        )

    def test_preservation_only_iteration_rejects_new_frontend_without_lineage(self) -> None:
        previous = {
            "version": 1,
            "frontend_scope": {"requested": False, "surfaces": []},
            "requirements": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"].append(
            _requirement(
                id="REQ-020",
                text="The Workbench home must preserve the approved prototype fidelity.",
                source="specs/storyboard-contract.md non-goals",
                acceptance_oracles=[
                    "Desktop and mobile screenshots remain visually unchanged."
                ],
                oracle_type="mixed",
                oracle_strength="semantic",
                notes="frontend_surface: home",
                frontend_surface=True,
            )
        )

        errors = validate_frontend_fidelity_trace(
            current,
            spec_text="Do not modify the approved Workbench visual design.",
            previous_trace=previous,
        )

        self.assertTrue(
            any(
                "requested=false forbids introducing" in error
                and "REQ-020" in error
                for error in errors
            )
        )

    def test_image_asset_snapshot_three_view_and_guidance_is_not_frontend_fidelity(self) -> None:
        requirement = _requirement(
            id="REQ-249",
            text=(
                "A canonical asset that observed story_panel_pollution switches to a "
                "low-entropy single-anchor image template."
            ),
            source="asset corrective recovery spec",
            acceptance_oracles=[
                "The canonical image must not become a character sheet or 三视图 layout.",
                (
                    "An empty wardrobe uses one typed snapshot, and the scorer and "
                    "corrective guidance consume that same snapshot."
                ),
            ],
            oracle_type="mixed",
            oracle_strength="semantic",
            evidence_boundary="system_boundary",
        )
        previous = {
            "version": 1,
            "frontend_scope": {"requested": False, "surfaces": []},
            "requirements": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"].append(requirement)

        errors = validate_frontend_fidelity_trace(
            current,
            previous_trace=previous,
        )

        self.assertFalse(requirement_is_frontend_fidelity(requirement))
        self.assertFalse(
            any("requested=false forbids introducing" in error for error in errors)
        )

    def test_legacy_untagged_ui_prototype_requirement_reports_match_fields(self) -> None:
        requirement = _requirement(
            id="REQ-020",
            text="The Workbench UI must match the approved prototype.",
            acceptance_oracles=[
                "The rendered page matches the prototype screenshots."
            ],
            notes="legacy trace without frontend metadata",
        )
        previous = {
            "version": 1,
            "frontend_scope": {"requested": False, "surfaces": []},
            "requirements": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"].append(requirement)

        errors = validate_frontend_fidelity_trace(
            current,
            previous_trace=previous,
        )

        self.assertTrue(requirement_is_frontend_fidelity(requirement))
        issue = next(
            error
            for error in errors
            if "requested=false forbids introducing" in error
        )
        self.assertIn("Classification signals:", issue)
        self.assertIn("text:ui", issue)
        self.assertIn("text:prototype", issue)

    def test_contextual_dom_snapshot_remains_a_legacy_fidelity_signal(self) -> None:
        requirement = _requirement(
            text="The UI page must match the approved DOM snapshot.",
        )

        self.assertTrue(requirement_is_frontend_fidelity(requirement))

    def test_preservation_only_iteration_allows_unchanged_historical_requirement(self) -> None:
        requirement = _requirement(
            id="REQ-009",
            text="Preserve the approved Workbench home prototype.",
            source="specs/previous-iteration.md",
            acceptance_oracles=["The rendered home remains visually unchanged."],
            oracle_type="mixed",
            oracle_strength="semantic",
            notes="frontend_surface: home",
            frontend_surface=True,
        )
        previous = {
            "version": 1,
            "frontend_scope": {"requested": False, "surfaces": []},
            "requirements": [requirement],
        }
        current = json.loads(json.dumps(previous))

        errors = validate_frontend_fidelity_trace(
            current,
            previous_trace=previous,
        )

        self.assertFalse(
            any("requested=false forbids" in error for error in errors)
        )

    def test_preservation_only_iteration_rejects_changed_historical_requirement(self) -> None:
        requirement = _requirement(
            id="REQ-009",
            text="Preserve the approved Workbench home prototype.",
            source="specs/previous-iteration.md",
            acceptance_oracles=["The rendered home remains visually unchanged."],
            oracle_type="mixed",
            oracle_strength="semantic",
            notes="frontend_surface: home",
            frontend_surface=True,
        )
        previous = {
            "version": 1,
            "frontend_scope": {"requested": False, "surfaces": []},
            "requirements": [requirement],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["acceptance_oracles"] = [
            "Regenerate desktop and mobile fidelity evidence."
        ]

        errors = validate_frontend_fidelity_trace(
            current,
            previous_trace=previous,
        )

        self.assertTrue(
            any(
                "requested=false forbids changing" in error
                and "REQ-009" in error
                for error in errors
            )
        )

    def test_preservation_only_frontend_requirement_cannot_create_rebinding_task(self) -> None:
        requirement = _requirement(
            id="REQ-020",
            text="The Workbench home must preserve the approved prototype fidelity.",
            source="specs/storyboard-contract.md non-goals",
            acceptance_oracles=[
                "Desktop and mobile screenshots remain visually unchanged."
            ],
            oracle_type="mixed",
            oracle_strength="semantic",
            notes="frontend_surface: home",
            frontend_surface=True,
        )
        trace = {
            "version": 1,
            "frontend_scope": {"requested": False, "surfaces": []},
            "requirements": [requirement],
        }
        task_plan = {
            "tasks": [
                {
                    "task_id": "task-377",
                    "title": "Rebind Workbench home prototype fidelity regression",
                    "status": "pending",
                    "requirement_ids": ["REQ-020"],
                }
            ]
        }

        coverage_errors = validate_task_requirement_coverage({"tasks": []}, trace)
        plan_errors = validate_frontend_fidelity_task_plan(task_plan, trace)

        self.assertFalse(
            any("mandatory active requirements" in error for error in coverage_errors)
        )
        self.assertTrue(
            any(
                "task task-377 binds preservation-only frontend requirements" in error
                for error in plan_errors
            )
        )

    def test_requirements_audit_matches_preservation_only_plan_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            requirement = _requirement(
                id="REQ-020",
                text="Preserve the approved Workbench home prototype.",
                source="specs/backend-contract.md non-goals",
                acceptance_oracles=[
                    "Desktop and mobile screenshots remain visually unchanged."
                ],
                oracle_type="mixed",
                oracle_strength="semantic",
                notes="frontend_surface: home",
                frontend_surface=True,
            )
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "frontend_scope": {"requested": False, "surfaces": []},
                    "requirements": [requirement],
                },
            )
            write_json(
                task_plan_path(project_root),
                {"oracle_proof_schema_version": 2, "tasks": []},
            )

            result = run_requirements_audit(project_root, [])

            issue = {item["requirement_id"]: item for item in result["issues"]}[
                "REQ-020"
            ]
            self.assertTrue(result["ok"], result["report"])
            self.assertEqual(issue["result"], "pass")
            self.assertFalse(
                any(
                    blocker.get("kind") in {"task_coverage", "oracle_proof_missing"}
                    for blocker in issue["blockers"]
                )
            )

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

    def test_frontend_visual_evidence_ignores_historical_id_collision(self) -> None:
        requirement = _requirement(
            id="REQ-027",
            text="Preserve the approved home page visual contract.",
            acceptance_oracles=["Desktop and mobile screenshots remain unchanged."],
            oracle_type="mixed",
            oracle_strength="semantic",
            notes="frontend_surface: Home",
            frontend_surface=True,
        )
        trace, _ = stamp_requirement_contract_hashes(
            {
                "version": 1,
                "frontend_surfaces": [
                    {
                        "name": "Home",
                        "route": "/",
                        "prototype_refs": ["specs/frontend_prototype/home.html"],
                        "viewports": ["desktop", "mobile"],
                        "requirement_ids": ["REQ-027"],
                    }
                ],
                "requirements": [requirement],
            }
        )
        historical_task = {
            "task_id": "task-old",
            "status": "done",
            "requirement_ids": ["REQ-027"],
            "requirement_proofs": [
                {
                    "requirement_id": "REQ-027",
                    "proof_type": "integration_test",
                    "evidence_refs": ["tests/test_provider_payload.py"],
                    "status": "verified",
                }
            ],
        }

        errors = validate_frontend_fidelity_task_plan(
            {"tasks": []},
            trace,
            historical_tasks=[historical_task],
        )

        self.assertFalse(
            any("page-level visual evidence" in error for error in errors)
        )

        historical_task["requirement_proofs"][0][
            "requirement_contract_sha256"
        ] = trace["requirements"][0]["contract_sha256"]
        matching_errors = validate_frontend_fidelity_task_plan(
            {"tasks": []},
            trace,
            historical_tasks=[historical_task],
        )
        self.assertTrue(
            any("page-level visual evidence" in error for error in matching_errors)
        )

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

    def test_plan_status_normalization_restores_only_trusted_done_identities(self) -> None:
        trusted_task = {
            "task_id": "repair-task-001",
            "title": "Canonical evidence repair",
            "description": "The authoritative run-state task already completed.",
            "acceptance": ["The regression remains verified."],
            "requirement_ids": ["REQ-001"],
            "requirement_proofs": [],
            "status": "done",
            "commit_message": "",
            "task_origin": "evidence_repair",
            "verify_history": [{"attempt": 1, "decision": "pass"}],
        }
        candidate_copy = dict(trusted_task)
        candidate_copy["title"] = "Planner-modified historical copy"
        candidate_copy["verify_history"] = []
        candidate_copy["status"] = "pending"
        untrusted_task = {
            "task_id": "repair-task-002",
            "title": "Untrusted generated completion",
            "description": "The planner invented this completed state.",
            "acceptance": ["The regression remains verified."],
            "requirement_ids": ["REQ-001"],
            "requirement_proofs": [],
            "status": "done",
            "commit_message": "",
            "task_origin": "evidence_repair",
        }
        plan = {
            "oracle_proof_schema_version": 1,
            "tasks": [candidate_copy, untrusted_task],
        }

        repaired, updates = normalize_generated_task_plan_statuses(
            plan,
            trusted_done_tasks=[trusted_task],
        )

        self.assertEqual(repaired["tasks"][0], trusted_task)
        self.assertEqual(repaired["tasks"][1]["status"], "pending")
        self.assertEqual(plan["tasks"][0]["title"], "Planner-modified historical copy")
        self.assertEqual(plan["tasks"][0]["status"], "pending")
        self.assertTrue(
            any("repair-task-001" in item and "authoritative done payload" in item for item in updates),
            updates,
        )
        self.assertTrue(
            any("repair-task-002" in item and "to 'pending'" in item for item in updates),
            updates,
        )

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
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            for kind in ("oracle_proof_missing", "oracle_proof_invalid"):
                with self.subTest(kind=kind):
                    route, hard_failure = orchestrator._audit_issue_route(
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

    def test_requirements_audit_builds_forbidden_pattern_corpus_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        _requirement(id="REQ-001", forbidden_patterns=["legacy_one"]),
                        _requirement(id="REQ-002", forbidden_patterns=["legacy_two"]),
                    ],
                },
            )
            write_text(
                project_root / "src" / "backend.py",
                "legacy_one = True\nlegacy_two = True\n",
            )

            with patch.object(
                requirements_module,
                "_forbidden_pattern_scan_files",
                wraps=requirements_module._forbidden_pattern_scan_files,
            ) as scan_files:
                result = run_requirements_audit(project_root, [])

            self.assertFalse(result["ok"])
            self.assertEqual(scan_files.call_count, 1)
            issues = {item["requirement_id"]: item for item in result["issues"]}
            for req_id in ("REQ-001", "REQ-002"):
                forbidden = [
                    item
                    for item in issues[req_id]["blockers"]
                    if item.get("kind") == "forbidden_pattern"
                ]
                self.assertEqual(forbidden[0]["path"], "src/backend.py")

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
                reference_lines = ["# Provider"]
                for heading in PROVIDER_REFERENCE_V2_HEADINGS:
                    reference_lines.extend(
                        ["", f"## {heading}", "", "Not applicable: refreshed fixture."]
                    )
                write_text(
                    self.project_root / self.reference,
                    "\n".join(reference_lines) + "\n",
                )
                write_json(
                    provider_references_lock_path(self.project_root),
                    {
                        "version": 1,
                        "references": {
                            "provider": {
                                "path": self.reference,
                                "status": "verified",
                                "contract_version": PROVIDER_REFERENCE_CONTRACT_VERSION,
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
