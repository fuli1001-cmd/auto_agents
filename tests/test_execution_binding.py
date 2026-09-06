from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.execution_binding import (
    ExecutionBindingError, repository_binding_error, validate_verification_binding,
)
from auto_agents.gate_execution import isolated_command
from auto_agents.models import SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from auto_agents.config import load_run_state, save_run_state
from test_session import _make_project


@pytest.mark.parametrize("command,expected", [
    ("python -m pytest tests/test_workbench_vitest_launcher.py", "python -m pytest tests/test_workbench_vitest_launcher.py"),
    ("conda run -p ./.conda python -m pytest test_vitest.py && python -m pytest tests/test_session.py", "conda run -p ./.conda python -m pytest test_vitest.py && python -m pytest tests/test_session.py"),
    ("vitest run a && python -m pytest b", "vitest run a --no-cache && python -m pytest b"),
    ("python -m pytest b && npm exec -- vitest run a", "python -m pytest b && npm exec -- vitest run a --no-cache"),
    ("vitest run --cache=false a; vitest run b", "vitest run --cache=false a; vitest run b --no-cache"),
    ("echo 'vitest && pytest'; env X=1 npx --yes vitest run 'a b'", "echo 'vitest && pytest'; env X=1 npx --yes vitest run 'a b' --no-cache"),
    ("vitest run >out 2>&1 && pytest test_vitest.py", "vitest run >out 2>&1 --no-cache && pytest test_vitest.py"),
    ("vitest run # keep comment\npytest test_vitest.py", "vitest run --no-cache # keep comment\npytest test_vitest.py"),
    ('python -c "print(\'vitest\')"', 'python -c "print(\'vitest\')"'),
    ('sh -c "vitest run"', 'sh -c "vitest run"'),
])
def test_cache_options_belong_to_the_actual_runner(command, expected):
    assert isolated_command(command) == expected
    assert isolated_command(expected) == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_shell_command_preserves_real_argument_boundaries(tmp_path, reverse):
    vitest = tmp_path / "vitest"
    vitest.write_text('#!/bin/sh\n[ "$1" = run ] && [ "$2" = --no-cache ] && [ "$#" = 2 ]\n')
    vitest.chmod(0o755)
    python_test = tmp_path / "test_vitest_launcher.py"
    python_test.write_text('import sys\nassert sys.argv[1:] == ["argument with spaces"]\n')
    commands = [shlex.join([str(vitest), "run"]), shlex.join([sys.executable, str(python_test), "argument with spaces"])]
    if reverse:
        commands.reverse()
    result = subprocess.run(isolated_command(" && ".join(commands)), shell=True, cwd=tmp_path, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_missing_conda_prefix_is_rejected_without_launching_a_process(tmp_path):
    with pytest.raises(ExecutionBindingError, match="conda environment does not exist"):
        validate_verification_binding("conda run -p ./.conda python -m pytest tests", tmp_path)
    (tmp_path / ".conda" / "conda-meta").mkdir(parents=True)
    validate_verification_binding("conda run -p ./.conda python -m pytest tests", tmp_path)
    with pytest.raises(ExecutionBindingError, match="interpreter does not exist"):
        validate_verification_binding("./.conda/bin/python -m pytest tests", tmp_path)


@pytest.mark.parametrize("command", [
    "cd /elsewhere && conda run -p ./.conda python -m pytest tests",
    "conda run --cwd /elsewhere python -m pytest tests",
    "python -m pytest /elsewhere/tests/test_session.py::SomeTest",
])
def test_verification_cannot_switch_repository(command, tmp_path):
    with pytest.raises(ExecutionBindingError):
        validate_verification_binding(command, tmp_path)


@pytest.mark.parametrize("target", ["fix", "run"])
@pytest.mark.parametrize("seed_key", ["issue_seed", "spec_seed", "top_level"])
def test_foreign_route_blocks_before_ambient_run_or_child_creation(tmp_path, target, seed_key):
    root = _make_project(str(tmp_path))
    orch = Orchestrator(root)
    coordinator = WorkflowCoordinator(orch)
    session = Session(orch, mode="collab", coordinator=coordinator)
    state = SessionState(session_id="parent", mode="collab", status="executing")
    target_payload = {"target_repository": str(tmp_path / "engine")}
    payload = target_payload if seed_key == "top_level" else {seed_key: target_payload}
    with patch.object(coordinator, "prepare_run_route") as prepare, patch.object(session, "_ensure_baseline") as baseline:
        result = session._prepare_workflow_handoff(state, target=target, reason="engine fix", payload=payload)
        assert result.status == "blocked"
        assert result.resolution == "execution_binding_mismatch"
        assert result.execution_log[-1]["retry_fix"] is False
        prepare.assert_not_called()
        baseline.assert_not_called()
    assert not state.active_handoff_id


@pytest.mark.parametrize("target", ["fix", "run", "resume"])
def test_restored_foreign_handoff_stops_without_run_mutation_or_provider(tmp_path, target):
    root = _make_project(str(tmp_path))
    orch = Orchestrator(root)
    coordinator = WorkflowCoordinator(orch)
    current = load_run_state(root)
    current.status = "pending"
    current.current_stage = "implement"
    current.active_blocker = {}
    save_run_state(root, current)
    run_path = root / ".auto-agents/state/run_state.json"
    before = run_path.read_bytes()
    snapshot = coordinator.store.create_root(WorkflowRef("collab", "parent"))
    handoff = coordinator.store.prepare_handoff(snapshot, parent=snapshot.root, target="fix" if target == "resume" else target,
        goal="video acceptance", reason="engine fix", payload={"issue_seed": {"target_repository": str(tmp_path / "engine")}})
    if target == "resume":
        handoff = coordinator.store.prepare_handoff(snapshot, parent=snapshot.root, target="resume",
            goal="video acceptance", reason="resume", payload={"resume_handoff_id": handoff.handoff_id})
    state = SessionState(session_id="parent", mode="collab", status="waiting_child", workflow_id=snapshot.workflow_id, active_handoff_id=handoff.handoff_id)
    with patch.object(coordinator, "_ensure_handoff_checkpoint") as checkpoint, patch.object(coordinator, "start_seeded_session") as child, patch("auto_agents.workflow_runtime.load_run_state", side_effect=AssertionError("ambient run read")):
        assert coordinator.prepare_run_route({"target_repository": str(tmp_path / "engine")})[0] is False
        returned = coordinator._drive_handoff(None, state, snapshot)
        assert returned.status == "blocked"
        assert returned.resolution == "execution_binding_mismatch"
        checkpoint.assert_not_called()
        child.assert_not_called()
        session = Session(orch, mode="collab", coordinator=coordinator)
        with patch.object(session, "_call_agent", side_effect=AssertionError("unexpected provider call")):
            assert coordinator._drive_session(session, returned, snapshot, root=True).status == "blocked"
    assert run_path.read_bytes() == before
    assert coordinator.store.load_handoff(handoff.handoff_id).child is None


def test_bad_environment_stops_fix_before_model_and_propagates_to_parent(tmp_path):
    root = _make_project(str(tmp_path))
    orch = Orchestrator(root)
    session = Session(orch, mode="fix")
    state = SessionState(session_id="child", mode="fix", status="executing", fix_verify_command="conda run -p ./.conda python -m pytest tests")
    with patch.object(session, "_call_agent") as agent, patch.object(session, "_ensure_baseline") as baseline:
        result = session._phase_fix_execute(state)
        assert result.status == "blocked"
        assert result.resolution == "verification_execution_binding"
        agent.assert_not_called()
        baseline.assert_not_called()
    coordinator = WorkflowCoordinator(orch)
    snapshot = coordinator.store.create_root(WorkflowRef("collab", "parent"))
    handoff = coordinator.store.prepare_handoff(snapshot, parent=snapshot.root, target="fix", goal="acceptance", reason="fix", payload={})
    handoff.result = coordinator._session_result(result, handoff)
    parent = SessionState(session_id="parent", mode="collab", status="waiting_child")
    assert coordinator._apply_child_result(parent, handoff).status == "blocked"


def test_same_repository_binding_is_allowed(tmp_path):
    assert repository_binding_error(tmp_path, {"target_repository": str(tmp_path)}) == ""
    assert repository_binding_error(tmp_path, {"issue_seed": {"target_repository": "."}}) == ""


@pytest.mark.parametrize("marker,body", [
    ("ROUTE_WORKFLOW", {"target": "fix", "issue_seed": {"summary": "engine fix"}}),
    ("FIX_DISPOSITION", {"decision": "fix", "summary": "engine fix"}),
    ("FIX_DISPOSITION", {"decision": "run_iteration", "spec_seed": {"title": "engine fix"}}),
])
def test_route_protocol_preserves_repository_binding(tmp_path, marker, body):
    root = _make_project(str(tmp_path))
    session = Session(Orchestrator(root), mode="collab")
    state = SessionState(session_id="parent", mode="collab", status="executing")
    body["target_repository"] = str(tmp_path / "engine")
    returned, error = session._route_collab_workflow_reply(state, f"{marker} v1: {json.dumps(body)}")
    assert not error
    assert returned.status == "blocked"
    assert returned.resolution == "execution_binding_mismatch"
    assert not returned.active_handoff_id


def test_explicit_conda_environment_and_repository_subdirectory_are_supported(tmp_path):
    (tmp_path / "workbench").mkdir()
    (tmp_path / ".conda/conda-meta").mkdir(parents=True)
    validate_verification_binding("cd workbench && conda run -p ../.conda python -m pytest tests", tmp_path)


def test_fix_classification_cannot_silently_change_target_repository(tmp_path):
    root = _make_project(str(tmp_path))
    session = Session(Orchestrator(root), mode="fix")
    state = SessionState(session_id="child", mode="fix", status="conversing", goal="repair a defect")
    disposition = {"decision": "fix", "summary": "engine fix", "target_repository": str(tmp_path / "engine")}
    with patch.object(session, "_call_agent", return_value="FIX_DISPOSITION v1: " + json.dumps(disposition)) as agent:
        returned = session._phase_converse(state)
    assert agent.call_count == 1
    assert returned.status == "blocked"
    assert returned.resolution == "execution_binding_mismatch"
