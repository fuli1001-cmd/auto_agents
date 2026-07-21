from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .frontend_fidelity import frontend_fidelity_requirement_ids, trace_frontend_surfaces
from .models import TaskSpec


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
NON_COMPARABLE_VISUAL_PURPOSES = {
    "layout_stability",
    "state_transition",
    "runtime_evidence",
    "dom_css_evidence",
    "evidence_only",
    "screenshot_evidence",
    "non_comparable",
    "none",
}
PROTOTYPE_COMPARISON_PURPOSES = {
    "prototype_fidelity",
    "prototype_comparison",
    "visual_fidelity",
    "visual_regression",
    "page_fidelity",
}
NON_COMPARABLE_PROOF_TERMS = (
    "layout stability",
    "layout is stable",
    "state transition",
    "state update",
    "transition screenshot",
    "no overflow",
    "no overlap",
    "no layout jump",
    "height stable",
    "stable height",
    "布局稳定",
    "状态更新",
    "状态切换",
    "阶段变化",
    "阶段文案更新",
    "高度稳定",
    "无文字溢出",
    "无溢出",
    "无元素重叠",
    "无重叠",
    "布局跳动",
    "文案溢出",
)
PROTOTYPE_COMPARISON_TERMS = (
    "prototype",
    "visual fidelity",
    "matches the prototype",
    "match prototype",
    "prototype match",
    "visual contract",
    "原型",
    "视觉一致",
    "视觉还原",
    "对齐原型",
    "匹配原型",
)


@dataclass
class VisualEvidencePair:
    requirement_id: str
    oracle_index: int
    surface: str
    viewport: str
    prototype_image_ref: str
    actual_image_ref: str
    prototype_source_ref: str = ""
    purpose: str = "prototype_fidelity"
    pair_id: str = ""
    proof_owners: List[Dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pair_id:
            self.pair_id = _visual_pair_id(self)
        if not self.proof_owners and self.requirement_id:
            self.proof_owners = [
                {
                    "requirement_id": self.requirement_id,
                    "oracle_index": self.oracle_index,
                }
            ]

    def add_owner(self, requirement_id: str, oracle_index: int) -> None:
        owner = {
            "requirement_id": requirement_id,
            "oracle_index": oracle_index,
        }
        if requirement_id and owner not in self.proof_owners:
            self.proof_owners.append(owner)

    def to_dict(self) -> Dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "requirement_id": self.requirement_id,
            "oracle_index": self.oracle_index,
            "proof_owners": list(self.proof_owners),
            "surface": self.surface,
            "viewport": self.viewport,
            "prototype_image_ref": self.prototype_image_ref,
            "actual_image_ref": self.actual_image_ref,
            "prototype_source_ref": self.prototype_source_ref,
            "purpose": self.purpose,
        }


@dataclass
class VisualEvidenceSelection:
    pairs: List[VisualEvidencePair] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)


@dataclass
class VisualJudgeReport:
    status: str
    score: int = 0
    threshold: int = 85
    provider: str = ""
    model: str = ""
    reason: str = ""
    findings: List[Dict[str, object]] = field(default_factory=list)
    pairs: List[Dict[str, object]] = field(default_factory=list)
    pair_results: List[Dict[str, object]] = field(default_factory=list)
    attempts: List[Dict[str, object]] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    report_path: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"passed", "skipped"}

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "score": self.score,
            "threshold": self.threshold,
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "findings": list(self.findings),
            "pairs": list(self.pairs),
            "pair_results": list(self.pair_results),
            "attempts": list(self.attempts),
            "diagnostics": list(self.diagnostics),
            "report_path": self.report_path,
        }


def task_needs_visual_judge(task: TaskSpec, trace_payload: object) -> bool:
    if not trace_frontend_surfaces(trace_payload):
        return False
    if not isinstance(trace_payload, Mapping):
        return False
    frontend_ids = set(frontend_fidelity_requirement_ids(trace_payload))
    if not frontend_ids:
        return False
    return any(str(req_id).strip() in frontend_ids for req_id in task.requirement_ids)


