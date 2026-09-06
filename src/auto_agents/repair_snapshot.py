"""Immutable target evidence shared by all candidates of one experiment."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .repository_guard import capture_repository_guard, guard_fingerprint


def freeze_target(project: Path, experiment_root: Path, invocation: dict) -> tuple[Path, str]:
    from .root_cause import RootCauseCoordinator
    from .self_repair_search import _atomic_json

    root = experiment_root / "replay-checkpoint"
    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("invocation", {}) != invocation:
            raise RuntimeError("repair checkpoint belongs to a different invocation")
        target = root / "target"
        digest = guard_fingerprint(capture_repository_guard(target, ignore_run_artifacts=True))
        if digest != manifest.get("target_sha256"):
            raise RuntimeError("frozen repair checkpoint was modified")
        return target, digest
    experiment_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="replay-checkpoint-", dir=experiment_root))
    try:
        target = temporary / "target"
        RootCauseCoordinator._copy_diagnostic_tree(project, target)
        state_path = project / ".auto-agents/state/run_state.json"
        if state_path.is_file():
            run_id = str(json.loads(state_path.read_bytes()).get("run_id", ""))
            if run_id and Path(run_id).name == run_id:
                for name in ("recovery_incidents", "attempt-checkpoints", "repair-cases"):
                    source = project / ".auto-agents/runs" / run_id / name
                    if source.is_dir():
                        shutil.copytree(source, target / ".auto-agents/runs" / run_id / name)
        digest = guard_fingerprint(capture_repository_guard(target, ignore_run_artifacts=True))
        _atomic_json(temporary / "manifest.json", {
            "schema_version": 1, "target_sha256": digest, "invocation": invocation,
        })
        os.replace(temporary, root)
        return root / "target", digest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
