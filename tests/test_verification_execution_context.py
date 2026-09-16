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
    return {path.relative_to(root).as_posix(): (
        path.lstat().st_mode,
        str(path.readlink()) if path.is_symlink() else
        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        for path in root.rglob('*')}


def _environment(tmp_path):
    conda = shutil.which('conda')
    assert conda, 'provisioned Conda is required'
    info = json.loads(subprocess.run([conda, 'info', '--json'], capture_output=True,
                                     text=True, check=True).stdout)
    provisioned = next(Path(path) for path in info['envs']
                       if Path(path).parent in map(Path, info['envs_dirs'])
                       and (Path(path) / 'conda-meta/history').is_file())
    # A local provisioned prefix adds observable activation hooks without
    # changing any installed environment. Installed Conda resolves and runs it.
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
    return conda, source, provisioned


def _retain_command(root, child, command, reference):
    config = load_project_config(root)
    config.gates.steps = []
    config.gates.commands = [command]
    config.gates.parallel_groups = []
    plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Retained execution',
                      'requirement_ids': ['REQ-owned'], 'verification_refs': [reference]}],
            'verification_steps': []}
    _retain_contract(root, child, config, plan)
    return {path: (root / path).read_bytes() for path in
            ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}


def _python_shared_controls(source):
    return '''
def check_shared_inputs():
    import errno, os
    from pathlib import Path
    prefix = Path(os.environ['CONDA_PREFIX'])
    (prefix / 'private-context-scratch').write_text('permitted')
    for base in (Path(SOURCE), prefix):
        for relative in ('etc/conda/activate.d/context.sh', 'conda-meta/history'):
            path = base / relative
            try:
                with path.open('r+'):
                    pass
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            else:
                raise AssertionError('shared input allowed write access: ' + str(path))
check_shared_inputs()
'''.replace('SOURCE', repr(str(source)))


def _javascript_shared_controls(source, packages):
    return '''
import {openSync, closeSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
function checkSharedInputs() {
    const prefix = process.env.CONDA_PREFIX;
    writeFileSync(join(prefix, 'private-context-scratch'), 'permitted');
    const inputs = [PACKAGE];
    for (const base of [SOURCE, prefix]) {
        inputs.push(join(base, 'etc/conda/activate.d/context.sh'), join(base, 'conda-meta/history'));
    }
    for (const input of inputs) {
        let fd;
        try { fd = openSync(input, 'r+'); }
        catch (error) {
            if (['EPERM', 'EACCES', 'EROFS'].includes(error.code)) continue;
            throw error;
        }
        closeSync(fd);
        throw Error('shared input allowed write access: ' + input);
    }
}
checkSharedInputs();
'''.replace('SOURCE', json.dumps(str(source))).replace('PACKAGE', json.dumps(str(packages / 'vitest/package.json')))


