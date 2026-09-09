"""Review punctuation cannot become a failing command or change a literal ID."""
import os
import copy
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from auto_agents.repair_test_refs import pytest_targets
from auto_agents.repair_schedule import verification_plan
from auto_agents.repair_test_refs import review_action


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


def _legacy_review_state():
    from auto_agents.self_repair_search import SelfRepairExperiment, SelfRepairFinding, SelfRepairCandidateRecord
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='repair',
        base_commit='base', expected_postconditions=['retain source'])
    obligation = next(key for key in state.contract_obligation_ids if key.startswith('root:'))
    text = "Run: python -m pytest -q 'tests/test_a.py::test_a[legacy]' tests/test_b.py::test_b. Keep the original receipt."
    finding = SelfRepairFinding('repair', disposition='candidate_regression', causal_obligation_id=obligation,
        required_test=text, status='confirmed')
    state.findings['repair'] = finding
    bad = "python -m pytest -q 'tests/test_a.py::test_a[legacy]' tests/test_b.py::test_b."
    good = bad[:-1]
    evidence = dict(command=bad, returncode=4, failure_kind='verification', evidence_id='native-collection',
        excerpt='no tests ran\nERROR: not found: /isolated/tests/test_b.py::test_b.\n')
    state.candidates['failed'] = SelfRepairCandidateRecord('failed', failure_evidence=[evidence])
    state.sticky_verification_commands = ['git diff --check', bad, good]
    state.consecutive_non_improvements = 3
    state.progress_credits['old-achievement'] = 'accepted'
    return state, bad, good


def test_loaded_history_migrates_proven_generated_command_without_rewriting_evidence():
    from auto_agents.self_repair_search import SelfRepairExperiment
    state, bad, good = _legacy_review_state()
    original_evidence = copy.deepcopy(state.candidates['failed'].failure_evidence)
    original_review = state.findings['repair'].required_test
    restored = SelfRepairExperiment.from_dict(state.to_dict())
    assert restored.sticky_verification_commands == ['git diff --check', good]
    assert restored.candidates['failed'].failure_evidence == original_evidence
    assert restored.findings['repair'].required_test == original_review
    assert restored.consecutive_non_improvements == 3
    assert restored.progress_credits == state.progress_credits
    receipt, = restored.diagnostic_actions.values()
    assert receipt['original_command'] == bad and receipt['command'] == good
    assert receipt['evidence_ids'] == ['native-collection']
    assert not restored.normalize_review_commands()
    assert review_action(restored, {'kind': 'repair_verification', 'command': bad}) == {
        'kind': 'repair_verification', 'command': good, 'original_command': bad,
        'command_source': 'review_command_migration'}
    # Re-observing an old failure must not reinsert its invalid derived command.
    restored.findings['repair'].required_test = 'New review: tests/test_other.py::test_other'
    restored.remember_sticky_verification_commands([bad])
    assert restored.sticky_verification_commands == ['git diff --check', good]
    reloaded = SelfRepairExperiment.from_dict(restored.to_dict())
    assert reloaded.sticky_verification_commands == restored.sticky_verification_commands
    assert len(reloaded.diagnostic_actions) == 1


@pytest.mark.parametrize('unproven', ['no_review', 'unrelated', 'assertion', 'different_missing_target', 'extra_flag'])
def test_unproven_or_explicit_commands_are_not_rewritten(unproven):
    state, bad, _ = _legacy_review_state()
    evidence = state.candidates['failed'].failure_evidence[0]
    if unproven == 'no_review':
        state.findings.clear()
    elif unproven == 'unrelated':
        state.findings['repair'].disposition = 'unrelated_observation'
    elif unproven == 'assertion':
        evidence.update(returncode=1, failure_kind='assertion')
    elif unproven == 'different_missing_target':
        evidence['excerpt'] = 'ERROR: not found: /isolated/tests/other.py::test_other'
    else:
        bad = bad.replace('-q ', '-q -x ', 1)
        evidence['command'] = bad
        state.sticky_verification_commands = [bad]
    assert not state.normalize_review_commands()
    assert bad in state.sticky_verification_commands
    assert not state.diagnostic_actions


def test_selector_preflight_and_execution_use_migrated_history(tmp_path):
    from unittest.mock import patch
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision, _VerificationResult
    state, bad, good = _legacy_review_state()
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(), target_project_root=tmp_path,
        error=RuntimeError(), decision=SelfRepairDecision(True))
    runner._experiment = state
    runner._candidate_group = {'group_id': 'owner', 'focused_tests': []}
    with patch.object(runner, '_run_verification_commands', return_value=_VerificationResult(True, 'collected', returncodes=(0,))) as execute:
        assert runner._candidate_selector_issues(tmp_path) == []
    collected_commands = [call.args[0][0] for call in execute.call_args_list]
    assert len(collected_commands) == 1
    assert 'tests/test_b.py::test_b.' not in shlex.split(collected_commands[0])
    assert 'tests/test_b.py::test_b' in shlex.split(collected_commands[0])
    assert good in verification_plan(state, runner._candidate_group)['commands']
    assert bad not in verification_plan(state, runner._candidate_group)['commands']
    assert review_action(state, {'kind': 'implement'}) == {'kind': 'implement'}