def collect_visual_evidence_for_task(
    task: TaskSpec,
    trace_payload: object,
    *,
    max_pairs: int,
) -> VisualEvidenceSelection:
    if not isinstance(trace_payload, Mapping) or not trace_frontend_surfaces(trace_payload):
        return VisualEvidenceSelection()

    selected: List[VisualEvidencePair] = []
    diagnostics: List[str] = []
    by_image_refs: Dict[Tuple[str, str], VisualEvidencePair] = {}
    metadata_by_image_refs: Dict[Tuple[str, str], Tuple[str, str, str]] = {}

    for proof_index, proof in enumerate(task.requirement_proofs, start=1):
        if not isinstance(proof, Mapping):
            continue
        raw_visual = proof.get("visual_evidence")
        if raw_visual is None:
            # evidence_refs may contain screenshots for runtime or state-transition proof.
            # They are intentionally never guessed into prototype-vs-actual pairs.
            continue
        pairs, proof_diagnostics = _explicit_visual_pairs(
            proof,
            raw_visual,
            prefix=f"requirement_proofs[{proof_index}].visual_evidence",
        )
        diagnostics.extend(proof_diagnostics)
        for pair in pairs:
            ref_key = _visual_pair_ref_key(pair)
            metadata = (
                pair.surface.strip().casefold(),
                pair.viewport.strip().casefold(),
                pair.purpose.strip().casefold(),
            )
            existing = by_image_refs.get(ref_key)
            if existing is None:
                by_image_refs[ref_key] = pair
                metadata_by_image_refs[ref_key] = metadata
                selected.append(pair)
                continue
            if metadata_by_image_refs[ref_key] != metadata:
                diagnostics.append(
                    f"visual pair {existing.pair_id} declares conflicting surface/viewport/purpose metadata"
                )
                continue
            existing.add_owner(pair.requirement_id, pair.oracle_index)

    return VisualEvidenceSelection(
        pairs=selected[: max(0, max_pairs)],
        diagnostics=diagnostics,
    )


def visual_evidence_pairs_for_task(
    task: TaskSpec,
    trace_payload: object,
    *,
    max_pairs: int,
) -> List[VisualEvidencePair]:
    return collect_visual_evidence_for_task(
        task,
        trace_payload,
        max_pairs=max_pairs,
    ).pairs


def visual_evidence_validation_errors(
    proof: Mapping[str, object],
    raw_visual: object,
    *,
    prefix: str = "visual_evidence",
) -> List[str]:
    _pairs, diagnostics = _explicit_visual_pairs(
        proof,
        raw_visual,
        prefix=prefix,
    )
    return diagnostics


def build_visual_judge_prompt(
    *,
    task: TaskSpec,
    pairs: Iterable[VisualEvidencePair],
    threshold: int,
) -> str:
    pair_dicts = []
    for index, pair in enumerate(pairs):
        payload = pair.to_dict()
        payload["prototype_attachment_index"] = index * 2 + 1
        payload["actual_attachment_index"] = index * 2 + 2
        pair_dicts.append(payload)
    return (
        "You are a visual fidelity judge for generated frontend pages.\n"
        "Compare each prototype screenshot only with its paired actual browser-rendered screenshot.\n"
        "Attachments are ordered exactly as declared by prototype_attachment_index and "
        "actual_attachment_index; never compare images from different pair_id values.\n"
        "Judge only same-state visual fidelity: layout, color, structure, typography, spacing, "
        "borders, radii, shadows, and visible prototype-extra or missing UI.\n"
        "Do not infer business-flow correctness from screenshots. Success/failure transitions, API "
        "behavior, navigation, and data updates are covered by deterministic tests.\n"
        "Do not review implementation code.\n\n"
        f"Task: {task.task_id} - {task.title}\n"
        f"Passing threshold per pair: {threshold}\n\n"
        "Evidence pairs:\n"
        f"{json.dumps(pair_dicts, ensure_ascii=False, indent=2)}\n\n"
        "Return ONLY a JSON object with this shape:\n"
        "{\n"
        "  \"status\": \"passed\" | \"failed\" | \"inconclusive\",\n"
        "  \"score\": 0,\n"
        "  \"pair_results\": [\n"
        "    {\"pair_id\": \"...\", \"status\": \"passed\" | \"failed\" | \"inconclusive\", "
        "\"score\": 0, \"findings\": [\n"
        "      {\"severity\": \"blocker\" | \"major\" | \"minor\", \"message\": \"...\"}\n"
        "    ]}\n"
        "  ],\n"
        "  \"summary\": \"short rationale\"\n"
        "}\n"
        "Return exactly one pair_results entry for every supplied pair_id. A pair fails only when "
        "its score is below the threshold or it has a blocker finding."
    )


