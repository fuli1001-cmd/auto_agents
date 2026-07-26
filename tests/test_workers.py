from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_execution import GateSnapshotManager
from auto_agents.distributed_gates import DistributedGatePlanExecutor
from auto_agents.gates import GateCommandMetadata, run_gate_plan
from auto_agents.models import (
    CommandResult,
    DistributedGatesConfig,
    GateConfig,
    GateIsolationConfig,
    GateParallelGroup,
    ProjectConfig,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.workers import (
    WORKER_PROTOCOL_VERSION,
    WorkerEndpoint,
    WorkerSlotLease,
    build_environment_manifest,
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


def test_environment_manifest_excludes_controller_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    python = project / ".conda" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    def fake_run(command, **_kwargs):
        if "-c" in command:
            return subprocess.CompletedProcess(command, 0, stdout="3.11\n", stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "auto-agents==0.7.0\n"
                "Auto_Agents==0.7.0\n"
                "Auto.Agents==0.7.0\n"
                "pytest==8.4.1\n"
                "-e /workspace/editable\n"
                "local-package @ file:///workspace/local-package\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("auto_agents.workers.subprocess.run", fake_run)

    manifest = build_environment_manifest(project)

    assert manifest["python"]["requirements"] == ["pytest==8.4.1"]


def test_worker_stage_execute_and_query(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    worker_config = _worker_config(tmp_path)
    monkeypatch.setenv("AUTO_AGENTS_WORKER_CONFIG", str(worker_config))
    monkeypatch.setenv("AUTO_AGENTS_STATE_HOME", str(tmp_path / "state"))
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
        f"{sys.executable} -c \"import os, socket; from pathlib import Path; "
        "assert Path('value.txt').read_text() == 'snapshot\\\\n'; "
        "port=int(os.environ['AUTO_AGENTS_GATE_PORT_API']); "
        "sock=socket.socket(); sock.bind(('127.0.0.1', port)); sock.close()\""
    )
    events = io.StringIO()
    result = worker_execute(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
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
            "dynamic_ports": ["api"],
            "artifact_max_files": 10,
            "artifact_max_bytes": 1024,
        },
        event_stream=events,
    )
    assert result.ok, result.stderr
    assert result.worker_id == "test-worker"
    assert "do-not-log" not in events.getvalue()
    record = worker_query("job-one")
    assert record["state"] == "terminal"
    assert record["result"]["ok"] is True
    assert 49152 <= record["dynamic_ports"]["api"] <= 65535
    assert worker_probe("test")["ok"]
    manager.close()


def test_worker_rejects_legacy_gate_protocol(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AUTO_AGENTS_WORKER_CONFIG",
        str(_worker_config(tmp_path)),
    )

    result = worker_execute(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION - 1,
            "job_id": "legacy-job",
            "command": "true",
        },
        event_stream=io.StringIO(),
    )

    assert not result.ok
    assert result.infrastructure_error
    assert "unsupported worker protocol version" in result.stderr


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


def test_worker_slot_lease_reports_persistent_memory_pressure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "auto_agents.workers._memory_available_bytes",
        lambda: 3 * 1024**3,
    )

    with pytest.raises(RuntimeError, match="memory capacity.*3072 MiB available"):
        with WorkerSlotLease(
            tmp_path,
            "test-worker",
            slots=2,
            required=2,
            memory_mb=4096,
            memory_reserve_mb=2048,
            memory_guard="required",
            timeout_seconds=0,
        ):
            pass


def test_worker_slot_lease_has_no_implicit_memory_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "auto_agents.workers._memory_available_bytes",
        lambda: 1,
    )

    with WorkerSlotLease(
        tmp_path,
        "test-worker",
        slots=2,
        required=2,
        timeout_seconds=0,
    ):
        pass


def test_worker_slot_lease_advisory_memory_guard_warns_and_continues(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(
        "auto_agents.workers._memory_available_bytes",
        lambda: 512 * 1024**2,
    )

    with WorkerSlotLease(
        tmp_path,
        "test-worker",
        slots=1,
        required=1,
        memory_mb=1024,
        memory_reserve_mb=256,
        memory_guard="advisory",
        timeout_seconds=0,
    ):
        pass

    assert "memory_guard=advisory" in caplog.text


def test_worker_slot_lease_honors_cancellation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "auto_agents.workers._memory_available_bytes",
        lambda: 3 * 1024**3,
    )
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(RuntimeError, match="slot acquisition was cancelled"):
        with WorkerSlotLease(
            tmp_path,
            "test-worker",
            slots=2,
            required=2,
            cancel_event=cancel_event,
        ):
            pass


def test_distributed_executor_uses_controller_as_local_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTO_AGENTS_CLUSTER_HOME", str(tmp_path / "cluster"))
    monkeypatch.setenv("AUTO_AGENTS_STATE_HOME", str(tmp_path / "state"))
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


