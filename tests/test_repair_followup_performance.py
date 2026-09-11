"""Regression cases from collab 96bfee35: reuse, diagnostics and review cost."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_planning import PlanningBlocked, prepare_component, review_scope
from auto_agents.repair_probe_recovery import probe_id
from auto_agents.repair_verification import run_component_checks
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_planning import setup
from test_repair_routing import git
from test_repair_verification_performance import runner, commands, passed


def finding(state):
    return SelfRepairFinding('diagnostic', status='confirmed', disposition='contract_violation',
        causal_obligation_id=state.contract_obligation_ids[0], reason='check retained boundary',
        affected_paths=['source.py'], evidence=['source.py:1'])


def scope_answer(item, verdict, probes=None):
    row = {'finding_id': item.finding_id, 'verdict': verdict,
           'obligation_id': item.causal_obligation_id, 'reason': 'examined current source',
           'evidence': ['source.py:1'], 'disproof': 'supported boundary is preserved'}
    return {'decisions': [row], **({'probes': probes} if probes else {})}


def probe(label):
    return {'command': 'python -B -c ' + repr('assert ' + label), 'expected': 'pass', 'purpose': 'boundary diagnostic'}


def test_scope_replaces_only_inconclusive_probe_and_keeps_success(setup):
    runner, state, plan, _ = setup
    item = finding(state)
    good, bad, corrected = probe('True'), probe('missing_api'), probe('1 == 1')
    requests, executed = [], []
    def provider(request):
        incoming = json.loads((request.output_path.parent / 'input.json').read_text())
        requests.append(incoming)
        if len(requests) == 1:
            payload = scope_answer(item, 'unknown', [good, bad])
        elif len(requests) == 2:
            assert incoming['probe_results'][0]['matches']
            payload = scope_answer(item, 'unknown', [{**corrected, 'replaces_probe': probe_id(bad)}])
        else:
            assert all(p['matches'] for p in incoming['probe_results'])
            payload = scope_answer(item, 'not_applicable')
        return AgentResult(True, [], request.output_path, summary=json.dumps(payload))
    def run(_runner, root, spec):
        executed.append(spec['command'])
        ok = spec['command'] != bad['command']
        return {'specification': spec, 'matches': ok, 'outcome': 'pass' if ok else 'inconclusive',
                'result': {'summary': 'passed' if ok else 'AttributeError: missing_api'}}
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', side_effect=run):
        review_scope(runner, runner.repo_root, [item])
    assert executed == [good['command'], bad['command'], corrected['command']]
    receipt = next(v for v in state.planning_receipts.values() if v.get('probe_corrections'))
    assert receipt['probe_corrections'] == 1 and len(receipt['probe_history']) == 1
    assert not state.progress_credits


@pytest.mark.parametrize('interrupt', [False, True])
def test_scope_correction_budget_and_interrupt_survive_reload(setup, interrupt):
    runner, state, plan, _ = setup
    item = finding(state)
    calls, executed = [], []
    def provider(request):
        incoming = json.loads((request.output_path.parent / 'input.json').read_text())
        calls.append(request)
        proposal = probe('missing_' + str(len(calls)))
        if incoming['probe_results']:
            proposal['replaces_probe'] = incoming['probe_results'][-1]['probe_id']
        return AgentResult(True, [], request.output_path, summary=json.dumps(scope_answer(item, 'unknown', [proposal])))
    def run(_runner, root, spec):
        executed.append(spec['command'])
        if interrupt and len(executed) == 1:
            raise KeyboardInterrupt
        return {'specification': spec, 'matches': False, 'outcome': 'inconclusive', 'result': {'summary': 'tool unavailable'}}
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', side_effect=run):
        if interrupt:
            with pytest.raises(KeyboardInterrupt):
                review_scope(runner, runner.repo_root, [item])
            runner._experiment = runner._experiment_store.load()
        with pytest.raises(PlanningBlocked, match='corrections exhausted'):
            review_scope(runner, runner.repo_root, [item])
        before = len(executed)
        runner._experiment = runner._experiment_store.load()
        with pytest.raises(PlanningBlocked):
            review_scope(runner, runner.repo_root, [item])
    assert before == len(executed) == 3
    assert len(set(executed)) == 3


def test_successful_probe_cannot_be_replaced_or_its_oracle_changed(setup):
    from auto_agents.repair_probe_recovery import queue_probes
    runner, state, _, _ = setup
    good, bad = probe('True'), probe('missing')
    state.planning_receipts['scope'] = {'probe_results': [
        {'specification': good, 'matches': True, 'outcome': 'pass'},
        {'specification': bad, 'matches': False, 'outcome': 'inconclusive'}]}
    for proposal in [{**probe('1'), 'replaces_probe': probe_id(good)},
                     {**bad, 'purpose': 'renamed only', 'replaces_probe': probe_id(bad)},
                     {**probe('1'), 'replaces_probe': probe_id(bad), 'expected': 'behavior_failure'}]:
        with pytest.raises(PlanningBlocked, match='preserve its expected outcome'):
            queue_probes(runner, 'scope', [proposal])


def test_stable_pool_reuses_paths_across_components_but_not_source_changes(runner, monkeypatch, tmp_path):
    # Keep pool state outside the versioned source used by this fixture.
    runner._continuous_workspace = tmp_path.parent / (tmp_path.name + '-continuous')
    paths = []
    monkeypatch.setattr(runner, '_run_verification_commands', lambda selected, root: paths.append((selected[0], root)) or passed(selected[0]))
    selected = commands()[:2]
    assert run_component_checks(runner, selected, runner.repo_root, parallel=False).ok
    first = dict(paths)
    runner._candidate_group = {'group_id': 'another-component'}
    paths.clear()
    assert run_component_checks(runner, list(reversed(selected)), runner.repo_root).ok
    assert dict(paths) == first
    assert len(set(first.values())) == 2
    (runner.repo_root / 'source.py').write_text('new_source = True\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'changed source')
    paths.clear()
    assert run_component_checks(runner, selected, runner.repo_root).ok
    assert not set(dict(paths).values()).intersection(first.values())


def test_modified_pool_workspace_is_preserved_and_cannot_supply_reused_proof(runner, monkeypatch, tmp_path):
    runner._continuous_workspace = tmp_path.parent / (tmp_path.name + '-continuous')
    paths = []
    monkeypatch.setattr(runner, '_run_verification_commands', lambda selected, root: paths.append(root) or passed(selected[0]))
    assert run_component_checks(runner, commands()[:1], runner.repo_root).ok
    retained = paths[-1]
    marker = retained / 'unreviewed.py'
    marker.write_text('changed = True\n')
    assert run_component_checks(runner, commands()[:1], runner.repo_root).ok
    assert paths[-1] != retained and marker.read_text() == 'changed = True\n'


def test_scope_bound_reference_migration_skips_model_formatting(setup):
    runner, state, plan, calls = setup
    item = finding(state)
    state.findings[item.finding_id] = item
    state.finding_groups[0]['finding_ids'] = [item.finding_id]
    runner._candidate_group = deepcopy(state.finding_groups[0])
    for scenario in plan['scenarios']:
        scenario['finding_ids'] = [item.finding_id]
    original = runner.target_orchestrator._call_with_failover
    def provider(request):
        if request.stage == 'self_repair_scope_review':
            return AgentResult(True, [], request.output_path, summary=json.dumps(scope_answer(item, 'not_applicable')))
        assert request.stage != 'self_repair_plan_format'
        return original(request)
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', side_effect=lambda r, w, s: {'specification': s, 'matches': True, 'outcome': 'pass'}):
        approved = prepare_component(runner, runner.repo_root)
        assert prepare_component(runner, runner.repo_root)['request_id'] == approved['request_id']
    assert len(calls) == 2
    for old, new in zip(plan['scenarios'], approved['plan']['scenarios']):
        assert new['finding_ids'] == [] and new['reference_ids'] == [item.finding_id]
        assert {k: v for k, v in old.items() if k != 'finding_ids'} == {k: v for k, v in new.items() if k not in {'finding_ids', 'reference_ids'}}


def test_unknown_plan_identity_is_not_silently_migrated(setup):
    from auto_agents.repair_planning import normalize_plan_references, validate_plan, PlanFormatError
    runner, state, plan, _ = setup
    plan['scenarios'][0]['finding_ids'] = ['unreviewed-observation']
    normalized = normalize_plan_references(runner, runner.repo_root, plan, runner._candidate_group)
    assert normalized == plan
    with pytest.raises(PlanFormatError):
        validate_plan(normalized, runner._candidate_group, set(state.contract_obligation_ids))


def test_independent_review_can_choose_verify_existing_without_changing_approved_plan(setup):
    runner, state, plan, calls = setup
    original = runner.target_orchestrator._call_with_failover
    def provider(request):
        reply = original(request)
        if request.stage == 'self_repair_plan_review':
            payload = json.loads(reply.summary)
            payload.update(implementation_required=False, remaining_changes=[])
            reply.summary = json.dumps(payload)
        return reply
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
        assert runner._candidate_group['mode'] == 'verify_existing'
        assert receipt['plan'].get('mode', 'implement') == 'implement'
        assert prepare_component(runner, runner.repo_root)['request_id'] == receipt['request_id']
        assert runner._candidate_group['mode'] == 'verify_existing'
        runner._candidate_next_action = {'kind': 'repair_code'}
        prepare_component(runner, runner.repo_root)
        assert runner._candidate_group.get('mode', 'implement') == 'implement'


@pytest.mark.parametrize('change', ['none', 'source', 'coverage', 'component', 'environment', 'policy', 'evidence', 'integration'])
def test_review_reuse_requires_identical_coverage_and_evidence(setup, change):
    runner, state, plan, _ = setup
    calls = []
    def provider(request):
        calls.append(request)
        return AgentResult(True, [], request.output_path,
                           summary=json.dumps({'decision': 'APPROVE', 'reason': 'checked', 'findings': [], 'resolved_finding_ids': []}))
    runner.target_orchestrator._call_with_failover = provider
    first = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert first.ok
    runner._experiment = runner._experiment_store.load()
    if change == 'source':
        (runner.repo_root / 'source.py').write_text('value = 2\n')
    elif change == 'coverage':
        runner._candidate_group['scenarios'] = [{'scenario_id': 'new-interaction'}]
    elif change == 'component':
        runner._candidate_group['group_id'] = 'different-owner'
    elif change == 'environment':
        runner._full_suite_environment_fingerprint = lambda: ('new',)
    elif change == 'policy':
        runner._review_effort = lambda: 'different-review-policy'
    elif change == 'evidence':
        runner._candidate_next_action = {'kind': 'repair_code', 'evidence_ids': ['new-counterexample']}
    second = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60,
                                      phase='integration' if change == 'integration' else 'quick')
    assert second.ok and len(calls) == (1 if change == 'none' else 2)


def test_cross_component_reviews_supply_context_without_approving_new_coverage(setup):
    from auto_agents.repair_memory import remember_review, review_context
    runner, state, _, _ = setup
    current = deepcopy(runner._candidate_group)
    previous = {**current, 'group_id': 'earlier', 'touched_paths': ['source.py']}
    remember_review(runner, runner.repo_root, previous, {'decision': 'APPROVE', 'findings': []})
    context = review_context(runner, runner.repo_root, current)
    assert context['mode'] == 'related_components'
    assert context['related_reviews'][0]['component'] == 'earlier'
    assert 'EVERY active scenario' in context['instruction']
    runner._full_suite_environment_fingerprint = lambda: ('changed',)
    assert review_context(runner, runner.repo_root, current)['mode'] == 'initial'


def test_review_changed_during_model_call_cannot_seed_a_reusable_approval(setup):
    runner, state, _, _ = setup
    calls = []
    def provider(request):
        calls.append(request)
        if len(calls) == 1:
            (runner.repo_root / 'source.py').write_text('changed_after_request = True\n')
        return AgentResult(True, [], request.output_path,
                           summary=json.dumps({'decision': 'APPROVE', 'reason': 'checked', 'findings': [], 'resolved_finding_ids': []}))
    runner.target_orchestrator._call_with_failover = provider
    runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert len(calls) == 2


@pytest.mark.skipif(not sys.platform.startswith('linux') or not shutil.which('codex'), reason='requires local Linux verification sandbox')
def test_real_namespace_cache_and_collection_receipts(tmp_path, monkeypatch):
    from auto_agents.managed_verification import engine_runner
    from auto_agents.verification_sandbox import landlock_abi
    if landlock_abi() < 3:
        pytest.skip('Landlock unavailable')
    for key, name in [('AUTO_AGENTS_VERIFICATION_ROOT', 'proofs'), ('AUTO_AGENTS_WORKER_ROOT', 'worker'),
                      ('AUTO_AGENTS_CLUSTER_HOME', 'cluster')]:
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_DISABLED', '1')
    source, target = tmp_path / 'source', tmp_path / 'readonly'
    source.mkdir(); target.mkdir()
    external = target / 'input'
    external.write_text('original')
    external.chmod(0o640)
    (source / 'tests').mkdir()
    (source / 'tests/test_value.py').write_text('import os\ndef test_value():\n    assert os.stat(' + repr(str(external)) + ').st_mode & 0o777 == 0o640\n')
    git(source, 'init', '-q'); git(source, 'add', '.'); git(source, 'commit', '-qm', 'immutable tests')
    runner = engine_runner(source, target, sys.executable, repository=source)
    collect = 'python -m pytest --collect-only -q tests/test_value.py'
    execute = 'python -m pytest -q tests/test_value.py'
    first = runner._run_verification_commands([collect], source)
    assert first.ok, first.summary
    cached = runner._run_verification_commands([collect], source)
    assert cached.ok and cached.payload['certificate_hits'] == 1
    assert cached.payload['command_timings'][0]['collected_cases'] == 1
    assert cached.payload['executed_tests'] == []
    first = runner._run_verification_commands([execute], source)
    assert first.ok, first.summary
    assert runner._run_verification_commands([execute], source).payload['certificate_hits'] == 1
    runner._continuous_workspace = tmp_path / 'continuous'
    runner._candidate_group = {'group_id': 'first'}
    runner._experiment = SimpleNamespace(component_memory={})
    initial = runner._guarded_component_checks([execute], source)
    assert initial.ok, initial.summary
    runner._candidate_group = {'group_id': 'second'}
    repeated = runner._guarded_component_checks([execute], source, parallel=True)
    assert repeated.ok and repeated.payload['certificate_hits'] == 1
    external.chmod(0o600)
    changed = runner._guarded_component_checks([execute], source, parallel=True)
    assert not changed.ok and changed.payload['certificate_hits'] == 0
    informational = 'python -m pytest --version'
    assert runner._run_verification_commands([informational], source, allow_pytest_no_tests=True).ok
    mandatory = runner._run_verification_commands([informational], source)
    assert not mandatory.ok and mandatory.payload['certificate_hits'] == 0
