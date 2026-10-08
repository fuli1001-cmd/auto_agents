"""Execution-context regressions through retained public child recovery."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import pytest
from auto_agents.config import load_project_config
from auto_agents.execution_binding import test_invocations as parse_invocations
from test_session_verification_ownership import project, run_session, _retain_contract
from test_vitest_selector_execution import _prepare_real_vitest

def _snapshot(root):
    return {path.relative_to(root).as_posix(): (path.lstat().st_mode, str(path.readlink()) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None) for path in root.rglob('*')}

def _environment(tmp_path):
    conda = shutil.which('conda')
    assert conda, 'provisioned Conda is required'
    info = json.loads(subprocess.run([conda, 'info', '--json'], capture_output=True, text=True, check=True).stdout)
    provisioned = next((Path(path) for path in info['envs'] if Path(path).parent in map(Path, info['envs_dirs']) and (Path(path) / 'conda-meta/history').is_file()))
    source = tmp_path / 'environments' / 'retained'
    source.mkdir(parents=True)
    for entry in provisioned.iterdir():
        if entry.name != 'etc':
            (source / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    if (provisioned / 'etc').exists():
        shutil.copytree(provisioned / 'etc', source / 'etc', symlinks=True)
    hooks = source / 'etc/conda/activate.d'
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / 'context.sh').write_text('export RETAINED_ACTIVATION="activated value"\n')
    return (conda, source, provisioned)

def _retain_command(root, child, command, reference):
    config = load_project_config(root)
    config.gates.steps = []
    config.gates.commands = [command]
    config.gates.parallel_groups = []
    plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Retained execution', 'requirement_ids': ['REQ-owned'], 'verification_refs': [reference]}], 'verification_steps': []}
    _retain_contract(root, child, config, plan)
    return {path: (root / path).read_bytes() for path in ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}

def _python_shared_controls(source):
    return "\ndef check_shared_inputs():\n    import errno, os\n    from pathlib import Path\n    prefix = Path(os.environ['CONDA_PREFIX'])\n    (prefix / 'private-context-scratch').write_text('permitted')\n    for base in (Path(SOURCE), prefix):\n        for relative in ('etc/conda/activate.d/context.sh', 'conda-meta/history'):\n            path = base / relative\n            try:\n                with path.open('r+'):\n                    pass\n            except OSError as error:\n                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)\n            else:\n                raise AssertionError('shared input allowed write access: ' + str(path))\ncheck_shared_inputs()\n".replace('SOURCE', repr(str(source)))

def _javascript_shared_controls(source, packages):
    return "\nimport {openSync, closeSync, writeFileSync} from 'node:fs';\nimport {join} from 'node:path';\nfunction checkSharedInputs() {\n    const prefix = process.env.CONDA_PREFIX;\n    writeFileSync(join(prefix, 'private-context-scratch'), 'permitted');\n    const inputs = [PACKAGE];\n    for (const base of [SOURCE, prefix]) {\n        inputs.push(join(base, 'etc/conda/activate.d/context.sh'), join(base, 'conda-meta/history'));\n    }\n    for (const input of inputs) {\n        let fd;\n        try { fd = openSync(input, 'r+'); }\n        catch (error) {\n            if (['EPERM', 'EACCES', 'EROFS'].includes(error.code)) continue;\n            throw error;\n        }\n        closeSync(fd);\n        throw Error('shared input allowed write access: ' + input);\n    }\n}\ncheckSharedInputs();\n".replace('SOURCE', json.dumps(str(source))).replace('PACKAGE', json.dumps(str(packages / 'vitest/package.json')))

def _pytest_environment_failure(tmp_path, monkeypatch, selector):
    from copy import deepcopy
    from execution_marker import ExecutionMarker
    root, child = project(tmp_path)
    conda = shutil.which('conda')
    assert conda
    body = ExecutionMarker(tmp_path / 'test-body')
    (root / 'tests/test_owned.py').write_text('def test_owned():\n    ' + body.source("'executed'") + '\n')
    environment = tmp_path / 'missing-environments'
    option = ['-n', 'unavailable-retained'] if selector == 'name' else ['-p', str(environment / 'unavailable')]
    command = shlex.join(['env', 'CONDA_ENVS_PATH=' + str(environment), conda, 'run', *option, 'python', '-m', 'pytest', '-q', 'tests/test_owned.py'])
    ambient = _retain_command(root, child, command, 'cmd:' + command)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status in {'failed', 'blocked'} and calls == ['fix'], saved.to_dict()
    record = next((entry['verification'] for entry in saved.execution_log if entry.get('action') == 'receipt_verification'))
    diagnostic = record['diagnostic']
    assert record['failure_kind'] == 'verification_environment'
    assert diagnostic['failure_kind'] == 'environment'
    assert diagnostic['phase'] == 'runner_preparation'
    assert diagnostic['original_command'] == command
    assert diagnostic['task_ids'] == ['task-owned']
    assert diagnostic['requirement_ids'] == ['REQ-owned']
    assert diagnostic['session_id'] == child.session_id and diagnostic['contract_fingerprint']
    assert diagnostic['retry_fix'] is False and record['retry_fix'] is False
    assert record['executed_commands'] == record['logical_commands'] == 0
    assert not body.exists()
    custody, attempt = (deepcopy(saved.candidate_custody), saved.current_attempt)
    for _ in range(2):
        repeated, calls, _ = run_session(root, monkeypatch)
        assert repeated.status == 'blocked' and calls == []
        assert repeated.candidate_custody == custody and repeated.current_attempt == attempt
        assert any((entry.get('diagnostic') == diagnostic for entry in repeated.execution_log))
        assert not body.exists()
    assert {path: (root / path).read_bytes() for path in ambient} == ambient

def _verification_entry_environment_change(tmp_path, monkeypatch, recovery):
    from auto_agents.session import Session
    from auto_agents.session_candidate import verification_identity
    from execution_marker import ExecutionMarker
    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'actual-environments')
    proof = root / 'tests/test_owned.py'
    source = 'import os\n' + proof.read_text()
    source = source.replace('def test_owned():\n', 'def test_owned():\n    ' + marker.source("os.environ['OWNED_EXPECTED_VALUE'] + '\\n'", append=True) + '\n')
    source = source.replace('"VALUE = 1"', '"VALUE = " + os.environ["OWNED_EXPECTED_VALUE"]')
    proof.write_text(source)
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', 'tests/test_owned.py::test_owned'])
    ambient = _retain_command(root, child, command, 'tests/test_owned.py::test_owned')
    monkeypatch.setenv('OWNED_EXPECTED_VALUE', '2')
    verify = Session._run_verify
    if recovery:
        with monkeypatch.context() as interrupted:

            def pause(*args, **kwargs):
                raise KeyboardInterrupt()
            interrupted.setattr(Session, '_run_verify', pause)
            paused, calls, _ = run_session(root, interrupted)
        assert paused.status == 'paused' and calls == ['fix'], paused.to_dict()
        assert paused.candidate_custody['receipt'] and (not marker.exists())
    admissions, executions = ([], [])

    def changed_at_entry(self, *args, **kwargs):
        admissions.append(verification_identity(self, self._current_state))
        with monkeypatch.context() as temporary:
            temporary.setenv('OWNED_EXPECTED_VALUE', '1')
            actual = verification_identity(self, self._current_state)
            result = verify(self, *args, **kwargs)
            assert result['ok'], result
            assert result['execution_identity'] == actual
            executions.append(actual)
            return result
    monkeypatch.setattr(Session, '_run_verify', changed_at_entry)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status in {'blocked', 'failed'}, saved.to_dict()
    assert calls == ([] if recovery else ['fix'])
    assert admissions and executions and (admissions[-1] != executions[-1])
    assert marker.read_text().splitlines() == ['1']
    record = next((entry for entry in reversed(saved.execution_log) if entry.get('action') == 'receipt_verification'))
    assert record['verification']['ok']
    assert record['identity'] == record['verification']['execution_identity'] == executions[-1]
    assert record['identity'] != admissions[-1]
    assert not saved.candidate_custody.get('delivered_revision')
    assert not any((entry.get('action') == 'receipt_completion' for entry in saved.execution_log))
    monkeypatch.setattr(Session, '_run_verify', verify)
    repeated, _, _ = run_session(root, monkeypatch)
    assert repeated.status != 'completed', repeated.to_dict()
    assert '2' in marker.read_text().splitlines(), 'the restored failing environment must really execute'
    assert not any((entry.get('action') == 'receipt_verification' and entry.get('identity') == admissions[-1] and entry['verification']['ok'] for entry in repeated.execution_log))
    assert {path: (root / path).read_bytes() for path in ambient} == ambient

@pytest.mark.parametrize('command,expected', [('python -m pytest ::test_contract', '::test_contract'), ('cd qa && python -m pytest ::test_contract', '::test_contract'), ("cd qa && python -m pytest ' ::test_contract'", ' ::test_contract'), ('cd qa && python -m pytest tests/test_contract.py::test_contract', 'qa/tests/test_contract.py::test_contract')])
def test_repository_targets_does_not_invent_missing_pytest_path(command, expected):
    assert parse_invocations(command)[0].repository_targets == [expected]
