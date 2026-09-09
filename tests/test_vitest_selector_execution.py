import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from auto_agents.gates import command_from_verification_step, run_gate_plan
from auto_agents.models import VerificationStep


def test_multiple_selectors_preserve_file_and_test_name_pairing():
    command = command_from_verification_step(VerificationStep(
        runner='vitest', targets=['a.test.js::first', 'a.test.js::second', 'b.test.js::third']))
    groups = command.split(' && ')
    assert len(groups) == 2
    first, second = map(shlex.split, groups)
    assert first.count('-t') == second.count('-t') == 1
    assert 'a.test.js' in first and 'b.test.js' not in first
    assert 'third' not in first[-1] and 'third' in second[-1]


def test_selector_compilation_preserves_unicode_escaping_and_existing_filters():
    command = command_from_verification_step(VerificationStep(
        runner='vitest', targets=['a.test.js::雨夜 [a+b]'],
        args=['--reporter=json', '-t', '雨夜', '--testNamePattern=other']))
    args = shlex.split(command)
    assert args.count('-t') == 1
    assert '--reporter=json' in args
    pattern = args[args.index('-t') + 1]
    result = subprocess.run(['node', '-e',
        'const p=new RegExp(process.argv[1], "u"); console.log(JSON.stringify([p.test("雨夜 [a+b]"), p.test("雨夜 aaab")]));',
        pattern], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, False]


def _prepare_real_vitest(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('npm_config_cache', str(tmp_path / 'npm-cache'))
    # The supervisor prepares the pinned verification toolchain beside Python.
    tools = Path(sys.prefix) / 'verification-tools'
    roots = [p.parent.parent for p in tools.glob('*/node_modules/vitest/package.json')]
    if not roots:
        from auto_agents.verification_dependencies import require_verification_executable
        binary = require_verification_executable('vitest')
        roots = [Path(binary).resolve().parent.parent]
    (tmp_path / 'node_modules').symlink_to(roots[0], target_is_directory=True)
    (tmp_path / 'package.json').write_text('{"type":"module"}')


def test_compiled_selectors_execute_exact_tests_with_real_vitest(tmp_path, monkeypatch):
    _prepare_real_vitest(tmp_path, monkeypatch)
    for filename, names in [('a.test.js', ['one [x]', 'two', 'three']), ('b.test.js', ['one [x]', 'three'])]:
        selected = {'a.test.js': {'one [x]', 'two'}, 'b.test.js': {'three'}}[filename]
        body = 'import { test, expect } from "vitest";\nimport { appendFileSync } from "node:fs";\n'
        for name in names:
            marker = json.dumps(filename + '::' + name + '\n')
            body += f'test({json.dumps(name)}, () => {{ appendFileSync("executed.txt", {marker}); expect({str(name in selected).lower()}).toBe(true); }});\n'
        (tmp_path / filename).write_text(body)
    command = command_from_verification_step(VerificationStep(
        runner='vitest', targets=['a.test.js::one [x]', 'a.test.js::two', 'b.test.js::three'],
        args=['--reporter=json', '--maxWorkers=1']))
    result = run_gate_plan([command], [], tmp_path, collect_all=True, command_timeout_seconds=60)
    assert result.ok, result.summary
    assert sorted((tmp_path / 'executed.txt').read_text().splitlines()) == [
        'a.test.js::one [x]', 'a.test.js::two', 'b.test.js::three']
    (tmp_path / 'executed.txt').unlink()
    repeated_filters = command_from_verification_step(VerificationStep(
        runner='vitest', targets=['a.test.js'],
        args=['-t', r'one \[x\]', '--testNamePattern', '^two$', '--maxWorkers=1']))
    result = run_gate_plan([repeated_filters], [], tmp_path, collect_all=True, command_timeout_seconds=60)
    assert result.ok, result.summary
    assert sorted((tmp_path / 'executed.txt').read_text().splitlines()) == ['a.test.js::one [x]', 'a.test.js::two']


# Keep the selectors recorded in retained repair contracts executable.
test_multiple_selectors_compile_without_repeated_single_value_options = test_multiple_selectors_preserve_file_and_test_name_pairing
test_compilation_preserves_file_selector_pairs_and_name_semantics = test_selector_compilation_preserves_unicode_escaping_and_existing_filters
test_compiled_multiple_selectors_execute_with_real_vitest = test_compiled_selectors_execute_exact_tests_with_real_vitest
test_real_vitest_selection_excludes_cross_file_matches = test_compiled_selectors_execute_exact_tests_with_real_vitest


@pytest.mark.parametrize('selector', ['fails', 'missing'])
@pytest.mark.parametrize('reporter', ['json', 'default'])
@pytest.mark.parametrize('isolated', [False, True], ids=['direct', 'isolated'])
def test_real_vitest_selected_failure_and_empty_selection_cannot_pass(tmp_path, monkeypatch, selector, reporter, isolated):
    from contextlib import nullcontext
    from auto_agents.gate_execution import LocalGatePlanExecutor
    from test_gate_execution import _config, _git

    _prepare_real_vitest(tmp_path, monkeypatch)
    (tmp_path / 'a.test.js').write_text(
        'import { test, expect } from "vitest";\n'
        'test("passes", () => expect(true).toBe(true));\n'
        'test("fails", () => expect(false).toBe(true));\n')
    command = command_from_verification_step(VerificationStep(
        runner='vitest', targets=[f'a.test.js::{selector}'],
        args=[f'--reporter={reporter}', '--maxWorkers=1']))
    if isolated:
        _git(tmp_path, 'init')
        _git(tmp_path, 'config', 'user.name', 'Test')
        _git(tmp_path, 'config', 'user.email', 'test@example.com')
        (tmp_path / '.gitignore').write_text('node_modules/\n.vitest/\n')
        _git(tmp_path, 'add', '-A')
        _git(tmp_path, 'commit', '-m', 'Vitest selector fixture')
    context = LocalGatePlanExecutor(tmp_path, _config(tmp_path), {}) if isolated else nullcontext()
    with context as executor:
        result = run_gate_plan([command], [], tmp_path, collect_all=True,
                              command_timeout_seconds=60, gate_executor=executor)
    assert not result.ok, result.summary
    assert result.commands
    assert any(not command.ok for command in result.commands)
    if selector == 'missing':
        assert 'Vitest selection executed no tests' in result.commands[0].stderr
