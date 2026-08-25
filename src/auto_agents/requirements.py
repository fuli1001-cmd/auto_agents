from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import regex as timeout_regex

from .config import (
    archived_task_plans_dir,
    load_project_config,
    provider_references_lock_path,
    requirements_audit_path,
    requirements_trace_path,
    run_state_path,
    task_plan_path,
)
from .frontend_fidelity import preservation_only_frontend_requirement_ids
from .io_utils import read_json, read_text, write_json, write_text
from .models import (
    PERSISTENCE_COMPATIBILITY_POLICIES,
    PERSISTENCE_STORAGE_TRANSITIONS,
    PERSISTENCE_STRATEGIES,
    TaskSpec,
)
from .requirements_audit_cache import RequirementsAuditCache


ALLOWED_REQUIREMENT_STATUSES = {"active", "deferred", "superseded"}
ALLOWED_REQUIREMENT_PRIORITIES = {"mandatory", "optional"}
BLOCKING_REFERENCE_STATUSES = {"missing", "blocked", "needs_user_input", "ambiguous"}
PASSING_REFERENCE_STATUSES = {"verified", "assumption_approved"}
ALLOWED_ORACLE_TYPES = {
    "deterministic_test",
    "integration_test",
    "runtime_evidence",
    "human_review",
    "judge_model",
    "benchmark",
    "mixed",
}
ALLOWED_ORACLE_STRENGTHS = {"proxy", "behavioral", "semantic", "human"}
ALLOWED_EVIDENCE_BOUNDARIES = {"internal_state", "system_boundary", "external_side_effect"}
DEFAULT_ORACLE_TYPE = "mixed"
DEFAULT_ORACLE_STRENGTH = "behavioral"
DEFAULT_EVIDENCE_BOUNDARY = "system_boundary"
ORACLE_PROOF_SCHEMA_VERSION = 1
LATEST_ORACLE_PROOF_SCHEMA_VERSION = 2
CONTRACT_IDENTITY_SCHEMA_VERSION = 1
ORACLE_STRENGTH_ORDER = {"proxy": 0, "behavioral": 1, "semantic": 2, "human": 3}
EVIDENCE_BOUNDARY_ORDER = {"internal_state": 0, "system_boundary": 1, "external_side_effect": 2}
NEGATIVE_CONTRACT_MARKERS = (
    "must not",
    "mustn't",
    "do not",
    "don't",
    "does not",
    "doesn't",
    "should not",
    "shouldn't",
    "cannot",
    "can't",
    "no ",
    "not ",
    "without ",
    "omit",
    "omits",
    "omitted",
    "exclude",
    "excludes",
    "excluded",
    "不",
    "无",
    "未",
    "禁止",
    "不得",
    "不能",
    "不要",
    "不可",
    "不应",
    "不再",
    "不存在",
    "不包含",
    "不携带",
    "不返回",
    "不嵌入",
    "排除",
    "移除",
)
CONTRACT_TOKEN_RE = re.compile(
    r"`([^`]+)`"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:\[\])?(?:[.][A-Za-z_][A-Za-z0-9_]*(?:\[\])?)+"
    r"|/[A-Za-z0-9_{}:/?.=&%+~-]+"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9]+)+"
)


def _normalized_contract_text(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).replace("\r\n", "\n")
    return re.sub(r"\s+", " ", text).strip()


def _normalized_string_list(value: object, *, ordered: bool) -> List[str]:
    if not isinstance(value, list):
        return []
    items = [_normalized_contract_text(item) for item in value if _normalized_contract_text(item)]
    return items if ordered else sorted(set(items))


def requirement_contract_payload(requirement: dict) -> dict:
    """Return the canonical, proof-bearing portion of a requirement record.

    Status and supersession links are lifecycle metadata and notes are explicitly
    non-normative.  Everything that can change audit scope or proof validity is
    included in the fingerprint.
    """
    return {
        "id": _normalized_contract_text(requirement.get("id")),
        "text": _normalized_contract_text(requirement.get("text")),
        "source": _normalized_contract_text(requirement.get("source")),
        "priority": _normalized_contract_text(requirement.get("priority", "mandatory")),
        "acceptance_oracles": _normalized_string_list(
            requirement.get("acceptance_oracles"), ordered=True
        ),
        "oracle_type": _normalized_contract_text(requirement.get("oracle_type")),
        "oracle_strength": _normalized_contract_text(requirement.get("oracle_strength")),
        "evidence_boundary": _normalized_contract_text(requirement.get("evidence_boundary")),
        "forbidden_proxy_oracles": _normalized_string_list(
            requirement.get("forbidden_proxy_oracles"), ordered=False
        ),
        "forbidden_patterns": sorted(
            str(item).replace("\r\n", "\n").strip()
            for item in requirement.get("forbidden_patterns", [])
            if isinstance(item, str) and item.strip()
        ),
        "external_docs_required": bool(requirement.get("external_docs_required", False)),
        "provider_references": sorted(provider_reference_paths(requirement)),
    }


def requirement_contract_sha256(requirement: dict) -> str:
    encoded = json.dumps(
        requirement_contract_payload(requirement),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def stamp_requirement_contract_hashes(payload: object) -> Tuple[object, List[str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("requirements"), list):
        return payload, []
    updated = copy.deepcopy(payload)
    updated["contract_identity_schema_version"] = CONTRACT_IDENTITY_SCHEMA_VERSION
    changes: List[str] = []
    for item in updated["requirements"]:
        if not isinstance(item, dict):
            continue
        expected = requirement_contract_sha256(item)
        if str(item.get("contract_sha256", "")).strip() != expected:
            item["contract_sha256"] = expected
            req_id = str(item.get("id", "")).strip() or "<unknown>"
            changes.append(f"requirement {req_id}: stamped contract_sha256")
        item.setdefault("supersedes", [])
        item.setdefault("superseded_by", [])
    return updated, changes


def stamp_task_plan_contract_hashes(
    plan_payload: object,
    trace_payload: object,
) -> Tuple[object, List[str]]:
    if not isinstance(plan_payload, dict) or not isinstance(trace_payload, dict):
        return plan_payload, []
    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list):
        return plan_payload, []
    # Plans created before proof binding existed are still valid legacy input.
    # Only upgrade a plan when it actually carries proof records; otherwise the
    # v2 schema marker would make every legacy task fail strict proof validation.
    try:
        declared_version = int(plan_payload.get("oracle_proof_schema_version", 0) or 0)
    except (TypeError, ValueError):
        declared_version = 0
    has_proof_records = any(
        isinstance(task, dict)
        and isinstance(task.get("requirement_proofs"), list)
        and bool(task.get("requirement_proofs"))
        for task in tasks
    )
    if declared_version < LATEST_ORACLE_PROOF_SCHEMA_VERSION and not has_proof_records:
        return plan_payload, []
    by_id = {
        str(item.get("id", "")).strip(): item
        for item in requirement_records(trace_payload)
    }
    updated = copy.deepcopy(plan_payload)
    updated["oracle_proof_schema_version"] = LATEST_ORACLE_PROOF_SCHEMA_VERSION
    changes: List[str] = []
    for task in updated.get("tasks", []):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", "")).strip() or "<unknown>"
        for proof in task.get("requirement_proofs", []) or []:
            if not isinstance(proof, dict):
                continue
            req_id = str(proof.get("requirement_id", "")).strip()
            requirement = by_id.get(req_id)
            if requirement is None:
                continue
            expected = requirement_contract_sha256(requirement)
            if str(proof.get("requirement_contract_sha256", "")).strip() != expected:
                proof["requirement_contract_sha256"] = expected
                changes.append(f"task {task_id}: bound {req_id} contract hash")
    return updated, changes


def _normalize_contract_token(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().strip("`'\"“”‘’.,;:，。；：、"))


def _contract_text_contains_token(text: str, token: str) -> bool:
    if not token:
        return True
    normalized_text = _normalize_contract_token(text).lower()
    normalized_token = _normalize_contract_token(token).lower()
    if not normalized_token:
        return True
    return normalized_token in normalized_text


def _split_contract_clauses(text: str) -> List[str]:
    return [
        item.strip()
        for item in re.split(r"[\n。；;]", text)
        if item.strip()
    ]


def _clause_has_negative_contract_marker(clause: str) -> bool:
    lowered = clause.lower()
    return any(marker in lowered for marker in NEGATIVE_CONTRACT_MARKERS)


def _contract_clause_tokens(clause: str) -> List[str]:
    tokens: List[str] = []
    for match in CONTRACT_TOKEN_RE.finditer(clause):
        token = _normalize_contract_token(match.group(1) or match.group(0))
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _negative_contract_atoms(text: str) -> List[dict]:
    atoms: List[dict] = []
    for clause in _split_contract_clauses(text):
        if not _clause_has_negative_contract_marker(clause):
            continue
        tokens = _contract_clause_tokens(clause)
        if not tokens:
            continue
        atoms.append({"clause": clause, "tokens": tokens})
    return atoms


def _task_contract_text_from_payload(task: dict) -> str:
    parts: List[str] = []
    for key in ("title", "description", "scope_boundaries"):
        value = task.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("acceptance", "expected_test_migrations"):
        value = task.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if isinstance(item, str))
    return "\n".join(parts)


def _task_contract_text_from_spec(task: TaskSpec) -> str:
    parts = [task.title, task.description, task.scope_boundaries]
    parts.extend(task.acceptance)
    parts.extend(task.expected_test_migrations)
    return "\n".join(str(item) for item in parts if str(item).strip())


def _oracle_preservation_messages(task_contract_text: str, oracle: str) -> List[str]:
    messages: List[str] = []
    for atom in _negative_contract_atoms(oracle):
        missing_tokens = [
            token
            for token in atom["tokens"]
            if not _contract_text_contains_token(task_contract_text, token)
        ]
        if missing_tokens:
            messages.append(
                "task acceptance weakens a negative requirement clause; "
                f"missing token(s) {', '.join(missing_tokens)} from oracle clause: {atom['clause']}"
            )
    return messages


def preserve_task_plan_negative_oracle_clauses(
    plan_payload: object,
    trace_payload: object,
) -> Tuple[object, List[str]]:
    """Copy missing negative oracle clauses into task acceptance text.

    Plan validation intentionally rejects tasks that weaken a negative requirement
    by dropping concrete field/path/API tokens from the task contract. Planner LLMs
    can still preserve the meaning while changing token formatting, for example
    writing ``fake` / `fixture`` instead of `fake/fixture`. This helper keeps the
    strict validator intact while normalizing generated plans before validation.
    """
    if not isinstance(plan_payload, dict) or not isinstance(trace_payload, dict):
        return plan_payload, []

    normalized_trace = normalize_requirements_trace_payload(trace_payload)
    if not isinstance(normalized_trace, dict):
        return plan_payload, []

    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list):
        return plan_payload, []

    by_req = {str(item.get("id", "")).strip(): item for item in requirement_records(normalized_trace)}
    repaired = copy.deepcopy(plan_payload)
    repaired_tasks = repaired.get("tasks")
    if not isinstance(repaired_tasks, list):
        return plan_payload, []

    updates: List[str] = []
    for task in repaired_tasks:
        if not isinstance(task, dict):
            continue
        proofs = task.get("requirement_proofs")
        acceptance = task.get("acceptance")
        if not isinstance(proofs, list) or not isinstance(acceptance, list):
            continue

        task_contract_text = _task_contract_text_from_payload(task)
        clauses_to_preserve: List[str] = []
        for proof in proofs:
            if not isinstance(proof, dict):
                continue
            req_id = str(proof.get("requirement_id", "")).strip()
            requirement = by_req.get(req_id)
            if requirement is None:
                continue
            matched_oracle = _matched_requirement_oracle(proof, requirement)
            if matched_oracle is None:
                continue
            for atom in _negative_contract_atoms(matched_oracle[1]):
                missing_tokens = [
                    token
                    for token in atom["tokens"]
                    if not _contract_text_contains_token(task_contract_text, token)
                ]
                if missing_tokens and atom["clause"] not in clauses_to_preserve:
                    clauses_to_preserve.append(atom["clause"])

        if not clauses_to_preserve:
            continue

        suffix = "Preserved negative acceptance oracle clause(s): " + "；".join(clauses_to_preserve)
        target_index: Optional[int] = None
        for index in range(len(acceptance) - 1, -1, -1):
            if isinstance(acceptance[index], str) and acceptance[index].strip():
                target_index = index
                break
        if target_index is None:
            acceptance.append(suffix)
        else:
            current = str(acceptance[target_index]).rstrip()
            acceptance[target_index] = f"{current} {suffix}"

        task_id = str(task.get("task_id", "")).strip() or "<unknown>"
        updates.append(
            f"task {task_id}: preserved {len(clauses_to_preserve)} negative oracle clause(s)"
        )

    if not updates:
        return plan_payload, []
    return repaired, updates


