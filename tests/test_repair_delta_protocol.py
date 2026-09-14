"""Accepted evidence survives scheduling and malformed reviewer envelopes."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.repair_completion import assess, refresh, _memory
from auto_agents.repair_delta_protocol import validate_reply
from auto_agents.repair_planning import PlanFormatError, PlanningBlocked, finding_key, prepare_component
from auto_agents.repair_response_schema import schema_for
from auto_agents.self_repair import SelfRepairResult
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_completion import completed
from test_repair_planning import setup
from test_repair_component_revalidation import response
from test_repair_routing import git


def test_valid_completion_selected_from_stale_status_skips_candidate_and_model(completed, tmp_path):
    runner, state, group, _, receipt = completed
    runner._continuous_workspace = tmp_path / 'continuous'
    git(runner.repo_root, 'worktree', 'add', '--detach', str(runner._continuous_workspace / 'repair'), 'HEAD')
    group['status'] = 'needs_revalidation'
    before = deepcopy(state.to_dict())
    with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('model called')), \
         patch.object(runner, '_candidate_deterministic_issues', side_effect=AssertionError('preflight repeated')), \
         patch.object(runner, '_early_candidate_checks', side_effect=AssertionError('checks repeated')):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=99,
                                       deadline=None, prior_failures=[], seen_fingerprints=set())
    assert result.status == 'component_evidence_reused' and not result.candidate_id
    assert group['status'] == 'completed'
    assert _memory(runner, group)['completion'] == receipt
    assert state.attempt_count == before['attempt_count'] and state.progress_credits == before['progress_credits']
    assert state.candidates == runner._experiment_store.load().candidates


def test_reselection_does_not_consume_an_attempt_or_diagnose_a_success(completed):
    runner, state, group, _, _ = completed
    state.base_commit = git(runner.repo_root, 'rev-parse', 'HEAD')
    group['status'] = 'needs_revalidation'
    state.attempt_count = 98
    calls = []
    def candidate(**kwargs):
        calls.append((runner._candidate_group['group_id'], kwargs['attempt']))
        if len(calls) == 1:
            assert prepare_component(runner, runner.repo_root)['decision'] == 'COMPLETED'
            return SelfRepairResult(False, 'component_evidence_reused', 'receipt valid', finding_group_id=group['group_id'])
        return SelfRepairResult(True, 'approved_candidate', 'all required gates passed', candidate_id='next-component',
            candidate_commit=state.base_commit, candidate_ref=state.base_commit)
    with patch.object(runner, '_load_or_create_experiment', return_value=(runner._experiment_store, state)), \
         patch.object(runner, '_ensure_approved_repair_design', return_value=True), \
         patch.object(runner, '_migrate_recoverable_candidate_to_pending'), \
         patch.object(runner, '_resume_pending_validation_candidate', return_value=None), \
         patch.object(runner, '_run_candidate', side_effect=candidate):
        result = runner._run_search()
    assert result.ok and calls == [(group['group_id'], 99), (state.finding_groups[1]['group_id'], 99)]
    assert len(state.candidates) == 2  # Original base plus the actual next candidate.


def test_failed_observation_is_reported_as_unavailable_then_recovers_without_review(completed):
    runner, state, group, commands, _ = completed
    with patch('auto_agents.repair_completion.context_parts', side_effect=OSError('temporary config read failure')):
        refresh(runner, runner.repo_root)
    decision = _memory(runner, group)['completion_assessment']
    assert decision['reason'] == 'completion context could not be observed'
    assert 'temporary config read failure' in decision['observation_error']
    assert decision['affected_checks'] == commands
    with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('model called')):
        assert prepare_component(runner, runner.repo_root)['decision'] == 'COMPLETED'
    assert group['status'] == 'completed'


def test_context_change_reports_the_changed_input_without_exposing_values(completed):
    runner, state, group, _, _ = completed
    runner._invocation_context = {'private-value': 'secret'}
    decision = assess(runner, runner.repo_root, group)
    assert decision['binding_changes']['components'] == ['invocation']
    assert 'secret' not in json.dumps(decision)
    assert decision['state'] == 'needs_revalidation'


@pytest.mark.parametrize('count', [0, 2])
def test_delta_schema_limits_scope_to_exact_pending_ids(count):
    context = {'findings': [{'finding_id': f'pending-{i}'} for i in range(count)]}
    for stage in ('self_repair_component_delta_review', 'self_repair_component_delta_format'):
        schema = schema_for(stage, context)
        decisions = schema['properties']['decisions']
        assert decisions['minItems'] == decisions['maxItems'] == count
        if count:
            assert decisions['items']['properties']['finding_id']['enum'] == ['pending-0', 'pending-1']
        assert schema['additionalProperties'] is False


@pytest.fixture
def revalidating(completed):
    runner, state, group, _, _ = completed
    runner._full_suite_environment_fingerprint = lambda: ('updated runtime',)
    assert prepare_component(runner, runner.repo_root)['decision'] == 'REVALIDATE'
    return runner, state, group


def test_format_correction_preserves_approval_and_never_repeats_scope_or_code_review(revalidating):
    runner, state, group = revalidating
    calls = []
    def provider(request):
        calls.append(request.stage)
        if len(calls) == 1:
            assert request.stage == 'self_repair_component_delta_review'
            return response(request, reason='')
        assert request.stage == 'self_repair_component_delta_format'
        assert request.response_schema and request.sandbox_mode == 'read-only'
        return response(request)
    runner.target_orchestrator._call_with_failover = provider
    review = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert review.ok and calls == ['self_repair_component_delta_review', 'self_repair_component_delta_format']
    assert group['status'] == 'needs_revalidation'
    assert runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick').ok
    assert len(calls) == 2


def test_interrupted_format_correction_resumes_draft_without_repeating_review(revalidating):
    runner, state, group = revalidating
    calls = []
    def provider(request):
        calls.append(request.stage)
        if len(calls) == 1:
            return response(request, reason='')
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return response(request)
    runner.target_orchestrator._call_with_failover = provider
    with pytest.raises(KeyboardInterrupt):
        runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    runner._experiment = runner._experiment_store.load()
    assert runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick').ok
    assert calls == ['self_repair_component_delta_review', 'self_repair_component_delta_format',
                     'self_repair_component_delta_format']


def test_format_exhaustion_is_durable_and_does_not_start_a_full_review(revalidating):
    runner, state, group = revalidating
    calls = []
    def provider(request):
        calls.append(request.stage)
        return response(request, reason='')
    runner.target_orchestrator._call_with_failover = provider
    for _ in range(2):
        with pytest.raises(PlanningBlocked, match='format corrections exhausted'):
            runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
        runner._experiment = runner._experiment_store.load()
    assert calls == ['self_repair_component_delta_review', 'self_repair_component_delta_format',
                     'self_repair_component_delta_format']


def test_format_correction_cannot_change_unreported_implementation_decision(revalidating):
    runner, state, _ = revalidating
    calls = []
    def provider(request):
        calls.append(request.stage)
        return response(request, reason='' if len(calls) == 1 else 'fixed reason',
                        implementation_required=False if len(calls) == 1 else True)
    runner.target_orchestrator._call_with_failover = provider
    with pytest.raises(PlanningBlocked, match='unreported substantive fields'):
        runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert len(calls) == 2


def test_extra_required_defect_goes_to_full_review_without_format_retry(revalidating):
    runner, state, group = revalidating
    calls = []
    def provider(request):
        calls.append(request.stage)
        if len(calls) == 1:
            return response(request, [{'finding_id': 'new-defect', 'verdict': 'required',
                'reason': 'a new counterexample needs repair', 'evidence': ['source.py:1']}])
        if request.stage == 'self_repair_scope_review':
            return response(request, [{'finding_id': 'new-defect', 'verdict': 'required',
                'reason': 'the retained ownership boundary regressed', 'evidence': ['tests/test_a.py:1'],
                'obligation_id': group['contract_obligation_ids'][0], 'trigger': 'resume the retained child',
                'consequence': 'the original owner is replaced', 'support_basis': 'the preserved ownership contract'}])
        assert request.stage == 'self_repair_candidate_review'
        assert 'a new counterexample needs repair' in request.prompt
        return response(request, decision='REJECT', reason='the new defect remains', implementation_required=True,
            findings=[{'finding_id': 'new-defect', 'severity': 'hard', 'disposition': 'candidate_regression',
                'causal_obligation_id': group['contract_obligation_ids'][0], 'affected_paths': ['tests/test_a.py'],
                'reason': 'a new counterexample needs repair', 'counterexample': 'the retained owner is replaced',
                'required_test': 'tests/test_a.py::test_a', 'evidence': ['source.py:1']}])
    runner.target_orchestrator._call_with_failover = provider
    result = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert not result.ok
    assert calls == ['self_repair_component_delta_review', 'self_repair_candidate_review', 'self_repair_scope_review']


def test_exhausted_review_format_is_not_recorded_as_another_failed_code_candidate(completed, tmp_path):
    runner, state, group, _, _ = completed
    runner._continuous_workspace = tmp_path / 'continuous'
    state.best_search_ref = git(runner.repo_root, 'rev-parse', 'HEAD')
    runner._full_suite_environment_fingerprint = lambda: ('updated runtime',)
    runner.target_orchestrator._call_with_failover = lambda request: response(request, reason='')
    with patch.object(runner, '_verification_environment_blocker', return_value=None):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=99,
                                       deadline=None, prior_failures=[], seen_fingerprints=set())
    assert result.status == 'planning_blocked'
    assert result.next_action['planning_failure']['code'] == 'delta_format_exhausted'
    assert group['status'] == 'needs_revalidation' and not state.progress_credits


@pytest.mark.parametrize('change', ['', 'unknown_id', 'required', 'reopened', 'changed_evidence', 'missing_disproof', 'duplicate'])
def test_only_exact_resolved_nonblocking_scope_repetitions_can_be_projected(completed, change):
    runner, state, group, _, _ = completed
    finding = SelfRepairFinding('already-fixed', status='resolved', disposition='candidate_regression',
        causal_obligation_id=group['contract_obligation_ids'][0], repair_group_id=group['group_id'], reason='old defect')
    state.findings[finding.finding_id] = finding
    proof = {'resolved': [finding.finding_id], 'findings': {finding.finding_id: finding_key(finding)}}
    row = {'finding_id': finding.finding_id, 'verdict': 'not_applicable', 'reason': 'the original failure is fixed',
           'evidence': ['tests/test_a.py:1'], 'disproof': 'the retained boundary rejects the counterexample'}
    payload = {'decision': 'APPROVE', 'reason': 'reviewed', 'implementation_required': False,
               'remaining_changes': [], 'findings': [], 'deferred_findings': [], 'decisions': [row]}
    if change == 'unknown_id': row['finding_id'] = 'new-unreviewed-defect'
    if change == 'required': row['verdict'] = 'required'
    if change == 'reopened': finding.status = 'reopened'
    if change == 'changed_evidence': finding.reason = 'new counterexample'
    if change == 'missing_disproof': row.pop('disproof')
    if change == 'duplicate': payload['decisions'].append(deepcopy(row))
    if change == 'required':
        conflicted = validate_reply(runner, payload, {'findings': []}, proof)
        assert conflicted['revalidation_protocol_error']['code'] == 'delta_scope_conflict'
        assert conflicted['decisions'] == payload['decisions']
    elif change:
        with pytest.raises(PlanFormatError):
            validate_reply(runner, payload, {'findings': []}, proof)
    else:
        normalized = validate_reply(runner, payload, {'findings': []}, proof)
        assert normalized['decisions'] == [] and payload['decisions'] == [row]
        assert not state.scope_decisions  # Extra rows cannot create new scope waivers.
