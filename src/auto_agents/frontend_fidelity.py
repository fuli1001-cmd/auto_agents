from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Sequence


FRONTEND_SURFACES_FIELD = "frontend_surfaces"

_FRONTEND_TERMS = (
    "frontend",
    "front-end",
    "ui",
    "web",
    "page",
    "screen",
    "route",
    "browser",
    "前端",
    "页面",
    "界面",
    "视图",
    "首页",
)
_PROTOTYPE_TERMS = (
    "prototype",
    "mockup",
    "wireframe",
    "figma",
    "screenshot",
    "snapshot",
    ".html",
    ".htm",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    "原型",
    "截图",
    "视觉稿",
    "设计稿",
)
_VISUAL_TERMS = (
    "visual",
    "style",
    "css",
    "dom",
    "layout",
    "viewport",
    "pixel",
    "playwright",
    "storybook",
    "runway",
    "视觉",
    "样式",
    "布局",
    "颜色",
)
_VISUAL_EVIDENCE_TERMS = (
    "playwright",
    "prototype",
    "screenshot",
    "snapshot",
    "visual",
    "vision",
    "judge",
    "pixel",
    "css",
    "dom",
    "e2e",
    "storybook",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    "截图",
    "视觉",
    "样式",
    "布局",
)
_PRESERVATION_MARKERS = (
    "preserve",
    "remain unchanged",
    "must not change",
    "do not change",
    "without changing",
    "no redesign",
    "保持",
    "保留",
    "不得改变",
    "不改变",
    "不得修改或重设计",
    "不修改或重设计",
    "不得修改",
    "不修改",
    "不得引入",
    "不引入",
    "不新增",
    "不得新增",
    "不重设计",
    "不得重设计",
    "不回退",
)
_NEGATED_CHANGE_MARKERS = (
    "must not change",
    "do not change",
    "without changing",
    "no redesign",
    "不得改变",
    "不改变",
    "不得修改或重设计",
    "不修改或重设计",
    "不得修改",
    "不修改",
    "不得引入",
    "不引入",
    "不新增",
    "不得新增",
    "不重设计",
    "不得重设计",
)
_POSITIVE_CHANGE_MARKERS = (
    "add ",
    "introduce ",
    "new interaction",
    "new ui",
    "redesign",
    "modify the ui",
    "modify the design",
    "modify the layout",
    "change the ui",
    "change the design",
    "change the layout",
    "replace ",
    "新增",
    "添加",
    "引入",
    "重设计",
    "改版",
    "修改",
    "改变",
    "替换",
)


def frontend_prototype_signals_from_text(text: str) -> List[str]:
    """Return broad signals that a spec is asking for frontend prototype fidelity."""

    lowered = str(text or "").lower()
    if not lowered:
        return []

    has_frontend = any(term in lowered for term in _FRONTEND_TERMS)
    has_prototype = any(term in lowered for term in _PROTOTYPE_TERMS)
    if not (has_frontend and has_prototype):
        return []

    signals: List[str] = []
    for term in (*_FRONTEND_TERMS, *_PROTOTYPE_TERMS):
        if term in lowered and term not in signals:
            signals.append(term)
        if len(signals) >= 8:
            break
    return signals


def trace_frontend_surfaces(trace_payload: object) -> List[Mapping[str, object]]:
    if not isinstance(trace_payload, Mapping):
        return []
    raw = trace_payload.get(FRONTEND_SURFACES_FIELD)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def frontend_surface_requirement_ids(trace_payload: object) -> List[str]:
    ids: List[str] = []
    for surface in trace_frontend_surfaces(trace_payload):
        raw_ids = surface.get("requirement_ids")
        if not isinstance(raw_ids, list):
            continue
        for item in raw_ids:
            req_id = str(item).strip()
            if req_id and req_id not in ids:
                ids.append(req_id)
    return ids


def requirement_is_frontend_fidelity(requirement: object) -> bool:
    if not isinstance(requirement, Mapping):
        return False
    if requirement.get("frontend_surface") is True:
        return True
    notes = str(requirement.get("notes", "")).lower()
    if "frontend_surface" in notes:
        return True
    fields: List[str] = [
        str(requirement.get("id", "")),
        str(requirement.get("text", "")),
        str(requirement.get("source", "")),
        str(requirement.get("oracle_type", "")),
        str(requirement.get("oracle_strength", "")),
        str(requirement.get("evidence_boundary", "")),
        str(requirement.get("notes", "")),
    ]
    for oracle in requirement.get("acceptance_oracles") or []:
        fields.append(str(oracle))
    combined = " ".join(fields).lower()
    has_frontend = any(term in combined for term in _FRONTEND_TERMS)
    has_prototype = any(term in combined for term in _PROTOTYPE_TERMS)
    has_visual = any(term in combined for term in _VISUAL_TERMS)
    return has_prototype and (has_frontend or has_visual)