def normalize_generated_task_plan_statuses(
    plan_payload: object,
    *,
    trusted_done_tasks: Iterable[dict] = (),
) -> Tuple[object, List[str]]:
    """Normalize planner-generated task statuses before strict plan validation.

    The plan stage writes the next executable plan, not a live run snapshot. LLMs
    can accidentally copy archived or run-state task statuses such as done or
    in_progress back into task_plan.json. In oracle proof schema mode that makes
    validation fail before implementation can begin, because done tasks require
    verified proofs. Restore task IDs supplied by the orchestrator as canonical
    done payloads, keep independently verified done tasks intact, and turn stale
    untrusted runtime statuses back into pending work.
    """
    if not isinstance(plan_payload, dict):
        return plan_payload, []
    try:
        schema_version = int(plan_payload.get("oracle_proof_schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version < ORACLE_PROOF_SCHEMA_VERSION:
        return plan_payload, []

    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list):
        return plan_payload, []

    repaired = copy.deepcopy(plan_payload)
    repaired_tasks = repaired.get("tasks")
    if not isinstance(repaired_tasks, list):
        return plan_payload, []

    trusted_done_by_id: Dict[str, dict] = {}
    for task in trusted_done_tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("status", "")).strip() != "done":
            continue
        task_id = str(task.get("task_id", "")).strip()
        if task_id:
            # Later authoritative inputs take precedence; the orchestrator
            # appends current-run records after archived records.
            trusted_done_by_id[task_id] = task

    updates: List[str] = []
    for index, task in enumerate(repaired_tasks):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", "")).strip()
        trusted_task = trusted_done_by_id.get(task_id)
        if trusted_task is not None:
            if task != trusted_task:
                repaired_tasks[index] = copy.deepcopy(trusted_task)
                updates.append(
                    f"task {task_id}: restored authoritative done payload before "
                    "generated-status normalization"
                )
            continue
        status = str(task.get("status", "pending")).strip()
        if status == "pending":
            continue
        proofs = task.get("requirement_proofs")
        proof_list = proofs if isinstance(proofs, list) else []
        has_only_verified_proofs = bool(proof_list) and all(
            isinstance(proof, dict) and str(proof.get("status", "")).strip() == "verified"
            for proof in proof_list
        )
        if status == "done" and has_only_verified_proofs:
            continue
        task["status"] = "pending"
        display_task_id = task_id or "<unknown>"
        updates.append(
            f"task {display_task_id}: normalized generated status {status!r} to 'pending'"
        )

    if not updates:
        return plan_payload, []
    return repaired, updates


def empty_requirements_trace() -> dict:
    return {"version": 1, "requirements": []}


def empty_provider_references_lock() -> dict:
    return {"version": 1, "references": {}}


def _normalize_requirement_record(item: dict) -> dict:
    normalized = dict(item)

    oracle_type = normalized.get("oracle_type")
    if oracle_type is None or (isinstance(oracle_type, str) and not oracle_type.strip()):
        normalized["oracle_type"] = DEFAULT_ORACLE_TYPE

    oracle_strength = normalized.get("oracle_strength")
    if oracle_strength is None or (isinstance(oracle_strength, str) and not oracle_strength.strip()):
        normalized["oracle_strength"] = DEFAULT_ORACLE_STRENGTH

    evidence_boundary = normalized.get("evidence_boundary")
    if evidence_boundary is None or (
        isinstance(evidence_boundary, str) and not evidence_boundary.strip()
    ):
        normalized["evidence_boundary"] = DEFAULT_EVIDENCE_BOUNDARY

    if "forbidden_proxy_oracles" not in normalized or normalized.get("forbidden_proxy_oracles") is None:
        normalized["forbidden_proxy_oracles"] = []
    normalized.setdefault("supersedes", [])
    normalized.setdefault("superseded_by", [])

    return normalized


def provider_reference_paths(requirement: dict) -> List[str]:
    """Return all local provider reference paths required by one requirement."""
    raw_list = requirement.get("provider_references")
    paths: List[str] = []
    if isinstance(raw_list, list):
        paths.extend(str(item).strip() for item in raw_list if str(item).strip())

    raw_single = requirement.get("provider_reference", "")
    if isinstance(raw_single, str) and raw_single.strip():
        # Backward-compatible parsing for historical traces that encoded several
        # local paths in the legacy singular field.
        for item in re.split(r"[;\n]+", raw_single):
            value = item.strip()
            if value:
                paths.append(value)

    seen = set()
    deduped: List[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def normalize_requirements_trace_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    requirements = payload.get("requirements")
    if isinstance(requirements, list):
        normalized["requirements"] = [
            _normalize_requirement_record(item) if isinstance(item, dict) else item
            for item in requirements
        ]
    return normalized


def load_requirements_trace(project_root: Path, *, normalize: bool = True) -> dict:
    payload = read_json(requirements_trace_path(project_root), default=None)
    if payload is None:
        return empty_requirements_trace()
    if isinstance(payload, dict):
        if normalize:
            normalized = normalize_requirements_trace_payload(payload)
            if isinstance(normalized, dict):
                return normalized
        return payload
    return empty_requirements_trace()


def load_provider_references_lock(project_root: Path) -> dict:
    payload = read_json(provider_references_lock_path(project_root), default=None)
    if payload is None:
        return empty_provider_references_lock()
    if isinstance(payload, dict):
        return payload
    return empty_provider_references_lock()


def forbidden_pattern_definition_reason(pattern: str) -> str:
    """Return why a forbidden-pattern definition is unsafe or invalid."""
    reason = _forbidden_pattern_safety_reason(pattern)
    if reason:
        return reason
    try:
        timeout_regex.compile(pattern)
    except timeout_regex.error as error:
        return f"invalid regular expression: {error}"
    return ""


def forbidden_pattern_definition_findings(payload: object) -> List[dict]:
    """Validate executable pattern definitions without scanning project files.

    Superseded requirements are archival contracts. Their original pattern text is
    preserved for contract identity, but it is never compiled or executed.
    """
    if not isinstance(payload, dict):
        return []
    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        return []

    findings: List[dict] = []
    for index, item in enumerate(requirements, start=1):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "active")).strip()
        if status not in {"active", "deferred"}:
            continue
        patterns = item.get("forbidden_patterns")
        if not isinstance(patterns, list):
            continue
        for pattern_index, raw_value in enumerate(patterns):
            if not isinstance(raw_value, str):
                continue
            reason = forbidden_pattern_definition_reason(raw_value)
            if not reason:
                continue
            finding = _forbidden_pattern_runtime_finding(
                item,
                raw_value,
                path=".auto-agents/state/requirements_trace.json",
                kind="forbidden_pattern_safety",
                reason=reason,
            )
            finding["requirement_index"] = index
            finding["pattern_index"] = pattern_index
            findings.append(finding)
    return findings


