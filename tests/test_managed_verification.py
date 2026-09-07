from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.cli import main
from auto_agents.managed_verification import attach_context, execute_engine, selected_tests
from auto_agents.models import AccelerationConfig, AgentRequest
from auto_agents.repair_control import Store, Supervisor, git
from test_repair_control import configuration, make_remote, registration


def test_explain_does_not_initialize_or_write_project(tmp_path):
    root = tmp_path / "engine"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_a.py").write_text("def test_a(): assert True\n")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    with patch("auto_agents.orchestrator.Orchestrator", side_effect=AssertionError("must remain read-only")), \
         patch("auto_agents.cli.reporting_command", side_effect=AssertionError("no diagnostics writes")):
        assert main(["verify", "--project", str(root), "--engine", "--explain"]) == 0
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


@pytest.mark.parametrize("target", ["--override-ini=x", "../outside.py", "tests/test_a.py; echo bad", "/etc/passwd"])
def test_selectors_cannot_escape_scope_or_become_commands(tmp_path, target):
    with pytest.raises(ValueError):
        selected_tests(tmp_path, [target])


def test_provider_context_is_stable_across_continuation(tmp_path):
    config = configuration(tmp_path)
    orch = SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace(acceleration=AccelerationConfig())),
        _repair_registration={"config": config, "subscriber": "owner"})
    request = AgentRequest(stage="fix", purpose="fix", effort="deep", prompt="fix the bug", cwd=tmp_path,
                           output_path=tmp_path / "out")
    with patch("auto_agents.managed_verification.rpc", return_value={"context": "token"}) as call:
        first = attach_context(orch, request)
        second = attach_context(orch, request)
    assert call.call_count == 1
    assert first.prompt == second.prompt
    assert "--verification-context" in first.prompt
    assert any(block.rule_id == "verification.managed" for block in first.prompt_spec.blocks)


def test_supervisor_context_requires_registered_owner_and_fences_cancel(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    (engine / "src/auto_agents").mkdir(parents=True)
    (engine / "src/auto_agents/verification_worker.py").write_text("# trusted worker fixture\n")
    git(engine, "add", ".")
    git(engine, "commit", "-m", "verification protocol fixture")
    supervisor = Supervisor(config)
    owner = registration(engine)
    owner["token"] = "token"
    subscriber = supervisor.store.register(owner)
    supervisor.registrations[subscriber] = {"payload": owner, "fds": [], "env": {}}
    request = {"version": 1, "op": "verify-context", "subscriber": subscriber, "workspace": str(engine),
               "runtime": str(engine), "engine": False, "_peer_pid": os.getpid()}
    with pytest.raises(RuntimeError, match="registered workflow"):
        supervisor.dispatch({**request, "_peer_pid": os.getpid() + 1000}, [])
    context = supervisor.dispatch(request, [])["context"]
    with pytest.raises(RuntimeError, match="explicit focused"):
        supervisor.dispatch({"version": 1, "op": "verify-submit", "context": context, "level": "release", "tests": ["tests/test_a.py"]}, [])
    job = supervisor.dispatch({"version": 1, "op": "verify-submit", "context": context, "level": "focused", "tests": ["tests/test_a.py"]}, [])
    assert job["verification"]
    with supervisor.store.connect() as db:
        db.execute("UPDATE subscribers SET state='cancelled' WHERE id=?", (subscriber,))
    supervisor.tick_verifications()
    with supervisor.store.connect() as db:
        assert db.execute("SELECT state FROM verifications").fetchone()[0] == "cancelled"


@pytest.mark.skipif(shutil.which("codex") is None, reason="local sandbox required")
def test_engine_managed_tests_reuse_receipts_and_protect_source(tmp_path):
    config = configuration(tmp_path)
    root = make_remote(config)
    (root / "tests").mkdir()
    (root / "tests/test_a.py").write_text("def test_a(): assert 1 + 1 == 2\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test")
    before = git(root, "status", "--porcelain")
    first = execute_engine(root, tests=["tests/test_a.py"])
    second = execute_engine(root, tests=["tests/test_a.py"])
    assert first["ok"], first
    assert second["ok"], second
    assert first["verification"]["payload"]["certificate_hits"] == 0
    assert second["verification"]["payload"]["certificate_hits"] == 1
    assert second["verification"]["payload"]["executed_tests"] == ["tests/test_a.py::test_a"]
    assert git(root, "status", "--porcelain") == before


def test_engine_release_shards_receive_dirty_and_untracked_snapshot(tmp_path):
    from auto_agents.self_repair import _VerificationResult
    config = configuration(tmp_path)
    root = make_remote(config)
    (root / "bug.py").write_text("value = 'dirty fix'\n")
    (root / "tests").mkdir()
    (root / "tests/test_new.py").write_text("def test_new(): assert True\n")
    before = git(root, "status", "--porcelain")
    head = git(root, "rev-parse", "HEAD")
    def full(snapshot):
        assert git(snapshot, "show", "HEAD:bug.py") == "value = 'dirty fix'"
        assert "test_new" in git(snapshot, "show", "HEAD:tests/test_new.py")
        return _VerificationResult(True, "captured full snapshot")
    with patch("auto_agents.managed_verification.engine_runner", return_value=SimpleNamespace(_run_full_suite_shards=full)):
        assert execute_engine(root, level="release")["ok"]
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "status", "--porcelain") == before


def test_resource_analysis_preserves_unicode_source_without_quadratic_slicing(tmp_path):
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_resources.py").write_text(
        'def test_one():\n    label = "中文"; host = "localhost"\n'
        'def test_two():\n    label = "正常"; data = "independent"\n')
    with patch("auto_agents.self_repair.ast.get_source_segment", side_effect=AssertionError("quadratic extraction")):
        first = AutoAgentsSelfRepairRunner._full_suite_shard_resources(tmp_path, "tests/test_resources.py", ("tests/test_resources.py::test_one",))
        second = AutoAgentsSelfRepairRunner._full_suite_shard_resources(tmp_path, "tests/test_resources.py", ("tests/test_resources.py::test_two",))
    assert "network:fixed-port" in first[0]
    assert "network:fixed-port" not in second[0]
