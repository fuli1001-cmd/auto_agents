"""Real failure modes from the c95 scope/plan/network resume path."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult, ProvidersExhaustedError
from auto_agents.repair_capability_checks import planning_capability_fingerprint
from auto_agents.repair_planning import (PlanningBlocked, _retained_scope,
    _scope_revalidation_reason, prepare_component, review_scope)
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_planning import setup


def retained_exclusion(setup):
    runner, state, plan, calls = setup
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        prepare_component(runner, runner.repo_root)
    finding = SelfRepairFinding('old-exclusion', status='confirmed', disposition='candidate_regression',
        causal_obligation_id=state.contract_obligation_ids[0],
        repair_group_id=state.finding_groups[0]['group_id'], affected_paths=['source.py'],
        counterexample='the retained owner is allegedly lost', reason='inspect the original allegation',
        evidence=['source.py:1'], required_test=plan['quick_checks'][0])
    state.findings[finding.finding_id] = finding
    state.finding_groups[0]['finding_ids'] = [finding.finding_id]
    row = dict(finding_id=finding.finding_id, verdict='not_applicable', reason='independently disproved',
        obligation_id=finding.causal_obligation_id, evidence=['source.py:1'],
        disproof='the supported trigger preserves ownership')
    original = runner.target_orchestrator._call_with_failover
    def provider(request):
        if request.stage == 'self_repair_scope_review':
            return AgentResult(True, [], request.output_path, summary=json.dumps({'decisions': [row]}))
        return original(request)
    runner.target_orchestrator._call_with_failover = provider
    review_scope(runner, runner.repo_root, [finding])
    # Environment changes must still refresh exclusions, even though the plan
    # and source remain intact. The combined request is an independent call.
    runner._full_suite_environment_fingerprint = lambda: ('new runtime',)
    runner._candidate_group = deepcopy(state.finding_groups[0])
    return runner, state, finding, row, original


def test_one_independent_refresh_admits_both_decisions_without_replanning(setup):
    runner, state, finding, row, original = retained_exclusion(setup)
    stages = []
    def provider(request):
        stages.append(request.stage)
        assert request.stage == 'self_repair_plan_review'
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        assert context['scope_findings'] == [finding.to_dict()]
        assert context['scope_revalidation'][finding.finding_id]['previous']['disproof'] == row['disproof']
        assert not request.resume_session_id and request.sandbox_mode == 'read-only'
        result = original(request)
        result.summary = json.dumps({**json.loads(result.summary), 'decisions': [row]})
        return result
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    assert stages == ['self_repair_plan_review']
    scope = state.scope_decisions[finding.finding_id]
    assert scope['request_id'] == receipt['request_id']
    assert _retained_scope(runner, scope, finding.to_dict())
    assert not state.progress_credits and state.attempt_count == 0
    # Tampering cannot turn a combined result into reusable scope evidence.
    path = runner._experiment_store.root / 'planning' / receipt['request_id'] / 'result.json'
    payload = json.loads(path.read_text())
    payload['reason'] = 'tampered'
    path.write_text(json.dumps(payload))
    assert not _retained_scope(runner, scope, finding.to_dict())


@pytest.mark.parametrize('failure', ['omitted', 'unknown', 'wrong_id', 'unsafe_deferral', 'changed_source', 'changed_environment'])
def test_incomplete_combined_review_never_authorizes_generation(setup, failure):
    runner, state, finding, row, original = retained_exclusion(setup)
    stages = []
    def provider(request):
        stages.append(request.stage)
        result = original(request)
        if failure == 'unknown':
            row['verdict'] = 'unknown'
        elif failure == 'wrong_id':
            row['finding_id'] = 'another-finding'
        elif failure == 'unsafe_deferral':
            row['verdict'] = 'follow_up'
        elif failure == 'changed_source':
            (runner.repo_root / 'source.py').write_text('value = 100\n')
        elif failure == 'changed_environment':
            runner._full_suite_environment_fingerprint = lambda: ('changed while reviewing',)
        payload = json.loads(result.summary)
        if failure != 'omitted':
            payload['decisions'] = [row]
        result.summary = json.dumps(payload)
        return result
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        with pytest.raises(PlanningBlocked):
            prepare_component(runner, runner.repo_root)
    assert stages == ['self_repair_plan_review']
    assert not state.progress_credits and state.attempt_count == 0
    assert 'planning_receipt' not in runner._candidate_group


def test_refresh_that_establishes_new_blocker_returns_to_actual_scope_planning(setup):
    runner, state, finding, row, original = retained_exclusion(setup)
    stages = []
    def provider(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_component_plan':
            context = json.loads((request.output_path.parent / 'input.json').read_text())
            assert [f['finding_id'] for f in context['findings']] == [finding.finding_id]
            assert context['planning_action'] == 'amend_plan'
            raise RuntimeError('reached required amendment without writing')
        result = original(request)
        row.update(verdict='required', trigger='the supported trigger loses the owner',
                   consequence='foreign work is claimed', support_basis='retain ownership')
        result.summary = json.dumps({**json.loads(result.summary), 'decisions': [row]})
        return result
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        with pytest.raises(RuntimeError, match='reached required amendment'):
            prepare_component(runner, runner.repo_root)
    assert stages == ['self_repair_plan_review', 'self_repair_component_plan']
    assert not state.progress_credits


def test_required_scope_survives_engine_upgrade_without_granting_verification(setup):
    runner, state, plan, calls = setup
    finding = dict(finding_id='regression', disposition='candidate_regression',
        causal_obligation_id=state.contract_obligation_ids[0], affected_paths=['source.py'])
    row = dict(finding_id='regression', verdict='required', reason='actual regression',
        obligation_id=finding['causal_obligation_id'], evidence=['source.py:1'],
        trigger='retained child resumes', consequence='wrong owner', support_basis='preserve ownership')
    runner.target_orchestrator._call_with_failover = lambda request: AgentResult(
        True, [], request.output_path, summary=json.dumps({'decisions': [row]}))
    review_scope(runner, runner.repo_root, [finding])
    state.base_commit = 'new engine revision'
    assert _scope_revalidation_reason(runner, runner.repo_root, finding, 'new environment') == ''
    assert not state.progress_credits


@pytest.mark.parametrize('failure', ['provider', 'host'])
def test_transport_recovery_retains_draft_and_does_not_spend_semantic_attempts(setup, failure):
    runner, state, plan, calls = setup
    original = runner.target_orchestrator._call_with_failover
    stages = []
    def provider(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_plan_review':
            if failure == 'host':
                raise KeyboardInterrupt
            raise ProvidersExhaustedError('connection lost', providers=['codex'], result=None, category='connection')
        return original(request)
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        for _ in range(3):
            with pytest.raises(KeyboardInterrupt if failure == 'host' else PlanningBlocked):
                prepare_component(runner, runner.repo_root)
            runner._experiment = runner._experiment_store.load()
            runner._candidate_group = deepcopy(runner._experiment.finding_groups[0])
        for _ in range(2):
            with pytest.raises(PlanningBlocked, match='transport recovery exhausted'):
                prepare_component(runner, runner.repo_root)
            runner._experiment = runner._experiment_store.load()
    assert stages == ['self_repair_component_plan'] + ['self_repair_plan_review'] * 3
    assert next(iter(runner._experiment.repair_episodes.values()))['semantic_attempts'] == 1
    assert not runner._experiment.progress_credits


def test_planning_ignores_only_known_custody_annotation():
    old = {'production_namespace': {'nested_gate_metadata': {'input_trace_activation': {
        'supported': True, 'owner': 'actual-owner-digest', 'protocol': 1}}}}
    current = deepcopy(old)
    current['production_namespace']['validation_protocol'] = dict(version=1,
        runtime='/new/runtime', runtime_commit='new revision', observer='observer digest',
        environment='environment', generation={'job': 'new job'}, acceptance_proof=False,
        activation='immutable selected controller; candidate changes require independently verified version handoff')
    assert planning_capability_fingerprint(old) == planning_capability_fingerprint(current)
    changed = deepcopy(current)
    changed['production_namespace']['nested_gate_metadata']['input_trace_activation']['owner'] = 'other owner'
    assert planning_capability_fingerprint(old) != planning_capability_fingerprint(changed)
    for key, value in [('version', 2), ('unknown_capability', True), ('acceptance_proof', True)]:
        changed = deepcopy(current)
        changed['production_namespace']['validation_protocol'][key] = value
        assert planning_capability_fingerprint(old) != planning_capability_fingerprint(changed)