def validate_requirements_trace_payload(
    payload: object,
    *,
    validate_forbidden_pattern_definitions: bool = True,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["requirements trace root must be a JSON object"]

    version = payload.get("version")
    if not isinstance(version, int) or version < 1:
        errors.append("requirements trace version must be an integer >= 1")

    decisions = payload.get("persistence_decisions", [])
    if not isinstance(decisions, list):
        errors.append("requirements trace persistence_decisions must be a list")
    else:
        seen_decisions: set[str] = set()
        for index, decision in enumerate(decisions, start=1):
            prefix = f"persistence decision #{index}"
            if not isinstance(decision, dict):
                errors.append(f"{prefix} must be an object")
                continue
            decision_id = str(decision.get("id", "")).strip()
            if not re.fullmatch(r"PERSIST-[0-9]{3,}", decision_id):
                errors.append(f"{prefix} id must match PERSIST-NNN")
            elif decision_id in seen_decisions:
                errors.append(f"{prefix} duplicates id '{decision_id}'")
            seen_decisions.add(decision_id)
            if "storage_transition" in decision or "compatibility_policy" in decision:
                if str(decision.get("storage_transition", "")) not in PERSISTENCE_STORAGE_TRANSITIONS:
                    errors.append(f"{prefix} has an invalid storage_transition")
                if str(decision.get("compatibility_policy", "")) not in PERSISTENCE_COMPATIBILITY_POLICIES:
                    errors.append(f"{prefix} has an invalid compatibility_policy")
            elif str(decision.get("strategy", "")) not in set(PERSISTENCE_STRATEGIES) - {"none"}:
                errors.append(f"{prefix} has an invalid strategy")
            targets = decision.get("target_ids")
            if (
                not isinstance(targets, list)
                or not targets
                or any(not isinstance(item, str) or not item.strip() for item in targets)
            ):
                errors.append(f"{prefix} target_ids must be a non-empty list")
            if str(decision.get("status", "")) not in {"active", "superseded"}:
                errors.append(f"{prefix} status must be active or superseded")
            if not isinstance(decision.get("source"), str) or not str(decision.get("source", "")).strip():
                errors.append(f"{prefix} source must be a non-empty string")

    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        return errors + ["requirements trace must contain a 'requirements' list"]

    seen_ids = set()
    try:
        identity_mode = int(payload.get("contract_identity_schema_version", 0) or 0)
    except (TypeError, ValueError):
        identity_mode = 0
        errors.append("contract_identity_schema_version must be an integer when present")
    for index, item in enumerate(requirements, start=1):
        prefix = f"requirement #{index}"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue

        req_id = str(item.get("id", "")).strip()
        if not req_id:
            errors.append(f"{prefix} id must be a non-empty string")
        elif req_id in seen_ids:
            errors.append(f"{prefix} duplicates id '{req_id}'")
        seen_ids.add(req_id)

        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{prefix} text must be a non-empty string")

        source = item.get("source")
        if not isinstance(source, str):
            errors.append(f"{prefix} source must be a string")

        status = str(item.get("status", "active")).strip()
        if status not in ALLOWED_REQUIREMENT_STATUSES:
            errors.append(
                f"{prefix} status must be one of: {', '.join(sorted(ALLOWED_REQUIREMENT_STATUSES))}"
            )

        priority = str(item.get("priority", "mandatory")).strip()
        if priority not in ALLOWED_REQUIREMENT_PRIORITIES:
            errors.append(
                f"{prefix} priority must be one of: {', '.join(sorted(ALLOWED_REQUIREMENT_PRIORITIES))}"
            )

        oracles = item.get("acceptance_oracles")
        if not isinstance(oracles, list) or any(not isinstance(entry, str) for entry in oracles):
            errors.append(f"{prefix} acceptance_oracles must be a list of strings")
        oracle_type = str(item.get("oracle_type", "")).strip()
        if oracle_type not in ALLOWED_ORACLE_TYPES:
            errors.append(
                f"{prefix} oracle_type must be one of: {', '.join(sorted(ALLOWED_ORACLE_TYPES))}"
            )
        oracle_strength = str(item.get("oracle_strength", "")).strip()
        if oracle_strength not in ALLOWED_ORACLE_STRENGTHS:
            errors.append(
                f"{prefix} oracle_strength must be one of: {', '.join(sorted(ALLOWED_ORACLE_STRENGTHS))}"
            )
        evidence_boundary = str(item.get("evidence_boundary", "")).strip()
        if evidence_boundary not in ALLOWED_EVIDENCE_BOUNDARIES:
            errors.append(
                f"{prefix} evidence_boundary must be one of: {', '.join(sorted(ALLOWED_EVIDENCE_BOUNDARIES))}"
            )
        forbidden_proxy_oracles = item.get("forbidden_proxy_oracles")
        if not isinstance(forbidden_proxy_oracles, list) or any(
            not isinstance(entry, str) for entry in forbidden_proxy_oracles
        ):
            errors.append(f"{prefix} forbidden_proxy_oracles must be a list of strings")

        forbidden = item.get("forbidden_patterns")
        if not isinstance(forbidden, list) or any(not isinstance(entry, str) for entry in forbidden):
            errors.append(f"{prefix} forbidden_patterns must be a list of strings")
        elif validate_forbidden_pattern_definitions and status != "superseded":
            for pattern_index, pattern in enumerate(forbidden):
                reason = forbidden_pattern_definition_reason(pattern)
                if reason:
                    errors.append(
                        f"{prefix} forbidden_patterns[{pattern_index}] definition is unsafe: {reason}"
                    )

        external_docs_required = item.get("external_docs_required", False)
        if not isinstance(external_docs_required, bool):
            errors.append(f"{prefix} external_docs_required must be a boolean")
        provider_reference = item.get("provider_reference", "")
        if not isinstance(provider_reference, str):
            errors.append(f"{prefix} provider_reference must be a string")
        provider_references = item.get("provider_references", [])
        if not isinstance(provider_references, list):
            errors.append(f"{prefix} provider_references must be a list of strings")
        elif isinstance(provider_references, list) and any(
            not isinstance(entry, str) for entry in provider_references
        ):
            errors.append(f"{prefix} provider_references must be a list of strings")
        if external_docs_required:
            if not provider_reference_paths(item):
                errors.append(
                    f"{prefix} provider_reference or provider_references must be non-empty when external_docs_required is true"
                )

        notes = item.get("notes")
        if not isinstance(notes, str):
            errors.append(f"{prefix} notes must be a string")

        for link_field in ("supersedes", "superseded_by"):
            links = item.get(link_field, [])
            if not isinstance(links, list) or any(
                not isinstance(entry, str) or not entry.strip() for entry in links
            ):
                errors.append(f"{prefix} {link_field} must be a list of non-empty strings")
        if identity_mode >= CONTRACT_IDENTITY_SCHEMA_VERSION:
            actual_hash = str(item.get("contract_sha256", "")).strip()
            expected_hash = requirement_contract_sha256(item)
            if actual_hash != expected_hash:
                errors.append(
                    f"{prefix} contract_sha256 is missing or stale; expected {expected_hash}"
                )

    by_id = {
        str(item.get("id", "")).strip(): item
        for item in requirements
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    for req_id, item in by_id.items():
        supersedes = [str(value).strip() for value in item.get("supersedes", []) or []]
        superseded_by = [str(value).strip() for value in item.get("superseded_by", []) or []]
        for old_id in supersedes:
            old = by_id.get(old_id)
            if old is None:
                errors.append(f"requirement {req_id} supersedes unknown requirement {old_id}")
            elif req_id not in (old.get("superseded_by", []) or []):
                errors.append(
                    f"requirement {req_id} supersedes {old_id}, but {old_id}.superseded_by is not reciprocal"
                )
        for new_id in superseded_by:
            new = by_id.get(new_id)
            if new is None:
                errors.append(f"requirement {req_id} is superseded_by unknown requirement {new_id}")
            elif req_id not in (new.get("supersedes", []) or []):
                errors.append(
                    f"requirement {req_id} is superseded_by {new_id}, but {new_id}.supersedes is not reciprocal"
                )
        if superseded_by and str(item.get("status", "")).strip() != "superseded":
            errors.append(f"requirement {req_id} with superseded_by must have status='superseded'")

    return errors


def validate_requirement_contract_transitions(
    previous_payload: object,
    current_payload: object,
    *,
    historical_tasks: Iterable[dict] = (),
) -> List[str]:
    """Reject in-place mutation of requirement IDs that already represent delivered work."""
    if not isinstance(previous_payload, dict) or not isinstance(current_payload, dict):
        return []
    previous = {
        str(item.get("id", "")).strip(): item
        for item in requirement_records(previous_payload)
    }
    current = {
        str(item.get("id", "")).strip(): item
        for item in requirement_records(current_payload)
    }
    proven_ids: set[str] = set()
    for task in historical_tasks:
        if not isinstance(task, dict) or str(task.get("status", "")).strip() != "done":
            continue
        proven_ids.update(
            str(value).strip()
            for value in task.get("requirement_ids", []) or []
            if isinstance(value, str) and value.strip()
        )
        proven_ids.update(
            str(proof.get("requirement_id", "")).strip()
            for proof in task.get("requirement_proofs", []) or []
            if isinstance(proof, dict)
            and str(proof.get("requirement_id", "")).strip()
        )

    errors: List[str] = []
    for req_id, before in previous.items():
        after = current.get(req_id)
        if after is None:
            errors.append(
                f"iteration trace deleted existing requirement {req_id}; preserve it and use status='superseded'"
            )
            continue
        before_status = str(before.get("status", "active")).strip()
        after_status = str(after.get("status", "active")).strip()
        if before_status == "superseded" and after_status != "superseded":
            errors.append(f"superseded requirement {req_id} cannot be reactivated under the same ID")
        before_hash = requirement_contract_sha256(before)
        after_hash = requirement_contract_sha256(after)
        if req_id in proven_ids and before_hash != after_hash:
            errors.append(
                f"requirement contract drift for {req_id}: delivered requirement IDs are immutable; "
                "restore the previous contract, mark it superseded with reciprocal supersession links, "
                "and append the replacement under a new unused REQ ID"
            )
        if req_id in proven_ids and after_status == "superseded" and not after.get("superseded_by"):
            errors.append(
                f"proven requirement {req_id} is superseded but has no superseded_by replacement"
            )
    return errors


def validate_provider_resolve_trace_transition(
    previous_payload: object,
    current_payload: object,
    *,
    deferred_requirement_ids: Iterable[str] = (),
) -> List[str]:
    """Enforce provider-resolve's deliberately narrow trace ownership.

    Provider recovery may preserve an explicit user decision in ``notes`` and
    may defer the exact requirements approved by the user.  It must never
    rewrite the proof-bearing requirement contract, contract identities, or
    supersession topology; those changes belong to clarify.
    """
    if not isinstance(previous_payload, dict) or not isinstance(current_payload, dict):
        return ["provider-resolve requires requirements trace objects before and after the attempt"]

    errors: List[str] = []
    previous_root = {key: value for key, value in previous_payload.items() if key != "requirements"}
    current_root = {key: value for key, value in current_payload.items() if key != "requirements"}
    if previous_root != current_root:
        changed = sorted(
            key
            for key in set(previous_root) | set(current_root)
            if previous_root.get(key) != current_root.get(key)
        )
        errors.append(
            "provider-resolve changed requirements trace root metadata: "
            + ", ".join(changed)
            + "; preserve it and route schema or contract changes to clarify"
        )

    previous_requirements = previous_payload.get("requirements")
    current_requirements = current_payload.get("requirements")
    if not isinstance(previous_requirements, list) or not isinstance(current_requirements, list):
        return errors + ["provider-resolve requires a requirements list before and after the attempt"]
    if any(not isinstance(item, dict) for item in previous_requirements + current_requirements):
        return errors + ["provider-resolve cannot transition a trace containing non-object requirements"]

    previous_ids = [str(item.get("id", "")).strip() for item in previous_requirements]
    current_ids = [str(item.get("id", "")).strip() for item in current_requirements]
    if previous_ids != current_ids:
        errors.append(
            "provider-resolve changed requirement IDs or ordering; preserve the trace shape and route "
            "additions, deletions, or replacements to clarify"
        )
        return errors

    approved_deferred_ids = {
        str(value).strip()
        for value in deferred_requirement_ids
        if isinstance(value, str) and str(value).strip()
    }
    permitted_fields = {"notes", "status"}
    for req_id, before, after in zip(previous_ids, previous_requirements, current_requirements):
        label = req_id or "<unknown requirement>"
        changed_fields = sorted(
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        )
        disallowed_fields = [field for field in changed_fields if field not in permitted_fields]
        if disallowed_fields:
            errors.append(
                f"provider-resolve changed contract-owned fields for {label}: "
                f"{', '.join(disallowed_fields)}; keep approval context in notes or route the change to clarify"
            )
        if requirement_contract_payload(before) != requirement_contract_payload(after):
            errors.append(
                f"provider-resolve changed the proof-bearing contract for {label}; restore it and route "
                "the semantic change to clarify"
            )
        if before.get("contract_sha256") != after.get("contract_sha256"):
            errors.append(
                f"provider-resolve changed engine-owned contract_sha256 for {label}; restore the original value"
            )

        before_status = str(before.get("status", "active")).strip()
        after_status = str(after.get("status", "active")).strip()
        if before_status == after_status:
            continue
        if not (
            before_status == "active"
            and after_status == "deferred"
            and label in approved_deferred_ids
        ):
            errors.append(
                f"provider-resolve changed status for {label} from {before_status!r} to {after_status!r} "
                "without an explicit session-owned defer approval"
            )
    return errors


def requirement_records(trace_payload: dict) -> List[dict]:
    normalized = normalize_requirements_trace_payload(trace_payload)
    if not isinstance(normalized, dict):
        return []
    raw = normalized.get("requirements", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def requirement_ids(trace_payload: dict) -> set[str]:
    return {
        str(item.get("id", "")).strip()
        for item in requirement_records(trace_payload)
        if str(item.get("id", "")).strip()
    }


def mandatory_active_requirement_ids(trace_payload: dict) -> set[str]:
    ids = set()
    for item in requirement_records(trace_payload):
        if str(item.get("status", "active")).strip() != "active":
            continue
        if str(item.get("priority", "mandatory")).strip() != "mandatory":
            continue
        req_id = str(item.get("id", "")).strip()
        if req_id:
            ids.add(req_id)
    return ids


def _previous_task_plan_archive_path(project_root: Path) -> Optional[Path]:
    state = read_json(run_state_path(project_root), default={})
    if not isinstance(state, dict):
        return None
    context = state.get("resume_context", {})
    if not isinstance(context, dict):
        return None
    raw_path = str(context.get("previous_task_plan_archive", "")).strip()
    if not raw_path:
        return None
    archive_path = Path(raw_path)
    if not archive_path.is_absolute():
        archive_path = project_root / archive_path
    return archive_path


def _archived_task_plan_paths(project_root: Path) -> List[Path]:
    paths: List[Path] = []
    previous_archive = _previous_task_plan_archive_path(project_root)
    if previous_archive is not None:
        paths.append(previous_archive)
    archive_root = archived_task_plans_dir(project_root)
    if archive_root.exists():
        paths.extend(sorted(archive_root.glob("*.json")))

    unique_paths: List[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)
    return unique_paths


def load_archived_done_task_payloads(project_root: Path) -> List[dict]:
    """Return done task payloads from all iteration archives."""
    done_tasks: List[dict] = []
    seen_task_ids: set[str] = set()
    for archive_path in _archived_task_plan_paths(project_root):
        if not archive_path.exists():
            continue
        payload = read_json(archive_path, default={})
        if not isinstance(payload, dict):
            continue
        tasks = payload.get("tasks", [])
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict) or str(task.get("status", "")).strip() != "done":
                continue
            task_id = str(task.get("task_id", "")).strip()
            if task_id and task_id in seen_task_ids:
                continue
            if task_id:
                seen_task_ids.add(task_id)
            done_tasks.append(task)
    return done_tasks


def load_archived_done_tasks(project_root: Path) -> List[TaskSpec]:
    tasks: List[TaskSpec] = []
    for payload in load_archived_done_task_payloads(project_root):
        try:
            tasks.append(TaskSpec.from_dict(payload))
        except (KeyError, TypeError, ValueError):
            continue
    return tasks


def _historical_task_requirement_ids(tasks: Iterable[dict], known_ids: set[str]) -> set[str]:
    covered: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or str(task.get("status", "")).strip() != "done":
            continue
        raw_ids = task.get("requirement_ids", [])
        if not isinstance(raw_ids, list):
            continue
        covered.update(
            str(item).strip()
            for item in raw_ids
            if isinstance(item, str) and str(item).strip() in known_ids
        )
    return covered


def _current_task_requirement_ids(tasks: Iterable[dict], known_ids: set[str]) -> set[str]:
    covered: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("status", "")).strip() == "done":
            continue
        raw_ids = task.get("requirement_ids", [])
        if not isinstance(raw_ids, list):
            continue
        covered.update(
            str(item).strip()
            for item in raw_ids
            if isinstance(item, str) and str(item).strip() in known_ids
        )
    return covered


def validate_task_requirement_coverage(
    plan_payload: object,
    trace_payload: dict,
    *,
    historical_tasks: Iterable[dict] = (),
) -> List[str]:
    errors: List[str] = []
    if not isinstance(plan_payload, dict):
        return errors
    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list):
        return errors

    known_ids = requirement_ids(trace_payload)
    preservation_only_ids = set(
        preservation_only_frontend_requirement_ids(trace_payload)
    )
    mandatory_ids = mandatory_active_requirement_ids(trace_payload) - preservation_only_ids
    if not known_ids:
        return errors

    historical_task_list = [task for task in historical_tasks if isinstance(task, dict)]
    current_done_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and str(task.get("status", "")).strip() == "done"
    ]
    covered_ids = _historical_task_requirement_ids(
        [*historical_task_list, *current_done_tasks],
        known_ids,
    )
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", f"#{index}"))
        if str(task.get("status", "")).strip() == "done":
            continue
        raw_ids = task.get("requirement_ids")
        if raw_ids is None:
            raw_ids = []
        if not isinstance(raw_ids, list) or any(not isinstance(item, str) or not item.strip() for item in raw_ids):
            errors.append(f"task {task_id} requirement_ids must be a list of non-empty strings")
            continue
        normalized = {item.strip() for item in raw_ids}
        unknown = sorted(normalized - known_ids)
        if unknown:
            errors.append(f"task {task_id} references unknown requirement_ids: {', '.join(unknown)}")
        covered_ids.update(normalized & known_ids)

    missing = sorted(mandatory_ids - covered_ids)
    if missing:
        errors.append(
            "mandatory active requirements are not covered by any task requirement_ids: "
            + ", ".join(missing)
        )
    errors.extend(
        validate_task_requirement_proofs(
            plan_payload,
            trace_payload,
            historical_tasks=historical_tasks,
        )
    )
    return errors