def requirement_is_frontend_preservation_only(requirement: object) -> bool:
    if not isinstance(requirement, Mapping):
        return False
    fields = [
        str(requirement.get("text", "")),
        str(requirement.get("notes", "")),
    ]
    fields.extend(str(item) for item in requirement.get("acceptance_oracles") or [])
    combined = " ".join(fields).lower()
    if not any(marker in combined for marker in _PRESERVATION_MARKERS):
        return False
    change_scan = combined
    for marker in _NEGATED_CHANGE_MARKERS:
        change_scan = change_scan.replace(marker, "")
    for marker in (
        "新增",
        "添加",
        "引入",
        "重设计",
        "改版",
        "修改",
        "改变",
        "替换",
    ):
        change_scan = re.sub(
            rf"(?:不|不得)[^，。；.!?]{{0,12}}{re.escape(marker)}",
            "",
            change_scan,
        )
    return not any(marker in change_scan for marker in _POSITIVE_CHANGE_MARKERS)


def frontend_requirement_ids_are_preservation_only(
    trace_payload: object,
    requirement_ids: Iterable[object],
) -> bool:
    if not isinstance(trace_payload, Mapping):
        return False
    required = {
        str(item).strip()
        for item in requirement_ids
        if str(item).strip()
    }
    if not required:
        return False
    raw_requirements = trace_payload.get("requirements")
    requirements = {
        str(item.get("id", "")).strip(): item
        for item in raw_requirements
        if isinstance(item, Mapping) and str(item.get("id", "")).strip()
    } if isinstance(raw_requirements, list) else {}
    return all(
        requirement_id in requirements
        and requirement_is_frontend_preservation_only(
            requirements[requirement_id]
        )
        for requirement_id in required
    )


def preservation_only_frontend_requirement_ids(trace_payload: object) -> List[str]:
    """Return visual contracts that must not create preservation-only iteration work."""

    if not isinstance(trace_payload, Mapping):
        return []
    frontend_scope = trace_payload.get("frontend_scope")
    if not (
        isinstance(frontend_scope, Mapping)
        and frontend_scope.get("requested") is False
    ):
        return []
    requirements = trace_payload.get("requirements")
    if not isinstance(requirements, list):
        return []
    return [
        str(requirement.get("id", "")).strip()
        for requirement in requirements
        if isinstance(requirement, Mapping)
        and requirement.get("status") == "active"
        and requirement.get("priority") == "mandatory"
        and str(requirement.get("id", "")).strip()
        and requirement_is_frontend_fidelity(requirement)
        and requirement_is_frontend_preservation_only(requirement)
    ]


def _requirements_by_id(trace_payload: object) -> Mapping[str, Mapping[str, object]]:
    if not isinstance(trace_payload, Mapping):
        return {}
    requirements = trace_payload.get("requirements")
    if not isinstance(requirements, list):
        return {}
    return {
        str(requirement.get("id", "")).strip(): requirement
        for requirement in requirements
        if isinstance(requirement, Mapping)
        and str(requirement.get("id", "")).strip()
    }


