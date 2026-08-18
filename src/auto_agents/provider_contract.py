from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


PROVIDER_REFERENCE_CONTRACT_VERSION = 2

PROVIDER_REFERENCE_V2_HEADINGS = (
    "Status",
    "Retrieved at",
    "Official sources",
    "Authentication",
    "Request",
    "Response",
    "Prompt / Content Construction",
    "Safety / Content Policy",
    "Semantic Error Routing",
    "Retry / Recovery Matrix",
    "Contract Test Requirements",
    "Unknowns / Ambiguities",
)

_HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.MULTILINE)


def provider_reference_lock_entry(
    lock_payload: object,
    reference_path: str,
) -> dict[str, Any] | None:
    if not isinstance(lock_payload, dict):
        return None
    references = lock_payload.get("references")
    if not isinstance(references, dict):
        return None
    for value in references.values():
        if not isinstance(value, dict):
            continue
        if str(value.get("path", "")).strip() == reference_path:
            return value
    return None


def provider_reference_contract_version(lock_entry: object) -> int:
    if not isinstance(lock_entry, dict):
        return 0
    try:
        return int(lock_entry.get("contract_version"))
    except (TypeError, ValueError):
        return 0


def validate_provider_reference_v2(
    reference_path: Path,
    lock_entry: object,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(lock_entry, dict):
        return ["missing provider-reference lock entry"]

    contract_version = provider_reference_contract_version(lock_entry)
    if contract_version < PROVIDER_REFERENCE_CONTRACT_VERSION:
        errors.append(
            "lock entry contract_version must be at least "
            f"{PROVIDER_REFERENCE_CONTRACT_VERSION}"
        )

    if not reference_path.exists():
        errors.append("provider reference file is missing")
        return errors

    text = reference_path.read_text(encoding="utf-8")
    matches = list(_HEADING_PATTERN.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = _normalize_heading(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[name] = text[match.end() : end].strip()

    for heading in PROVIDER_REFERENCE_V2_HEADINGS:
        normalized = _normalize_heading(heading)
        if normalized not in sections:
            errors.append(f"missing required section: {heading}")
        elif not sections[normalized]:
            errors.append(f"required section is empty: {heading}")
    return errors


def provider_policy_prompt_lines(stage: str) -> list[str]:
    normalized = str(stage).strip().lower()
    shared = [
        "PROVIDER CONTENT-SAFETY CONTRACT: when an external provider receives user-derived or model-derived content, treat content-policy handling as a first-class provider boundary rather than a generic HTTP error.",
        "Classify a provider response from its documented stable body code/status/message before falling back to the coarse HTTP status; do not collapse a recognizable safety/refusal outcome into malformed-request or transport failure semantics.",
        "Never blindly retry an unchanged request after a safety/refusal outcome. A bounded transformed retry is allowed only when an explicit product/provider contract defines a semantics-preserving safe transformation; otherwise fail closed with an actionable, redacted result.",
        "Keep provider-facing content positive-first, concise, deduplicated, and compatible with the typed subject. Keep policy-sensitive forbidden concepts as internal structured policy where possible instead of repeatedly spelling them out in outbound prompts.",
    ]
    stage_specific = {
        "clarify": [
            "For requirements that send user/model content to a generative provider, add acceptance oracles for semantic safety classification, typed outbound content construction, unchanged-retry prohibition, and system-boundary proof of the chosen recovery policy.",
            "Mark the requirement external_docs_required and bind a local provider reference whenever provider-specific safety signals or retry behavior affect the contract.",
        ],
        "design": [
            "Architecture must separate authentication/permission, malformed request, quota/rate limit, transport availability, provider safety/refusal, and valid-but-unacceptable content outcomes, including their distinct retry budgets and public projections.",
            "For generated prompts/content, define a typed compilation boundary that can select subject-appropriate templates and test the final serialized outbound request.",
        ],
        "plan": [
            "Create owned task acceptance and executable system-boundary proofs for provider safety signals returned under non-canonical HTTP statuses, positive-first typed prompt compilation, deduplication, and bounded recovery without unchanged safety retries.",
            "Do not treat configuration text, internal policy metadata, or a fake success response as proof of the final outbound content or safety-error routing.",
        ],
        "provider_research": [
            f"Every created or refreshed provider reference must use contract_version={PROVIDER_REFERENCE_CONTRACT_VERSION} in its lock entry.",
            "Every created or refreshed reference must contain non-empty H2 sections named: "
            + ", ".join(PROVIDER_REFERENCE_V2_HEADINGS)
            + ".",
            "If prompt/content construction or content policy is not applicable, state 'Not applicable' with a concrete reason; do not omit the section.",
            "The Semantic Error Routing and Retry / Recovery Matrix sections must distinguish provider body semantics from HTTP fallback and record unchanged versus transformed retry behavior. Unknown behavior must stay explicit and cannot be marked verified by invention.",
        ],
        "implement": [
            "For provider integrations, implement and test the provider-reference safety/error matrix at the serialized request/response boundary. Preserve stable semantic categories and bounded redacted evidence.",
            "Do not satisfy a safety requirement by expanding a long list of policy-sensitive negative phrases into every outbound prompt or by applying a human-specific fallback template to animals, objects, or other incompatible subject types.",
        ],
        "review": [
            "Fail the review when a provider integration classifies only by HTTP status despite a recognizable body-level safety/refusal signal, retries an unchanged safety-blocked request, or lacks the provider-reference v2 system-boundary tests owned by the task.",
            "Also fail when outbound generated content repeats policy-sensitive forbidden phrases unnecessarily or applies a subject-incompatible fallback template in violation of the task/provider contract.",
        ],
        "fix": [
            "When the bug crosses an external generative-provider boundary, inspect the bound provider reference and preserve distinct safety/refusal, malformed-request, quota, and transient failure semantics; add a focused serialized-boundary regression test.",
        ],
        "collab": [
            "When debugging an external generative-provider failure, inspect redacted body-level semantics and the final outbound content before changing retryability; do not repeatedly call a paid provider with an unchanged safety-blocked request.",
        ],
    }
    return [*shared, *stage_specific.get(normalized, [])]


def format_provider_reference_v2_errors(
    reference_path: str,
    errors: Iterable[str],
) -> list[str]:
    return [f"{reference_path}: {error}" for error in errors]


def _normalize_heading(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())