def _plan_oracle_proof_schema_enabled(plan_payload: dict) -> bool:
    try:
        if int(plan_payload.get("oracle_proof_schema_version") or 0) >= ORACLE_PROOF_SCHEMA_VERSION:
            return True
    except (TypeError, ValueError):
        pass
    tasks = plan_payload.get("tasks")
    return isinstance(tasks, list) and any(
        isinstance(task, dict) and "requirement_proofs" in task for task in tasks
    )


def _historical_verified_proofs_by_requirement(
    tasks: Iterable[dict],
    trace_payload: dict,
) -> Dict[str, List[dict]]:
    by_req = {str(item.get("id", "")).strip(): item for item in requirement_records(trace_payload)}
    proofs_by_requirement: Dict[str, List[dict]] = {}
    for task in tasks:
        if not isinstance(task, dict) or str(task.get("status", "")).strip() != "done":
            continue
        requirement_ids = {
            str(item).strip()
            for item in task.get("requirement_ids", [])
            if isinstance(item, str) and str(item).strip()
        }
        proofs = task.get("requirement_proofs", [])
        if not isinstance(proofs, list):
            continue
        task_contract_text = _task_contract_text_from_payload(task)
        for proof in proofs:
            if not isinstance(proof, dict):
                continue
            req_id = str(proof.get("requirement_id", "")).strip()
            requirement = by_req.get(req_id)
            if requirement is None or req_id not in requirement_ids:
                continue
            matched_oracle = _matched_requirement_oracle(proof, requirement)
            if matched_oracle is None:
                continue
            if str(proof.get("status", "")).strip() != "verified":
                continue
            if _oracle_preservation_messages(task_contract_text, matched_oracle[1]):
                continue
            if _proof_contract_messages(
                requirement,
                proof,
                require_verified=True,
                oracle=matched_oracle[1],
            ):
                continue
            proofs_by_requirement.setdefault(req_id, []).append(proof)
    return proofs_by_requirement


def historical_verified_proofs_by_requirement(
    project_root: Path,
    trace_payload: Optional[dict] = None,
) -> Dict[str, List[dict]]:
    if trace_payload is None:
        trace_payload = load_requirements_trace(project_root)
    return _historical_verified_proofs_by_requirement(
        load_archived_done_task_payloads(project_root),
        trace_payload,
    )


def verified_proofs_by_requirement_from_task_payloads(
    tasks: Iterable[dict],
    trace_payload: dict,
) -> Dict[str, List[dict]]:
    return _historical_verified_proofs_by_requirement(tasks, trace_payload)


def task_is_fully_historically_covered(
    task: TaskSpec,
    trace_payload: dict,
    historical_proofs_by_requirement: Dict[str, List[dict]],
) -> bool:
    if task.status == "done":
        return False
    if task.task_origin != "planned":
        return False
    requirement_ids = {
        str(item).strip()
        for item in task.requirement_ids
        if isinstance(item, str) and str(item).strip()
    }
    if not requirement_ids or not task.requirement_proofs:
        return False
    by_req = {str(item.get("id", "")).strip(): item for item in requirement_records(trace_payload)}
    covered_any = False
    proved_requirement_ids: set[str] = set()
    for proof in task.requirement_proofs:
        if not isinstance(proof, dict):
            return False
        req_id = str(proof.get("requirement_id", "")).strip()
        if req_id not in requirement_ids:
            return False
        requirement = by_req.get(req_id)
        if requirement is None or not _is_active_mandatory_requirement(requirement):
            return False
        matched_oracle = _matched_requirement_oracle(proof, requirement)
        if matched_oracle is None:
            return False
        if not any(
            _proof_matches_oracle(candidate, matched_oracle[1], matched_oracle[0])
            for candidate in historical_proofs_by_requirement.get(req_id, [])
        ):
            return False
        proved_requirement_ids.add(req_id)
        covered_any = True
    return covered_any and proved_requirement_ids == requirement_ids


def validate_task_requirement_proofs(
    plan_payload: object,
    trace_payload: dict,
    *,
    historical_tasks: Iterable[dict] = (),
) -> List[str]:
    errors: List[str] = []
    if not isinstance(plan_payload, dict):
        return errors
    if not _plan_oracle_proof_schema_enabled(plan_payload):
        return errors
    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list):
        return errors
    try:
        proof_schema_version = int(plan_payload.get("oracle_proof_schema_version") or 0)
    except (TypeError, ValueError):
        proof_schema_version = 0

    historical_task_list = [task for task in historical_tasks if isinstance(task, dict)]
    current_done_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and str(task.get("status", "")).strip() == "done"
    ]
    by_req = {str(item.get("id", "")).strip(): item for item in requirement_records(trace_payload)}
    known_ids = set(by_req)
    historical_requirement_ids = _historical_task_requirement_ids(historical_task_list, known_ids)
    current_requirement_ids = _current_task_requirement_ids(tasks, known_ids)
    current_done_requirement_ids = _historical_task_requirement_ids(current_done_tasks, known_ids)
    preservation_only_ids = set(
        preservation_only_frontend_requirement_ids(trace_payload)
    )
    proofs_by_requirement: Dict[str, List[dict]] = _historical_verified_proofs_by_requirement(
        historical_task_list,
        trace_payload,
    )
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", f"#{index}"))
        task_status = str(task.get("status", "")).strip()
        task_done = task_status == "done"
        requirement_ids = {
            str(item).strip()
            for item in task.get("requirement_ids", [])
            if isinstance(item, str) and str(item).strip()
        }
        proofs = task.get("requirement_proofs")
        if requirement_ids and proofs is None:
            errors.append(f"task {task_id} must define requirement_proofs in oracle proof schema mode")
            continue
        if proofs is None:
            continue
        if not isinstance(proofs, list):
            errors.append(f"task {task_id} requirement_proofs must be a list")
            continue
        task_contract_text = _task_contract_text_from_payload(task)
        for proof_index, proof in enumerate(proofs, start=1):
            prefix = f"task {task_id} requirement_proofs[{proof_index}]"
            if not isinstance(proof, dict):
                errors.append(f"{prefix} must be an object")
                continue
            req_id = str(proof.get("requirement_id", "")).strip()
            if not req_id:
                errors.append(f"{prefix} requirement_id must be a non-empty string")
                continue
            if req_id not in by_req:
                if not task_done:
                    errors.append(f"{prefix} references unknown requirement_id: {req_id}")
                continue
            if req_id not in requirement_ids:
                errors.append(f"{prefix} requirement_id must also appear in task requirement_ids: {req_id}")
            if proof_schema_version >= LATEST_ORACLE_PROOF_SCHEMA_VERSION:
                expected_contract_hash = requirement_contract_sha256(by_req[req_id])
                if str(proof.get("requirement_contract_sha256", "")).strip() != expected_contract_hash:
                    errors.append(
                        f"{prefix} requirement_contract_sha256 must equal {expected_contract_hash}"
                    )
            matched_oracle = _matched_requirement_oracle(proof, by_req[req_id])
            if matched_oracle is None:
                errors.append(f"{prefix} must identify an acceptance oracle by oracle_index or exact acceptance_oracle")
            else:
                for message in _oracle_preservation_messages(task_contract_text, matched_oracle[1]):
                    errors.append(f"{prefix} {message}")
            for key in ("proof_type", "oracle_strength", "evidence_boundary", "status"):
                if not isinstance(proof.get(key), str) or not str(proof.get(key)).strip():
                    errors.append(f"{prefix} {key} must be a non-empty string")
            status = str(proof.get("status", "")).strip()
            if status and status not in {"planned", "verified"}:
                errors.append(f"{prefix} status must be planned or verified")
            evidence_refs = proof.get("evidence_refs")
            if (
                not isinstance(evidence_refs, list)
                or not evidence_refs
                or any(not isinstance(item, str) or not item.strip() for item in evidence_refs)
            ):
                errors.append(f"{prefix} evidence_refs must be a non-empty list of strings")
            for key in ("forbidden_proxy_oracles", "proxy_oracles"):
                value = proof.get(key, [])
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                ):
                    errors.append(f"{prefix} {key} must be a list of strings")
            for message in _proof_contract_messages(
                by_req[req_id],
                proof,
                require_verified=task_done,
                require_forbidden_proxy_exclusions=not task_done,
                oracle=matched_oracle[1] if matched_oracle is not None else "",
            ):
                errors.append(f"{prefix} {message}")
            proofs_by_requirement.setdefault(req_id, []).append(proof)

    for req_id, requirement in by_req.items():
        if req_id in preservation_only_ids:
            continue
        if str(requirement.get("status", "active")).strip() != "active":
            continue
        if str(requirement.get("priority", "mandatory")).strip() != "mandatory":
            continue
        if (
            req_id in historical_requirement_ids or req_id in current_done_requirement_ids
        ) and req_id not in current_requirement_ids:
            continue
        for index, oracle in enumerate(_requirement_acceptance_oracles(requirement)):
            if not any(_proof_matches_oracle(proof, oracle, index) for proof in proofs_by_requirement.get(req_id, [])):
                errors.append(f"mandatory requirement {req_id} acceptance oracle #{index + 1} is not covered by requirement_proofs")
    return errors


def _proof_matches_any_requirement_oracle(proof: dict, requirement: dict) -> bool:
    return any(
        _proof_matches_oracle(proof, oracle, index)
        for index, oracle in enumerate(_requirement_acceptance_oracles(requirement))
    )


def _matched_requirement_oracle(proof: dict, requirement: dict) -> Tuple[int, str] | None:
    proof_hash = str(proof.get("requirement_contract_sha256", "")).strip()
    if proof_hash and proof_hash != requirement_contract_sha256(requirement):
        return None
    for index, oracle in enumerate(_requirement_acceptance_oracles(requirement)):
        if _proof_matches_oracle(proof, oracle, index):
            return index, oracle
    return None


def requirements_for_task(project_root: Path, task: TaskSpec) -> List[dict]:
    if not task.requirement_ids:
        return []
    trace = load_requirements_trace(project_root)
    by_id = {str(item.get("id", "")).strip(): item for item in requirement_records(trace)}
    return [by_id[req_id] for req_id in task.requirement_ids if req_id in by_id]


def _oracle_proof_audit_enabled(project_root: Path, tasks: Iterable[TaskSpec]) -> bool:
    plan = read_json(task_plan_path(project_root), default={})
    if isinstance(plan, dict):
        try:
            if int(plan.get("oracle_proof_schema_version") or 0) >= ORACLE_PROOF_SCHEMA_VERSION:
                return True
        except (TypeError, ValueError):
            pass
    return any(bool(task.requirement_proofs) for task in tasks)


def _requirement_acceptance_oracles(requirement: dict) -> List[str]:
    values = requirement.get("acceptance_oracles", [])
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _proofs_for_requirement(
    tasks: Iterable[TaskSpec],
    requirement_id: str,
    assume_done_task_ids: Optional[Iterable[str]] = None,
) -> List[Tuple[TaskSpec, dict]]:
    assumed = {str(item).strip() for item in (assume_done_task_ids or []) if str(item).strip()}
    proofs: List[Tuple[TaskSpec, dict]] = []
    for task in tasks:
        if task.status != "done" and str(task.task_id) not in assumed:
            continue
        for proof in task.requirement_proofs:
            if not isinstance(proof, dict):
                continue
            if str(proof.get("requirement_id", "")).strip() == requirement_id:
                proofs.append((task, proof))
    return proofs


