from __future__ import annotations

import os
from pathlib import Path
import json
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_execution import (
    LocalGatePlanExecutor,
    discover_dependency_links,
    dynamic_port_lease,
    gate_environment,
    isolated_command,
    self_referential_dependency_links,
)
from auto_agents.gates import GateCommandMetadata, run_gate_plan
from auto_agents.models import GateConfig, GateIsolationConfig, GateParallelGroup


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
