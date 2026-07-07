from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from .frontend_fidelity import requirement_is_frontend_fidelity, trace_frontend_surfaces
from .models import TaskSpec


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


@dataclass
class VisualEvidencePair:
    requirement_id: str
    oracle_index: int
    surface: str
    viewport: str
    prototype_image_ref: str
    actual_image_ref: str
    prototype_source_ref: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "oracle_index": self.oracle_index,
            "surface": self.surface,
            "viewport": self.viewport,
            "prototype_image_ref": self.prototype_image_ref,
            "actual_image_ref": self.actual_image_ref,
            "prototype_source_ref": self.prototype_source_ref,
        }


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
            "report_path": self.report_path,
        }


def task_needs_visual_judge(task: TaskSpec, trace_payload: object) -> bool:
    if not trace_frontend_surfaces(trace_payload):
        return False
    if not isinstance(trace_payload, Mapping):
        return False
    requirements = trace_payload.get("requirements")
    if not isinstance(requirements, list):
        return False
    frontend_ids = {
        str(item.get("id", "")).strip()
        for item in requirements
        if isinstance(item, Mapping) and requirement_is_frontend_fidelity(item)
    }
    if not frontend_ids:
        return False
    return any(str(req_id).strip() in frontend_ids for req_id in task.requirement_ids)


def visual_evidence_pairs_for_task(
    task: TaskSpec,
    trace_payload: object,
    *,
    max_pairs: int,
) -> List[VisualEvidencePair]:
    if not isinstance(trace_payload, Mapping):
        return []
    surfaces = trace_frontend_surfaces(trace_payload)
    if not surfaces:
        return []
    surface_by_name = {
        str(surface.get("name", "")).strip().lower(): surface
        for surface in surfaces
        if str(surface.get("name", "")).strip()
    }
    pairs: List[VisualEvidencePair] = []
    for proof in task.requirement_proofs:
        if not isinstance(proof, Mapping):
            continue
        raw_visual = proof.get("visual_evidence")
        explicit = _explicit_visual_pairs(proof, raw_visual)
        if explicit:
            pairs.extend(explicit)
        else:
            inferred = _infer_visual_pair(proof, surface_by_name)
            if inferred is not None:
                pairs.append(inferred)
        if len(pairs) >= max_pairs:
            return pairs[:max_pairs]
    return pairs[:max_pairs]


def build_visual_judge_prompt(
    *,
    task: TaskSpec,
    pairs: Iterable[VisualEvidencePair],
    threshold: int,
) -> str:
    pair_dicts = [pair.to_dict() for pair in pairs]
    return (
        "You are a visual fidelity judge for generated frontend pages.\n"
        "Compare each prototype screenshot with the actual browser-rendered screenshot.\n"
        "Use the attached image files and the JSON evidence below as the source of truth.\n"
        "Do not review implementation code. Judge only visual fidelity and obvious prototype-extra/missing UI.\n\n"
        f"Task: {task.task_id} - {task.title}\n"
        f"Passing threshold: {threshold}\n"
        "Rubric dimensions: overall layout, color theme, card/modal structure, typography, spacing, "
        "radius/borders/shadows, interaction state visibility when shown, responsive framing, and "
        "prototype-extra old workbench/debug/internal UI.\n\n"
        "Evidence pairs:\n"
        f"{json.dumps(pair_dicts, ensure_ascii=False, indent=2)}\n\n"
        "Return ONLY a JSON object with this shape:\n"
        "{\n"
        "  \"status\": \"passed\" | \"failed\" | \"inconclusive\",\n"
        "  \"score\": 0,\n"
        "  \"findings\": [\n"
        "    {\"severity\": \"blocker\" | \"major\" | \"minor\", \"surface\": \"...\", "
        "\"viewport\": \"...\", \"message\": \"...\"}\n"
        "  ],\n"
        "  \"summary\": \"short rationale\"\n"
        "}\n"
        "Use status=failed if score is below threshold or any blocker finding exists."
    )


