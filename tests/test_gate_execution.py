from __future__ import annotations

import os
from pathlib import Path
import json
import shutil
import socket
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_execution import (
    GateSnapshotManager,
    LocalGatePlanExecutor,
    discover_dependency_links,
    dynamic_port_lease,
    gate_environment,
    isolated_command,
    repository_exclusion_paths,
    self_referential_dependency_links,
)
from auto_agents.gates import GateCommandMetadata, run_gate_plan
from auto_agents.models import CommandResult, GateConfig, GateIsolationConfig, GateParallelGroup


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


def test_gate_plan_loads_timing_once_and_refreshes_executed_commands(tmp_path):
    metadata = {f"check-{index}": GateCommandMetadata() for index in range(5)}
    executor = LocalGatePlanExecutor(tmp_path, _config(tmp_path), metadata)
    for command in metadata:
        executor.timing_store.record(command, CommandResult(
            command=command, ok=True, returncode=0, duration_seconds=4.0,
        ), metadata[command])

    with patch.object(executor.timing_store, "_connect", wraps=executor.timing_store._connect) as connect:
        for command in metadata:
            assert executor.priority(command)[1] == -4.0
            assert executor.estimated_duration(command) == 4.0
        assert connect.call_count == 1

    executor.record_timing("check-0", CommandResult(
        command="check-0", ok=True, returncode=0, duration_seconds=8.0,
    ))
    assert executor.estimated_duration("check-0") == 6.0
    assert executor.estimated_duration("check-1") == 4.0


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


def test_snapshot_excludes_installed_dependency_links(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".gitignore").write_text(
        ".tmp-tests/\n.conda/\n",
        encoding="utf-8",
    )
    control_file = project / ".auto-agents" / "runtime" / "stale.json"
    control_file.parent.mkdir(parents=True)
    control_file.write_text("{}\n", encoding="utf-8")
    gate_cache = project / ".auto-agents-gate-cache" / "stale.txt"
    gate_cache.parent.mkdir()
    gate_cache.write_text("stale\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "track runtime fixtures")

    dependency_source = tmp_path / "dependency-source"
    dependency_source.mkdir()
    (project / ".conda").symlink_to(dependency_source, target_is_directory=True)
    custom_source = tmp_path / "custom-dependency-source"
    custom_source.mkdir()
    (project / "tool-runtime").symlink_to(custom_source, target_is_directory=True)
    (project / "staged.txt").write_text("preserve index\n", encoding="utf-8")
    _git(project, "add", "staged.txt")
    staged_before = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    ).stdout

    dependency_links = discover_dependency_links(project)
    dependency_links["tool-runtime"] = custom_source
    with LocalGatePlanExecutor(
        project,
        _config(tmp_path),
        {},
        dependency_links=dependency_links,
    ) as executor:
        assert executor.snapshot is not None
        snapshot_paths = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", executor.snapshot.commit_sha],
            cwd=project,
            check=True,
            text=True,
            encoding="utf-8",
            capture_output=True,
        ).stdout.splitlines()

    assert ".conda" not in snapshot_paths
    assert "tool-runtime" not in snapshot_paths
    assert control_file.relative_to(project).as_posix() not in snapshot_paths
    assert gate_cache.relative_to(project).as_posix() not in snapshot_paths
    assert "staged.txt" in snapshot_paths
    assert (project / ".conda").is_symlink()
    assert control_file.read_text(encoding="utf-8") == "{}\n"
    assert gate_cache.read_text(encoding="utf-8") == "stale\n"
    staged_after = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    ).stdout
    assert staged_after == staged_before


def test_snapshot_excludes_ignored_dependency_directories(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    (project / ".gitignore").write_text(".conda/\n", encoding="utf-8")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-m", "ignore dependency directory")
    dependency_marker = project / ".conda" / "conda-meta" / "history"
    dependency_marker.parent.mkdir(parents=True)
    dependency_marker.write_text("runtime-only\n", encoding="utf-8")

    manager = GateSnapshotManager(
        project,
        "ignored-dependency-directory",
        excluded_paths=repository_exclusion_paths(project),
    )
    try:
        snapshot = manager.create()
        snapshot_paths = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", snapshot.commit_sha],
            cwd=project,
            check=True,
            text=True,
            encoding="utf-8",
            capture_output=True,
        ).stdout.splitlines()
    finally:
        manager.close()

    assert not any(
        path == ".conda" or path.startswith(".conda/")
        for path in snapshot_paths
    )
    assert dependency_marker.read_text(encoding="utf-8") == "runtime-only\n"