def _proof_matches_oracle(proof: dict, oracle: str, zero_based_index: int) -> bool:
    raw_index = proof.get("oracle_index")
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        index = None
    exact_oracle = str(proof.get("acceptance_oracle", "")).strip()
    if index == zero_based_index + 1:
        if str(proof.get("requirement_contract_sha256", "")).strip():
            return not exact_oracle or exact_oracle == oracle
        return True
    return exact_oracle == oracle


def _proof_list_field(proof: dict, key: str) -> List[str]:
    values = proof.get(key, [])
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _evidence_ref_path(ref: str) -> str:
    value = str(ref).strip()
    if "::" in value:
        value = value.split("::", 1)[0]
    return value.replace("\\", "/").strip()


def _is_executable_test_evidence_ref(ref: str) -> bool:
    path = _evidence_ref_path(ref)
    file_name = path.rsplit("/", 1)[-1].lower()
    if path.endswith(".py"):
        return (
            file_name.startswith("test_")
            or file_name.endswith("_test.py")
            or "/tests/" in f"/{path}"
        )
    return file_name.endswith(
        (
            ".test.js",
            ".test.jsx",
            ".test.ts",
            ".test.tsx",
            ".spec.js",
            ".spec.jsx",
            ".spec.ts",
            ".spec.tsx",
        )
    )


def _documentation_oracle_messages(oracle: str, proof: dict) -> List[str]:
    lowered = oracle.lower()
    documentation_markers = (
        "document",
        "documentation",
        "docs",
        ".md",
        "readme",
        "文档",
    )
    if not any(marker in lowered for marker in documentation_markers):
        return []

    evidence_refs = _proof_list_field(proof, "evidence_refs")
    messages: List[str] = []
    if not any(_is_executable_test_evidence_ref(ref) for ref in evidence_refs):
        messages.append("documentation oracle proof must include an executable test evidence_ref")

    architecture_markers = ("architecture.md", "architecture doc", "架构文档", "架构")
    if any(marker in lowered for marker in architecture_markers):
        if not any(
            _evidence_ref_path(ref).endswith(".auto-agents/docs/architecture.md")
            or _evidence_ref_path(ref).endswith("architecture.md")
            for ref in evidence_refs
        ):
            messages.append("architecture documentation oracle proof must cite .auto-agents/docs/architecture.md")

    return messages


def _rank_meets(value: str, required: str, ranking: Dict[str, int]) -> bool:
    if required not in ranking:
        return True
    if value not in ranking:
        return False
    return ranking[value] >= ranking[required]


def _proof_contract_messages(
    requirement: dict,
    proof: dict,
    *,
    require_verified: bool,
    require_forbidden_proxy_exclusions: bool = True,
    oracle: str = "",
) -> List[str]:
    required_type = str(requirement.get("oracle_type", "")).strip()
    required_strength = str(requirement.get("oracle_strength", "")).strip()
    required_boundary = str(requirement.get("evidence_boundary", "")).strip()
    forbidden_proxy_oracles = [
        str(item).strip()
        for item in requirement.get("forbidden_proxy_oracles", [])
        if isinstance(item, str) and str(item).strip()
    ]
    forbidden_proxy_set = set(forbidden_proxy_oracles)
    proof_type = str(proof.get("proof_type", "")).strip()
    proof_strength = str(proof.get("oracle_strength", "")).strip()
    proof_boundary = str(proof.get("evidence_boundary", "")).strip()
    evidence_refs = _proof_list_field(proof, "evidence_refs")
    proxy_oracles = set(_proof_list_field(proof, "proxy_oracles"))
    recorded_forbidden = set(_proof_list_field(proof, "forbidden_proxy_oracles"))
    messages: List[str] = []

    if require_verified and str(proof.get("status", "")).strip() != "verified":
        messages.append("proof is not verified")
    if not proof_type:
        messages.append("missing proof_type")
    elif proof_type not in ALLOWED_ORACLE_TYPES:
        messages.append(f"proof_type {proof_type} is not allowed")
    elif required_type and required_type != "mixed" and proof_type not in {required_type, "mixed"}:
        messages.append(f"proof_type {proof_type} does not satisfy {required_type}")
    if not proof_strength:
        messages.append("missing oracle_strength")
    elif proof_strength not in ALLOWED_ORACLE_STRENGTHS:
        messages.append(f"oracle_strength {proof_strength} is not allowed")
    elif not _rank_meets(proof_strength, required_strength, ORACLE_STRENGTH_ORDER):
        messages.append(f"oracle_strength {proof_strength} is weaker than {required_strength}")
    if not proof_boundary:
        messages.append("missing evidence_boundary")
    elif proof_boundary not in ALLOWED_EVIDENCE_BOUNDARIES:
        messages.append(f"evidence_boundary {proof_boundary} is not allowed")
    elif not _rank_meets(proof_boundary, required_boundary, EVIDENCE_BOUNDARY_ORDER):
        messages.append(f"evidence_boundary {proof_boundary} is weaker than {required_boundary}")
    if not evidence_refs:
        messages.append("missing evidence_refs")
    forbidden_used = sorted(proxy_oracles & forbidden_proxy_set)
    if forbidden_used:
        messages.append("uses forbidden proxy oracle(s): " + ", ".join(forbidden_used))
    if require_forbidden_proxy_exclusions:
        missing_forbidden = sorted(forbidden_proxy_set - recorded_forbidden)
        if missing_forbidden:
            messages.append("does not record forbidden proxy exclusion(s): " + ", ".join(missing_forbidden))
    if oracle:
        messages.extend(_documentation_oracle_messages(oracle, proof))
    return messages


def _is_active_mandatory_requirement(requirement: dict) -> bool:
    return (
        str(requirement.get("status", "active")).strip() == "active"
        and str(requirement.get("priority", "mandatory")).strip() == "mandatory"
    )


def validate_done_task_requirement_proofs(task: TaskSpec, trace_payload: dict) -> List[dict]:
    """Validate the oracle proofs that would allow a task to become done."""
    requirement_ids = {
        str(item).strip()
        for item in task.requirement_ids
        if isinstance(item, str) and str(item).strip()
    }
    if not requirement_ids and not task.requirement_proofs:
        return []

    by_req = {str(item.get("id", "")).strip(): item for item in requirement_records(trace_payload)}
    active_bound_requirements = {
        req_id: by_req[req_id]
        for req_id in requirement_ids
        if req_id in by_req and _is_active_mandatory_requirement(by_req[req_id])
    }
    if not active_bound_requirements:
        return []

    findings: List[dict] = []
    task_contract_text = _task_contract_text_from_spec(task)
    proofs_by_requirement: Dict[str, List[dict]] = {req_id: [] for req_id in active_bound_requirements}
    for proof_index, proof in enumerate(task.requirement_proofs, start=1):
        prefix = f"requirement_proofs[{proof_index}]"
        if not isinstance(proof, dict):
            findings.append(
                {
                    "kind": "oracle_proof_invalid",
                    "task_id": task.task_id,
                    "requirement_id": "",
                    "oracle_index": "",
                    "message": f"{prefix} must be an object",
                }
            )
            continue
        req_id = str(proof.get("requirement_id", "")).strip()
        requirement = by_req.get(req_id)
        if requirement is None:
            findings.append(
                {
                    "kind": "oracle_proof_invalid",
                    "task_id": task.task_id,
                    "requirement_id": req_id,
                    "oracle_index": str(proof.get("oracle_index", "")),
                    "message": f"{prefix} references unknown requirement_id: {req_id or '(missing)'}",
                }
            )
            continue
        if not _is_active_mandatory_requirement(requirement):
            continue
        if req_id not in requirement_ids:
            findings.append(
                {
                    "kind": "oracle_proof_invalid",
                    "task_id": task.task_id,
                    "requirement_id": req_id,
                    "oracle_index": str(proof.get("oracle_index", "")),
                    "message": f"{prefix} requirement_id must also appear in task requirement_ids: {req_id}",
                }
            )
            continue
        proofs_by_requirement.setdefault(req_id, []).append(proof)
        matched_oracle = _matched_requirement_oracle(proof, requirement)
        if matched_oracle is None:
            findings.append(
                {
                    "kind": "oracle_proof_invalid",
                    "task_id": task.task_id,
                    "requirement_id": req_id,
                    "oracle_index": str(proof.get("oracle_index", "")),
                    "message": f"{prefix} must identify an acceptance oracle by oracle_index or exact acceptance_oracle",
                }
            )
        else:
            for message in _oracle_preservation_messages(task_contract_text, matched_oracle[1]):
                findings.append(
                    {
                        "kind": "oracle_proof_invalid",
                        "task_id": task.task_id,
                        "requirement_id": req_id,
                        "oracle_index": str(proof.get("oracle_index", "")),
                        "message": f"{prefix} {message}",
                    }
                )
        for message in _proof_contract_messages(
            requirement,
            proof,
            require_verified=True,
            oracle=matched_oracle[1] if matched_oracle is not None else "",
        ):
            findings.append(
                {
                    "kind": "oracle_proof_invalid",
                    "task_id": task.task_id,
                    "requirement_id": req_id,
                    "oracle_index": str(proof.get("oracle_index", "")),
                    "message": f"{prefix} {message}",
                }
            )

    for req_id in active_bound_requirements:
        if not proofs_by_requirement.get(req_id):
            findings.append(
                {
                    "kind": "oracle_proof_missing",
                    "task_id": task.task_id,
                    "requirement_id": req_id,
                    "oracle_index": "",
                    "message": f"task {task.task_id} has no requirement_proofs for active mandatory requirement {req_id}",
                }
            )
    return findings


def _oracle_proof_findings(requirement: dict, proofs: List[Tuple[TaskSpec, dict]]) -> List[dict]:
    req_id = str(requirement.get("id", "")).strip()
    blockers: List[dict] = []
    oracles = _requirement_acceptance_oracles(requirement)

    if not proofs:
        blockers.append(
            {
                "kind": "oracle_proof_missing",
                "message": f"{req_id} has no done-task oracle proof entries",
            }
        )
        return blockers

    expected_hash = requirement_contract_sha256(requirement)
    for task, proof in proofs:
        proof_hash = str(proof.get("requirement_contract_sha256", "")).strip()
        exact_oracle = str(proof.get("acceptance_oracle", "")).strip()
        try:
            proof_index = int(proof.get("oracle_index")) - 1
        except (TypeError, ValueError):
            proof_index = -1
        legacy_oracle_drift = bool(
            exact_oracle
            and 0 <= proof_index < len(oracles)
            and exact_oracle != oracles[proof_index]
        )
        if (proof_hash and proof_hash != expected_hash) or legacy_oracle_drift:
            blockers.append(
                {
                    "kind": "requirement_contract_drift",
                    "task_id": task.task_id,
                    "message": (
                        f"{req_id} historical proof from {task.task_id} is bound to a different "
                        "requirement contract; preserve the delivered requirement as superseded "
                        "and append the replacement under a new REQ ID"
                    ),
                }
            )

    for index, oracle in enumerate(oracles):
        matching = [
            (task, proof)
            for task, proof in proofs
            if _proof_matches_oracle(proof, oracle, index)
        ]
        if not matching:
            blockers.append(
                {
                    "kind": "oracle_proof_missing",
                    "message": f"{req_id} acceptance oracle #{index + 1} has no proof entry",
                }
            )
            continue
        oracle_ok = False
        proof_messages: List[str] = []
        for task, proof in matching:
            local_messages = _proof_contract_messages(
                requirement,
                proof,
                require_verified=True,
                oracle=oracle,
            )
            local_messages.extend(
                _oracle_preservation_messages(_task_contract_text_from_spec(task), oracle)
            )
            if not local_messages:
                oracle_ok = True
                break
            proof_messages.append(f"{task.task_id}: " + "; ".join(local_messages))
        if not oracle_ok:
            blockers.append(
                {
                    "kind": "oracle_proof_invalid",
                    "message": (
                        f"{req_id} acceptance oracle #{index + 1} has no valid verified proof"
                        + (": " + " | ".join(proof_messages[:3]) if proof_messages else "")
                    ),
                }
            )
    return blockers


