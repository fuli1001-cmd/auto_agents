"""Durable, non-approvable checkpoints for interrupted prototype generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from .config import requirements_trace_path, run_path, state_dir
from .frontend_design import sha256_file
from .io_utils import read_json, write_json
from .prototype_variants import variant_design_path, variant_dir


class PrototypeGenerationCheckpoint:
    def __init__(self, project_root: Path, run_id: str, inputs: Mapping[str, object]):
        self.project_root = project_root
        self.run_id = run_id
        trace = requirements_trace_path(project_root)
        identity = {
            **inputs,
            "run_id": run_id,
            "requirements_sha256": sha256_file(trace) if trace.is_file() else "",
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.path = state_dir(project_root) / "prototype-generations" / f"{digest}.json"
        raw = read_json(self.path, default={})
        self.payload = dict(raw) if isinstance(raw, Mapping) else {}

    def resumable_variant_id(self) -> str:
        payload = self.payload
        if payload.get("status") not in {"running", "interrupted"}:
            return ""
        if payload.get("phase") != "generation":
            return ""
        variant_id = str(payload.get("variant_id", ""))
        try:
            design = variant_design_path(self.project_root, variant_id)
        except ValueError:
            return ""
        if not design.is_file() or sha256_file(design) != payload.get("design_sha256"):
            return ""
        return variant_id

    def save(self, **updates: object) -> None:
        self.payload.update(updates)
        write_json(self.path, self.payload)

    def capture_continuation(self) -> None:
        variant_id = str(self.payload["variant_id"])
        reports = run_path(self.project_root, self.run_id) / "outputs" / "provider-attempts"
        candidates = sorted(
            reports.glob(f"prototype-generate-{variant_id}-*-resume-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            report = read_json(path, default={})
            if not isinstance(report, dict) or report.get("stage") != "prototype":
                continue
            if report.get("cwd") != str(self.project_root):
                continue
            # Never turn a running process into an explicit resume: the normal
            # provider recovery path must perform its process-identity guard.
            if report.get("status") not in {"terminated", "interrupted"}:
                return
            if report.get("reason") not in {
                "timed_out", "tool_stalled", "semantic_stall", "loop_detected",
                "external_interrupt", "provider_idle",
            }:
                return
            self.save(
                continuation={
                    "session_id": str(report.get("session_id", "")),
                    "provider": str(report.get("provider", "")),
                    "prompt_hash": str(report.get("prompt_metadata", {}).get("compatibility_hash", "")),
                    "report_path": str(path),
                },
            )
            return

    def continuation(self) -> Mapping[str, object]:
        raw = self.payload.get("continuation", {})
        return raw if isinstance(raw, Mapping) else {}

    def preserve_interruption(self, error: str) -> None:
        self.save(status="interrupted", error=error)
        self.capture_continuation()
        # Each abandoned draft retains its own record even if a subsequent
        # attempt must select a new design or starts a different generation.
        write_json(
            variant_dir(self.project_root, str(self.payload["variant_id"])) / "interruption.json",
            self.payload,
        )
