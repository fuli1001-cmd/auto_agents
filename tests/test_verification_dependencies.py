import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from unittest.mock import patch

import pytest

from auto_agents.verification_dependencies import (
    MissingDependency, VerificationDependencyError, detect_verification_dependencies,
)
from auto_agents.repair_dependencies import prepare_verification_dependency, verification_dependency_state
from test_repair_dependencies import runner_for


@pytest.mark.parametrize("output,key", [
    ("/bin/sh: 1: ffmpeg: not found", "executable:ffmpeg"),
    ("/bin/bash: line 4: pandoc: command not found", "executable:pandoc"),
    ("jq: command not found", "executable:jq"),
    ("/bin/sh: 1: /opt/tool: Permission denied", "executable:/opt/tool"),
    ("RuntimeError: No media-encoder exe could be found. Install it on your system.", "executable:media-encoder"),
    ("RuntimeError: arbitrary-compiler binary is not installed", "executable:arbitrary-compiler"),
    ("RuntimeError: Could not find executable 'custom-linter'", "executable:custom-linter"),
    ("ExecutableNotFound: failed to execute PosixPath('dot'), make sure it is on PATH", "executable:dot"),
    ("E auto_agents.verification_dependencies.VerificationDependencyError: verification environment prerequisite executable:custom: unavailable", "executable:custom"),
    ("env: ‘custom-runtime’: No such file or directory", "executable:custom-runtime"),
    ("'compiler.exe' is not recognized as an internal or external command", "executable:compiler.exe"),
    ("E ModuleNotFoundError: No module named 'requests'", "python:requests"),
    ("E ModuleNotFoundError: No module named 'PIL'", "python:PIL"),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@example/compiler' imported from /tmp/a.js", "node:@example/compiler"),
    ("Error: Cannot find module 'typescript/lib/typescript.js'", "node:typescript"),
    ("Error: Cannot find module '/tmp/pkg/node_modules/esbuild/bin/esbuild'", "node:esbuild"),
    ("npm error code ENOTCACHED\nnpm error request to https://registry.example/@example%2fcompiler failed: cache mode is 'only-if-cached'", "node:@example/compiler"),
    ("tool: error while loading shared libraries: libuncommon.so.9: cannot open shared object file: No such file or directory", "shared_library:libuncommon.so.9"),
    ("E OSError: libother.so: cannot open shared object file: No such file or directory", "shared_library:libother.so"),
    ("browserType.launch: Executable doesn't exist at /opt/browser/engine", "executable:/opt/browser/engine"),
])
def test_software_detection_is_not_a_list_of_tool_names(tmp_path, output, key):
    assert [item.key for item in detect_verification_dependencies(output, workspace=tmp_path)] == [key]


@pytest.mark.parametrize("output", [
    "E AssertionError: expected 2, got 3",
    "E FileNotFoundError: [Errno 2] No such file or directory: 'input.json'",
    "E ModuleNotFoundError: No module named 'auto_agents.new_feature'",
    "E ModuleNotFoundError: No module named 'local_package.new_feature'",
    "Error: Cannot find module './new-file.js'",
    "Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/tmp/source/new-file.js'",
    "/bin/sh: 1: scripts/build.sh: not found",
    "npm error Missing script: 'compile'",
    "E ImportError: cannot import name 'function' from 'package'",
    "RuntimeError: No data file could be found",
    "RuntimeError: Could not find input 'input.json'",
])
def test_code_and_input_failures_remain_candidate_failures(tmp_path, output):
    (tmp_path / "src/local_package").mkdir(parents=True)
    assert not detect_verification_dependencies(output, workspace=tmp_path)


