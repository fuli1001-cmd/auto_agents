from __future__ import annotations

import copy
import datetime as _dt
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import (
    provider_references_lock_path,
    requirements_audit_path,
    requirements_trace_path,
    task_plan_path,
)
from .io_utils import read_json, write_json, write_text
from .models import TaskSpec


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

    return normalized


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


def validate_requirements_trace_payload(payload: object) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["requirements trace root must be a JSON object"]

    version = payload.get("version")
    if not isinstance(version, int) or version < 1:
        errors.append("requirements trace version must be an integer >= 1")

    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        return errors + ["requirements trace must contain a 'requirements' list"]

    seen_ids = set()
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
        else:
            for pattern in forbidden:
                try:
                    re.compile(pattern)
                except re.error as error:
                    errors.append(f"{prefix} forbidden pattern is not valid regex: {pattern} ({error})")

        external_docs_required = item.get("external_docs_required", False)
        if not isinstance(external_docs_required, bool):
            errors.append(f"{prefix} external_docs_required must be a boolean")
        provider_reference = item.get("provider_reference", "")
        if not isinstance(provider_reference, str):
            errors.append(f"{prefix} provider_reference must be a string")
        if external_docs_required:
            if not provider_reference.strip():
                errors.append(
                    f"{prefix} provider_reference must be a non-empty string when external_docs_required is true"
                )

        notes = item.get("notes")
        if not isinstance(notes, str):
            errors.append(f"{prefix} notes must be a string")

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


def validate_task_requirement_coverage(plan_payload: object, trace_payload: dict) -> List[str]:
    errors: List[str] = []
    if not isinstance(plan_payload, dict):
        return errors
    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list):
        return errors

    known_ids = requirement_ids(trace_payload)
    mandatory_ids = mandatory_active_requirement_ids(trace_payload)
    if not known_ids:
        return errors

    covered_ids = set()
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", f"#{index}"))
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
    errors.extend(validate_task_requirement_proofs(plan_payload, trace_payload))
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


def validate_task_requirement_proofs(plan_payload: object, trace_payload: dict) -> List[str]:
    errors: List[str] = []
    if not isinstance(plan_payload, dict):
        return errors
    if not _plan_oracle_proof_schema_enabled(plan_payload):
        return errors
    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list):
        return errors

    by_req = {str(item.get("id", "")).strip(): item for item in requirement_records(trace_payload)}
    proofs_by_requirement: Dict[str, List[dict]] = {}
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", f"#{index}"))
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
                errors.append(f"{prefix} references unknown requirement_id: {req_id}")
                continue
            if req_id not in requirement_ids:
                errors.append(f"{prefix} requirement_id must also appear in task requirement_ids: {req_id}")
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
            require_verified = str(task.get("status", "")).strip() == "done"
            for message in _proof_contract_messages(
                by_req[req_id],
                proof,
                require_verified=require_verified,
                oracle=matched_oracle[1] if matched_oracle is not None else "",
            ):
                errors.append(f"{prefix} {message}")
            proofs_by_requirement.setdefault(req_id, []).append(proof)

    for req_id, requirement in by_req.items():
        if str(requirement.get("status", "active")).strip() != "active":
            continue
        if str(requirement.get("priority", "mandatory")).strip() != "mandatory":
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


def _proofs_for_requirement(tasks: Iterable[TaskSpec], requirement_id: str) -> List[Tuple[TaskSpec, dict]]:
    proofs: List[Tuple[TaskSpec, dict]] = []
    for task in tasks:
        if task.status != "done":
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
    if index in {zero_based_index, zero_based_index + 1}:
        return True
    return str(proof.get("acceptance_oracle", "")).strip() == oracle


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
            reference = str(item.get("provider_reference", "")).strip()
            lines.append(f"  External docs required: yes; provider reference: {reference or '(missing)'}")
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