@pytest.mark.parametrize('active', [False, True])
def test_named_conda_vitest_discovery_preserves_activation_cwd_and_shared_environment(tmp_path, monkeypatch, active):
    from execution_marker import ExecutionMarker

    root, child = project(tmp_path)
    _prepare_real_vitest(root, monkeypatch)
    conda, source, provisioned = _environment(tmp_path)
    shared_before, provisioned_before = _snapshot(source), _snapshot(provisioned)
    packages = (root / 'node_modules').resolve()
    packages_before = _snapshot(packages)
    if active:
        monkeypatch.setenv('CONDA_PREFIX', str(source))
        monkeypatch.setenv('CONDA_SHLVL', '1')
    (root / 'web/tests').mkdir(parents=True)
    (root / 'web/vitest.config.js').write_text(
        _javascript_shared_controls(source, packages) +
        'if (process.env.RETAINED_ACTIVATION !== "activated value") throw Error("activation lost");\n'
        'if (process.env.INLINE_CONTEXT !== "inline value") throw Error("assignment lost");\n'
        'export default {test: {include: ["tests/*.test.js"]}};\n')
    marker = ExecutionMarker(tmp_path / 'executions')
    (root / 'web/tests/owned.test.js').write_text(
        _javascript_shared_controls(source, packages) +
        'import {test, expect} from "vitest";\n'
        'import {readFileSync} from "node:fs";\n'
        'test("retained", async () => {\n'
        'checkSharedInputs();\n'
        'expect(process.env.RETAINED_ACTIVATION).toBe("activated value");\n'
        'expect(process.env.INLINE_CONTEXT).toBe("inline value");\n'
        'const value = readFileSync("../value.py", "utf8");\n'
        f'await {marker.javascript_source("value", append=True)};\n'
        'expect(value).toBe("VALUE = 1\\n");\n});\n')
    command = shlex.join(['env', 'CONDA_ENVS_PATH=' + str(source.parent),
                         'INLINE_CONTEXT=inline value', conda, 'run', '--cwd', 'web',
                         '--name', source.name, '--no-capture-output', 'npx', '--no-install',
                         'vitest', 'run', 'owned.test.js', '--maxWorkers=1',
                         '--reporter=json', '--outputFile=report.json']) + ' > runner.log 2>&1'
    ambient = _retain_command(root, child, command, 'web/owned.test.js')
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    assert 'VALUE = 1' in marker.read_text().splitlines()
    assert command in saved.verification_binding['required_commands']
    assert 'web/tests/owned.test.js' in saved.verification_binding['proof_sources']
    assert _snapshot(source) == shared_before
    assert _snapshot(provisioned) == provisioned_before
    assert _snapshot(packages) == packages_before
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('selector', ['name', 'prefix', 'prefix_equals'])
def test_relative_prefix_and_named_launchers_share_execution_semantics(tmp_path, monkeypatch, selector):
    from execution_marker import ExecutionMarker

    root, child = project(tmp_path)
    conda, source, provisioned = _environment(tmp_path)
    before, installed_before = _snapshot(source), _snapshot(provisioned)
    (root / 'launch').mkdir()
    (root / 'launch/environment').symlink_to(source, target_is_directory=True)
    (root / 'web/tests').mkdir(parents=True)
    marker = ExecutionMarker(tmp_path / 'python-executed')
    # Use the provisioned engine interpreter while retaining Conda activation.
    (root / 'web/tests/test_context.py').write_text(
        _python_shared_controls(source) +
        'import os\nfrom pathlib import Path\n'
        'def test_context():\n'
        '    check_shared_inputs()\n'
        '    assert os.environ["RETAINED_ACTIVATION"] == "activated value"\n'
        '    assert os.environ["INLINE_CONTEXT"] == "inline value"\n'
        '    assert Path.cwd().name == "web"\n'
        f'    assert Path(os.environ["CONDA_PREFIX"]).resolve() != Path({str(source)!r})\n'
        '    assert (Path(os.environ["CONDA_PREFIX"]) / "conda-meta").is_symlink()\n'
        '    value = Path("../value.py").read_text()\n'
        f'    {marker.source("value")}\n'
        '    assert value == "VALUE = 1\\n"\n')
    options = {'name': ['-n', source.name], 'prefix': ['-p', './environment'],
               'prefix_equals': ['--prefix=./environment']}[selector]
    command = 'cd launch && ' + shlex.join([
        'env', 'CONDA_ENVS_PATH=' + str(source.parent), 'INLINE_CONTEXT=inline value',
        conda, 'run', *options, '--cwd=../web', '--no-capture-output', sys.executable,
        '-m', 'pytest', '-q', '--log-auto-indent=2', '--junit-xml=report.xml',
        '--junit-prefix', 'tests/not-a-selector.py', '--', 'tests/test_context.py'])
    ambient = _retain_command(root, child, command, 'web/tests/test_context.py')
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    assert marker.read_text() == 'VALUE = 1\n'
    assert command in saved.verification_binding['required_commands']
    assert _snapshot(source) == before
    assert _snapshot(provisioned) == installed_before
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('option', ['--junit-xml', '--junit-prefix', '--log-auto-indent'])
@pytest.mark.parametrize('equals', [False, True])
def test_runner_option_values_and_unknown_arity_do_not_become_targets(tmp_path, monkeypatch, option, equals):
    value = '2' if option == '--log-auto-indent' else 'tests/not-a-selector.py'
    option_text = option + '=' + value if equals else option + ' ' + value
    invocation, = parse_invocations('cd web && env INLINE_CONTEXT=value conda run -n retained --cwd tests '
        'python -m pytest ' + option_text + ' -- test_owned.py')
    assert invocation.targets == ('test_owned.py',)
    assert invocation.repository_targets == ['web/tests/test_owned.py']
    assert invocation.shell_cwd == 'web'
    assert ('INLINE_CONTEXT', 'value') in invocation.environment
    unknown, = parse_invocations('python -m pytest --unknown-option tests/value.py qa/regression.py')
    assert unknown.targets is None
    root, child = project(tmp_path)
    command = './.conda/bin/python -m pytest --unknown-option tests/value.py qa/regression.py'
    _retain_command(root, child, command, 'cmd:' + command)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'blocked', saved.to_dict()
    assert calls == []
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['failure_kind'] == 'unsupported_invocation'
    assert diagnostic['retry_fix'] is False
    assert diagnostic['session_id'] == child.session_id