def parse_visual_judge_response(
    text: str,
    *,
    threshold: int,
    expected_pair_ids: Optional[Sequence[str]] = None,
) -> VisualJudgeReport:
    payload = _extract_json_object(text)
    if payload is None:
        return VisualJudgeReport(
            status="inconclusive",
            threshold=threshold,
            reason="visual judge response did not contain a JSON object",
        )

    expected = [str(item).strip() for item in (expected_pair_ids or []) if str(item).strip()]
    raw_pair_results = payload.get("pair_results")
    pair_results: List[Dict[str, object]] = []
    diagnostics: List[str] = []

    if isinstance(raw_pair_results, list):
        seen_ids = set()
        for index, item in enumerate(raw_pair_results, start=1):
            if not isinstance(item, Mapping):
                diagnostics.append(f"pair_results[{index}] must be an object")
                continue
            pair_id = str(item.get("pair_id", "")).strip()
            if not pair_id:
                diagnostics.append(f"pair_results[{index}] is missing pair_id")
                continue
            if expected and pair_id not in expected:
                diagnostics.append(f"pair_results[{index}] references unknown pair_id {pair_id}")
                continue
            if pair_id in seen_ids:
                diagnostics.append(f"pair_results contains duplicate pair_id {pair_id}")
                continue
            seen_ids.add(pair_id)
            pair_results.append(_normalize_pair_result(item, pair_id=pair_id, threshold=threshold))
        missing = [pair_id for pair_id in expected if pair_id not in seen_ids]
        if missing:
            diagnostics.append("pair_results is missing pair_id values: " + ", ".join(missing))
    elif len(expected) <= 1:
        pair_id = expected[0] if expected else ""
        legacy = dict(payload)
        legacy["pair_id"] = pair_id
        pair_results.append(_normalize_pair_result(legacy, pair_id=pair_id, threshold=threshold))
    else:
        diagnostics.append("multi-pair visual judge response must include pair_results")

    if diagnostics or not pair_results:
        status = "inconclusive"
    elif any(item["status"] == "failed" for item in pair_results):
        status = "failed"
    elif any(item["status"] == "inconclusive" for item in pair_results):
        status = "inconclusive"
    else:
        status = "passed"

    scores = [int(item.get("score", 0)) for item in pair_results]
    findings: List[Dict[str, object]] = []
    for item in pair_results:
        pair_id = str(item.get("pair_id", "")).strip()
        for finding in item.get("findings", []) or []:
            if not isinstance(finding, dict):
                continue
            normalized = dict(finding)
            if pair_id:
                normalized["pair_id"] = pair_id
            findings.append(normalized)

    reason = str(payload.get("summary", "")).strip()
    if diagnostics:
        diagnostic_reason = "; ".join(diagnostics)
        reason = f"{reason}; {diagnostic_reason}" if reason else diagnostic_reason
    return VisualJudgeReport(
        status=status,
        score=min(scores) if scores else 0,
        threshold=threshold,
        reason=reason,
        findings=findings,
        pair_results=pair_results,
        diagnostics=diagnostics,
    )


