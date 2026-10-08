from __future__ import annotations
import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from auto_agents.execution_binding import ExecutionBindingError, repository_binding_error, validate_verification_binding
from auto_agents.gate_execution import isolated_command
from auto_agents.models import SessionState
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from auto_agents.config import load_run_state, save_run_state
from test_session import _make_project

@pytest.mark.parametrize('command,expected', [('python -m pytest tests/test_workbench_vitest_launcher.py', 'python -m pytest tests/test_workbench_vitest_launcher.py'), ('conda run -p ./.conda python -m pytest test_vitest.py && python -m pytest tests/test_session.py', 'conda run -p ./.conda python -m pytest test_vitest.py && python -m pytest tests/test_session.py'), ('vitest run a && python -m pytest b', 'vitest run a --no-cache && python -m pytest b'), ('python -m pytest b && npm exec -- vitest run a', 'python -m pytest b && npm exec -- vitest run a --no-cache'), ('vitest run --cache=false a; vitest run b', 'vitest run --cache=false a; vitest run b --no-cache'), ("echo 'vitest && pytest'; env X=1 npx --yes vitest run 'a b'", "echo 'vitest && pytest'; env X=1 npx --yes vitest run 'a b' --no-cache"), ('vitest run >out 2>&1 && pytest test_vitest.py', 'vitest run >out 2>&1 --no-cache && pytest test_vitest.py'), ('vitest run # keep comment\npytest test_vitest.py', 'vitest run --no-cache # keep comment\npytest test_vitest.py'), ('python -c "print(\'vitest\')"', 'python -c "print(\'vitest\')"'), ('sh -c "vitest run"', 'sh -c "vitest run"')])
def test_cache_options_belong_to_the_actual_runner(command, expected):
    assert isolated_command(command) == expected
    assert isolated_command(expected) == expected

@pytest.mark.parametrize('reverse', [False, True])
def test_mixed_shell_command_preserves_real_argument_boundaries(tmp_path, reverse):
    vitest = tmp_path / 'vitest'
    vitest.write_text('#!/bin/sh\n[ "$1" = run ] && [ "$2" = --no-cache ] && [ "$#" = 2 ]\n')
    vitest.chmod(493)
    python_test = tmp_path / 'test_vitest_launcher.py'
    python_test.write_text('import sys\nassert sys.argv[1:] == ["argument with spaces"]\n')
    commands = [shlex.join([str(vitest), 'run']), shlex.join([sys.executable, str(python_test), 'argument with spaces'])]
    if reverse:
        commands.reverse()
    result = subprocess.run(isolated_command(' && '.join(commands)), shell=True, cwd=tmp_path, capture_output=True)
    assert result.returncode == 0, result.stderr

def test_missing_conda_prefix_is_rejected_without_launching_a_process(tmp_path):
    with pytest.raises(ExecutionBindingError, match='conda environment does not exist'):
        validate_verification_binding('conda run -p ./.conda python -m pytest tests', tmp_path)
    (tmp_path / '.conda' / 'conda-meta').mkdir(parents=True)
    validate_verification_binding('conda run -p ./.conda python -m pytest tests', tmp_path)
    with pytest.raises(ExecutionBindingError, match='interpreter does not exist'):
        validate_verification_binding('./.conda/bin/python -m pytest tests', tmp_path)

@pytest.mark.parametrize('command', ['cd /elsewhere && conda run -p ./.conda python -m pytest tests', 'conda run --cwd /elsewhere python -m pytest tests', 'python -m pytest /elsewhere/tests/test_session.py::SomeTest'])
def test_verification_cannot_switch_repository(command, tmp_path):
    with pytest.raises(ExecutionBindingError):
        validate_verification_binding(command, tmp_path)

def test_same_repository_binding_is_allowed(tmp_path):
    assert repository_binding_error(tmp_path, {'target_repository': str(tmp_path)}) == ''
    assert repository_binding_error(tmp_path, {'issue_seed': {'target_repository': '.'}}) == ''

def test_explicit_conda_environment_and_repository_subdirectory_are_supported(tmp_path):
    (tmp_path / 'workbench').mkdir()
    (tmp_path / '.conda/conda-meta').mkdir(parents=True)
    validate_verification_binding('cd workbench && conda run -p ../.conda python -m pytest tests', tmp_path)