@pytest.mark.parametrize('failure', ['environment', 'discovery', 'missing'])
def test_discovery_reports_environment_failure_separately_from_missing_selection(tmp_path, monkeypatch, failure):
    root, child = project(tmp_path)
    _prepare_real_vitest(root, monkeypatch)
    (root / 'tests/owned.test.js').write_text('import {test} from "vitest"; test("retained", () => {});\n')
    conda = shutil.which('conda')
    assert conda
    command = 'npx --no-install vitest run owned.test.js --maxWorkers=1'
    if failure == 'environment':
        command = shlex.join([conda, 'run', '-p', str(tmp_path / 'absent-environment')]) + ' ' + command
    else:
        (root / 'vitest.config.js').write_text('throw Error("invalid retained config");\n' if failure == 'discovery'
            else 'export default {test: {exclude: ["**/owned.test.js"]}};\n')
    ambient = _retain_command(root, child, command, 'owned.test.js')
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'blocked', saved.to_dict()
    assert calls == []
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['retry_fix'] is False
    assert diagnostic['contract_fingerprint']
    if failure != 'missing':
        assert diagnostic['task_ids'] == ['task-owned']
        assert diagnostic['requirement_ids'] == ['REQ-owned']
    if failure == 'missing':
        assert diagnostic.get('failure_kind') not in {'environment', 'discovery'}
    else:
        assert diagnostic['failure_kind'] == failure
        assert diagnostic['command']
    assert {path: (root / path).read_bytes() for path in ambient} == ambient

    if failure == 'environment':
        for selector in ('name', 'prefix'):
            directory = tmp_path / ('pytest-' + selector)
            directory.mkdir()
            with monkeypatch.context() as isolated:
                _pytest_environment_failure(directory, isolated, selector)


def _pytest_environment_failure(tmp_path, monkeypatch, selector):
    from copy import deepcopy
    from execution_marker import ExecutionMarker

    root, child = project(tmp_path)
    conda = shutil.which('conda')
    assert conda
    body = ExecutionMarker(tmp_path / 'test-body')
    (root / 'tests/test_owned.py').write_text(
        'def test_owned():\n    ' + body.source("'executed'") + '\n')
    environment = tmp_path / 'missing-environments'
    option = ['-n', 'unavailable-retained'] if selector == 'name' else ['-p', str(environment / 'unavailable')]
    command = shlex.join(['env', 'CONDA_ENVS_PATH=' + str(environment), conda, 'run', *option,
                         'python', '-m', 'pytest', '-q', 'tests/test_owned.py'])
    ambient = _retain_command(root, child, command, 'cmd:' + command)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status in {'failed', 'blocked'} and calls == ['fix'], saved.to_dict()
    record = next(entry['verification'] for entry in saved.execution_log
                  if entry.get('action') == 'receipt_verification')
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
    custody, attempt = deepcopy(saved.candidate_custody), saved.current_attempt
    for _ in range(2):
        repeated, calls, _ = run_session(root, monkeypatch)
        assert repeated.status == 'blocked' and calls == []
        assert repeated.candidate_custody == custody and repeated.current_attempt == attempt
        assert any(entry.get('diagnostic') == diagnostic for entry in repeated.execution_log)
        assert not body.exists()
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


def test_public_resume_uses_admitted_environment_during_dispatch(tmp_path, monkeypatch):
    from auto_agents.gate_execution import LocalGatePlanExecutor
    from execution_marker import ExecutionMarker

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'admitted-body')
    (root / 'qa').mkdir()
    (root / 'pytest.ini').write_text('[pytest]\n')
    (root / 'qa/regression_helpers.py').write_text(
        'import os\nfrom pathlib import Path\ndef test_context():\n'
        '    assert os.environ["PYTEST_ADDOPTS"] == "-o pythonpath=qa"\n'
        '    value = Path("value.py").read_text()\n'
        '    ' + marker.source('value', append=True) + '\n'
        '    assert value == "VALUE = 1\\n"\n')
    (root / 'tests/test_context.py').write_text('from regression_helpers import test_context\n')
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', 'tests/test_context.py::test_context'])
    ambient = _retain_command(root, child, command, 'tests/test_context.py::test_context')
    monkeypatch.setenv('PYTEST_ADDOPTS', '-o pythonpath=qa')
    run = LocalGatePlanExecutor._run_command
    changed = []
    def dispatch(self, command, **kwargs):
        assert self.execution_environment['PYTEST_ADDOPTS'] == '-o pythonpath=qa'
        changed.append(command)
        with monkeypatch.context() as late:
            late.setenv('PYTEST_ADDOPTS', '--setup-only')
            return run(self, command, **kwargs)
    monkeypatch.setattr(LocalGatePlanExecutor, '_run_command', dispatch)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed' and calls == ['fix'], saved.to_dict()
    assert changed and 'VALUE = 1' in marker.read_text().splitlines()
    assert 'qa/regression_helpers.py' in saved.verification_binding['proof_sources']
    assert {path: (root / path).read_bytes() for path in ambient} == ambient
    for recovery in (False, True):
        directory = tmp_path / ('receipt-recovery' if recovery else 'receipt-fresh')
        directory.mkdir()
        with monkeypatch.context() as case:
            case.setattr(LocalGatePlanExecutor, '_run_command', run)
            _verification_entry_environment_change(directory, case, recovery)


