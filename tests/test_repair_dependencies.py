import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult, CommandResult, GateResult, RunState
from auto_agents.io_utils import write_json
from auto_agents.repair_dependencies import (
    VerificationDependencyError, missing_verification_dependency,
    prepare_verification_dependency, verification_dependency_state,
)
from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision


MISSING = ("E AssertionError: npm error code ENOTCACHED\n"
           "E npm error request to https://registry.npmjs.org/vitest failed: "
           "cache mode is 'only-if-cached' but no cached response is available.\n1 failed, 24 passed")


def runner_for(root):
    runner = AutoAgentsSelfRepairRunner(
        SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace())),
        target_project_root=root, error=RuntimeError("engine failure"),
        decision=SelfRepairDecision(True, category="dependency", fingerprint="dependency"),
    )
    runner.repo_root = root
    runner._verification_python_cache = sys.executable
    return runner


@pytest.mark.parametrize("output,expected", [
    (MISSING, "vitest"),
    ("E RuntimeError: Vitest integration dependency is missing: supervisor must provision it", "vitest"),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'vitest' imported from /tmp/test.js", "vitest"),
    ("/bin/sh: 1: vitest: not found", "vitest"),
    ("FAILED tests/test_vitest.py AssertionError: expected 2 but got 3", ""),
    ('raise RuntimeError("Vitest integration dependency is missing:")', ""),
    ("npm error code ENOTCACHED\nrequest to https://registry.npmjs.org/unrelated failed", ""),
    ("ModuleNotFoundError: No module named 'auto_agents.new_feature'", ""),
])
def test_only_concrete_allowlisted_dependency_errors_trigger_preparation(output, expected):
    assert missing_verification_dependency(output) == expected


def test_same_candidate_proof_is_retried_after_dependency_preparation(tmp_path):
    runner = runner_for(tmp_path)
    command = "python -m pytest -q tests/test_vitest.py"
    results = [GateResult(False, [CommandResult(command, False, 1, stdout=MISSING)], "missing"),
               GateResult(True, [CommandResult(command, True, 0, stdout="25 passed")], "passed")]
    with patch("auto_agents.self_repair.run_commands", side_effect=results) as execute, \
         patch.object(runner, "_prepare_verification_dependency") as prepare:
        result = runner._run_verification_commands([command], tmp_path)
    assert result.ok and "25 passed" in result.summary
    prepare.assert_called_once()
    assert prepare.call_args.args[0].key == "node:vitest"
    assert prepare.call_args.args[1] == MISSING + "\n"
    assert execute.call_count == 2
    assert execute.call_args_list[0] == execute.call_args_list[1]


def test_preparation_is_bounded_and_requires_supervisor_interpreter(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    runner._real_project_root = tmp_path / "target"
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"root": str(tmp_path / "control")}))
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_CONTROL_CONFIG", str(config))
    with patch("auto_agents.repair_dependencies.prepare_verification_dependency") as prepare:
        runner._prepare_verification_dependency("vitest", MISSING)
        with pytest.raises(VerificationDependencyError, match="still unavailable"):
            runner._prepare_verification_dependency("vitest", MISSING)
    prepare.assert_called_once()
    with pytest.raises(RuntimeError, match="supervisor-owned"):
        prepare_verification_dependency({"root": str(tmp_path / "control")}, sys.executable, "vitest")