def write_visual_judge_report(path: Path, report: VisualJudgeReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def visual_judge_failure_summary(report: VisualJudgeReport) -> str:
    if report.status == "skipped":
        return report.reason or "visual judge skipped"
    findings = []
    for item in report.findings[:5]:
        pair_id = str(item.get("pair_id", "")).strip()
        surface = str(item.get("surface", "")).strip()
        viewport = str(item.get("viewport", "")).strip()
        message = str(item.get("message", "")).strip()
        prefix = " / ".join(part for part in (pair_id, surface, viewport) if part)
        findings.append(f"{prefix}: {message}" if prefix else message)
    details = "; ".join(item for item in findings if item)
    base = f"visual judge {report.status} with score {report.score}/{report.threshold}"
    if details:
        return f"{base}: {details}"
    return f"{base}: {report.reason}" if report.reason else base


def _explicit_visual_pairs(
    proof: Mapping[str, object],
    raw_visual: object,
    *,
    prefix: str,
) -> Tuple[List[VisualEvidencePair], List[str]]:
    entries: List[object]
    if isinstance(raw_visual, Mapping):
        entries = [raw_visual]
    elif isinstance(raw_visual, list):
        entries = list(raw_visual)
    else:
        return [], [f"{prefix} must be an object or list of objects"]

    pairs: List[VisualEvidencePair] = []
    diagnostics: List[str] = []
    for index, raw_entry in enumerate(entries, start=1):
        entry_prefix = prefix if len(entries) == 1 else f"{prefix}[{index}]"
        if not isinstance(raw_entry, Mapping):
            diagnostics.append(f"{entry_prefix} must be an object")
            continue
        entry = raw_entry
        declared_purpose = _visual_evidence_purpose(entry)
        if declared_purpose and declared_purpose not in (
            PROTOTYPE_COMPARISON_PURPOSES | NON_COMPARABLE_VISUAL_PURPOSES
        ):
            diagnostics.append(
                f"{entry_prefix}.purpose has unsupported value {declared_purpose!r}"
            )
            continue
        if _visual_evidence_entry_is_non_comparable(proof, entry):
            continue
        prototype = str(entry.get("prototype_image_ref", "")).strip()
        actual = str(entry.get("actual_image_ref", "")).strip()
        if not prototype:
            diagnostics.append(f"{entry_prefix}.prototype_image_ref must be a non-empty image path")
        elif not _is_image_ref(prototype):
            diagnostics.append(
                f"{entry_prefix}.prototype_image_ref must reference a PNG, JPEG, or WebP screenshot; "
                "put HTML in prototype_source_ref"
            )
        if not actual:
            diagnostics.append(f"{entry_prefix}.actual_image_ref must be a non-empty image path")
        elif not _is_image_ref(actual):
            diagnostics.append(
                f"{entry_prefix}.actual_image_ref must reference a PNG, JPEG, or WebP screenshot"
            )
        if prototype and actual and _normalize_image_ref(prototype) == _normalize_image_ref(actual):
            diagnostics.append(f"{entry_prefix} must use distinct prototype and actual screenshots")
        if (
            not prototype
            or not actual
            or not _is_image_ref(prototype)
            or not _is_image_ref(actual)
            or _normalize_image_ref(prototype) == _normalize_image_ref(actual)
        ):
            continue
        purpose = declared_purpose or "prototype_fidelity"
        pairs.append(
            VisualEvidencePair(
                requirement_id=str(proof.get("requirement_id", "")).strip(),
                oracle_index=int(proof.get("oracle_index", 0) or 0),
                surface=str(entry.get("surface", "")).strip()
                or str(proof.get("requirement_id", "")).strip(),
                viewport=str(entry.get("viewport", "")).strip() or "unspecified",
                prototype_image_ref=prototype,
                actual_image_ref=actual,
                prototype_source_ref=str(entry.get("prototype_source_ref", "")).strip(),
                purpose=purpose,
            )
        )
    return pairs, diagnostics


def _normalize_pair_result(
    item: Mapping[str, object],
    *,
    pair_id: str,
    threshold: int,
) -> Dict[str, object]:
    raw_score = item.get("score")
    try:
        score = int(raw_score)
    except (TypeError, ValueError):
        score = -1
    findings = [entry for entry in item.get("findings", []) or [] if isinstance(entry, dict)]
    has_blocker = any(
        str(entry.get("severity", "")).strip().lower() == "blocker"
        for entry in findings
    )
    declared_status = str(item.get("status", "")).strip().lower()
    if score < 0 or score > 100 or declared_status not in {"passed", "failed", "inconclusive"}:
        status = "inconclusive"
        score = max(0, min(100, score))
    elif declared_status == "inconclusive":
        status = "inconclusive"
    elif score < threshold or has_blocker:
        status = "failed"
    elif declared_status == "passed":
        status = "passed"
    else:
        # A declared failure without a below-threshold score or blocker is not a
        # confirmed visual mismatch and must be isolated/retried.
        status = "inconclusive"
    return {
        "pair_id": pair_id,
        "status": status,
        "score": score,
        "findings": findings,
    }


def _visual_evidence_purpose(entry: Mapping[str, object]) -> str:
    for key in ("purpose", "evidence_kind", "comparison_type", "visual_judge_purpose"):
        value = str(entry.get(key, "")).strip().lower()
        if value:
            return value
    return ""


def _visual_evidence_entry_is_non_comparable(
    proof: Mapping[str, object],
    entry: Mapping[str, object],
) -> bool:
    explicit_judge = entry.get("visual_judge", entry.get("judge"))
    if isinstance(explicit_judge, bool) and not explicit_judge:
        return True

    purpose = _visual_evidence_purpose(entry)
    if purpose in PROTOTYPE_COMPARISON_PURPOSES:
        return False
    if purpose in NON_COMPARABLE_VISUAL_PURPOSES:
        return True

    return _proof_oracle_indicates_non_comparable_visual_evidence(proof)


def _proof_oracle_indicates_non_comparable_visual_evidence(proof: Mapping[str, object]) -> bool:
    fields = [
        str(proof.get("exact_acceptance_oracle", "")),
        str(proof.get("acceptance_oracle", "")),
        str(proof.get("proof_type", "")),
        str(proof.get("oracle_type", "")),
    ]
    for ref in proof.get("evidence_refs", []) or []:
        if isinstance(ref, str):
            fields.append(ref)
    text = " ".join(fields).lower()
    if any(term in text for term in PROTOTYPE_COMPARISON_TERMS):
        return False
    return any(term in text for term in NON_COMPARABLE_PROOF_TERMS)


def _extract_json_object(text: str) -> Optional[dict]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_image_ref(ref: str) -> bool:
    path = _image_ref_path(ref).lower()
    return Path(path).suffix in IMAGE_SUFFIXES


def _image_ref_path(ref: str) -> str:
    value = str(ref).strip()
    if "::" in value:
        value = value.split("::", 1)[0]
    return value.split("?", 1)[0].split("#", 1)[0]


def _normalize_image_ref(ref: str) -> str:
    return posixpath.normpath(_image_ref_path(ref).replace("\\", "/"))


def _visual_pair_ref_key(pair: VisualEvidencePair) -> Tuple[str, str]:
    return (
        _normalize_image_ref(pair.prototype_image_ref),
        _normalize_image_ref(pair.actual_image_ref),
    )


def _visual_pair_id(pair: VisualEvidencePair) -> str:
    payload = {
        "prototype": _normalize_image_ref(pair.prototype_image_ref),
        "actual": _normalize_image_ref(pair.actual_image_ref),
        "surface": pair.surface.strip().casefold(),
        "viewport": pair.viewport.strip().casefold(),
        "purpose": pair.purpose.strip().casefold(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"pair-{digest}"