def parse_visual_judge_response(text: str, *, threshold: int) -> VisualJudgeReport:
    payload = _extract_json_object(text)
    if payload is None:
        return VisualJudgeReport(
            status="failed",
            threshold=threshold,
            reason="visual judge response did not contain a JSON object",
        )
    status = str(payload.get("status", "")).strip().lower()
    if status not in {"passed", "failed", "inconclusive"}:
        status = "inconclusive"
    try:
        score = int(payload.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    findings = [
        item
        for item in payload.get("findings", [])
        if isinstance(item, dict)
    ]
    has_blocker = any(str(item.get("severity", "")).strip().lower() == "blocker" for item in findings)
    if status == "passed" and (score < threshold or has_blocker):
        status = "failed"
    if status == "inconclusive":
        status = "failed"
    return VisualJudgeReport(
        status=status,
        score=score,
        threshold=threshold,
        reason=str(payload.get("summary", "")).strip(),
        findings=findings,
    )


def write_visual_judge_report(path: Path, report: VisualJudgeReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def visual_judge_failure_summary(report: VisualJudgeReport) -> str:
    if report.status == "skipped":
        return report.reason or "visual judge skipped"
    findings = []
    for item in report.findings[:5]:
        surface = str(item.get("surface", "")).strip()
        viewport = str(item.get("viewport", "")).strip()
        message = str(item.get("message", "")).strip()
        prefix = " / ".join(part for part in (surface, viewport) if part)
        findings.append(f"{prefix}: {message}" if prefix else message)
    details = "; ".join(item for item in findings if item)
    base = f"visual judge {report.status} with score {report.score}/{report.threshold}"
    return f"{base}: {details}" if details else base


def _explicit_visual_pairs(proof: Mapping[str, object], raw_visual: object) -> List[VisualEvidencePair]:
    entries: List[Mapping[str, object]]
    if isinstance(raw_visual, Mapping):
        entries = [raw_visual]
    elif isinstance(raw_visual, list):
        entries = [item for item in raw_visual if isinstance(item, Mapping)]
    else:
        return []

    pairs: List[VisualEvidencePair] = []
    for entry in entries:
        prototype = str(entry.get("prototype_image_ref", "")).strip()
        actual = str(entry.get("actual_image_ref", "")).strip()
        if not prototype or not actual:
            continue
        pairs.append(
            VisualEvidencePair(
                requirement_id=str(proof.get("requirement_id", "")).strip(),
                oracle_index=int(proof.get("oracle_index", 0) or 0),
                surface=str(entry.get("surface", "")).strip() or str(proof.get("requirement_id", "")).strip(),
                viewport=str(entry.get("viewport", "")).strip() or "unspecified",
                prototype_image_ref=prototype,
                actual_image_ref=actual,
                prototype_source_ref=str(entry.get("prototype_source_ref", "")).strip(),
            )
        )
    return pairs


def _infer_visual_pair(
    proof: Mapping[str, object],
    surface_by_name: Mapping[str, Mapping[str, object]],
) -> Optional[VisualEvidencePair]:
    refs = [
        str(item).strip()
        for item in proof.get("evidence_refs", [])
        if isinstance(item, str) and _is_image_ref(item)
    ]
    if len(refs) < 2:
        return None
    prototype = next((ref for ref in refs if _looks_like_prototype_ref(ref)), refs[0])
    actual = next((ref for ref in refs if ref != prototype and not _looks_like_prototype_ref(ref)), "")
    if not actual:
        actual = next((ref for ref in refs if ref != prototype), "")
    if not prototype or not actual:
        return None
    surface = _surface_name_for_refs(refs, surface_by_name) or str(proof.get("requirement_id", "")).strip()
    return VisualEvidencePair(
        requirement_id=str(proof.get("requirement_id", "")).strip(),
        oracle_index=int(proof.get("oracle_index", 0) or 0),
        surface=surface,
        viewport=_viewport_for_refs(refs),
        prototype_image_ref=prototype,
        actual_image_ref=actual,
    )


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
    lowered = ref.lower()
    return any(suffix in lowered for suffix in IMAGE_SUFFIXES)


def _looks_like_prototype_ref(ref: str) -> bool:
    lowered = ref.lower()
    return any(token in lowered for token in ("prototype", "frondend_prototype", "frontend_prototype", "mockup", "figma"))


def _surface_name_for_refs(refs: List[str], surface_by_name: Mapping[str, Mapping[str, object]]) -> str:
    lowered = " ".join(refs).lower()
    for name in surface_by_name:
        if name and name in lowered:
            return str(surface_by_name[name].get("name", "")).strip()
    return ""


def _viewport_for_refs(refs: List[str]) -> str:
    lowered = " ".join(refs).lower()
    for token in ("mobile", "desktop", "tablet"):
        if token in lowered:
            return token
    for token in ("移动", "桌面", "平板"):
        if token in lowered:
            return token
    return "unspecified"