def _verification_entry_environment_change(tmp_path, monkeypatch, recovery):
    from auto_agents.session import Session
    from auto_agents.session_candidate import verification_identity
    from execution_marker import ExecutionMarker

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'actual-environments')
    proof = root / 'tests/test_owned.py'
    source = 'import os\n' + proof.read_text()
    source = source.replace('def test_owned():\n', 'def test_owned():\n    '
                            + marker.source("os.environ['OWNED_EXPECTED_VALUE'] + '\\n'", append=True) + '\n')
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
        assert paused.candidate_custody['receipt'] and not marker.exists()
    admissions, executions = [], []
    def changed_at_entry(self, *args, **kwargs):
        # The caller has already captured its receipt/admission identity.
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
    assert admissions and executions and admissions[-1] != executions[-1]
    assert marker.read_text().splitlines() == ['1']
    record = next(entry for entry in reversed(saved.execution_log)
                  if entry.get('action') == 'receipt_verification')
    assert record['verification']['ok']
    assert record['identity'] == record['verification']['execution_identity'] == executions[-1]
    assert record['identity'] != admissions[-1]
    assert not saved.candidate_custody.get('delivered_revision')
    assert not any(entry.get('action') == 'receipt_completion' for entry in saved.execution_log)
    monkeypatch.setattr(Session, '_run_verify', verify)
    repeated, _, _ = run_session(root, monkeypatch)
    assert repeated.status != 'completed', repeated.to_dict()
    assert '2' in marker.read_text().splitlines(), 'the restored failing environment must really execute'
    assert not any(entry.get('action') == 'receipt_verification'
                   and entry.get('identity') == admissions[-1] and entry['verification']['ok']
                   for entry in repeated.execution_log)
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


def test_prepared_gate_keeps_shared_prefix_readonly(tmp_path, monkeypatch):

    root, child = project(tmp_path)
    conda, source, provisioned = _environment(tmp_path)
    before, installed_before = _snapshot(source), _snapshot(provisioned)
    hook = source / 'etc/conda/activate.d/context.sh'
    (root / 'test_context.py').write_text(
        _python_shared_controls(source) +
        'import os\nfrom pathlib import Path\nimport pytest\n'
        'def test_context():\n'
        '    check_shared_inputs()\n'
        '    assert os.environ["RETAINED_ACTIVATION"] == "activated value"\n'
        '    prefix = Path(os.environ["CONDA_PREFIX"])\n'
        f'    assert prefix != Path({str(source)!r})\n'
        '    (prefix / "private-scratch").write_text("permitted")\n'
        '    with pytest.raises(PermissionError):\n'
        f'        Path({str(hook)!r}).write_text("foreign mutation")\n')
    command = shlex.join(['env', 'CONDA_ENVS_PATH=' + str(source.parent), conda, 'run',
                         '--name', source.name, '--no-capture-output', sys.executable,
                         '-m', 'pytest', '-q', 'test_context.py'])
    ambient = _retain_command(root, child, command, 'cmd:' + command)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed' and calls == ['fix'], json.dumps(saved.to_dict(), indent=2)
    assert _snapshot(source) == before
    assert _snapshot(provisioned) == installed_before
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('command,expected', [
    ('python -m pytest ::test_contract', '::test_contract'),
    ('cd qa && python -m pytest ::test_contract', '::test_contract'),
    ("cd qa && python -m pytest ' ::test_contract'", ' ::test_contract'),
    ('cd qa && python -m pytest tests/test_contract.py::test_contract', 'qa/tests/test_contract.py::test_contract'),
])
def test_repository_targets_does_not_invent_missing_pytest_path(command, expected):
    assert parse_invocations(command)[0].repository_targets == [expected]
