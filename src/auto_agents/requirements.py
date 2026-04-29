from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .config import (
    provider_references_lock_path,
    requirements_audit_path,
    requirements_trace_path,
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


def empty_requirements_trace() -> dict:
    return {"version": 1, "requirements": []}


def empty_provider_references_lock() -> dict:
    return {"version": 1, "references": {}}


def load_requirements_trace(project_root: Path) -> dict:
    payload = read_json(requirements_trace_path(project_root), default=None)
    if payload is None:
        return empty_requirements_trace()
    if isinstance(payload, dict):
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
    raw = trace_payload.get("requirements", [])
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
    return errors


def requirements_for_task(project_root: Path, task: TaskSpec) -> List[dict]:
    if not task.requirement_ids:
        return []
    trace = load_requirements_trace(project_root)
    by_id = {str(item.get("id", "")).strip(): item for item in requirement_records(trace)}
    return [by_id[req_id] for req_id in task.requirement_ids if req_id in by_id]


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
    trace = load_requirements_trace(project_root)
    lock = load_provider_references_lock(project_root)
    lines = [
        "# Requirements Audit",
        "",
        f"Generated at: {_dt.datetime.utcnow().replace(microsecond=0).isoformat()}Z",
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


def _forbidden_pattern_findings(project_root: Path, requirement: dict) -> List[dict]:
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


def write_provider_reference_lock(project_root: Path, payload: dict) -> None:
    write_json(provider_references_lock_path(project_root), payload)
