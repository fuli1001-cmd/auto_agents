from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.worker_cluster import (
    DiscoveredWorker,
    certificate_fingerprint,
    consume_pairing_token,
    create_pairing_invite,
    decode_pairing_invite,
    init_cluster,
    join_cluster,
    load_cluster_state,
)
from auto_agents.cli import build_parser
from auto_agents.models import DistributedGatesConfig
from auto_agents.worker_service import WorkerClient, WorkerService
from auto_agents.workers import WORKER_PROTOCOL_VERSION


def _free_tcp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_lan_worker_cli_and_legacy_mode_migration() -> None:
    args = build_parser().parse_args(
        ["worker", "serve", "--slots", "2", "--port", "48000"]
    )

    assert args.worker_command == "serve"
    assert args.slots == "2"
    assert args.port == 48000
    assert DistributedGatesConfig.from_dict({"enabled": True}).mode == "auto"
    assert DistributedGatesConfig.from_dict({"enabled": False}).mode == "off"


def test_pairing_invite_is_single_use(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_AGENTS_CLUSTER_HOME", str(tmp_path / "cluster"))
    state = init_cluster(name="test")
    code = create_pairing_invite(host="127.0.0.1", ttl_seconds=60)
    invite = decode_pairing_invite(code)

    with pytest.raises(RuntimeError, match="already paired"):
        join_cluster(code)

    consumed = consume_pairing_token(str(invite["token"]))

    assert consumed.cluster_id == state.cluster_id
    with pytest.raises(PermissionError):
        consume_pairing_token(str(invite["token"]))


def test_worker_service_uses_pinned_authenticated_https(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTO_AGENTS_WORKER_CONFIG", raising=False)
    monkeypatch.setenv("AUTO_AGENTS_CLUSTER_HOME", str(tmp_path / "cluster"))
    monkeypatch.setenv("AUTO_AGENTS_WORKER_ROOT", str(tmp_path / "worker"))
    monkeypatch.setenv("AUTO_AGENTS_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AUTO_AGENTS_WORKER_SLOTS", "1")
    state = init_cluster(name="test")
    port = _free_tcp_port()
    service = WorkerService(bind="127.0.0.1", port=port)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    worker = DiscoveredWorker(
        worker_id=state.node_id,
        hostname=state.hostname,
        host="127.0.0.1",
        port=port,
        tls_fingerprint=certificate_fingerprint(),
        max_slots=1,
        capabilities=(),
        last_seen=time.time(),
    )
    client = WorkerClient(worker, timeout_seconds=2)
    deadline = time.monotonic() + 5
    while True:
        try:
            probe = client.probe()
            break
        except (ConnectionError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)

    assert probe["ok"] is True
    assert probe["worker_id"] == state.node_id
    assert load_cluster_state(required=True) == state

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=project,
        check=True,
    )
    (project / "value.txt").write_text("snapshot\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    snapshot = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    ).stdout.strip()
    bundle = tmp_path / "snapshot.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle), "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    assert client.stage(
        project_key="test-project",
        snapshot=snapshot,
        source_ref="HEAD",
        bundle=bundle,
    )["ok"]
    manifest = {
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "project_key": "test-project",
        "snapshot": snapshot,
        "plan_id": "test-plan",
        "job_id": "test-job",
        "lane": "",
        "command": (
            "test -f value.txt && "
            "test -n \"$AUTO_AGENTS_GATE_PORT_API\""
        ),
        "resource_class": "normal",
        "environment_manifest": {
            "environment_id": "a" * 64,
            "python": {},
            "node": [],
        },
        "environment": {},
        "timeout_seconds": 30,
        "adaptive_timeout_enabled": True,
        "idle_timeout_seconds": 10,
        "artifact_globs": [],
        "exclusive_resources": [],
        "dynamic_ports": ["api"],
        "artifact_max_files": 10,
        "artifact_max_bytes": 1024,
    }
    assert client.submit(manifest)["job_id"] == "test-job"
    assert client.submit(manifest)["job_id"] == "test-job"
    deadline = time.monotonic() + 10
    while True:
        record = client.query("test-job")
        if record.get("state") == "terminal":
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"worker job did not finish: {record}")
        time.sleep(0.05)
    assert record["result"]["ok"] is True
    assert 49152 <= record["dynamic_ports"]["api"] <= 65535

    wrong_worker = DiscoveredWorker(
        **{
            **worker.to_dict(),
            "tls_fingerprint": "0" * 64,
        }
    )
    with pytest.raises(RuntimeError, match="fingerprint changed"):
        WorkerClient(wrong_worker, timeout_seconds=2).probe()

    service.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
