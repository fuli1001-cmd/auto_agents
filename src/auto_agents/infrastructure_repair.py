from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Dict, List

from .execution_recovery import ExecutionIncident
from .workers import enrich_worker_probe, worker_probe


@dataclass
class InfrastructureRepairResult:
    repaired: bool
    capability: str
    action: str
    reason: str
    environment: Dict[str, str] = field(default_factory=dict)
    manifest_path: str = ""
    artifact_fingerprint: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _incident_capability(incident: ExecutionIncident) -> str:
    text = " ".join(
        [
            incident.command,
            incident.stdout_tail,
            incident.stderr_tail,
            str(incident.process_snapshot.get("infrastructure_failure_id", "")),
        ]
    ).lower()
    if any(token in text for token in ("browser", "chrome", "chromium", "devtools")):
        return "chrome"
    return ""


def _managed_root() -> Path:
    configured = os.environ.get("AUTO_AGENTS_WORKER_MANAGED_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".local" / "share" / "auto-agents-worker"


def repair_execution_infrastructure(
    incident: ExecutionIncident,
) -> InfrastructureRepairResult:
    """Perform only allowlisted, user-owned runtime recovery operations."""
    capability = _incident_capability(incident)
    if not capability:
        return InfrastructureRepairResult(
            repaired=False,
            capability="",
            action="no_managed_driver",
            reason="no managed capability repair driver matches this incident",
        )
    if capability != "chrome":
        return InfrastructureRepairResult(
            repaired=False,
            capability=capability,
            action="unsupported_capability",
            reason=f"managed repair is not implemented for {capability}",
        )

    probe = enrich_worker_probe(worker_probe(""))
    details = probe.get("capability_details", {})
    chrome = details.get("chrome", {}) if isinstance(details, dict) else {}
    if not isinstance(chrome, dict) or chrome.get("state") != "healthy":
        return InfrastructureRepairResult(
            repaired=False,
            capability="chrome",
            action="health_probe_failed",
            reason=str(chrome.get("error", "no healthy Chrome candidate found")),
            artifact_fingerprint=str(chrome.get("artifact_sha256", "")),
        )

    binary = str(chrome.get("path", "")).strip()
    fingerprint = str(chrome.get("artifact_sha256", "")).strip()
    if not binary or not fingerprint:
        return InfrastructureRepairResult(
            repaired=False,
            capability="chrome",
            action="invalid_probe_evidence",
            reason="healthy Chrome probe omitted its path or artifact fingerprint",
        )

    root = _managed_root()
    runtime_root = root / "runtimes" / "chrome" / fingerprint
    crash_root = root / "crashes" / incident.run_id / incident.incident_id
    report_root = root / "infrastructure-reports" / incident.run_id
    runtime_root.mkdir(parents=True, exist_ok=True)
    crash_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)
    manifest_path = runtime_root / "manifest.json"
    payload = {
        "schema_version": 1,
        "capability": "chrome",
        "state": "healthy",
        "path": binary,
        "version": str(chrome.get("version", "")),
        "artifact_sha256": fingerprint,
        "probe_kind": str(chrome.get("probe_kind", "")),
        "failure_domain": probe.get("failure_domain", {}),
        "checked_at": _utc_now(),
    }
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    environment = {
        "AUTO_AGENTS_CAPABILITY_CHROME_PATH": binary,
        "AUTO_AGENTS_CAPABILITY_CHROME_VERSION": str(chrome.get("version", "")),
        "AUTO_AGENTS_CAPABILITY_CHROME_SHA256": fingerprint,
        "AUTO_AGENTS_CRASH_DIR": str(crash_root),
        "AUTO_AGENTS_INFRA_REPORT_PATH": str(
            report_root / f"{incident.incident_id}.json"
        ),
    }
    os.environ.update(environment)
    return InfrastructureRepairResult(
        repaired=True,
        capability="chrome",
        action="selected_healthy_managed_runtime",
        reason="a healthy Chrome candidate was recorded in the managed runtime",
        environment=environment,
        manifest_path=str(manifest_path),
        artifact_fingerprint=fingerprint,
    )


def managed_diagnostic_refs(incident: ExecutionIncident) -> List[Dict[str, object]]:
    root = _managed_root()
    candidates = [
        root / "crashes" / incident.run_id / incident.incident_id,
        root / "infrastructure-reports" / incident.run_id,
    ]
    refs: List[Dict[str, object]] = []
    total = 0
    for directory in candidates:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > 16 * 1024 * 1024 or total + size > 32 * 1024 * 1024:
                continue
            refs.append(
                {
                    "path": str(path),
                    "size": size,
                    "kind": (
                        "minidump"
                        if path.suffix == ".dmp"
                        else "infrastructure_report"
                        if path.suffix == ".json"
                        else "crash_log"
                    ),
                }
            )
            total += size
    return refs


def scoped_verification_repair_instructions() -> str:
    return (
        "Repair only verification launchers, runtime selection/locks, or diagnostic "
        "collection. Consume AUTO_AGENTS_CAPABILITY_<NAME>_PATH when compatible. "
        "Do not skip, delete, focus, weaken, or remove tests/assertions; do not lower "
        "runtime minimums, remove required capabilities, suppress infrastructure "
        "markers, or fabricate evidence. The exact original verification command must "
        "pass before integration."
    )


_FORBIDDEN_REPAIR_PATTERNS = (
    re.compile(r"^\+.*\.(?:skip|todo|only)\s*\(", re.MULTILINE),
    re.compile(r"^\+.*(?:pytest\.mark\.skip|pytest\.skip)\b", re.MULTILINE),
    re.compile(r"^\+.*AUTO_AGENTS_INFRA_FAILURE.*(?:disable|false)", re.MULTILINE),
    re.compile(r"^\-.*(?:expect|assert|should)\b", re.MULTILINE),
)


def verification_repair_guard(diff_text: str) -> List[str]:
    """Reject obvious attempts to weaken verification during scoped repair."""
    findings: List[str] = []
    labels = (
        "new skipped or focused test",
        "new pytest skip",
        "infrastructure reporting disabled",
        "assertion removed",
    )
    for label, pattern in zip(labels, _FORBIDDEN_REPAIR_PATTERNS):
        if pattern.search(diff_text):
            findings.append(label)
    return findings
