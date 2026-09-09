"""Review punctuation cannot become a failing command or change a literal ID."""
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from auto_agents.repair_test_refs import pytest_targets
from auto_agents.repair_schedule import verification_plan


@pytest.mark.parametrize('text, expected', [
    ('Run: python -m pytest -q tests/test_a.py::TestA::test_run.', ['tests/test_a.py::TestA::test_run']),
    ('Run (`tests/test_a.py::test_run`), then tests/test_b.py::test_b。',
     ['tests/test_a.py::test_run', 'tests/test_b.py::test_b']),
    ('tests/test_a.py::test_run[value.].', ['tests/test_a.py::test_run[value.]']),
    ('tests/test_a.py::test_run[[value, other]].', ['tests/test_a.py::test_run[[value, other]]']),
    ('"tests/a space/test_a.py::test_run[with space, punctuation.]".',
     ['tests/a space/test_a.py::test_run[with space, punctuation.]']),
    ("'tests/test_a.py::test_literal.'", ['tests/test_a.py::test_literal.']),
    ('./tests/test_a.py::test_run; tests/test_a.py::test_run.', ['tests/test_a.py::test_run']),
    ('tests/../outside.py::test_run ../tests/test_a.py::test_run /tmp/tests/test_a.py::test_run', []),
    ('tests/test_a.py::test_run[unfinished', []),
    ('tests/test_a.py::.', []),
    ('tests/test_a.py::Test.::test_run.', ['tests/test_a.py::Test.::test_run']),
    ('tests/test_a.py::test_literal.[value].', ['tests/test_a.py::test_literal.[value]']),
    ('tests/test_a.py.bak tests/test_a.py::test_run[value]suffix', []),
])
def test_review_reference_boundaries(text, expected):
    assert pytest_targets(text) == expected


def test_command_identity_preserves_literal_unquoted_punctuation():
    assert pytest_targets('python -m pytest tests/test_a.py::test_literal.', prose=False) == [
        'tests/test_a.py::test_literal.']


def test_review_schedule_executes_exact_native_pytest_ids(tmp_path):
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_refs.py').write_text(
        'import pytest\n'
        '@pytest.mark.parametrize("value", [1, 2], ids=["value.", "with space"])\n'
        'def test_value(value):\n    assert value > 0\n'
        'def _literal():\n    assert True\n'
        'globals()["test_literal."] = _literal\n')
    finding = SimpleNamespace(finding_id='repair', status='confirmed', disposition='candidate_regression',
        required_test="Run: python -m pytest -q 'tests/test_refs.py::test_value[value.]' "
                      'tests/test_refs.py::test_value[with space]. Also `tests/test_refs.py::test_literal.`.')
    group = dict(group_id='owner', finding_ids=['repair'], focused_tests=[])
    state = SimpleNamespace(findings={'repair': finding}, finding_groups=[group],
                            candidates={}, sticky_verification_commands=[])
    command, = verification_plan(state, group)['commands']
    args = shlex.split(command)
    assert args[4:] == ['tests/test_refs.py::test_value[value.]',
                        'tests/test_refs.py::test_value[with space]', 'tests/test_refs.py::test_literal.']
    env = dict(os.environ)
    env.pop('PYTEST_ADDOPTS', None)
    result = subprocess.run([sys.executable, *args[1:], '--rootdir=' + str(tmp_path),
        '--confcutdir=' + str(tmp_path), '-p', 'no:cacheprovider'], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '3 passed' in result.stdout


def test_review_sentence_punctuation_does_not_change_finding_progress_identity():
    from auto_agents.repair_progress import finding_identity, achievements
    from auto_agents.self_repair_search import SelfRepairExperiment, SelfRepairFinding, SelfRepairCandidateRecord
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='repair',
        base_commit='base', expected_postconditions=['retain source'])
    obligation = next(key for key in state.contract_obligation_ids if key.startswith('root:'))
    finding = SelfRepairFinding('repair', disposition='contract_violation', causal_obligation_id=obligation,
        required_test='Run tests/test_a.py::test_run.')
    identity = finding_identity(finding, state)
    finding.required_test = 'Run tests/test_a.py::test_run'
    assert finding_identity(finding, state) == identity
    finding.required_test += '.'
    state.findings['repair'] = finding
    record = SelfRepairCandidateRecord('candidate', review_completed=True, resolved_finding_ids=['repair'],
        verified_check_ids=['tests/test_a.py::test_run'])
    assert 'finding:' + identity in achievements(state, record)
