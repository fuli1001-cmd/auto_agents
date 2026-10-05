import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from unittest.mock import patch

import pytest


def test_explicit_requirement_survives_separate_launcher_module_identity():
    from types import SimpleNamespace
    from auto_agents.verification_dependencies import exception_dependencies
    error = RuntimeError('verification prerequisite missing')
    error.requirement = SimpleNamespace(kind='executable', name='another-build-tool')
    assert [item.key for item in exception_dependencies(error)] == ['executable:another-build-tool']


@pytest.mark.parametrize('name', ['vitest', 'another-build-tool'])
def test_required_executable_reports_a_typed_prerequisite(name, monkeypatch):
    from auto_agents.verification_dependencies import require_verification_executable, VerificationDependencyError
    monkeypatch.setattr('auto_agents.verification_dependencies.shutil.which', lambda _name: None)
    with pytest.raises(VerificationDependencyError) as failure:
        require_verification_executable(name)
    assert failure.value.requirement.key == 'executable:' + name

from auto_agents.verification_dependencies import (
    MissingDependency, VerificationDependencyError, detect_verification_dependencies,
)


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
