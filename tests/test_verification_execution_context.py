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


@pytest.mark.parametrize('active', [False, True])
def test_named_conda_vitest_discovery_preserves_activation_cwd_and_shared_environment(tmp_path, monkeypatch, active):
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
        'if (process.env.RETAINED_ACTIVATION !== "activated value") throw Error("activation lost");\n'
        'if (process.env.INLINE_CONTEXT !== "inline value") throw Error("assignment lost");\n'
        'export default {test: {include: ["tests/*.test.js"]}};\n')
    marker = tmp_path / 'executions'
    (root / 'web/tests/owned.test.js').write_text(
        'import {test, expect} from "vitest";\n'
        'import {readFileSync, appendFileSync} from "node:fs";\n'
        'test("retained", () => {\n'
        'expect(process.env.RETAINED_ACTIVATION).toBe("activated value");\n'
        'expect(process.env.INLINE_CONTEXT).toBe("inline value");\n'
        'const value = readFileSync("../value.py", "utf8");\n'
        f'appendFileSync({json.dumps(str(marker))}, value);\n'
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
    root, child = project(tmp_path)
    conda, source, provisioned = _environment(tmp_path)
    before, installed_before = _snapshot(source), _snapshot(provisioned)
    (root / 'launch').mkdir()
    (root / 'launch/environment').symlink_to(source, target_is_directory=True)
    (root / 'web/tests').mkdir(parents=True)
    marker = tmp_path / 'python-executed'
    # Use the provisioned engine interpreter while retaining Conda activation.
    (root / 'web/tests/test_context.py').write_text(
        'import os\nfrom pathlib import Path\n'
        'def test_context():\n'
        '    assert os.environ["RETAINED_ACTIVATION"] == "activated value"\n'
        '    assert os.environ["INLINE_CONTEXT"] == "inline value"\n'
        '    assert Path.cwd().name == "web"\n'
        f'    assert Path(os.environ["CONDA_PREFIX"]).resolve() != Path({str(source)!r})\n'
        '    assert (Path(os.environ["CONDA_PREFIX"]) / "conda-meta").is_symlink()\n'
        '    value = Path("../value.py").read_text()\n'
        f'    Path({str(marker)!r}).write_text(value)\n'
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


def test_prepared_gate_keeps_shared_prefix_readonly(tmp_path):
    from auto_agents.gate_execution import LocalGatePlanExecutor
    from auto_agents.gates import GateCommandMetadata, run_gate_plan
    from auto_agents.session_verification import prepare_retained_vitest_command
    from test_gate_execution import _project, _config, _git

    root = _project(tmp_path)
    conda, source, provisioned = _environment(tmp_path)
    before, installed_before = _snapshot(source), _snapshot(provisioned)
    hook = source / 'etc/conda/activate.d/context.sh'
    (root / 'test_context.py').write_text(
        'import os\nfrom pathlib import Path\nimport pytest\n'
        'def test_context():\n'
        '    assert os.environ["RETAINED_ACTIVATION"] == "activated value"\n'
        '    prefix = Path(os.environ["CONDA_PREFIX"])\n'
        f'    assert prefix != Path({str(source)!r})\n'
        '    (prefix / "private-scratch").write_text("permitted")\n'
        '    with pytest.raises(PermissionError):\n'
        f'        Path({str(hook)!r}).write_text("foreign mutation")\n')
    _git(root, 'add', 'test_context.py')
    _git(root, 'commit', '-m', 'retain activation check')
    command = shlex.join(['env', 'CONDA_ENVS_PATH=' + str(source.parent), conda, 'run',
                         '--name', source.name, '--no-capture-output', sys.executable,
                         '-m', 'pytest', '-q', 'test_context.py'])
    with LocalGatePlanExecutor(root, _config(tmp_path), {command: GateCommandMetadata()}) as executor:
        executor.prepare_retained_command = prepare_retained_vitest_command
        executor.sandbox_target = root
        result = run_gate_plan([command], [], root, collect_all=True, gate_executor=executor)
    assert result.ok, result.summary
    assert _snapshot(source) == before
    assert _snapshot(provisioned) == installed_before
