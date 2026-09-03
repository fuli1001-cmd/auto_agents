from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from .session_health import (
    SESSION_PROGRESS_SCHEMA_VERSION,
    build_session_progress_identity,
    session_progress_disagrees,
)


POSTCONDITION_CLAIM_SCHEMA_VERSION = 1
POSTCONDITION_RECEIPT_SCHEMA_VERSION = 1
SESSION_HEALTH_BOUNDARY_VERIFIER = "session_health_boundary"
LEGACY_SESSION_HEALTH_CATEGORIES = frozenset(
    {
        "session_health_projection_and_resume_boundary_mismatch",
        "session_health_projection_mismatch",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PostconditionClaim:
    verifier_id: str
    verifier_version: int
    input_digest: str
    expected: Mapping[str, object]
    parameters: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = POSTCONDITION_CLAIM_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PostconditionClaim":
        verifier_id = str(payload.get("verifier_id", "")).strip()
        verifier_version = int(payload.get("verifier_version", 0) or 0)
        input_digest = str(payload.get("input_digest", "")).strip()
        expected = payload.get("expected", {})
        parameters = payload.get("parameters", {})
        if (
            int(payload.get("schema_version", 0) or 0)
            != POSTCONDITION_CLAIM_SCHEMA_VERSION
            or not verifier_id
            or verifier_version < 1
            or re.fullmatch(r"sha256:[0-9a-f]{64}", input_digest) is None
            or not isinstance(expected, Mapping)
            or not isinstance(parameters, Mapping)
            or any(
                str(key).strip().lower()
                in {"command", "commands", "argv", "shell", "script"}
                for key in parameters
            )
        ):
            raise ValueError("invalid versioned postcondition claim")
        return cls(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            input_digest=input_digest,
            expected=dict(expected),
            parameters=dict(parameters),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "expected": dict(self.expected),
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class PostconditionReceipt:
    claim_digest: str
    engine_revision: str
    result: str
    observed_digest: str
    verifier_id: str
    verifier_version: int
    reason: str = ""
    checked_at: str = field(default_factory=_utc_now)
    schema_version: int = POSTCONDITION_RECEIPT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


Verifier = Callable[[PostconditionClaim], tuple[bool, Mapping[str, object], str]]


class PostconditionVerifierRegistry:
    def __init__(self) -> None:
        self._verifiers: dict[tuple[str, int], Verifier] = {
            (SESSION_HEALTH_BOUNDARY_VERIFIER, 1): (
                self._verify_session_health_boundary_v1
            ),
        }

    def verify(
        self,
        claim: PostconditionClaim,
        *,
        engine_revision: str,
    ) -> PostconditionReceipt:
        verifier = self._verifiers.get(
            (claim.verifier_id, claim.verifier_version)
        )
        if verifier is None:
            return PostconditionReceipt(
                claim_digest=claim.digest,
                engine_revision=engine_revision,
                result="unsupported",
                observed_digest=_digest({}),
                verifier_id=claim.verifier_id,
                verifier_version=claim.verifier_version,
                reason="postcondition verifier is not registered",
            )
        try:
            passed, observed, reason = verifier(claim)
        except Exception as error:
            passed = False
            observed = {"error_type": type(error).__name__}
            reason = f"postcondition verifier failed: {error}"
        expected_result = str(claim.expected.get("result", "pass")).strip()
        result = "pass" if passed and expected_result == "pass" else "fail"
        return PostconditionReceipt(
            claim_digest=claim.digest,
            engine_revision=engine_revision,
            result=result,
            observed_digest=_digest(observed),
            verifier_id=claim.verifier_id,
            verifier_version=claim.verifier_version,
            reason=reason,
        )

    @staticmethod
    def _verify_session_health_boundary_v1(
        claim: PostconditionClaim,
    ) -> tuple[bool, Mapping[str, object], str]:
        if int(claim.parameters.get("progress_schema_version", 0) or 0) != int(
            SESSION_PROGRESS_SCHEMA_VERSION
        ):
            return False, {"schema_match": False}, "progress schema changed"
        payload = {
            "goal": "verify health boundary",
            "conversation": [{"role": "user", "content": "goal"}],
            "execution_log": [],
            "current_attempt": 1,
            "resolution": "",
            "status": "executing",
            "last_diff_hash": "",
            "last_verify_sig": "",
            "workflow_id": "wf-verifier",
            "active_handoff_id": "hf-verifier",
            "return_phase": "",
        }
        identity = build_session_progress_identity(
            payload,
            run_token="verifier-token",
        )
        matching = dict(identity)
        tampered = {**identity, "progress_digest": "tampered"}
        stale_token = {**tampered, "run_token": "stale-token"}
        stale_schema = {
            **tampered,
            "progress_schema_version": SESSION_PROGRESS_SCHEMA_VERSION + 1,
        }
        stale_state = {**tampered, "state_digest": "stale-state"}
        observed = {
            "matching_disagrees": session_progress_disagrees(matching, identity),
            "tampered_disagrees": session_progress_disagrees(tampered, identity),
            "stale_token_disagrees": session_progress_disagrees(
                stale_token, identity
            ),
            "stale_schema_disagrees": session_progress_disagrees(
                stale_schema, identity
            ),
            "stale_state_disagrees": session_progress_disagrees(
                stale_state, identity
            ),
            "projection_fields": sorted(dict(identity["progress"])),
        }
        passed = observed == {
            "matching_disagrees": False,
            "tampered_disagrees": True,
            "stale_token_disagrees": False,
            "stale_schema_disagrees": False,
            "stale_state_disagrees": False,
            "projection_fields": sorted(
                {
                    "goal_set",
                    "conversation_entries",
                    "execution_entries",
                    "attempt",
                    "resolution_set",
                    "status",
                    "diff",
                    "verify",
                    "workflow_id",
                    "active_handoff_id",
                    "return_phase",
                }
            ),
        }
        return passed, observed, "" if passed else "health boundary contract failed"


def legacy_postcondition_claims(
    blocker: Mapping[str, object],
) -> list[PostconditionClaim]:
    """Project a narrow set of known v1 blockers without parsing prose."""

    if (
        str(blocker.get("owner", "")) != "auto_agents"
        or str(blocker.get("category", ""))
        not in LEGACY_SESSION_HEALTH_CATEGORIES
    ):
        return []
    failure = blocker.get("self_repair_failure", {})
    approval = blocker.get("self_repair_approval", {})
    diagnosis = blocker.get("root_cause_diagnosis", {})
    if (
        not isinstance(failure, Mapping)
        or not isinstance(approval, Mapping)
        or not isinstance(diagnosis, Mapping)
    ):
        return []
    final = diagnosis.get("final", {})
    if not isinstance(final, Mapping):
        return []
    expected = [
        " ".join(str(item).split())
        for item in final.get("expected_postconditions", []) or []
        if " ".join(str(item).split())
    ]
    if (
        not expected
        or not (
            str(failure.get("verification", "")).strip()
            or str(approval.get("verification_digest", "")).strip()
        )
        or not str(blocker.get("self_repair_commit", "")).strip()
    ):
        return []
    material = {
        "category": str(blocker.get("category", "")),
        "fingerprint": str(blocker.get("fingerprint", "")),
        "diagnosis_id": str(diagnosis.get("diagnosis_id", "")),
        "expected_postconditions": expected,
        "self_repair_commit": str(blocker.get("self_repair_commit", "")),
    }
    return [
        PostconditionClaim(
            verifier_id=SESSION_HEALTH_BOUNDARY_VERIFIER,
            verifier_version=1,
            input_digest=_digest(material),
            expected={"result": "pass"},
            parameters={
                "progress_schema_version": SESSION_PROGRESS_SCHEMA_VERSION,
                "legacy_projection": "legacy_session_health_boundary_projection_v1",
            },
        )
    ]


def postcondition_claims_for_blocker(
    blocker: Mapping[str, object],
) -> list[PostconditionClaim]:
    raw = blocker.get("postcondition_claims", [])
    if isinstance(raw, list) and raw:
        claims: list[PostconditionClaim] = []
        try:
            for item in raw:
                if not isinstance(item, Mapping):
                    return []
                claims.append(PostconditionClaim.from_dict(item))
        except (TypeError, ValueError):
            return []
        if str(blocker.get("category", "")) in LEGACY_SESSION_HEALTH_CATEGORIES:
            projected = legacy_postcondition_claims(blocker)
            if not projected or [
                (
                    item.verifier_id,
                    item.verifier_version,
                    item.input_digest,
                )
                for item in claims
            ] != [
                (
                    item.verifier_id,
                    item.verifier_version,
                    item.input_digest,
                )
                for item in projected
            ]:
                return []
        return claims
    return legacy_postcondition_claims(blocker)


def verify_blocker_postconditions(
    blocker: Mapping[str, object],
    *,
    engine_revision: str,
) -> tuple[list[PostconditionClaim], list[PostconditionReceipt]]:
    claims = postcondition_claims_for_blocker(blocker)
    registry = PostconditionVerifierRegistry()
    receipts = [
        registry.verify(claim, engine_revision=engine_revision)
        for claim in claims
    ]
    return claims, receipts


def postcondition_set_digest(claims: list[PostconditionClaim]) -> str:
    return _digest([claim.to_dict() for claim in claims])
