import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

from auto_agents.gate_result_cache import GateResultCache
from auto_agents.models import CommandResult
from auto_agents.repair_control import Repository, Supervisor, atomic_json, git, start_ticks
from test_repair_control import configuration, make_remote, registration, failure


def test_artifact_proof_restores_missing_output_but_never_overwrites_user_edit(tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    artifact = source / "report.txt"
    artifact.write_text("tested output")
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    cache = GateResultCache(source, cache_path=tmp_path / "private/cache.db")
    options = dict(source_fingerprint="source", cache_scope="source", result_cache_scope="candidate", metadata_signature="policy")
    cache.record("test", CommandResult("test", True, 0, artifacts={"report.txt": expected}), **options)
    artifact.unlink()
    result = cache.lookup("test", **options)
    assert result and result.cached and result.artifacts == {"report.txt": expected}
    assert artifact.read_text() == "tested output"
    artifact.write_text("user edit")
    assert cache.lookup("test", **options) is None
    assert artifact.read_text() == "user edit"


def test_artifact_proof_rejects_symlink_escape(tmp_path):
    root = tmp_path / "project"
    (root / "out").mkdir(parents=True)
    path = root / "out/report.txt"
    path.write_text("proof")
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    cache = GateResultCache(root, cache_path=tmp_path / "cache/proofs.db")
    options = dict(source_fingerprint="source", cache_scope="source", result_cache_scope="candidate", metadata_signature="policy")
    cache.record("test", CommandResult("test", True, 0, artifacts={"out/report.txt": expected}), **options)
    path.unlink()
    path.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "out").symlink_to(outside, target_is_directory=True)
    assert cache.lookup("test", **options) is None
    assert not list(outside.iterdir())


@pytest.mark.skipif(shutil.which("codex") is None, reason="local sandbox required")
def test_supervisor_runs_real_verification_worker_and_reuses_engine_proof(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "src", engine / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(source / "pyproject.toml", engine / "pyproject.toml")
    (engine / "tests").mkdir()
    (engine / "tests/test_probe.py").write_text("def test_probe(): assert 2 + 2 == 4\n")
    git(engine, "add", ".")
    git(engine, "commit", "-m", "trusted verification implementation")
    repository = Repository(config)
    commit = git(engine, "rev-parse", "HEAD")
    repository.import_commit(engine, commit)
    workspace = repository.worktree(commit, "candidate")
    project = tmp_path / "live"
    project.mkdir()
    (project / "input.txt").write_text("untouched")
    atomic_json(Path(config["root"]) / "operator.json", config)
    supervisor = Supervisor(config)
    subscriber = supervisor.store.register(registration(project))
    job = supervisor.store.submit(subscriber, failure(project))
    supervisor.store.transition(job, "repairing")
    supervisor.workers[job] = (SimpleNamespace(pid=os.getpid()), 1, "repair")
    context = supervisor.dispatch({"version": 1, "op": "verify-context", "job": job,
        "workspace": str(workspace), "runtime": str(engine), "python": sys.executable,
        "engine": True, "_peer_pid": os.getpid(),
        "resource_environment": {key: os.environ[key] for key in ("AUTO_AGENTS_VERIFICATION_ROOT", "AUTO_AGENTS_WORKER_ROOT", "AUTO_AGENTS_CLUSTER_HOME")}}, [])["context"]
    results = []
    try:
        for _ in range(2):
            verification = supervisor.dispatch({"version": 1, "op": "verify-submit", "context": context,
                "level": "focused", "tests": ["tests/test_probe.py"]}, [])["verification"]
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                supervisor.tick_verifications()
                status = supervisor.dispatch({"version": 1, "op": "verify-status", "context": context,
                                               "verification": verification}, [])
                if status["state"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)
            assert status["state"] == "completed", status
            results.append(status["result"])
    finally:
        for process in supervisor.verification_processes.values():
            if process.poll() is None:
                os.killpg(process.pid, 15)
                process.wait(timeout=10)
    assert results[0]["verification"]["payload"]["certificate_hits"] == 0
    assert results[1]["verification"]["payload"]["certificate_hits"] == 1
    assert (project / "input.txt").read_text() == "untouched"