def init_repo(path):
    path.mkdir()
    for args in [("init", "-q"), ("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid")]:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("test repository\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


@pytest.mark.parametrize("output", [MISSING, "/bin/sh: 1: ffmpeg: not found",
    "E ModuleNotFoundError: No module named 'missing_verifier_package'",
    "Error: Cannot find module '@unlisted/compiler'",
    "E OSError: libunlisted.so: cannot open shared object file: No such file or directory"])
def test_missing_environment_retains_candidate_without_redesign_or_codegen_retry(tmp_path, monkeypatch, output):
    engine, target = tmp_path / "engine", tmp_path / "target"
    init_repo(engine)
    init_repo(target)
    write_json(target / ".auto-agents/state/run_state.json", RunState(run_id="run", status="blocked").to_dict())
    runner = runner_for(target)
    runner.repo_root = engine
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_CONFIG", raising=False)
    calls = []

    def generate(request):
        calls.append(request)
        assert len(calls) == 1, "environment failure must not ask for another patch"
        (request.cwd / "fix.py").write_text("FIXED = True\n")
        return AgentResult(True, [], request.output_path, summary="COMMIT_MESSAGE: fix engine")

    runner.target_orchestrator._call_with_failover = generate
    failure = GateResult(False, [CommandResult("pytest", False, 1, stdout=output)], "missing")
    with patch("auto_agents.self_repair.auto_agents_repo_root", return_value=engine), \
         patch("auto_agents.self_repair.run_commands", return_value=failure), \
         patch.object(runner, "_automatic_contract_reanalysis") as redesign:
        result = runner.run()
    assert result.status == "infrastructure_blocked", result.reason
    assert result.infrastructure_failure and not result.ok
    assert len(calls) == 1
    redesign.assert_not_called()
    patches = list(target.glob(".auto-agents/runs/run/self-repair/*/c*/partial-candidate.diff"))
    assert patches and "FIXED = True" in patches[0].read_text()
    assert not (target / "fix.py").exists()


@pytest.mark.parametrize("fail", [False, True])
def test_tool_setup_uses_trusted_lock_and_never_certifies_failed_install(tmp_path, monkeypatch, fail):
    from auto_agents import artifact_runtime
    from auto_agents.artifact_store import ArtifactStore
    artifact_runtime.activate(scope="test-dependencies")
    config = {"root": str(tmp_path / "controller")}
    python = Path(config["root"]) / "environments/python/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    node = tmp_path / "node"
    node.write_bytes(b"node binary identity")
    npm = tmp_path / "npm"
    npm.write_bytes(b"npm binary identity")
    calls = []
    original_which = __import__("shutil").which
    monkeypatch.setattr("auto_agents.repair_dependencies.shutil.which",
                        lambda name, **kwargs: str(node if name == "node" else npm) if name in {"node", "npm"} else original_which(name, **kwargs))

    def execute(self, command, **kwargs):
        calls.append((command, kwargs))
        if "ci" in command and fail:
            raise subprocess.CalledProcessError(1, command, stderr="registry unavailable")
        if "ci" in command:
            entry = kwargs["cwd"] / "node_modules/vitest/vitest.mjs"
            entry.parent.mkdir(parents=True)
            entry.write_text("// installed fixture")
        return subprocess.CompletedProcess(command, 0, "v22.20.0\n" if command[0] == str(node) else "vitest/5.0.0", "")

    monkeypatch.setattr("auto_agents.repair_dependencies.EnvironmentSetupLog.run", execute)
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            prepare_verification_dependency(config, str(python), "vitest")
        assert not verification_dependency_state(python)
        assert not list(python.parent.parent.glob("verification-tools/*/ready.json"))
        return
    state = prepare_verification_dependency(config, str(python), "vitest")
    assert verification_dependency_state(python)["tools"]["node-tools"] == state
    root = Path(state["root"])
    assert json.loads((root / "package.json").read_text())["dependencies"] == {"vitest": "5.0.0"}
    installation = next(call for call in calls if "ci" in call[0])
    assert "--ignore-scripts" in installation[0] and "--engine-strict" in installation[0]
    assert installation[1]["cwd"] == root
    prepare_verification_dependency(config, str(python), "vitest")
    assert sum("ci" in call[0] for call in calls) == 1
    records = [record for record in ArtifactStore().rows() if record["path"] == str(root)]
    assert len(records) == 1 and records[0]["kind"] == "environment"


def test_toolchain_change_invalidates_cached_environment_identity(tmp_path):
    runner = runner_for(tmp_path)
    with patch("auto_agents.repair_dependencies.verification_dependency_state", side_effect=[{}, {"fingerprint": "vitest-ready"}]), \
         patch("auto_agents.self_repair.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "same python", "")):
        before = runner._full_suite_environment_fingerprint()
        after = runner._full_suite_environment_fingerprint()
    assert before != after
    assert before[:-1] == after[:-1]