def test_distributed_executor_reserves_heavy_slots_atomically(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    executor = DistributedGatePlanExecutor(project, GateConfig(), {})
    endpoint = WorkerEndpoint(
        worker_id="local-worker",
        transport="local",
        max_slots=2,
    )
    executor.endpoints = [endpoint]
    executor._active_slots[endpoint.worker_id] = 0
    start = threading.Barrier(3)
    results: list[bool] = []

    def acquire() -> None:
        start.wait()
        results.append(executor._try_acquire(endpoint, 2))

    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=1)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    assert executor._active_slots[endpoint.worker_id] == 2


def test_distributed_executor_prefers_declared_cpu_slots(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    command = "declared command"
    executor = DistributedGatePlanExecutor(
        project,
        GateConfig(),
        {
            command: GateCommandMetadata(
                resource_class="normal",
                cpu_slots=3,
                memory_mb=4096,
                memory_reserve_mb=1024,
                memory_guard="required",
            )
        },
    )

    assert executor._required_slots(command) == 3
    assert executor._memory_policy(command) == (4096, 1024, "required")


def test_distributed_executor_slot_wait_honors_cancellation(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    command = "heavy command"
    executor = DistributedGatePlanExecutor(
        project,
        GateConfig(),
        {command: GateCommandMetadata(resource_class="heavy")},
    )
    endpoint = WorkerEndpoint(
        worker_id="local-worker",
        transport="local",
        max_slots=2,
    )
    executor.endpoints = [endpoint]
    executor._active_slots[endpoint.worker_id] = 2
    cancel_event = threading.Event()
    waiting = threading.Event()
    results = []
    original_try_acquire = executor._try_acquire

    def try_acquire(endpoint: WorkerEndpoint, required: int) -> bool:
        waiting.set()
        return original_try_acquire(endpoint, required)

    executor._try_acquire = try_acquire  # type: ignore[method-assign]

    def run() -> None:
        results.append(
            executor.run(
                command,
                timeout_seconds=30,
                adaptive_timeout_enabled=False,
                idle_timeout_seconds=10,
                cancel_event=cancel_event,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert waiting.wait(timeout=1)
    cancel_event.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].returncode == 130
    assert results[0].termination_reason == "cancelled"


def test_distributed_executor_slot_wait_has_absolute_deadline(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    command = "heavy command"
    executor = DistributedGatePlanExecutor(
        project,
        GateConfig(),
        {command: GateCommandMetadata(resource_class="heavy")},
    )
    endpoint = WorkerEndpoint(
        worker_id="local-worker",
        transport="local",
        max_slots=2,
    )
    executor.endpoints = [endpoint]
    executor._active_slots[endpoint.worker_id] = 2

    with pytest.raises(RuntimeError, match="slot scheduling timed out"):
        executor._acquire_endpoint(
            command,
            exclude=set(),
            lane="",
            wait_timeout_seconds=0.01,
        )


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


def test_reported_infrastructure_rotates_each_worker_once() -> None:
    executor = object.__new__(DistributedGatePlanExecutor)
    executor.gate_config = GateConfig(
        distributed=DistributedGatesConfig(
            mode="auto",
            reported_infrastructure_max_workers=8,
        )
    )
    executor._lane_successes = {}
    executor._lane_endpoint = {}
    endpoints = [
        WorkerEndpoint(worker_id=f"worker-{index}", transport="local", max_slots=1)
        for index in range(1, 4)
    ]
    calls: list[str] = []

    def acquire(command, *, exclude, **kwargs):
        endpoint = next(item for item in endpoints if item.worker_id not in exclude)
        return endpoint, 1

    def run_endpoint(endpoint, command, **kwargs):
        calls.append(endpoint.worker_id)
        if endpoint.worker_id == "worker-3":
            return CommandResult(
                command=command,
                ok=True,
                returncode=0,
                worker_id=endpoint.worker_id,
                backend="local-isolated",
            )
        return CommandResult(
            command=command,
            ok=False,
            returncode=1,
            stdout=(
                "Error: AUTO_AGENTS_INFRA_FAILURE "
                "id=browser_launch_failed"
            ),
            worker_id=endpoint.worker_id,
            backend="local-isolated",
        )

    executor._acquire_endpoint = acquire
    executor._run_on_endpoint = run_endpoint
    executor._release = lambda endpoint, required: None
    first = CommandResult(
        command="npm test",
        ok=False,
        returncode=1,
        stdout="AUTO_AGENTS_INFRA_FAILURE id=browser_launch_failed",
        worker_id="worker-1",
        infrastructure_error=True,
        infrastructure_failure_id="browser_launch_failed",
    )

    result = executor._retry_reported_infrastructure(
        command="npm test",
        lane="",
        first_result=first,
        attempted={"worker-1"},
        timeout_seconds=60,
        adaptive_timeout_enabled=True,
        idle_timeout_seconds=30,
        cancel_event=None,
        progress=None,
    )

    assert result.ok
    assert calls == ["worker-2", "worker-3"]
    assert [item["worker_id"] for item in result.infrastructure_attempts] == [
        "worker-1",
        "worker-2",
        "worker-3",
    ]