def _frontend_requirement_contract_changed(
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> bool:
    previous_hash = str(previous.get("contract_sha256", "")).strip()
    current_hash = str(current.get("contract_sha256", "")).strip()
    if previous_hash and current_hash:
        return previous_hash != current_hash
    contract_fields = (
        "text",
        "source",
        "priority",
        "acceptance_oracles",
        "oracle_type",
        "oracle_strength",
        "evidence_boundary",
        "forbidden_proxy_oracles",
        "forbidden_patterns",
        "external_docs_required",
        "provider_reference",
        "provider_references",
    )
    return any(previous.get(field) != current.get(field) for field in contract_fields)


def frontend_fidelity_requirement_ids(trace_payload: object) -> List[str]:
    if not isinstance(trace_payload, Mapping):
        return []
    requirements = trace_payload.get("requirements") if isinstance(trace_payload.get("requirements"), list) else []
    known_ids = {
        str(item.get("id", "")).strip()
        for item in requirements
        if isinstance(item, Mapping)
    }

    explicit_ids = [
        req_id
        for req_id in frontend_surface_requirement_ids(trace_payload)
        if req_id in known_ids
    ]
    tagged_ids = [
        str(item.get("id", "")).strip()
        for item in requirements
        if isinstance(item, Mapping)
        and (
            item.get("frontend_surface") is True
            or "frontend_surface" in str(item.get("notes", "")).lower()
        )
        and str(item.get("id", "")).strip()
    ]
    scoped_ids: List[str] = []
    for req_id in (*explicit_ids, *tagged_ids):
        if req_id and req_id not in scoped_ids:
            scoped_ids.append(req_id)
    if scoped_ids:
        return scoped_ids

    # Legacy fallback for traces that predate frontend_surfaces.requirement_ids.
    return [
        str(item.get("id", "")).strip()
        for item in requirements
        if isinstance(item, Mapping)
        and item.get("status") == "active"
        and item.get("priority") == "mandatory"
        and requirement_is_frontend_fidelity(item)
        and str(item.get("id", "")).strip()
    ]


def validate_frontend_fidelity_trace(
    trace_payload: object,
    *,
    spec_text: str = "",
    previous_trace: object = None,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(trace_payload, Mapping):
        return errors

    signals = frontend_prototype_signals_from_text(spec_text)
    raw_surfaces = trace_payload.get(FRONTEND_SURFACES_FIELD)
    surfaces = trace_frontend_surfaces(trace_payload)
    requirements = trace_payload.get("requirements") if isinstance(trace_payload.get("requirements"), list) else []
    scoped_fidelity_ids = set(frontend_fidelity_requirement_ids(trace_payload))
    active_fidelity_requirements = [
        item
        for item in requirements
        if isinstance(item, Mapping)
        and item.get("status") == "active"
        and item.get("priority") == "mandatory"
        and str(item.get("id", "")).strip() in scoped_fidelity_ids
    ]

    frontend_scope = trace_payload.get("frontend_scope")
    explicit_no_frontend_work = (
        isinstance(frontend_scope, Mapping)
        and frontend_scope.get("requested") is False
    )
    pending_generated_prototype = (
        isinstance(frontend_scope, Mapping)
        and frontend_scope.get("requested") is True
        and isinstance(frontend_scope.get("surfaces"), list)
        and bool(frontend_scope.get("surfaces"))
    )

    if explicit_no_frontend_work and previous_trace is not None:
        previous_requirements = _requirements_by_id(previous_trace)
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                continue
            if requirement.get("status") != "active":
                continue
            if not requirement_is_frontend_fidelity(requirement):
                continue
            requirement_id = str(requirement.get("id", "")).strip()
            previous_requirement = previous_requirements.get(requirement_id)
            if previous_requirement is None:
                errors.append(
                    f"frontend_scope.requested=false forbids introducing active frontend "
                    f"fidelity requirement {requirement_id or '<unknown>'}; preserve historical "
                    "contracts unchanged and keep regression checks in affected or release verification."
                )
            elif _frontend_requirement_contract_changed(
                previous_requirement,
                requirement,
            ):
                errors.append(
                    f"frontend_scope.requested=false forbids changing frontend fidelity "
                    f"requirement {requirement_id}; restore its previous contract."
                )

    if (
        signals
        and not surfaces
        and not pending_generated_prototype
        and not explicit_no_frontend_work
    ):
        preview = ", ".join(signals[:5])
        errors.append(
            "input spec appears to require frontend prototype fidelity "
            f"({preview}); requirements_trace.json must define a non-empty "
            f"{FRONTEND_SURFACES_FIELD} array with prototype_refs/viewports and active mandatory "
            "requirements whose acceptance_oracles preserve the page-level visual contract."
        )

    if raw_surfaces is not None and not isinstance(raw_surfaces, list):
        errors.append(f"{FRONTEND_SURFACES_FIELD} must be an array when present.")

    for index, surface in enumerate(surfaces, start=1):
        name = str(surface.get("name", "")).strip()
        prototype_refs = surface.get("prototype_refs")
        viewports = surface.get("viewports")
        if not name:
            errors.append(f"{FRONTEND_SURFACES_FIELD}[{index}] must include a non-empty name.")
        if not isinstance(prototype_refs, list) or not any(str(ref).strip() for ref in prototype_refs):
            errors.append(
                f"{FRONTEND_SURFACES_FIELD}[{index}] must include non-empty prototype_refs "
                "pointing to the source prototype, screenshot, or design artifact."
            )
        if viewports is not None and not isinstance(viewports, list):
            errors.append(f"{FRONTEND_SURFACES_FIELD}[{index}].viewports must be an array when present.")

    if surfaces and not active_fidelity_requirements:
        errors.append(
            f"{FRONTEND_SURFACES_FIELD} is present, but no active mandatory requirement is tagged "
            "or worded as a frontend/prototype visual fidelity contract."
        )

    return errors


def validate_frontend_fidelity_task_plan(
    plan_payload: object,
    trace_payload: object,
    *,
    historical_tasks: Iterable[dict] = (),
) -> List[str]:
    if not isinstance(plan_payload, Mapping) or not isinstance(trace_payload, Mapping):
        return []

    errors: List[str] = []
    preservation_only_ids = set(
        preservation_only_frontend_requirement_ids(trace_payload)
    )
    current_tasks = plan_payload.get("tasks")
    if preservation_only_ids and isinstance(current_tasks, list):
        for index, task in enumerate(current_tasks, start=1):
            if not isinstance(task, Mapping):
                continue
            if str(task.get("status", "pending")).strip() == "done":
                continue
            task_id = str(task.get("task_id", f"#{index}")).strip()
            requirement_ids = {
                str(item).strip()
                for item in task.get("requirement_ids", []) or []
                if isinstance(item, str) and item.strip()
            }
            invalid_ids = sorted(requirement_ids & preservation_only_ids)
            if invalid_ids:
                errors.append(
                    f"task {task_id} binds preservation-only frontend requirements while "
                    "frontend_scope.requested=false: "
                    + ", ".join(invalid_ids)
                    + ". Do not create standalone implementation or proof-rebinding work; "
                    "run relevant regressions only as affected or release verification."
                )

    if not trace_frontend_surfaces(trace_payload):
        return errors

    requirements = trace_payload.get("requirements") if isinstance(trace_payload.get("requirements"), list) else []
    scoped_fidelity_ids = set(frontend_fidelity_requirement_ids(trace_payload))
    required_ids = [
        str(item.get("id", "")).strip()
        for item in requirements
        if isinstance(item, Mapping)
        and item.get("status") == "active"
        and item.get("priority") == "mandatory"
        and str(item.get("id", "")).strip() in scoped_fidelity_ids
        and str(item.get("id", "")).strip()
    ]
    if not required_ids:
        return []

    tasks: List[Mapping[str, object]] = []
    current_tasks = plan_payload.get("tasks")
    if isinstance(current_tasks, list):
        tasks.extend(item for item in current_tasks if isinstance(item, Mapping))
    tasks.extend(item for item in historical_tasks if isinstance(item, Mapping))

    for req_id in required_ids:
        proofs = _task_proofs_for_requirement(tasks, req_id)
        if not proofs:
            continue
        if not any(_proof_has_visual_evidence(proof) for _task, proof in proofs):
            errors.append(
                f"frontend prototype fidelity requirement {req_id} needs page-level visual evidence "
                "(for example DOM/CSS checks plus Playwright screenshots or an optional vision judge); "
                "payload-only or internal-state tests are not sufficient."
            )
    return errors


def _task_proofs_for_requirement(
    tasks: Sequence[Mapping[str, object]], req_id: str
) -> List[tuple[Mapping[str, object], Mapping[str, object]]]:
    matches: List[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for task in tasks:
        proofs = task.get("requirement_proofs")
        if not isinstance(proofs, list):
            continue
        for proof in proofs:
            if not isinstance(proof, Mapping):
                continue
            if str(proof.get("requirement_id", "")).strip() == req_id:
                matches.append((task, proof))
    return matches


def _proof_has_visual_evidence(proof: Mapping[str, object]) -> bool:
    fields: List[str] = [
        str(proof.get("proof_type", "")),
        str(proof.get("oracle_strength", "")),
        str(proof.get("evidence_boundary", "")),
    ]
    for item in proof.get("evidence_refs") or []:
        fields.append(str(item))
    combined = " ".join(fields).lower()
    return any(term in combined for term in _VISUAL_EVIDENCE_TERMS)