def test_trusted_pytest_distinguishes_missing_program_from_missing_data(tmp_path):
    from auto_agents import verification_pytest
    root = tmp_path / "candidate"
    root.mkdir()
    not_executable = tmp_path / "unavailable-program"
    not_executable.write_text("#!/bin/sh\nexit 0\n")
    (root / "test_dependencies.py").write_text('''import subprocess
import importlib
import pytest

def test_missing_program():
    subprocess.run(["auto-agents-test-unavailable-executable-3748"], check=True)

def test_missing_import():
    importlib.import_module("auto_agents_test_unavailable_distribution_3748")

def test_missing_data():
    open("missing-input.json")

def test_expected_missing_program_is_not_the_failure():
    with pytest.raises((FileNotFoundError, PermissionError)):
        subprocess.run(["auto-agents-test-expected-missing-3748"])
    assert False, "a separate assertion"
''')
    with (root / "test_dependencies.py").open("a") as source:
        source.write(f"\ndef test_program_not_executable():\n    subprocess.run([{str(not_executable)!r}], check=True)\n")
        source.write(f"\ndef test_missing_working_directory():\n    subprocess.run([{sys.executable!r}, '-c', 'pass'], cwd={str(tmp_path / 'missing-cwd')!r})\n")
    receipt = tmp_path / "receipt.json"
    result = subprocess.run([sys.executable, verification_pytest.__file__, str(root), str(receipt),
                             "-q", str(root / "test_dependencies.py")], cwd=root, text=True, capture_output=True, timeout=30)
    assert result.returncode == 1, result.stderr
    data = json.loads(receipt.read_text())
    assert {f"{item['kind']}:{item['name']}" for item in data["missing_dependencies"]} == {
        "executable:auto-agents-test-unavailable-executable-3748",
        "python:auto_agents_test_unavailable_distribution_3748",
        "executable:" + str(not_executable),
    }
    # A subprocess traceback elsewhere in the same pytest batch must not turn
    # the missing input.json or an expected caught exception into software.
    detected = detect_verification_dependencies(result.stdout, workspace=root, structured=data["missing_dependencies"])
    assert "executable:missing-input.json" not in {item.key for item in detected}


def managed_environment(tmp_path):
    root = tmp_path / "control"
    python = root / "environments/python/bin/python"
    python.parent.mkdir(parents=True)
    root.chmod(0o700)
    python.symlink_to(sys.executable)
    specs = root / "dependency-specs"
    specs.mkdir()
    return {"root": str(root)}, python, specs


def fixture_wheel(specs):
    wheel = specs / "verifier_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("verifier_fixture.py", "VALUE = 42\n")
        archive.writestr("verifier_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: verifier-fixture\nVersion: 1.0\n")
        archive.writestr("verifier_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("verifier_fixture-1.0.dist-info/RECORD", "")
    (specs / "requirements.lock").write_text(f"verifier-fixture @ {wheel.as_uri()} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n")


def test_declared_python_and_executable_are_prepared_without_changing_engine_python(tmp_path):
    config, python, specs = managed_environment(tmp_path)
    fixture_wheel(specs)
    program = specs / "custom-program"
    program.write_text("#!/bin/sh\nprintf 'READY\\n'\n")
    program.chmod(0o755)
    config["verification_dependencies"] = {
        "python-fixture": {"installer": "pip", "requirements": "requirements.lock", "provides": ["python:verifier_fixture"]},
        "binary-fixture": {"installer": "existing", "path": str(program), "sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                           "provides": ["executable:fixture-program"]},
    }
    (Path(config["root"]) / "operator.json").write_text(json.dumps(config))
    python_state = prepare_verification_dependency(config, str(python), MissingDependency("python", "verifier_fixture"))
    binary_state = prepare_verification_dependency(config, str(python), MissingDependency("executable", "fixture-program"))
    active = verification_dependency_state(python)
    assert len(active["tools"]) == 2
    result = subprocess.run([str(python), "-c", "import verifier_fixture; assert verifier_fixture.VALUE == 42"],
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(active["python_paths"])}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(Path(binary_state["root"]) / "bin/fixture-program")], capture_output=True, text=True)
    assert result.stdout == "READY\n"
    program.write_text("#!/bin/sh\nprintf 'changed source\\n'\n")
    result = subprocess.run([str(Path(binary_state["root"]) / "bin/fixture-program")], capture_output=True, text=True)
    assert result.stdout == "READY\n"  # Execute the approved snapshot, not a mutable host file.
    assert Path(python_state["root"]).is_relative_to(python.parent.parent)
    result = subprocess.run([str(python), "-c", "import importlib.util; assert importlib.util.find_spec('verifier_fixture') is None"], capture_output=True)
    assert result.returncode == 0
    previous = active["fingerprint"]
    lock = specs / "requirements.lock"
    lock.write_text("# changed operator declaration\n" + lock.read_text())
    changed = verification_dependency_state(python)
    assert "python-fixture" not in changed["tools"] and changed["fingerprint"] != previous


