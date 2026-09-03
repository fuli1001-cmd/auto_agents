import pytest

from auto_agents.postconditions import (
    PostconditionClaim,
    PostconditionVerifierRegistry,
    legacy_postcondition_claims,
    postcondition_claims_for_blocker,
    postcondition_set_digest,
    verify_blocker_postconditions,
)
from auto_agents.repair_cases import REPAIR_CASE_SCHEMA_VERSION, RepairCase
from auto_agents.session_health import SESSION_PROGRESS_SCHEMA_VERSION


def _legacy_health_blocker() -> dict:
    return {
        "owner": "auto_agents",
        "category": "session_health_projection_and_resume_boundary_mismatch",
        "fingerprint": "health-root",
        "self_repair_commit": "repair-commit",
        "self_repair_failure": {"verification": "focused tests passed"},
        "root_cause_diagnosis": {
            "diagnosis_id": "diagnosis-1",
            "final": {
                "expected_postconditions": [
                    "publisher and auditor use the same projection",
                    "stale boundaries do not report disagreement",
                ]
            },
        },
    }


def test_known_legacy_blocker_projects_to_versioned_claim_and_passes() -> None:
    blocker = _legacy_health_blocker()

    claims, receipts = verify_blocker_postconditions(
        blocker,
        engine_revision="engine-2",
    )

    assert len(claims) == 1
    assert claims[0].verifier_id == "session_health_boundary"
    assert claims[0].parameters["progress_schema_version"] == (
        SESSION_PROGRESS_SCHEMA_VERSION
    )
    assert len(receipts) == 1
    assert receipts[0].result == "pass"
    assert receipts[0].engine_revision == "engine-2"
    assert postcondition_set_digest(claims).startswith("sha256:")


def test_unknown_legacy_category_is_not_inferred_from_free_text() -> None:
    blocker = _legacy_health_blocker()
    blocker["category"] = "other_engine_problem"
    blocker["root_cause_diagnosis"]["final"]["expected_postconditions"] = [
        "session health projection should match"
    ]

    assert legacy_postcondition_claims(blocker) == []


def test_stored_legacy_claim_is_rejected_when_source_evidence_changes() -> None:
    blocker = _legacy_health_blocker()
    blocker["postcondition_claims"] = [
        claim.to_dict() for claim in legacy_postcondition_claims(blocker)
    ]
    blocker["fingerprint"] = "different-health-root"

    assert postcondition_claims_for_blocker(blocker) == []


def test_unknown_versioned_verifier_fails_closed() -> None:
    claim = PostconditionClaim(
        verifier_id="unknown_verifier",
        verifier_version=1,
        input_digest="sha256:" + "a" * 64,
        expected={"result": "pass"},
    )

    receipt = PostconditionVerifierRegistry().verify(
        claim,
        engine_revision="engine-2",
    )

    assert receipt.result == "unsupported"


def test_claim_parser_rejects_arbitrary_command_contract() -> None:
    with pytest.raises(ValueError):
        PostconditionClaim.from_dict(
            {
                "schema_version": 1,
                "verifier_id": "",
                "verifier_version": 1,
                "input_digest": "echo unsafe",
                "expected": {"result": "pass"},
            }
        )
    with pytest.raises(ValueError):
        PostconditionClaim.from_dict(
            {
                "schema_version": 1,
                "verifier_id": "session_health_boundary",
                "verifier_version": 1,
                "input_digest": "sha256:" + "b" * 64,
                "expected": {"result": "pass"},
                "parameters": {"command": "echo unsafe"},
            }
        )


def test_repair_case_v1_loads_without_rewriting_and_v2_adds_receipts() -> None:
    legacy = RepairCase.from_dict(
        {
            "schema_version": 1,
            "case_id": "case-1",
            "run_id": "run-1",
            "source": "health_watch",
            "kind": "session_health",
            "severity": "blocking",
        }
    )
    legacy.postcondition_claims = [{"verifier_id": "session_health_boundary"}]
    legacy.postcondition_receipts = [{"result": "pass"}]

    payload = legacy.to_dict()

    assert payload["schema_version"] == REPAIR_CASE_SCHEMA_VERSION == 2
    assert payload["postcondition_claims"][0]["verifier_id"] == (
        "session_health_boundary"
    )
    assert payload["postcondition_receipts"][0]["result"] == "pass"
