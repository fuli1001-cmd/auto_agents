import pytest

from auto_agents.repair_v2.audit import protect_tests, TestProtectionError as ProtectionError
from auto_agents.repair_v2.workspace import git


def audit(tmp_path, original, current):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'tests').mkdir()
    path = root / 'tests/test_cases.py'
    path.write_text(original)
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'original tests')
    base = git(root, 'rev-parse', 'HEAD')
    path.write_text(current)
    return lambda: protect_tests(root, base, root)


def test_strengthening_assertion_keeps_original_requirement(tmp_path):
    audit(tmp_path,
        'def test_source():\n    assert live.read_text() == "original"\n',
        'def test_source():\n    assert live.read_text() == "original" and live.stat().st_mode == mode\n')()


def test_extra_parameter_case_does_not_change_old_conditional_expectation(tmp_path):
    original = '''
@pytest.mark.parametrize('matching', [True, False])
def test_authority(matching):
    if matching:
        assert saved.status == 'completed'
    else:
        assert 'conflict' in saved.result
'''
    current = original.replace('[True, False]', '[True, False, "unscoped"]').replace(
        'if matching:', 'if matching is True:').replace(
        "'conflict' in", "('unresolved' if matching == 'unscoped' else 'conflict') in")
    audit(tmp_path, original, current)()


def test_literal_parameterization_still_contains_original_constant_case(tmp_path):
    audit(tmp_path,
        "def test_reference():\n    assert diagnostic['ref'] == 'owned.contract'\n",
        "@pytest.mark.parametrize('reference', ['owned.contract', 'tests/owned.py'])\n"
        "def test_reference(reference):\n    assert diagnostic['ref'] == reference\n")()


@pytest.mark.parametrize('current', [
    'def test_value():\n    assert True\n',
    'def test_value():\n    assert value == 1 or True\n',
    'def test_value():\n    assert value == 2\n',
    'def test_value():\n    pass\ndef test_other():\n    assert value == 1\n',
    'def test_renamed():\n    assert value == 1\n',
])
def test_weakened_or_moved_assertion_is_actionable_failure(tmp_path, current):
    check = audit(tmp_path, 'def test_value():\n    assert value == 1\n', current)
    with pytest.raises(ProtectionError) as error:
        check()
    finding = error.value.findings[0]
    assert finding['path'] == 'tests/test_cases.py'
    assert finding['changes'][0]['scope'] == 'test_value'
    assert finding['baseline'] and finding['code'] == 'tests_weakened'


def test_removing_one_conjunct_is_still_rejected(tmp_path):
    check = audit(tmp_path, 'def test_value():\n    assert value == 1 and other == 2\n',
                  'def test_value():\n    assert value == 1\n')
    with pytest.raises(ProtectionError, match='assertions'): check()


@pytest.mark.parametrize('change', ['wrong_old_expectation', 'drop_old_case', 'reassign_parameter', 'indirect', 'introduce_indirect'])
def test_conditional_normalization_cannot_waive_an_old_case(tmp_path, change):
    original = "@pytest.mark.parametrize('case', ['owned'])\ndef test_value(case):\n    assert value == 1\n"
    current = "@pytest.mark.parametrize('case', ['owned', 'extra'])\ndef test_value(case):\n    assert value == (2 if case == 'extra' else 1)\n"
    if change == 'wrong_old_expectation': current = current.replace("case == 'extra'", "case == 'owned'")
    if change == 'drop_old_case': current = current.replace("['owned', 'extra']", "['extra']")
    if change == 'reassign_parameter': current = current.replace('    assert', "    case = 'extra'\n    assert")
    if change == 'indirect':
        original = original.replace("['owned'])", "['owned'], indirect=True)")
    if change in ('indirect', 'introduce_indirect'):
        current = current.replace("['owned', 'extra'])", "['owned', 'extra'], indirect=True)")
    check = audit(tmp_path, original, current)
    with pytest.raises(ProtectionError): check()


@pytest.mark.parametrize('separate', [False, True])
def test_parameter_rows_keep_correlations_when_matching_old_assertions(tmp_path, separate):
    old_check = 'assert left == 1\n    assert right == 2' if separate else 'assert (left, right) == (1, 2)'
    new_check = 'assert left == a and right == b' if separate else 'assert (left, right) == (a, b)'
    check = audit(tmp_path, 'def test_value():\n    ' + old_check + '\n',
        "@pytest.mark.parametrize('a,b', [(1, 3), (4, 2)])\n"
        'def test_value(a,b):\n    ' + new_check + '\n')
    with pytest.raises(ProtectionError): check()


def test_parameterizing_missing_constant_does_not_pass(tmp_path):
    check = audit(tmp_path, 'def test_value():\n    assert value == 1\n',
        "@pytest.mark.parametrize('expected', [2, 3])\n"
        'def test_value(expected):\n    assert value == expected\n')
    with pytest.raises(ProtectionError): check()


@pytest.mark.parametrize('mark', ['pytest.mark.skip', 'pytest.mark.skipif(True)', 'pytest.mark.xfail()'])
def test_added_skip_or_changed_skip_condition_cannot_hide_failure(tmp_path, mark):
    original = '@pytest.mark.skipif(False)\ndef test_value():\n    assert value == 1\n'
    check = audit(tmp_path, original, original.replace('pytest.mark.skipif(False)', mark))
    with pytest.raises(ProtectionError): check()


def test_all_changed_files_are_reported_in_one_audit(tmp_path):
    root = tmp_path / 'repo'; root.mkdir(); (root / 'tests').mkdir()
    git(root, 'init', '-q')
    for name in ('first', 'second'):
        (root / f'tests/test_{name}.py').write_text(f'def test_{name}():\n    assert value == 1\n')
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'original tests')
    base = git(root, 'rev-parse', 'HEAD')
    for name in ('first', 'second'):
        (root / f'tests/test_{name}.py').write_text(f'def test_{name}():\n    assert True\n')
    with pytest.raises(ProtectionError) as error: protect_tests(root, base, root)
    assert {f['path'] for f in error.value.findings} == {'tests/test_first.py', 'tests/test_second.py'}


@pytest.mark.parametrize('name,text', [('pytest.ini', '[pytest'), ('pyproject.toml', '[tool.pytest')])
def test_malformed_selection_is_repair_feedback(tmp_path, name, text):
    original = 'def test_value():\n    assert value == 1\n'
    check = audit(tmp_path, original, original)
    (tmp_path / 'repo' / name).write_text(text)
    with pytest.raises(ProtectionError) as error: check()
    assert error.value.code == 'tests_invalid'
    assert name in error.value.findings[0]['reason']