@pytest.mark.parametrize("dependency", [MissingDependency("executable", "unlisted-compiler"), MissingDependency("python", "unlisted_distribution"),
                                        MissingDependency("node", "@unlisted/compiler"), MissingDependency("shared_library", "libunlisted.so")])
def test_undeclared_software_blocks_without_guessing_an_install_command(tmp_path, dependency):
    config, python, _ = managed_environment(tmp_path)
    with patch("auto_agents.repair_dependencies.EnvironmentSetupLog.run") as install:
        with pytest.raises(VerificationDependencyError, match="no trusted preparation recipe") as failure:
            prepare_verification_dependency(config, str(python), dependency)
    install.assert_not_called()
    result = failure.value.to_result()
    assert result["failure_domain"] == "execution_environment"
    assert result["missing_dependencies"][0]["kind"] == dependency.kind


def test_failed_preparation_is_not_repeated_by_a_new_worker_in_the_same_generation(tmp_path, monkeypatch):
    from auto_agents.repair_control import Store
    from test_repair_control import registration, failure
    config, python, specs = managed_environment(tmp_path)
    (specs / "requirements.lock").write_text("unavailable==1 --hash=sha256:" + "0" * 64)
    config["verification_dependencies"] = {"fixture": {"installer": "pip", "provides": ["python:unavailable"], "requirements": "requirements.lock"}}
    store = Store(config["root"])
    subscriber = store.register(registration(tmp_path / "project"))
    job = store.submit(subscriber, failure(tmp_path / "project"))
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_JOB", job)
    with patch("auto_agents.repair_dependencies.EnvironmentSetupLog.run", side_effect=RuntimeError("download failed")) as install:
        with pytest.raises(RuntimeError, match="download failed"):
            prepare_verification_dependency(config, str(python), MissingDependency("python", "unavailable"))
        with pytest.raises(VerificationDependencyError, match="already attempted"):
            prepare_verification_dependency(config, str(python), MissingDependency("python", "unavailable"))
    assert install.call_count == 1
    assert not verification_dependency_state(python)


def test_stale_parallel_failure_retries_new_environment_instead_of_preparing_again(tmp_path):
    runner = runner_for(tmp_path)
    with patch("auto_agents.repair_dependencies.verification_dependency_state", return_value={"fingerprint": "prepared"}), \
         patch.object(runner, "_prepare_verification_dependency") as prepare:
        assert runner._handle_verification_dependencies("/bin/sh: 1: ffmpeg: not found", tmp_path, dependency_state={})
    prepare.assert_not_called()


