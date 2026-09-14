"""Public repair transitions: independent evidence, durable work and recurrence."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_actions import prepare_action
from auto_agents.repair_convergence import route, summary
from auto_agents.repair_memory import read_record, remember_review, save_record
from auto_agents.repair_planning import prepare_component, review_scope, _retained_scope
from auto_agents.repair_review_protocol import normalized_controls
from auto_agents.repair_schedule import quick_verification_plan, verification_plan
from auto_agents.repair_work import memory, work_id
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairFinding
from test_repair_planning import setup
from test_repair_routing import git
from test_repair_completion import completed


@pytest.fixture
def approved(setup):
    runner, state, plan, calls = setup
    plan['touched_paths'].append('source.py')
    positive = 'python -m pytest -q tests/test_contract.py::test_compatible'
    plan['scenarios'][1]['check'] = positive
    plan['quick_checks'].append(positive)
    (runner.repo_root / 'tests/test_contract.py').write_text(
        'from source import value\ndef test_contract(): assert value == 1\n'
        'def test_compatible(): assert value >= 0\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'positive and negative contract controls')
    state.base_commit = git(runner.repo_root, 'rev-parse', 'HEAD')
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    return runner, state, plan, receipt


def finding(runner, plan, identity='retained-owner'):
    obligation = runner._candidate_group['contract_obligation_ids'][0]
    return {'finding_id': identity, 'status': 'confirmed', 'severity': 'hard', 'disposition': 'candidate_regression',
        'repair_group_id': runner._candidate_group['group_id'],
        'causal_obligation_id': obligation, 'affected_paths': ['source.py'], 'reason': 'retained owner changes',
        'counterexample': 'retained owner must remain 1 after resume', 'required_test': plan['quick_checks'][0],
        'evidence': ['source.py:1'], 'defer_until': '', 'repair_kind': 'implementation',
        'scenario_ids': ['failure', 'compatibility'], 'scope': {'verdict': 'required',
            'obligation_id': obligation, 'trigger': 'resume existing child', 'consequence': 'different owner',
            'support_basis': 'original ownership must survive', 'reason': 'introduced supported regression',
            'evidence': ['source.py:1'], 'disproof': ''},
        'controls': {'negative': {'command': plan['quick_checks'][0], 'scenario_ids': ['failure'], 'purpose': 'original owner'},
                     'positive': {'command': plan['quick_checks'][1], 'scenario_ids': ['compatibility'], 'purpose': 'valid owner stays usable'}}}


def candidate(runner, plan, number, *, identity='retained-owner'):
    (runner.repo_root / 'source.py').write_text('value = ' + str(number + 1) + '\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'candidate ' + str(number))
    row = finding(runner, plan, identity)
    record = SelfRepairCandidateRecord('c' + str(number), candidate_commit=git(runner.repo_root, 'rev-parse', 'HEAD'),
        finding_group_id=runner._candidate_group['group_id'], status='candidate_review_rejected', review_completed=True)
    ref = remember_review(runner, runner.repo_root, runner._candidate_group,
                          {'decision': 'REJECT', 'reason': 'supported regression', 'findings': [row]})
    runner._experiment.register_candidate(record, findings=[SelfRepairFinding.from_dict(row)])
    runner._experiment_store.save(runner._experiment)
    return record, ref


def test_changed_execution_scope_and_restart_keep_one_work_item(approved):
    runner, state, plan, _ = approved
    original = work_id(state, runner._candidate_group)
    record, reference = candidate(runner, plan, 1)
    changed = {**runner._candidate_group, 'touched_paths': ['source.py', 'tests/new_control.py']}
    assert memory(runner, changed)['code_review'] == reference
    runner._experiment_store.save(state)
    runner._experiment = runner._experiment_store.load()
    assert work_id(runner._experiment, changed) == original
    assert memory(runner, changed)['code_review'] == reference
    assert len(runner._experiment.work_items[original]['definitions']) >= 2
    other = {**changed, 'group_id': state.finding_groups[1]['group_id']}
    assert not memory(runner, other).get('code_review')
    assert route(runner, record, {'kind': 'implement', 'evidence_ids': []})['kind'] == 'repair_code'


def test_one_independent_code_review_supplies_scope_and_both_controls(approved):
    runner, state, plan, _ = approved
    (runner.repo_root / 'source.py').write_text('value = 2\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'regression')
    row = finding(runner, plan)
    calls = []
    def provider(request):
        calls.append(request.stage)
        assert request.stage == 'self_repair_candidate_review'
        assert request.response_schema and request.sandbox_mode == 'read-only'
        return AgentResult(True, [], request.output_path, summary=json.dumps(
            {'decision': 'REJECT', 'reason': 'supported counterexample', 'findings': [row], 'resolved_finding_ids': []}))
    runner.target_orchestrator._call_with_failover = provider
    review = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert not review.ok and len(review.payload['findings']) == 1
    normalized = review.payload['findings'][0]
    receipt = state.scope_decisions[row['finding_id']]
    assert _retained_scope(runner, receipt, normalized)
    assert receipt['code_review_scope'] and calls == ['self_repair_candidate_review']
    state.findings[row['finding_id']] = SelfRepairFinding.from_dict(normalized)
    runner._candidate_group['finding_ids'] = [row['finding_id']]
    quick = quick_verification_plan(state, runner._candidate_group)
    expanded = verification_plan(state, runner._candidate_group)
    assert quick['commands'][:2] == plan['quick_checks']
    assert set(plan['quick_checks']).issubset(expanded['commands'])
    runner._experiment_store.save(state)
    runner._experiment = runner._experiment_store.load()
    review_scope(runner, runner.repo_root, [normalized])
    assert calls == ['self_repair_candidate_review']
    assert not state.progress_credits


@pytest.mark.parametrize('mutation', ['tampered', 'different_finding', 'different_contract'])
def test_combined_scope_is_bound_to_the_original_independent_evidence(approved, mutation):
    from auto_agents.repair_review_protocol import admit_scope
    from auto_agents.repair_control import digest
    from auto_agents.verification_ledger import source_identity
    runner, state, plan, _ = approved
    row = finding(runner, plan)
    incoming = save_record(runner, 'code_review_input', {'source': source_identity(runner.repo_root),
        'environment': digest(runner._full_suite_environment_fingerprint()), 'contract_fingerprint': state.contract_fingerprint})
    admit_scope(runner, runner.repo_root, runner._candidate_group, [row], review_input=incoming,
                reviewed_source=source_identity(runner.repo_root),
                reviewed_environment=digest(runner._full_suite_environment_fingerprint()))
    receipt = state.scope_decisions[row['finding_id']]
    if mutation == 'tampered':
        path = runner._experiment_store.root / 'planning' / receipt['request_id'] / 'memory.json'
        path.write_text(path.read_text().replace('different owner', 'different supported behavior'))
    elif mutation == 'different_finding':
        row['counterexample'] = 'an unrelated new counterexample'
    else:
        state.contract_fingerprint = 'different'
    assert not _retained_scope(runner, receipt, row)


def test_repeated_counterexample_gets_one_diagnosis_and_cannot_reset_through_renaming(approved):
    runner, state, plan, _ = approved
    first, _ = candidate(runner, plan, 1, identity='first-label')
    assert route(runner, first, {'kind': 'implement', 'evidence_ids': []})['kind'] == 'repair_code'
    second, _ = candidate(runner, plan, 2, identity='new-label')
    action = route(runner, second, {'kind': 'implement', 'evidence_ids': []})
    assert action['kind'] == 'diagnose_failure' and len(action['recurrence_cases']) == 1
    calls = []
    def diagnose(request):
        calls.append(request)
        assert request.stage == 'self_repair_failure_diagnosis'
        assert 'retained owner must remain 1' in request.prompt
        return AgentResult(True, [], request.output_path, summary=json.dumps({
            'kind': 'repair_code', 'cause': 'wrong fallback owner', 'completion': plan['quick_checks'][0],
            'evidence_ids': action['evidence_ids'], 'invalidated_assumption': 'a positive owner is sufficient',
            'next_check': plan['quick_checks'][0] + ' plus the positive control'}))
    runner.target_orchestrator._call_with_failover = diagnose
    assert prepare_action(runner, state)['kind'] == 'repair_code'
    runner._experiment = runner._experiment_store.load()
    assert prepare_action(runner, runner._experiment)['kind'] == 'repair_code'
    assert len(calls) == 1
    third, _ = candidate(runner, plan, 3, identity='renamed-again')
    third_action = route(runner, third, {'kind': 'implement', 'evidence_ids': []})
    assert third_action['kind'] == 'diagnose_failure'
    assert third_action['counterexample_history'][0]['diagnosis']['invalidated_assumption'] == 'a positive owner is sufficient'
    action = third_action
    assert prepare_action(runner, runner._experiment)['kind'] == 'blocked'  # Repeating the disproved hypothesis is refused.
    result = summary(runner._experiment)
    assert result['diagnosis_calls'] == 2 and result['recurring_counterexamples'] == 1
    assert not runner._experiment.progress_credits


def test_repeated_review_of_identical_source_is_not_a_failed_repair_attempt(approved):
    runner, state, plan, _ = approved
    record, _ = candidate(runner, plan, 1)
    for _ in range(3):
        remember_review(runner, runner.repo_root, runner._candidate_group,
                        {'decision': 'REJECT', 'findings': [finding(runner, plan)]})
    assert route(runner, record, {'kind': 'implement', 'evidence_ids': []})['kind'] == 'repair_code'
    assert summary(state)['recurring_counterexamples'] == 0


def test_controls_do_not_replace_full_acceptance_or_infer_positive_coverage(approved):
    runner, state, plan, _ = approved
    row = finding(runner, plan)
    row['controls']['positive']['scenario_ids'] = ['failure']
    assert normalized_controls(row, runner._candidate_group) is None
    row = finding(runner, plan)
    row['controls']['negative']['command'] = 'python -m pytest -q tests/test_contract.py::test_contract; rm source.py'
    assert normalized_controls(row, runner._candidate_group) is None


def test_restored_completed_source_can_revalidate_once_without_inventing_code_progress(completed):
    from auto_agents.repair_convergence import admit_duplicate_validation
    runner, state, group, _, _ = completed
    state.candidates['completed'] = SelfRepairCandidateRecord('completed', patch_fingerprint='same-tree',
        status='candidate_group_completed', finding_group_id=group['group_id'])
    assert admit_duplicate_validation(runner, 'same-tree')
    runner._experiment = runner._experiment_store.load()
    assert not admit_duplicate_validation(runner, 'same-tree')
    assert not admit_duplicate_validation(runner, 'unreviewed-tree')
    assert not runner._experiment.progress_credits


@pytest.mark.parametrize('bad', [-1, True, 'reset'])
def test_corrupt_work_budgets_cannot_be_reset_during_reload(approved, bad):
    from auto_agents.self_repair_search import SelfRepairExperiment
    runner, state, _, _ = approved
    raw = state.to_dict()
    work = raw['work_items'][work_id(state, runner._candidate_group)]
    work['diagnoses']['case'] = {'calls': bad}
    with pytest.raises(ValueError, match='diagnosis counters'):
        SelfRepairExperiment.from_dict(raw)