def format_requirement_context(requirements: Iterable[dict]) -> str:
    records = list(requirements)
    if not records:
        return ""
    lines = ["Bound requirements and acceptance oracles:"]
    for item in records:
        req_id = str(item.get("id", "")).strip()
        lines.append(f"- {req_id}: {str(item.get('text', '')).strip()}")
        source = str(item.get("source", "")).strip()
        if source:
            lines.append(f"  Source: {source}")
        oracles = item.get("acceptance_oracles", [])
        if isinstance(oracles, list) and oracles:
            lines.append("  Acceptance oracles:")
            lines.extend(f"  - {str(oracle).strip()}" for oracle in oracles if str(oracle).strip())
        oracle_type = str(item.get("oracle_type", "")).strip()
        oracle_strength = str(item.get("oracle_strength", "")).strip()
        evidence_boundary = str(item.get("evidence_boundary", "")).strip()
        if oracle_type:
            lines.append(f"  Oracle type: {oracle_type}")
        if oracle_strength:
            lines.append(f"  Oracle strength: {oracle_strength}")
        if evidence_boundary:
            lines.append(f"  Evidence boundary: {evidence_boundary}")
        forbidden_proxy_oracles = item.get("forbidden_proxy_oracles", [])
        if isinstance(forbidden_proxy_oracles, list) and forbidden_proxy_oracles:
            lines.append("  Forbidden proxy oracles:")
            lines.extend(
                f"  - {str(oracle).strip()}"
                for oracle in forbidden_proxy_oracles
                if str(oracle).strip()
            )
        forbidden = item.get("forbidden_patterns", [])
        if isinstance(forbidden, list) and forbidden:
            lines.append("  Forbidden patterns:")
            lines.extend(f"  - {str(pattern).strip()}" for pattern in forbidden if str(pattern).strip())
        if bool(item.get("external_docs_required", False)):
            references = provider_reference_paths(item)
            rendered = "; ".join(references) if references else "(missing)"
            lines.append(f"  External docs required: yes; provider references: {rendered}")
    return "\n".join(lines)


def external_doc_requirements(trace_payload: dict) -> List[dict]:
    return [
        item
        for item in requirement_records(trace_payload)
        if str(item.get("status", "active")).strip() == "active"
        and bool(item.get("external_docs_required", False))
    ]


def provider_reference_status(lock_payload: dict, reference_path: str) -> str:
    refs = lock_payload.get("references", {})
    if not isinstance(refs, dict):
        return "missing"
    for value in refs.values():
        if not isinstance(value, dict):
            continue
        if str(value.get("path", "")).strip() == reference_path:
            return str(value.get("status", "missing")).strip() or "missing"
    return "missing"