def test_changing_temporary_binary_path_does_not_reset_preparation_budget(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    runner._real_project_root = tmp_path / "target"
    config, _, _ = managed_environment(tmp_path)
    configured = Path(config["root"]) / "operator.json"
    configured.write_text(json.dumps(config))
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_CONTROL_CONFIG", str(configured))
    with patch("auto_agents.repair_dependencies.prepare_verification_dependency") as prepare:
        runner._prepare_verification_dependency(MissingDependency("executable", "/tmp/first/tool"), "missing")
        with pytest.raises(VerificationDependencyError, match="still unavailable"):
            runner._prepare_verification_dependency(MissingDependency("executable", "/tmp/second/tool"), "missing")
    prepare.assert_called_once()


def test_recipe_cycles_block_before_installation(tmp_path):
    config, python, _ = managed_environment(tmp_path)
    config["verification_dependencies"] = {
        "a": {"provides": ["executable:cycle-a"], "requires": ["executable:cycle-b"], "installer": "existing"},
        "b": {"provides": ["executable:cycle-b"], "requires": ["executable:cycle-a"], "installer": "existing"},
    }
    with patch("auto_agents.repair_dependencies.EnvironmentSetupLog.run") as install:
        with pytest.raises(VerificationDependencyError, match="cyclic"):
            prepare_verification_dependency(config, str(python), MissingDependency("executable", "cycle-a"))
    install.assert_not_called()


@pytest.mark.parametrize("mismatch", ["none", "generation", "pid", "ticks", "workspace", "inode"])
def test_blocker_receipt_only_stops_its_bound_owner(tmp_path, mismatch):
    from auto_agents.repair_control import atomic_json, start_ticks
    root = tmp_path / "candidate"
    root.mkdir()
    runner = runner_for(root)
    runner._repair_control_binding = {"root": str(tmp_path / "control"), "job": "job", "generation": 2}
    info = root.stat()
    record = {"job": "job", "generation": 2, "owner_pid": os.getpid(), "owner_ticks": start_ticks(os.getpid()),
              "workspace": str(root), "inode": [info.st_dev, info.st_ino],
              "result": VerificationDependencyError(MissingDependency("executable", "new-tool"), "unavailable").to_result()}
    key = {"pid": "owner_pid", "ticks": "owner_ticks"}.get(mismatch, mismatch)
    if mismatch != "none":
        record[key] = "different"
    atomic_json(tmp_path / "control/jobs/job/verification-environment-g2.json", record)
    assert bool(runner._verification_environment_blocker(root)) == (mismatch == "none")


def test_dependency_blocker_interrupts_generation_and_preserves_unfinished_patch(tmp_path):
    from auto_agents.models import AgentResult, AgentTermination, RunState
    from auto_agents.io_utils import write_json
    from auto_agents.repair_control import atomic_json, start_ticks
    from test_repair_dependencies import init_repo
    engine, target = tmp_path / "engine", tmp_path / "target"
    init_repo(engine)
    init_repo(target)
    write_json(target / ".auto-agents/state/run_state.json", RunState(run_id="run", status="blocked").to_dict())
    runner = runner_for(target)
    runner.repo_root = engine
    runner._repair_control_binding = {"root": str(tmp_path / "control"), "job": "job", "generation": 1}
    calls = []

    def generate(request):
        calls.append(request)
        assert len(calls) == 1
        (request.cwd / "fix.py").write_text("FIXED = True\n")
        info = request.cwd.stat()
        error = VerificationDependencyError(MissingDependency("executable", "never-seen-tool"), "no trusted preparation recipe")
        atomic_json(tmp_path / "control/jobs/job/verification-environment-g1.json", {
            "job": "job", "generation": 1, "owner_pid": os.getpid(), "owner_ticks": start_ticks(os.getpid()),
            "workspace": str(request.cwd), "inode": [info.st_dev, info.st_ino], "result": error.to_result()})
        assert request.termination_probe() == "verification_environment_blocked"
        return AgentResult(False, [], request.output_path, termination=AgentTermination("verification_environment_blocked"))

    runner.target_orchestrator._call_with_failover = generate
    with patch("auto_agents.self_repair.auto_agents_repo_root", return_value=engine), \
         patch.object(runner, "_automatic_contract_reanalysis") as redesign:
        result = runner.run()
    assert result.status == "infrastructure_blocked", result.reason
    assert runner._verification_dependency_failure["missing_dependencies"][0]["name"] == "never-seen-tool"
    assert len(calls) == 1
    redesign.assert_not_called()
    patches = list(target.glob(".auto-agents/runs/run/self-repair/*/c*/partial-candidate.diff"))
    assert patches and "FIXED = True" in patches[0].read_text()


@pytest.mark.parametrize("obsolete", [False, True])
def test_trusted_worker_result_reaches_live_owner_without_crossing_generations(tmp_path, obsolete):
    from types import SimpleNamespace
    from auto_agents.repair_control import Supervisor, atomic_json
    from test_repair_control import configuration
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    result = VerificationDependencyError(MissingDependency("python", "unknown_import"), "unavailable").to_result()
    root = Path(config["root"]) / "verifications/check"
    atomic_json(root / "result.json", result)
    context = {"job": "job", "generation": 3, "pid": os.getpid(), "ticks": 42, "workspace": str(tmp_path), "inode": [1, 2]}
    with supervisor.store.connect() as db:
        db.execute("INSERT INTO verifications VALUES('check','context','running','{}',0)")
    supervisor.verification_processes["check"] = SimpleNamespace(poll=lambda: 1)
    with patch.object(supervisor, "verification_context", return_value=context,
                      side_effect=RuntimeError("obsolete generation") if obsolete else None):
        supervisor.tick_verifications()
    marker = Path(config["root"]) / "jobs/job/verification-environment-g3.json"
    assert marker.exists() == (not obsolete)
    if not obsolete:
        assert json.loads(marker.read_text())["result"]["missing_dependencies"][0]["name"] == "unknown_import"