def test_snapshot_excludes_installed_dependency_links_from_commit_paths(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    dependency_file = project / ".conda" / "tracked-runtime.txt"
    dependency_file.parent.mkdir()
    dependency_file.write_text("base runtime\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "base with tracked runtime")
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    ).stdout.strip()

    (project / "tracked.txt").write_text("source version\n", encoding="utf-8")
    dependency_file.write_text("source runtime\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "source changes")
    source_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    ).stdout.strip()

    manager = GateSnapshotManager(
        project,
        "commit-path-exclusions",
        excluded_paths=repository_exclusion_paths(project),
    )
    try:
        snapshot = manager.create_from_commit_paths(
            base_ref=base_ref,
            source_ref=source_ref,
            paths=["tracked.txt", ".conda/tracked-runtime.txt"],
        )
        snapshot_paths = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", snapshot.commit_sha],
            cwd=project,
            check=True,
            text=True,
            encoding="utf-8",
            capture_output=True,
        ).stdout.splitlines()
        tracked_content = subprocess.run(
            ["git", "show", f"{snapshot.commit_sha}:tracked.txt"],
            cwd=project,
            check=True,
            text=True,
            encoding="utf-8",
            capture_output=True,
        ).stdout
    finally:
        manager.close()

    assert tracked_content == "source version\n"
    assert not any(
        path == ".conda" or path.startswith(".conda/")
        for path in snapshot_paths
    )