def provider_reference_consumer_contract_sha256(
    trace_payload: dict,
    reference_path: str,
) -> str:
    consumers = []
    for requirement in requirement_records(trace_payload):
        if str(requirement.get("status", "active")).strip() != "active":
            continue
        if reference_path not in provider_reference_paths(requirement):
            continue
        consumers.append(
            {
                "requirement_id": str(requirement.get("id", "")).strip(),
                "contract_sha256": requirement_contract_sha256(requirement),
            }
        )
    encoded = json.dumps(
        sorted(consumers, key=lambda item: (item["requirement_id"], item["contract_sha256"])),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def provider_reference_effective_status(
    lock_payload: dict,
    trace_payload: dict,
    reference_path: str,
) -> str:
    refs = lock_payload.get("references", {})
    if not isinstance(refs, dict):
        return "missing"
    for value in refs.values():
        if not isinstance(value, dict):
            continue
        if str(value.get("path", "")).strip() != reference_path:
            continue
        status = str(value.get("status", "missing")).strip() or "missing"
        if status not in {"verified", "assumption_approved", "deferred"}:
            return status
        try:
            identity_mode = int(trace_payload.get("contract_identity_schema_version", 0) or 0)
        except (TypeError, ValueError):
            identity_mode = 0
        if identity_mode < CONTRACT_IDENTITY_SCHEMA_VERSION:
            return status
        expected = provider_reference_consumer_contract_sha256(trace_payload, reference_path)
        recorded = str(value.get("consumer_contract_sha256", "")).strip()
        return status if recorded == expected else "needs_refresh"
    return "missing"


def stamp_provider_reference_consumer_hashes(
    lock_payload: object,
    trace_payload: dict,
    *,
    reference_paths: Optional[Iterable[str]] = None,
) -> Tuple[object, List[str]]:
    if not isinstance(lock_payload, dict):
        return lock_payload, []
    refs = lock_payload.get("references")
    if not isinstance(refs, dict):
        return lock_payload, []
    updated = copy.deepcopy(lock_payload)
    changes: List[str] = []
    allowed_paths = (
        {str(path).strip() for path in reference_paths if str(path).strip()}
        if reference_paths is not None
        else None
    )
    for key, entry in updated.get("references", {}).items():
        if not isinstance(entry, dict):
            continue
        reference = str(entry.get("path", "")).strip()
        status = str(entry.get("status", "")).strip()
        if allowed_paths is not None and reference not in allowed_paths:
            continue
        if not reference or status not in {"verified", "assumption_approved", "deferred"}:
            continue
        expected = provider_reference_consumer_contract_sha256(trace_payload, reference)
        if str(entry.get("consumer_contract_sha256", "")).strip() != expected:
            entry["consumer_contract_sha256"] = expected
            changes.append(str(key))
    return updated, changes


def migrate_legacy_provider_reference_consumer_hashes(
    lock_payload: object,
    previous_trace_payload: object,
    current_trace_payload: object,
) -> Tuple[object, List[str]]:
    """Backfill unchanged legacy locks without forcing unrelated re-research.

    A missing consumer hash means "not migrated", not "contract changed".  The
    pre-clarify trace is the authoritative comparison point: only references
    whose aggregate active-consumer contract is identical before and after the
    clarify pass are grandfathered. Changed contracts remain unbound and will
    correctly resolve to ``needs_refresh``.
    """
    if (
        not isinstance(lock_payload, dict)
        or not isinstance(previous_trace_payload, dict)
        or not isinstance(current_trace_payload, dict)
    ):
        return lock_payload, []
    refs = lock_payload.get("references")
    if not isinstance(refs, dict):
        return lock_payload, []
    updated = copy.deepcopy(lock_payload)
    changes: List[str] = []
    for key, entry in updated.get("references", {}).items():
        if not isinstance(entry, dict):
            continue
        reference = str(entry.get("path", "")).strip()
        status = str(entry.get("status", "")).strip()
        if (
            not reference
            or status not in {"verified", "assumption_approved", "deferred"}
            or str(entry.get("consumer_contract_sha256", "")).strip()
        ):
            continue
        previous_hash = provider_reference_consumer_contract_sha256(
            previous_trace_payload, reference
        )
        current_hash = provider_reference_consumer_contract_sha256(
            current_trace_payload, reference
        )
        if previous_hash != current_hash:
            continue
        entry["consumer_contract_sha256"] = current_hash
        changes.append(str(key))
    return updated, changes


def _historical_snapshot_advisory_blockers(blockers: List[dict]) -> bool:
    if not blockers:
        return False
    for blocker in blockers:
        kind = str(blocker.get("kind", "")).strip()
        message = str(blocker.get("message", "")).strip()
        if kind == "oracle_proof_missing" and "acceptance oracle #" in message:
            continue
        if (
            kind == "oracle_proof_invalid"
            and "does not record forbidden proxy exclusion(s)" in message
            and "uses forbidden proxy oracle(s)" not in message
        ):
            continue
        return False
    return True


def _current_spec_scope_tokens(current_spec: Optional[Path]) -> set:
    if not current_spec:
        return set()
    name = Path(str(current_spec)).name.strip()
    if not name:
        return set()
    tokens = {name}
    stem = Path(name).stem.strip()
    if stem:
        tokens.add(stem)
    return tokens


def _requirement_in_current_scope(requirement: dict, spec_tokens: set) -> bool:
    # When no current spec is known (e.g. the standalone CLI audit) every requirement is
    # treated as in-scope, preserving strict legacy behaviour.
    if not spec_tokens:
        return True
    source = str(requirement.get("source", ""))
    return any(token and token in source for token in spec_tokens)


def requirements_audit_context_sha256(
    project_root: Path,
    tasks: Iterable[TaskSpec],
    *,
    current_spec: Optional[Path] = None,
    assume_done_task_ids: Optional[Iterable[str]] = None,
) -> str:
    trace = load_requirements_trace(project_root)
    references = sorted(
        {
            reference
            for requirement in external_doc_requirements(trace)
            for reference in provider_reference_paths(requirement)
        }
    )
    file_inputs = {}
    for relative in [
        ".auto-agents/docs/project_brief.md",
        ".auto-agents/docs/architecture.md",
        *references,
    ]:
        file_inputs[relative] = read_text(project_root / relative)
    spec_value = ""
    spec_path = ""
    if current_spec is not None:
        candidate = Path(current_spec)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        spec_path = str(candidate.resolve())
        spec_value = read_text(candidate)
    task_payloads = []
    for task in tasks:
        task_payload = task.to_dict()
        # commit_sha is execution metadata. It is intentionally absent from
        # task_plan.json and must not invalidate a semantic audit cache when a
        # completed task is persisted or a process resumes.
        task_payload.pop("commit_sha", None)
        task_payloads.append(task_payload)
    payload = {
        "context_schema_version": 2,
        "trace": trace,
        "tasks": task_payloads,
        "assume_done_task_ids": sorted(
            str(item).strip()
            for item in (assume_done_task_ids or [])
            if str(item).strip()
        ),
        "current_spec_path": spec_path,
        "current_spec": spec_value,
        "provider_lock": load_provider_references_lock(project_root),
        "source_files": file_inputs,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def run_requirements_audit(
    project_root: Path,
    tasks: Iterable[TaskSpec],
    current_spec: Optional[Path] = None,
    assume_done_task_ids: Optional[Iterable[str]] = None,
) -> dict:
    audit_started = time.monotonic()
    try:
        audit_config = load_project_config(project_root).execution.requirements_audit
    except (FileNotFoundError, TypeError, ValueError):
        from .models import RequirementsAuditConfig

        audit_config = RequirementsAuditConfig()
    deadline = audit_started + audit_config.total_timeout_seconds
    audit_cache = RequirementsAuditCache(project_root) if audit_config.cache_enabled else None
    audit_metrics: Dict[str, object] = {
        "files": 0,
        "bytes": 0,
        "patterns": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "matcher_calls": 0,
    }
    current_tasks = list(tasks)
    assumed_done = {
        str(item).strip() for item in (assume_done_task_ids or []) if str(item).strip()
    }

    def _is_effectively_done(task: TaskSpec) -> bool:
        return task.status == "done" or str(task.task_id) in assumed_done

    archived_tasks = load_archived_done_tasks(project_root)
    trace = load_requirements_trace(project_root)
    known_ids = requirement_ids(trace)
    spec_tokens = _current_spec_scope_tokens(current_spec)
    current_requirement_ids = {
        req_id
        for task in current_tasks
        if not _is_effectively_done(task)
        for req_id in task.requirement_ids
        if req_id in known_ids
    }
    current_done_requirement_ids = {
        req_id
        for task in current_tasks
        if _is_effectively_done(task)
        for req_id in task.requirement_ids
        if req_id in known_ids
    }
    archived_requirement_ids = {
        req_id
        for task in archived_tasks
        if task.status == "done"
        for req_id in task.requirement_ids
        if req_id in known_ids
    }
    tasks = list(current_tasks)
    if archived_tasks:
        current_task_ids = {task.task_id for task in tasks}
        tasks = tasks + [task for task in archived_tasks if task.task_id not in current_task_ids]
    lock = load_provider_references_lock(project_root)
    oracle_proof_audit = _oracle_proof_audit_enabled(project_root, tasks)
    context_sha256 = requirements_audit_context_sha256(
        project_root,
        current_tasks,
        current_spec=current_spec,
        assume_done_task_ids=assumed_done,
    )
    lines = [
        "# Requirements Audit",
        "",
        f"Input context: {context_sha256}",
        f"Generated at: {_dt.datetime.utcnow().replace(microsecond=0).isoformat()}Z",
        f"Oracle proof audit: {'strict' if oracle_proof_audit else 'legacy'}",
        "",
    ]
    ok = True
    issues: List[dict] = []
    task_requirements = set()
    for task in tasks:
        if _is_effectively_done(task):
            task_requirements.update(task.requirement_ids)

    trace_requirements = requirement_records(trace)
    forbidden_scan_files: List[Tuple[str, str]] = []
    forbidden_content_hashes: Dict[str, str] = {}
    forbidden_scan_timed_out = False
    forbidden_findings_by_requirement: Dict[str, List[dict]] = {}
    if any(
        item.get("status", "active") == "active" and item.get("forbidden_patterns")
        for item in trace_requirements
    ):
        try:
            forbidden_scan_files = _forbidden_pattern_scan_files(
                project_root, _deadline=deadline
            )
        except _ForbiddenPatternTotalTimeout:
            forbidden_scan_timed_out = True
        audit_metrics["files"] = len(forbidden_scan_files)
        audit_metrics["bytes"] = sum(len(content) for _, content in forbidden_scan_files)
        forbidden_content_hashes = {
            rel: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for rel, content in forbidden_scan_files
        }

    if not forbidden_scan_timed_out:
        safe_patterns: List[str] = []
        pattern_owners: Dict[str, List[str]] = {}
        for item in trace_requirements:
            req_id = str(item.get("id", "")).strip()
            if str(item.get("status", "active")).strip() != "active" or not req_id:
                continue
            raw_patterns = item.get("forbidden_patterns", [])
            if not isinstance(raw_patterns, list):
                continue
            for raw_value in raw_patterns:
                raw = str(raw_value)
                reason = forbidden_pattern_definition_reason(raw)
                if reason:
                    forbidden_findings_by_requirement.setdefault(req_id, []).append(
                        _forbidden_pattern_runtime_finding(
                            item,
                            raw,
                            path=".auto-agents/state/requirements_trace.json",
                            kind="forbidden_pattern_safety",
                            reason=reason,
                        )
                    )
                    continue
                if raw not in pattern_owners:
                    safe_patterns.append(raw)
                    pattern_owners[raw] = []
                if req_id not in pattern_owners[raw]:
                    pattern_owners[raw].append(req_id)
        if safe_patterns:
            global_findings = forbidden_pattern_findings(
                project_root,
                {
                    "id": "requirements-audit",
                    "status": "active",
                    "forbidden_patterns": safe_patterns,
                },
                current_spec=current_spec,
                _scan_files=forbidden_scan_files,
                _cache=audit_cache,
                _pattern_timeout_ms=audit_config.pattern_timeout_ms,
                _deadline=deadline,
                _metrics=audit_metrics,
                _content_hashes=forbidden_content_hashes,
            )
            for finding in global_findings:
                if finding.get("kind") == "forbidden_pattern":
                    owners = pattern_owners.get(str(finding.get("pattern", "")), [])
                else:
                    # Matching stopped before the global set was fully audited.
                    # Fail every owner closed; none may assume its pattern ran.
                    owners = sorted({owner for values in pattern_owners.values() for owner in values})
                for owner in owners:
                    forbidden_findings_by_requirement.setdefault(owner, []).append(
                        dict(finding)
                    )

    for item in trace_requirements:
        req_id = str(item.get("id", "")).strip()
        status = str(item.get("status", "active")).strip()
        priority = str(item.get("priority", "mandatory")).strip()
        if not req_id:
            continue
        blockers: List[dict] = []
        if forbidden_scan_timed_out and item.get("forbidden_patterns"):
            blockers.append(
                _forbidden_pattern_runtime_finding(
                    item,
                    "",
                    path=".auto-agents/state/requirements_trace.json",
                    kind="forbidden_pattern_total_timeout",
                    reason="repository corpus scan exceeded the total audit time limit",
                )
            )
        if status == "active" and priority == "mandatory" and req_id not in task_requirements:
            blockers.append(
                {
                    "kind": "task_coverage",
                    "message": "not covered by any done task",
                }
            )
        if status == "active" and priority == "mandatory" and oracle_proof_audit:
            blockers.extend(
                _oracle_proof_findings(
                    item, _proofs_for_requirement(tasks, req_id, assumed_done)
                )
            )
        if status == "active" and bool(item.get("external_docs_required", False)):
            references = provider_reference_paths(item)
            if not references:
                blockers.append(
                    {
                        "kind": "provider_reference",
                        "message": "provider reference is missing",
                        "reference": "",
                        "reference_status": "missing",
                    }
                )
            for reference in references:
                ref_status = provider_reference_effective_status(lock, trace, reference)
                if ref_status not in PASSING_REFERENCE_STATUSES:
                    blockers.append(
                        {
                            "kind": "provider_reference",
                            "message": f"provider reference is {ref_status}",
                            "reference": reference,
                            "reference_status": ref_status,
                        }
                    )
        if not forbidden_scan_timed_out:
            blockers.extend(forbidden_findings_by_requirement.get(req_id, []))

        # Design: corroboration rule for forbidden-pattern findings.
        # A forbidden pattern that only appears in auto_agents-internal working memory or
        # reference material (task_plan.json, run_state.json, provider_references, ...) is not
        # a real product violation on its own — it is usually the plan discussing or
        # instructing the REMOVAL of the forbidden concept. It hard-fails only when the SAME
        # pattern also appears in an authoritative product file; otherwise it is advisory and
        # stays visible in the report without blocking the run. Genuine product violations
        # (authoritative-file hits) and non-forbidden-pattern blockers are unaffected.
        authoritative_patterns = {
            str(entry.get("pattern"))
            for entry in blockers
            if entry.get("kind") == "forbidden_pattern" and entry.get("authoritative")
        }
        for entry in blockers:
            if (
                entry.get("kind") == "forbidden_pattern"
                and not entry.get("authoritative")
                and str(entry.get("pattern")) not in authoritative_patterns
            ):
                entry["advisory"] = True
        blocking_blockers = [entry for entry in blockers if not entry.get("advisory")]

        historical_only = (
            req_id in archived_requirement_ids or req_id in current_done_requirement_ids
        ) and req_id not in current_requirement_ids
        historical_advisory = historical_only and _historical_snapshot_advisory_blockers(
            blocking_blockers
        )
        # A requirement whose recorded source does not reference the current iteration's spec
        # is out-of-run-scope backlog: report its gaps as advisory instead of hard-failing the
        # run, so a run for one spec cannot be blocked (or generate 补齐 tasks) for unrelated
        # historical requirements from earlier iterations.
        out_of_scope_backlog = (
            bool(spec_tokens)
            and not _requirement_in_current_scope(item, spec_tokens)
            and req_id not in current_requirement_ids
        )
        if (
            blocking_blockers
            and status == "active"
            and priority == "mandatory"
            and not historical_advisory
            and not out_of_scope_backlog
        ):
            ok = False
            result = "fail"
        elif blockers:
            result = "advisory"
        else:
            result = "pass"

        text = str(item.get("text", "")).strip()
        oracle_type = str(item.get("oracle_type", "")).strip()
        oracle_strength = str(item.get("oracle_strength", "")).strip()
        evidence_boundary = str(item.get("evidence_boundary", "")).strip()
        forbidden_proxy_oracles = item.get("forbidden_proxy_oracles", [])
        issues.append(
            {
                "requirement_id": req_id,
                "result": result,
                "status": status,
                "priority": priority,
                "text": text,
                "blockers": blockers,
                "out_of_scope_backlog": bool(out_of_scope_backlog and blockers),
            }
        )

        lines.append(f"## {req_id}: {result}")
        lines.append("")
        if out_of_scope_backlog and blockers:
            lines.append(
                "Out-of-scope backlog: this requirement's source does not reference the current "
                "iteration spec; gaps are reported as advisory and do not block this run."
            )
            lines.append("")
        if text:
            lines.append(text)
            lines.append("")
        if oracle_type or oracle_strength or evidence_boundary:
            lines.append(
                "Oracle contract: "
                f"type={oracle_type or '(missing)'}; "
                f"strength={oracle_strength or '(missing)'}; "
                f"evidence_boundary={evidence_boundary or '(missing)'}"
            )
            lines.append("")
        if isinstance(forbidden_proxy_oracles, list) and forbidden_proxy_oracles:
            lines.append("Forbidden proxy oracles:")
            lines.extend(
                f"- {str(oracle).strip()}"
                for oracle in forbidden_proxy_oracles
                if str(oracle).strip()
            )
            lines.append("")
        if not oracle_proof_audit and status == "active" and priority == "mandatory":
            lines.append(
                "Oracle proof audit is in legacy mode; requirement pass only confirms done-task coverage."
            )
            lines.append("")
        if blockers:
            lines.append("Findings:")
            lines.extend(
                f"- {entry['message']}"
                + (
                    " [advisory: no corroborating authoritative product-file match]"
                    if entry.get("advisory")
                    else ""
                )
                for entry in blockers
            )
            lines.append("")

    if not requirement_records(trace):
        lines.append("No requirements are currently tracked.")
        lines.append("")

    if audit_cache is not None:
        audit_cache.close()

    lines.insert(2, f"Result: {'pass' if ok else 'fail'}")
    audit_metrics["elapsed_seconds"] = round(time.monotonic() - audit_started, 3)
    lines.insert(
        6,
        "Audit metrics: "
        f"files={audit_metrics['files']}; bytes={audit_metrics['bytes']}; "
        f"patterns={audit_metrics['patterns']}; cache_hits={audit_metrics['cache_hits']}; "
        f"cache_misses={audit_metrics['cache_misses']}; "
        f"matcher_calls={audit_metrics['matcher_calls']}; "
        f"elapsed_seconds={audit_metrics['elapsed_seconds']}",
    )
    report = "\n".join(lines).rstrip() + "\n"
    write_text(requirements_audit_path(project_root), report)
    return {
        "ok": ok,
        "report": report,
        "issues": issues,
        "path": str(requirements_audit_path(project_root)),
        "input_context_sha256": context_sha256,
        "metrics": audit_metrics,
    }


def audit_requirements(
    project_root: Path,
    tasks: Iterable[TaskSpec],
    current_spec: Optional[Path] = None,
    assume_done_task_ids: Optional[Iterable[str]] = None,
) -> Tuple[bool, str]:
    result = run_requirements_audit(
        project_root,
        tasks,
        current_spec=current_spec,
        assume_done_task_ids=assume_done_task_ids,
    )
    return bool(result["ok"]), str(result["report"])


def _current_spec_relpath(project_root: Path, current_spec: Optional[Path]) -> Optional[str]:
    """Return the current iteration spec path relative to project_root (posix), or None."""
    if current_spec is None:
        return None
    raw = str(current_spec).replace("\\", "/").strip()
    if not raw:
        return None
    candidate = Path(current_spec)
    try:
        return str(candidate.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        # Not under project_root (or unresolvable). Fall back to a relative literal.
        if not candidate.is_absolute():
            return raw
    return None


def _is_noncurrent_spec_file(rel: str, current_spec_rel: Optional[str]) -> bool:
    """Return True when `rel` is a spec markdown file from a DIFFERENT iteration than the
    current run's spec.

    Historical spec files are immutable records of past iterations, not the current product
    contract. Their forbidden-pattern hits (e.g. an old spec that legitimately described a
    now-forbidden `详情页`) must not hard-fail the current run and must never force rewriting
    history. They are corroboration-only. The CURRENT spec stays authoritative.
    """
    if not current_spec_rel:
        return False
    if not rel.lower().endswith(".md"):
        return False
    if rel == current_spec_rel:
        return False
    spec_dir = current_spec_rel.rsplit("/", 1)[0] if "/" in current_spec_rel else ""
    # Only treat files inside a real spec directory as specs. A root-level current spec
    # (spec_dir == "") must not turn README.md and other root docs into "specs".
    if not spec_dir:
        return False
    file_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return file_dir == spec_dir


def _forbidden_pattern_corroboration_only_path(
    rel: str,
    current_spec_rel: Optional[str] = None,
) -> bool:
    """Return True for files whose forbidden-pattern hits are corroboration-only (not
    authoritative product source-of-truth).

    A forbidden pattern appearing only in these files is usually the orchestrator's own plan,
    review commentary, fetched provider reference, or a historical spec discussing (or
    instructing the REMOVAL of) a forbidden concept, not the current product implementing it.
    Such hits must not hard-fail on their own; they only matter when the same pattern also
    appears in an authoritative product file. Product docs such as project_brief.md and
    architecture.md, and the CURRENT iteration spec, are NOT listed here — they remain
    authoritative.
    """
    normalized = str(rel).replace("\\", "/").strip()
    if normalized.startswith(".auto-agents/state/"):
        return True
    if normalized.startswith(".auto-agents/docs/provider_references/"):
        return True
    if _is_executable_test_evidence_ref(normalized):
        return True
    if _is_noncurrent_spec_file(normalized, current_spec_rel):
        return True
    return False


_NEGATED_FORBIDDEN_PATTERN_MARKERS = (
    "不得",
    "不能",
    "不可",
    "不再",
    "不要",
    "不展示",
    "不显示",
    "不暴露",
    "不出现",
    "不包含",
    "不提供",
    "不适合",
    "无需",
    "无须",
    "移除",
    "删除",
    "下线",
    "禁用",
    "禁止",
    "严禁",
    "避免",
    "hide",
    "remove",
    "removes",
    "removed",
    "without",
    "must not",
    "should not",
    "do not",
    "don't",
)


def _forbidden_pattern_match_is_negated(content: str, start: int, end: int) -> bool:
    line_start = content.rfind("\n", 0, start) + 1
    line_end = content.find("\n", end)
    if line_end == -1:
        line_end = len(content)
    line = content[line_start:line_end]
    relative_start = max(start - line_start, 0)
    relative_end = max(end - line_start, relative_start)
    before = line[max(0, relative_start - 32) : relative_start]
    after = line[relative_end : min(len(line), relative_end + 32)]
    window = f"{before}{line[relative_start:relative_end]}{after}".lower()
    return any(marker in window for marker in _NEGATED_FORBIDDEN_PATTERN_MARKERS)


def _forbidden_pattern_match_kinds(
    content: str,
    pattern: timeout_regex.Pattern,
    *,
    timeout_seconds: float,
) -> Tuple[bool, bool]:
    matched = False
    for match in pattern.finditer(content, timeout=timeout_seconds):
        matched = True
        if not _forbidden_pattern_match_is_negated(content, match.start(), match.end()):
            return True, True
    return matched, False


def forbidden_pattern_findings(
    project_root: Path,
    requirement: dict,
    *,
    include_paths: Optional[Iterable[str]] = None,
    current_spec: Optional[Path] = None,
    _scan_files: Optional[List[Tuple[str, str]]] = None,
    _cache: Optional[RequirementsAuditCache] = None,
    _pattern_timeout_ms: int = 250,
    _deadline: Optional[float] = None,
    _metrics: Optional[Dict[str, object]] = None,
    _content_hashes: Optional[Dict[str, str]] = None,
) -> List[dict]:
    status = str(requirement.get("status", "active")).strip()
    if status != "active":
        return []
    patterns = requirement.get("forbidden_patterns", [])
    if not isinstance(patterns, list) or not patterns:
        return []
    findings: List[dict] = []
    compiled: List[Tuple[str, timeout_regex.Pattern]] = []
    for index, raw_value in enumerate(patterns):
        raw = str(raw_value)
        safety_reason = forbidden_pattern_definition_reason(raw)
        if safety_reason:
            return [
                _forbidden_pattern_runtime_finding(
                    requirement,
                    raw,
                    path=".auto-agents/state/requirements_trace.json",
                    kind="forbidden_pattern_safety",
                    reason=safety_reason,
                )
            ]
        compiled.append((raw, timeout_regex.compile(raw)))
    if not compiled:
        return findings
    if _metrics is not None:
        _metrics["patterns"] = int(_metrics.get("patterns", 0)) + len(compiled)
    scan_files = _scan_files
    if scan_files is None:
        scan_files = _forbidden_pattern_scan_files(
            project_root,
            include_paths=include_paths,
        )
    current_spec_rel = _current_spec_relpath(project_root, current_spec)
    pattern_set_hash = hashlib.sha256(
        json.dumps([raw for raw, _ in compiled], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    for rel, content in scan_files:
        if _deadline is not None and time.monotonic() >= _deadline:
            return findings + [
                _forbidden_pattern_runtime_finding(
                    requirement,
                    "",
                    path=rel,
                    kind="forbidden_pattern_total_timeout",
                    reason="requirements forbidden-pattern audit exceeded its total time limit",
                )
            ]
        content_sha256 = (
            _content_hashes.get(rel, "") if _content_hashes is not None else ""
        ) or hashlib.sha256(content.encode("utf-8")).hexdigest()
        cached = _cache.get(pattern_set_hash, rel, content_sha256) if _cache else None
        if cached is not None:
            matched_indexes, non_negated_indexes = (set(cached[0]), set(cached[1]))
            if _metrics is not None:
                _metrics["cache_hits"] = int(_metrics.get("cache_hits", 0)) + 1
        else:
            if _metrics is not None:
                _metrics["cache_misses"] = int(_metrics.get("cache_misses", 0)) + 1
            matched_indexes = set()
            non_negated_indexes = set()
            try:
                for pattern_index, (_, pattern) in enumerate(compiled):
                    if _deadline is not None and time.monotonic() >= _deadline:
                        raise _ForbiddenPatternTotalTimeout
                    if _metrics is not None:
                        _metrics["matcher_calls"] = int(_metrics.get("matcher_calls", 0)) + 1
                    matched, non_negated = _forbidden_pattern_match_kinds(
                        content,
                        pattern,
                        timeout_seconds=max(0.001, _pattern_timeout_ms / 1000.0),
                    )
                    if matched:
                        matched_indexes.add(pattern_index)
                    if non_negated:
                        non_negated_indexes.add(pattern_index)
            except TimeoutError:
                raw = compiled[pattern_index][0]
                return findings + [
                    _forbidden_pattern_runtime_finding(
                        requirement,
                        raw,
                        path=rel,
                        kind="forbidden_pattern_timeout",
                        reason=f"pattern exceeded {_pattern_timeout_ms}ms for one file",
                    )
                ]
            except _ForbiddenPatternTotalTimeout:
                return findings + [
                    _forbidden_pattern_runtime_finding(
                        requirement,
                        "",
                        path=rel,
                        kind="forbidden_pattern_total_timeout",
                        reason="requirements forbidden-pattern audit exceeded its total time limit",
                    )
                ]
            if _cache:
                _cache.put(
                    pattern_set_hash,
                    rel,
                    content_sha256,
                    sorted(matched_indexes),
                    sorted(non_negated_indexes),
                )
        for pattern_index, (raw, _) in enumerate(compiled):
            authoritative = not _forbidden_pattern_corroboration_only_path(
                rel, current_spec_rel
            )
            matching_indexes = (
                non_negated_indexes
                if authoritative and rel == current_spec_rel
                else matched_indexes
            )
            if pattern_index not in matching_indexes:
                continue
            findings.append(
                {
                    "kind": "forbidden_pattern",
                    "message": f"forbidden pattern '{raw}' found in {rel}",
                    "pattern": raw,
                    "path": rel,
                    "authoritative": authoritative,
                }
            )
    return findings


class _ForbiddenPatternTotalTimeout(Exception):
    pass


def _forbidden_pattern_safety_reason(pattern: str) -> str:
    if len(pattern) > 1024:
        return "pattern exceeds the 1024-character safety limit"
    dotall = "(?s" in pattern.lower()
    if dotall and re.search(r"(?<!\\)\.\s*[*+]", pattern):
        return "DOTALL combined with an unbounded wildcard is unsafe"
    if re.search(r"\((?:[^()\\]|\\.)*[*+](?:[^()\\]|\\.)*\)\s*[*+]", pattern):
        return "nested unbounded quantifiers are unsafe"
    if re.search(r"(?:\.\*|\.\+|\[[^\]]+\][*+])(?:[^|]{0,80})(?:\.\*|\.\+)", pattern):
        return "multiple unbounded wildcard spans are unsafe; use bounded spans such as [\\s\\S]{0,N}?"
    return ""


def _forbidden_pattern_runtime_finding(
    requirement: dict,
    pattern: str,
    *,
    path: str,
    kind: str,
    reason: str,
) -> dict:
    req_id = str(requirement.get("id", "")).strip() or "(unknown requirement)"
    literal = f" '{pattern}'" if pattern else ""
    return {
        "kind": kind,
        "requirement_id": req_id,
        "message": (
            f"forbidden-pattern audit stopped for {req_id}: pattern{literal} at {path}: {reason}. "
            "Replace broad wildcards with bounded spans such as [\\s\\S]{0,500}? and rerun."
        ),
        "pattern": pattern,
        "reason": reason,
        "path": path,
        "authoritative": True,
    }


def _forbidden_pattern_scan_files(
    project_root: Path,
    *,
    include_paths: Optional[Iterable[str]] = None,
    _deadline: Optional[float] = None,
) -> List[Tuple[str, str]]:
    """Read the forbidden-pattern corpus once for a complete audit.

    A requirements audit may contain hundreds of requirements. Walking and
    reading the repository once per requirement made every task baseline take
    many minutes on large projects. Callers can now reuse this immutable
    corpus while preserving the existing per-requirement matching semantics.
    """
    ignored_dirs = {
        ".git",
        ".auto-agents/history",
        ".auto-agents/runs",
        # Agent conversation transcripts and per-session scratchpads. These are internal
        # working memory (like history/ and runs/), not product source-of-truth or planner
        # decisions, and they routinely quote requirement language (including the forbidden
        # concepts they were told to remove). Scanning them produces false-positive forbidden
        # pattern hits that the pipeline cannot resolve because it must not rewrite past
        # session logs.
        ".auto-agents/state/sessions",
        ".conda",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".data",
        ".next",
        ".tmp",
        ".tmp-tests",
        "build",
        "dist",
    }
    ignored_files = {
        ".auto-agents/state/gate_baseline_cache.json",
        ".auto-agents/state/requirements_trace.json",
        ".auto-agents/state/provider_references.lock.json",
        ".auto-agents/docs/requirements_audit.md",
        ".auto-agents/docs/review.md",
    }
    included = {
        str(path).strip().replace("\\", "/")
        for path in (include_paths or [])
        if str(path).strip()
    }
    scan_files: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(project_root):
        if _deadline is not None and time.monotonic() >= _deadline:
            raise _ForbiddenPatternTotalTimeout
        rel_root = str(Path(root).relative_to(project_root)).replace("\\", "/")
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in ignored_dirs
            and f"{'' if rel_root == '.' else rel_root + '/'}{directory}" not in ignored_dirs
        ]
        for filename in files:
            if _deadline is not None and time.monotonic() >= _deadline:
                raise _ForbiddenPatternTotalTimeout
            path = Path(root) / filename
            rel = str(path.relative_to(project_root)).replace("\\", "/")
            if rel in ignored_files:
                continue
            if included and rel not in included:
                continue
            if path.suffix.lower() not in {".py", ".md", ".json", ".toml", ".yaml", ".yml", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if rel in {
                ".auto-agents/state/task_plan.json",
                ".auto-agents/state/run_state.json",
            }:
                content = _state_payload_for_forbidden_pattern_scan(content)
            scan_files.append((rel, content))
    return scan_files


def _state_payload_for_forbidden_pattern_scan(content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content

    lines: List[str] = []

    def collect_task(task: dict) -> None:
        for key in (
            "task_id",
            "title",
            "description",
            "scope_boundaries",
            "review_summary",
            "block_reason",
        ):
            value = task.get(key)
            if isinstance(value, str):
                lines.append(value)
        for key in ("expected_test_migrations", "verification_refs"):
            value = task.get(key)
            if isinstance(value, list):
                lines.extend(str(item) for item in value if isinstance(item, str))
        for history_key in ("review_history", "recovery_history"):
            history = task.get(history_key)
            if not isinstance(history, list):
                continue
            for item in history:
                if not isinstance(item, dict):
                    continue
                for value in item.values():
                    if isinstance(value, str):
                        lines.append(value)

    if isinstance(payload, dict):
        for key in ("last_error", "rejection_reason"):
            value = payload.get(key)
            if isinstance(value, str):
                lines.append(value)
        review_cache = payload.get("review_cache")
        if isinstance(review_cache, dict):
            for value in review_cache.values():
                if isinstance(value, dict):
                    summary = value.get("summary")
                    if isinstance(summary, str):
                        lines.append(summary)
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            for task in tasks:
                if isinstance(task, dict):
                    collect_task(task)
    return "\n".join(lines)


def _forbidden_pattern_findings(
    project_root: Path,
    requirement: dict,
    current_spec: Optional[Path] = None,
) -> List[dict]:
    return forbidden_pattern_findings(project_root, requirement, current_spec=current_spec)


def write_provider_reference_lock(project_root: Path, payload: dict) -> None:
    write_json(provider_references_lock_path(project_root), payload)