def run_requirements_audit(project_root: Path, tasks: Iterable[TaskSpec]) -> dict:
    tasks = list(tasks)
    trace = load_requirements_trace(project_root)
    lock = load_provider_references_lock(project_root)
    oracle_proof_audit = _oracle_proof_audit_enabled(project_root, tasks)
    lines = [
        "# Requirements Audit",
        "",
        f"Generated at: {_dt.datetime.utcnow().replace(microsecond=0).isoformat()}Z",
        f"Oracle proof audit: {'strict' if oracle_proof_audit else 'legacy'}",
        "",
    ]
    ok = True
    issues: List[dict] = []
    task_requirements = set()
    for task in tasks:
        if task.status == "done":
            task_requirements.update(task.requirement_ids)

    for item in requirement_records(trace):
        req_id = str(item.get("id", "")).strip()
        status = str(item.get("status", "active")).strip()
        priority = str(item.get("priority", "mandatory")).strip()
        if not req_id:
            continue
        blockers: List[dict] = []
        if status == "active" and priority == "mandatory" and req_id not in task_requirements:
            blockers.append(
                {
                    "kind": "task_coverage",
                    "message": "not covered by any done task",
                }
            )
        if status == "active" and priority == "mandatory" and oracle_proof_audit:
            blockers.extend(_oracle_proof_findings(item, _proofs_for_requirement(tasks, req_id)))
        if status == "active" and bool(item.get("external_docs_required", False)):
            reference = str(item.get("provider_reference", "")).strip()
            ref_status = provider_reference_status(lock, reference)
            if ref_status not in PASSING_REFERENCE_STATUSES:
                blockers.append(
                    {
                        "kind": "provider_reference",
                        "message": f"provider reference is {ref_status}",
                        "reference": reference,
                        "reference_status": ref_status,
                    }
                )
        blockers.extend(_forbidden_pattern_findings(project_root, item))

        if blockers and status == "active" and priority == "mandatory":
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
            }
        )

        lines.append(f"## {req_id}: {result}")
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
            lines.extend(f"- {entry['message']}" for entry in blockers)
            lines.append("")

    if not requirement_records(trace):
        lines.append("No requirements are currently tracked.")
        lines.append("")

    lines.insert(2, f"Result: {'pass' if ok else 'fail'}")
    report = "\n".join(lines).rstrip() + "\n"
    write_text(requirements_audit_path(project_root), report)
    return {
        "ok": ok,
        "report": report,
        "issues": issues,
        "path": str(requirements_audit_path(project_root)),
    }


def audit_requirements(project_root: Path, tasks: Iterable[TaskSpec]) -> Tuple[bool, str]:
    result = run_requirements_audit(project_root, tasks)
    return bool(result["ok"]), str(result["report"])


def forbidden_pattern_findings(
    project_root: Path,
    requirement: dict,
    *,
    include_paths: Optional[Iterable[str]] = None,
) -> List[dict]:
    status = str(requirement.get("status", "active")).strip()
    if status != "active":
        return []
    patterns = requirement.get("forbidden_patterns", [])
    if not isinstance(patterns, list) or not patterns:
        return []
    findings: List[str] = []
    compiled = []
    for raw in patterns:
        try:
            compiled.append((str(raw), re.compile(str(raw))))
        except re.error:
            continue
    if not compiled:
        return findings
    ignored_dirs = {".git", ".auto-agents/runs", ".conda", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
    ignored_files = {
        ".auto-agents/state/requirements_trace.json",
        ".auto-agents/state/provider_references.lock.json",
        ".auto-agents/docs/requirements_audit.md",
    }
    included = {
        str(path).strip().replace("\\", "/")
        for path in (include_paths or [])
        if str(path).strip()
    }
    for root, dirs, files in os.walk(project_root):
        rel_root = str(Path(root).relative_to(project_root)).replace("\\", "/")
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in ignored_dirs
            and f"{'' if rel_root == '.' else rel_root + '/'}{directory}" not in ignored_dirs
        ]
        for filename in files:
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
            for raw, pattern in compiled:
                if pattern.search(content):
                    findings.append(
                        {
                            "kind": "forbidden_pattern",
                            "message": f"forbidden pattern '{raw}' found in {rel}",
                            "pattern": raw,
                            "path": rel,
                        }
                    )
    return findings


def _forbidden_pattern_findings(project_root: Path, requirement: dict) -> List[dict]:
    return forbidden_pattern_findings(project_root, requirement)


def write_provider_reference_lock(project_root: Path, payload: dict) -> None:
    write_json(provider_references_lock_path(project_root), payload)