def test_candidate_result_cache_reuses_identical_snapshot_across_executors(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    auto_dir = project / ".auto-agents"
    auto_dir.mkdir()
    (auto_dir / ".gitignore").write_text(
        "state/gate_baseline_cache.sqlite3\n"
        "state/gate_baseline_cache.sqlite3-*\n",
        encoding="utf-8",
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "ignore gate cache")
    command = f"{sys.executable} -c \"print('checked')\""
    metadata = {
        command: GateCommandMetadata(
            cache_scope="source",
            result_cache_scope="candidate",
        )
    }
    config = _config(tmp_path)
    config.verification_policy_version = 2

    with LocalGatePlanExecutor(
        project,
        config,
        metadata,
        environment_fingerprint="env-1",
    ) as executor:
        first = executor.run(
            command,
            timeout_seconds=60,
            adaptive_timeout_enabled=False,
            idle_timeout_seconds=60,
        )
        assert executor.cached_result(command) is not None
    with LocalGatePlanExecutor(
        project,
        config,
        metadata,
        environment_fingerprint="env-1",
    ) as executor:
        second = executor.run(
            command,
            timeout_seconds=60,
            adaptive_timeout_enabled=False,
            idle_timeout_seconds=60,
        )

    assert first.ok and not first.cached
    assert second.ok and second.cached


def test_proof_audit_sample_reexecutes_a_valid_cache_hit(tmp_path: Path) -> None:
    project = _project(tmp_path)
    command = f"{sys.executable} -c \"print('checked')\""
    metadata = {
        command: GateCommandMetadata(
            cache_scope="source",
            result_cache_scope="candidate",
        )
    }
    config = _config(tmp_path)
    config.verification_policy_version = 2
    cache_path = tmp_path / "proof-cache.sqlite3"

    with LocalGatePlanExecutor(
        project, config, metadata, cache_path=cache_path
    ) as executor:
        first = executor.run(
            command,
            timeout_seconds=60,
            adaptive_timeout_enabled=False,
            idle_timeout_seconds=60,
        )
    with LocalGatePlanExecutor(
        project,
        config,
        metadata,
        cache_path=cache_path,
        proof_audit_sample_rate=1.0,
    ) as executor:
        second = executor.run(
            command,
            timeout_seconds=60,
            adaptive_timeout_enabled=False,
            idle_timeout_seconds=60,
        )

    assert first.ok and not first.cached
    assert second.ok and not second.cached
    assert second.cache_miss_reason == "proof_audit_sample"


def test_auto_result_cache_reuses_when_only_unobserved_source_changes(
    tmp_path: Path,
) -> None:
    if shutil.which("strace") is None:
        return
    project = _project(tmp_path)
    auto_dir = project / ".auto-agents"
    auto_dir.mkdir()
    (auto_dir / ".gitignore").write_text(
        "state/gate_baseline_cache.sqlite3\n"
        "state/gate_baseline_cache.sqlite3-*\n",
        encoding="utf-8",
    )
    (project / "input.txt").write_text("one\n", encoding="utf-8")
    (project / "unrelated.txt").write_text("first\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "add cache probe inputs")
    command = 'test "$(cat input.txt)" = one'
    metadata = {
        command: GateCommandMetadata(
            cache_scope="source",
            result_cache_scope="auto",
        )
    }
    config = _config(tmp_path)
    config.verification_policy_version = 3

    with LocalGatePlanExecutor(
        project,
        config,
        metadata,
        environment_fingerprint="env-auto-1",
    ) as executor:
        first = executor.run(
            command,
            timeout_seconds=60,
            adaptive_timeout_enabled=False,
            idle_timeout_seconds=60,
        )
    (project / "unrelated.txt").write_text("second\n", encoding="utf-8")
    with LocalGatePlanExecutor(
        project,
        config,
        metadata,
        environment_fingerprint="env-auto-1",
    ) as executor:
        second = executor.run(
            command,
            timeout_seconds=60,
            adaptive_timeout_enabled=False,
            idle_timeout_seconds=60,
        )

    assert first.ok and first.input_trace_complete
    assert second.ok and second.cached
    assert second.backend == "result-cache-observed-inputs"


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


def test_dependency_discovery_skips_self_referential_links(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = project / "workbench"
    workspace.mkdir()
    (workspace / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (project / ".conda").symlink_to(project / ".conda")
    (workspace / "node_modules").symlink_to(workspace / "node_modules")

    assert discover_dependency_links(project) == {}
    assert self_referential_dependency_links(project) == [
        ".conda",
        "workbench/node_modules",
    ]


def test_gate_environment_injects_ports_and_clears_stale_values(
    tmp_path: Path,
) -> None:
    env = gate_environment(
        tmp_path,
        job_id="job-one",
        base={
            "AUTO_AGENTS_GATE_HOST": "stale",
            "AUTO_AGENTS_GATE_PORT_OLD": "1234",
            "AUTO_AGENTS_GATE_PORTS_JSON": "{\"old\":1234}",
        },
        dynamic_ports={"api": 51234, "next_app": 51235},
    )

    assert env["AUTO_AGENTS_GATE_HOST"] == "127.0.0.1"
    assert env["AUTO_AGENTS_GATE_PORT_API"] == "51234"
    assert env["AUTO_AGENTS_GATE_PORT_NEXT_APP"] == "51235"
    assert json.loads(env["AUTO_AGENTS_GATE_PORTS_JSON"]) == {
        "api": 51234,
        "next_app": 51235,
    }
    assert "AUTO_AGENTS_GATE_PORT_OLD" not in env


def test_dynamic_port_lease_exposes_bindable_tcp_and_udp_ports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTO_AGENTS_STATE_HOME", str(tmp_path / "state"))

    with dynamic_port_lease(["api", "frontend"]) as ports:
        assert set(ports) == {"api", "frontend"}
        assert len(set(ports.values())) == 2
        for port in ports.values():
            tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                tcp_socket.bind(("127.0.0.1", port))
                udp_socket.bind(("127.0.0.1", port))
            finally:
                tcp_socket.close()
                udp_socket.close()


def test_parallel_isolated_gates_receive_distinct_dynamic_ports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTO_AGENTS_STATE_HOME", str(tmp_path / "state"))
    project = _project(tmp_path)

    def command(label: str) -> str:
        return (
            f"{sys.executable} -c \"import json, os, socket, time; "
            "port=int(os.environ['AUTO_AGENTS_GATE_PORT_API']); "
            "assert json.loads(os.environ['AUTO_AGENTS_GATE_PORTS_JSON'])['api'] == port; "
            "sock=socket.socket(); sock.bind(('127.0.0.1', port)); sock.listen(1); "
            f"print('{label}:' + str(port), flush=True); time.sleep(0.25); sock.close()\""
        )

    commands = [command("one"), command("two")]
    metadata = {
        item: GateCommandMetadata(dynamic_ports=["api"])
        for item in commands
    }
    with LocalGatePlanExecutor(
        project,
        _config(tmp_path),
        metadata,
    ) as executor:
        result = run_gate_plan(
            [],
            [GateParallelGroup(name="ports", commands=commands)],
            project,
            collect_all=True,
            parallel_workers=2,
            gate_executor=executor,
        )

    assert result.ok
    ports = {
        int(item.stdout.rsplit(":", 1)[1])
        for item in result.commands
    }
    assert len(ports) == 2
