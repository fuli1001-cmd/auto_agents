from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_execution import LocalGatePlanExecutor, isolated_command
from auto_agents.gates import GateCommandMetadata, run_gate_plan
from auto_agents.models import GateConfig, GateIsolationConfig


def _git(project: Path, *args: str) -> None:
    process = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert process.returncode == 0, process.stderr


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.com")
    (project / ".gitignore").write_text(".tmp-tests/\n", encoding="utf-8")
    (project / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "initial")
    return project


def _config(tmp_path: Path) -> GateConfig:
    return GateConfig(
        isolation=GateIsolationConfig(
            enabled=True,
            worktree_root=str(tmp_path / "worktrees"),
        )
    )


def test_isolated_gate_snapshots_dirty_and_untracked_files(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (project / "untracked.txt").write_text("present\n", encoding="utf-8")
    command = (
        f"{sys.executable} -c \"from pathlib import Path; "
        "assert Path('tracked.txt').read_text() == 'dirty\\\\n'; "
        "assert Path('untracked.txt').read_text() == 'present\\\\n'; "
        "p=Path('.tmp-tests/evidence.txt'); p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text('evidence')\""
    )
    metadata = {
        command: GateCommandMetadata(
            artifact_globs=[".tmp-tests/evidence.txt"],
        )
    }
    with LocalGatePlanExecutor(
        project,
        _config(tmp_path),
        metadata,
    ) as executor:
        result = run_gate_plan(
            [command],
            [],
            project,
            collect_all=False,
            gate_executor=executor,
        )
    assert result.ok
    assert (project / ".tmp-tests/evidence.txt").read_text() == "evidence"
    assert (project / "tracked.txt").read_text() == "dirty\n"
    assert (project / "untracked.txt").read_text() == "present\n"


def test_serial_lane_preserves_ignored_producer_artifact(tmp_path: Path) -> None:
    project = _project(tmp_path)
    producer = (
        f"{sys.executable} -c \"from pathlib import Path; "
        "p=Path('.tmp-tests/shared.txt'); p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text('ready')\""
    )
    consumer = (
        f"{sys.executable} -c \"from pathlib import Path; "
        "assert Path('.tmp-tests/shared.txt').read_text() == 'ready'\""
    )
    metadata = {
        consumer: GateCommandMetadata(
            artifact_globs=[".tmp-tests/shared.txt"],
        )
    }
    with LocalGatePlanExecutor(
        project,
        _config(tmp_path),
        metadata,
    ) as executor:
        result = run_gate_plan(
            [producer, consumer],
            [],
            project,
            collect_all=False,
            gate_executor=executor,
        )
    assert result.ok
    assert (project / ".tmp-tests/shared.txt").read_text() == "ready"


def test_isolated_gate_rejects_tracked_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    command = (
        f"{sys.executable} -c \"from pathlib import Path; "
        "Path('tracked.txt').write_text('changed')\""
    )
    with LocalGatePlanExecutor(
        project,
        _config(tmp_path),
        {},
    ) as executor:
        result = run_gate_plan(
            [command],
            [],
            project,
            collect_all=False,
            gate_executor=executor,
        )
    assert result.ok
    assert result.commands[0].mutation_paths == ["tracked.txt"]
    assert (project / "tracked.txt").read_text() == "committed\n"


def test_vitest_cache_is_disabled_for_shared_dependencies() -> None:
    assert isolated_command("npm exec -- vitest run workbench/src").endswith(
        " --no-cache"
    )
    assert isolated_command("vitest run --no-cache") == "vitest run --no-cache"
