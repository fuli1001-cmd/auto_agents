from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_execution import GateSnapshotManager
from auto_agents.distributed_gates import DistributedGatePlanExecutor
from auto_agents.gates import GateCommandMetadata, run_gate_plan
from auto_agents.models import (
    DistributedGatesConfig,
    GateConfig,
    GateIsolationConfig,
    GateParallelGroup,
    ProjectConfig,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.workers import (
    forwarded_environment,
    worker_execute,
    worker_probe,
    worker_query,
    worker_stage,
)


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert process.returncode == 0, process.stderr
    return process


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.com")
    (project / ".gitignore").write_text(".tmp-tests/\n", encoding="utf-8")
    (project / "value.txt").write_text("snapshot\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "initial")
    return project


def _worker_config(tmp_path: Path) -> Path:
    path = tmp_path / "worker.json"
    path.write_text(
        json.dumps(
            {
                "worker_id": "test-worker",
                "managed_root": str(tmp_path / "worker-root"),
                "max_slots": 2,
                "environments": {"test": {"links": {}, "executables": {}}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_forwarded_environment_uses_fixed_denylist() -> None:
    result = forwarded_environment(
        {
            "HOME": "/secret/home",
            "PATH": "/bin",
            "SSH_AUTH_SOCK": "/tmp/agent",
            "AUTO_AGENTS_INTERNAL": "hidden",
            "HTTP_PROXY": "http://proxy",
            "PROJECT_TOKEN": "token",
            "EXTRA_BLOCKED": "blocked",
        },
        ["EXTRA_BLOCKED"],
    )
    assert result == {
        "HTTP_PROXY": "http://proxy",
        "PROJECT_TOKEN": "token",
    }


def test_worker_stage_execute_and_query(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    worker_config = _worker_config(tmp_path)
    monkeypatch.setenv("AUTO_AGENTS_WORKER_CONFIG", str(worker_config))
    manager = GateSnapshotManager(project, "worker-test")
    snapshot = manager.create()
    bundle = tmp_path / "snapshot.bundle"
    process = subprocess.run(
        ["git", "bundle", "create", str(bundle), snapshot.ref_name],
        cwd=project,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert process.returncode == 0, process.stderr
    with bundle.open("rb") as stream:
        staged = worker_stage(
            key="project-key",
            snapshot_sha=snapshot.commit_sha,
            source_ref=snapshot.ref_name,
            stream=stream,
        )
    assert staged["ok"]

    command = (
        f"{sys.executable} -c \"from pathlib import Path; "
        "assert Path('value.txt').read_text() == 'snapshot\\\\n'\""
    )
    events = io.StringIO()
    result = worker_execute(
        {
            "protocol_version": 1,
            "project_key": "project-key",
            "snapshot": snapshot.commit_sha,
            "plan_id": "plan-one",
            "job_id": "job-one",
            "lane": "",
            "command": command,
            "resource_class": "normal",
            "environment_id": "test",
            "environment": {"PROJECT_TOKEN": "do-not-log"},
            "timeout_seconds": 30,
            "adaptive_timeout_enabled": True,
            "idle_timeout_seconds": 10,
            "artifact_globs": [],
            "artifact_max_files": 10,
            "artifact_max_bytes": 1024,
        },
        event_stream=events,
    )
    assert result.ok
    assert result.worker_id == "test-worker"
    assert "do-not-log" not in events.getvalue()
    record = worker_query("job-one")
    assert record["state"] == "terminal"
    assert record["result"]["ok"] is True
    assert worker_probe("test")["ok"]
    manager.close()


def test_worker_probe_reports_ffmpeg_and_ffprobe_capabilities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTO_AGENTS_WORKER_CONFIG",
        str(_worker_config(tmp_path)),
    )
    executables = {
        "ffmpeg": "/opt/media/bin/ffmpeg",
        "ffprobe": "/opt/media/bin/ffprobe",
    }
    monkeypatch.setattr(
        "auto_agents.workers.shutil.which",
        lambda program: executables.get(program),
    )

    def fake_run(command, **_kwargs):
        executable = Path(command[0]).name
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{executable} version test\n",
            stderr="",
        )

    monkeypatch.setattr("auto_agents.workers.subprocess.run", fake_run)

    probe = worker_probe()

    assert probe["ok"]
    assert probe["max_slots"] == 2
    assert {"ffmpeg", "ffprobe"}.issubset(probe["capabilities"])
    assert probe["runtimes"]["ffmpeg"] == "ffmpeg version test"
    assert probe["runtimes"]["ffprobe"] == "ffprobe version test"


def test_distributed_executor_uses_controller_as_local_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv(
        "AUTO_AGENTS_WORKER_CONFIG",
        str(_worker_config(tmp_path)),
    )
    commands = [
        f"{sys.executable} -c \"from pathlib import Path; assert Path('value.txt').exists()\"",
        f"{sys.executable} -c \"from pathlib import Path; assert Path('value.txt').read_text() == 'snapshot\\\\n'\"",
    ]
    config = GateConfig(
        max_auto_workers=2,
        isolation=GateIsolationConfig(
            enabled=True,
            worktree_root=str(tmp_path / "worktrees"),
        ),
        distributed=DistributedGatesConfig(mode="auto"),
    )
    metadata = {
        command: GateCommandMetadata(resource_class="normal")
        for command in commands
    }
    with DistributedGatePlanExecutor(
        project,
        config,
        metadata,
    ) as executor:
        result = run_gate_plan(
            [],
            [GateParallelGroup(name="parallel", commands=commands)],
            project,
            collect_all=True,
            parallel_workers=2,
            gate_executor=executor,
        )
    assert result.ok
    assert {item.worker_id for item in result.commands} == {"test-worker"}


def test_distributed_auto_parallelism_uses_gate_capacity() -> None:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.config = ProjectConfig(project_name="test")
    orchestrator.config.gates.max_auto_workers = 5
    orchestrator.config.gates.parallel_workers = "auto"

    assert orchestrator._gate_parallel_workers() == 5

    orchestrator.config.gates.max_auto_workers = "auto"
    assert orchestrator._gate_parallel_workers() == 32

    orchestrator.config.gates.distributed.mode = "off"
    assert orchestrator._gate_parallel_workers() == 2

    orchestrator.config.gates.parallel_workers = 3
    assert orchestrator._gate_parallel_workers() == 3
