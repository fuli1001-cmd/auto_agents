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
_PROVENANCE_LABEL_PATTERN = re.compile(r"\*\*\[([^\]]+)\]\*\*")
_MALFORMED_PROVENANCE_LABEL_PATTERN = re.compile(
    r"\*\*(?:Provenance|Source)\s*:\s*\[([^\]]+)\]\*\*",
    re.IGNORECASE,
)
_NUMBERED_RULE_PATTERN = re.compile(r"^\d+[.)]\s+")
_TABLE_SEPARATOR_PATTERN = re.compile(r"^:?-{3,}:?$")

_PROVENANCE_SECTIONS = (
    "Prompt / Content Construction",
    "Safety / Content Policy",
    "Semantic Error Routing",
    "Retry / Recovery Matrix",
)


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
    for heading in _PROVENANCE_SECTIONS:
        section = sections.get(_normalize_heading(heading), "")
        if not section or _is_explained_not_applicable(section):
            continue
        if heading == "Retry / Recovery Matrix":
            errors.extend(_validate_recovery_provenance(section))
        else:
            errors.extend(_validate_rule_provenance(heading, section))
    return errors


def _provenance_kinds(text: str) -> set[str]:
    kinds: set[str] = set()
    for raw_label in _PROVENANCE_LABEL_PATTERN.findall(text):
        label = raw_label.strip().lower()
        if "official" in label or "source=official" in label:
            kinds.add("official")
        if "observed" in label or "source=observed" in label:
            kinds.add("observed")
        if (
            "assumption" in label
            or "unknown" in label
            or "source=assumption" in label
        ):
            kinds.add("assumption")
        if "policy" in label or "source=local-policy" in label:
            kinds.add("local-policy")
    return kinds


def _malformed_provenance_labels(text: str) -> list[str]:
    return [
        label.strip()
        for label in _MALFORMED_PROVENANCE_LABEL_PATTERN.findall(text)
        if label.strip()
    ]


def _malformed_provenance_error(
    location: str,
    labels: Iterable[str],
) -> str:
    label = next((str(item).strip() for item in labels if str(item).strip()), "label")
    return (
        f"{location} uses unsupported provenance syntax "
        f"'**Provenance: [{label}]**'; use canonical '**[{label}]**'"
    )


def _is_explained_not_applicable(section: str) -> bool:
    text = " ".join(section.split()).strip()
    match = re.match(r"(?i)^not applicable\s*[:\u2014-]\s*(.+)$", text)
    return bool(match and len(match.group(1).strip()) >= 8)


def _is_table_separator(line: str) -> bool:
    if not line.startswith("|"):
        return False
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return bool(cells) and all(_TABLE_SEPARATOR_PATTERN.fullmatch(cell) for cell in cells)


def _validate_rule_provenance(heading: str, section: str) -> list[str]:
    errors: list[str] = []
    for line_number, raw_line in enumerate(section.splitlines(), start=1):
        line = raw_line.strip()
        if not line or _is_table_separator(line):
            continue
        malformed_labels = _malformed_provenance_labels(line)
        if malformed_labels:
            errors.append(
                _malformed_provenance_error(
                    f"{heading} line {line_number}",
                    malformed_labels,
                )
            )
            continue
        if line.startswith("|"):
            # Tables in these sections carry provenance per data row. Header rows
            # are descriptive and do not need a source label.
            if "official" in line.lower() or "provenance" in line.lower() or "source" in line.lower():
                continue
        is_rule = line.startswith("- ") or bool(_NUMBERED_RULE_PATTERN.match(line))
        is_prose = not line.startswith("|")
        if (is_rule or is_prose) and not _provenance_kinds(line):
            errors.append(
                f"{heading} line {line_number} lacks direct provenance"
            )
    return errors


def _validate_recovery_provenance(section: str) -> list[str]:
    errors: list[str] = []
    table_lines = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if not table_lines:
        return ["Retry / Recovery Matrix must contain a sourced table"]

    header_cells = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    provenance_indexes = [
        index
        for index, cell in enumerate(header_cells)
        if cell.lower() in {"source", "provenance"}
    ]
    if not provenance_indexes:
        errors.append("Retry / Recovery Matrix must include a Source or Provenance column")
    else:
        provenance_index = provenance_indexes[0]
        for row_number, row in enumerate(table_lines[2:], start=3):
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            if len(cells) != len(header_cells):
                errors.append(
                    f"Retry / Recovery Matrix row {row_number} has an inconsistent column count"
                )
                continue
            provenance_cell = cells[provenance_index]
            malformed_labels = _malformed_provenance_labels(provenance_cell)
            if malformed_labels:
                errors.append(
                    _malformed_provenance_error(
                        f"Retry / Recovery Matrix row {row_number}",
                        malformed_labels,
                    )
                )
            elif not _provenance_kinds(provenance_cell):
                errors.append(
                    f"Retry / Recovery Matrix row {row_number} lacks provenance"
                )

    for line_number, raw_line in enumerate(section.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("|"):
            continue
        malformed_labels = _malformed_provenance_labels(line)
        if malformed_labels:
            errors.append(
                _malformed_provenance_error(
                    f"Retry / Recovery Matrix narrative line {line_number}",
                    malformed_labels,
                )
            )
        elif not _provenance_kinds(line):
            errors.append(
                f"Retry / Recovery Matrix narrative line {line_number} lacks direct provenance"
            )
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
            "Provider references and their lock are provider_research-owned read-only inputs during implementation. Plan implementation tasks to consume and test them, never to refresh or rewrite them.",
        ],
        "provider_research": [
            f"Every created or refreshed provider reference must use contract_version={PROVIDER_REFERENCE_CONTRACT_VERSION} in its lock entry.",
            "Every created or refreshed reference must contain non-empty H2 sections named: "
            + ", ".join(PROVIDER_REFERENCE_V2_HEADINGS)
            + ".",
            "If prompt/content construction or content policy is not applicable, state 'Not applicable' with a concrete reason; do not omit the section.",
            "The Semantic Error Routing and Retry / Recovery Matrix sections must distinguish provider body semantics from HTTP fallback and record unchanged versus transformed retry behavior. Unknown behavior must stay explicit and cannot be marked verified by invention.",
            "Every normative paragraph, bullet, and numbered rule in the four provider content-safety sections must carry direct official, observed, assumption/unknown, or local-policy provenance. Retry / Recovery Matrix must include a Source or Provenance column with a sourced value on every data row.",
            "Use canonical bold-bracket provenance labels such as **[OpenAI official]**, **[APIYI official]**, **[SDGP observed]**, **[SDGP compatibility assumption]**, and **[SDGP policy]**. The wrapper form **Provenance: [OpenAI official]** (or **Source: [OpenAI official]**) is invalid and must not be used.",
        ],
        "implement": [
            "For provider integrations, implement and test the provider-reference safety/error matrix at the serialized request/response boundary. Preserve stable semantic categories and bounded redacted evidence.",
            "Do not satisfy a safety requirement by expanding a long list of policy-sensitive negative phrases into every outbound prompt or by applying a human-specific fallback template to animals, objects, or other incompatible subject types.",
            "Treat .auto-agents provider references as read-only provider_research-owned inputs. If a bound reference is incomplete, report it for owner-stage recovery instead of weakening tests or editing the reference during implementation.",
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
