"""A proposed plan, a model verdict, and a successful repair are distinct facts."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_planning import (
    POLICY_VERSION, PlanningBlocked, _probe, finding_key, history_report, nonblocking_scope, prepare_component, review_scope, validate_plan,
)
from auto_agents.repair_schedule import canonical_commands
from auto_agents.self_repair import _VerificationResult
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_routing import make_runner, git


@pytest.fixture
def setup(tmp_path):
    runner, state, _ = make_runner(tmp_path)
    runner._compact_diagnosis_payload = lambda: {'original_request': 'retain custody'}
    runner._full_suite_environment_fingerprint = lambda: ('provisioned',)
    runner._verification_python_cache = __import__('sys').executable
    path = runner.repo_root / 'tests/test_contract.py'
    path.parent.mkdir()
    path.write_text('def test_contract():\n    assert 2 + 2 == 4\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'retained tests')
    state.base_commit = git(runner.repo_root, 'rev-parse', 'HEAD')
    state.best_search_ref = state.base_commit
    command = 'python -m pytest -q tests/test_contract.py::test_contract'
    group = state.finding_groups[0]
    group.update(touched_paths=['tests/test_contract.py'], focused_tests=[command], implementation_steps=['preserve custody'])
    runner._candidate_group = dict(group)
    obligation = group['contract_obligation_ids'][0]
    plan = dict(implementation_steps=['preserve the retained source'], touched_paths=['tests/test_contract.py'],
        quick_checks=[command], scenarios=[dict(scenario_id=kind, kind=kind, obligation_ids=[obligation], finding_ids=[],
            trigger='retained child resumes', expected='original ownership survives', evidence=['source.py:1'], check=command)
            for kind in ('failure', 'compatibility', 'recovery', 'interaction')],
        probes=[dict(command=command, expected='pass', purpose='retained compatibility')])
    calls = []
    def provider(request):
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        calls.append((request, context))
        if request.stage == 'self_repair_component_plan':
            answer = {**deepcopy(plan), 'decision': 'APPROVE', 'planning_receipt': 'forged', 'focused_tests': []}
        elif request.stage == 'self_repair_plan_review':
            answer = dict(decision='APPROVE', reason='independently checked', issues=[],
                          scenario_ids=[s['scenario_id'] for s in context['proposed_plan']['scenarios']])
        else:
            raise AssertionError(request.stage)
        return AgentResult(True, [], request.output_path, summary=json.dumps(answer))
    runner.target_orchestrator._call_with_failover = provider
    return runner, state, plan, calls


def test_independent_plan_and_real_probe_do_not_issue_repair_credit(setup):
    runner, state, plan, calls = setup
    before = git(runner.repo_root, 'status', '--porcelain')
    receipt = prepare_component(runner, runner.repo_root)
    assert [r.stage for r, _ in calls] == ['self_repair_component_plan', 'self_repair_plan_review']
    assert all(r.sandbox_mode == 'read-only' and not r.resume_session_id for r, _ in calls)
    assert receipt['planner_request'] != receipt['request_id']
    assert receipt['probe_results'][0]['outcome'] == 'pass'
    assert not state.progress_credits and state.attempt_count == 0
    assert runner._candidate_group['focused_tests'] == state.finding_groups[0]['focused_tests']
    assert runner._candidate_group['planning_receipt'] != 'forged'
    assert not getattr(runner, '_candidate_verified_check_ids', set())
    assert before == git(runner.repo_root, 'status', '--porcelain')
    assert prepare_component(runner, runner.repo_root) == receipt
    assert len(calls) == 2


def test_rejection_is_bounded_and_persisted_without_writing_code(setup):
    runner, state, plan, calls = setup
    original = runner.target_orchestrator._call_with_failover
    def reject(request):
        result = original(request)
        if request.stage == 'self_repair_plan_review':
            result.summary = json.dumps(dict(decision='REVISE', reason='missing recovery mechanism',
                issues=['old authority still overwritten'], scenario_ids=[]))
        return result
    runner.target_orchestrator._call_with_failover = reject
    with pytest.raises(PlanningBlocked, match='exhausted'):
        prepare_component(runner, runner.repo_root)
    assert len(calls) == 6
    assert max(state.planning_attempts.values()) == 3
    runner._experiment = runner._experiment_store.load()
    with pytest.raises(PlanningBlocked, match='exhausted'):
        prepare_component(runner, runner.repo_root)
    assert len(calls) == 6 and not state.progress_credits


@pytest.mark.parametrize('mutation', ['environment', 'foreign_source', 'new_counterexample'])
def test_approval_reuse_requires_unchanged_assumptions(setup, mutation):
    runner, state, plan, calls = setup
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        first = prepare_component(runner, runner.repo_root)
        runner._candidate_group = dict(state.finding_groups[0])
        if mutation == 'environment':
            runner._full_suite_environment_fingerprint = lambda: ('different',)
        elif mutation == 'foreign_source':
            (runner.repo_root / 'source.py').write_text('changed dependency\n')
        else:
            state.finding_groups[0]['implementation_steps'] += ['new uncovered counterexample']
            runner._candidate_group = dict(state.finding_groups[0])
        second = prepare_component(runner, runner.repo_root)
    assert first['request_id'] != second['request_id']
    # Changed scope needs an amendment; source/environment revalidation can
    # audit the existing draft directly, with fresh probes and approval.
    assert len(calls) == (4 if mutation == 'new_counterexample' else 3)


def test_implementation_edits_within_approved_plan_do_not_force_replanning(setup):
    runner, state, plan, calls = setup
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        first = prepare_component(runner, runner.repo_root)
        (runner.repo_root / 'tests/test_contract.py').write_text('def test_contract(): assert True\n')
        runner._candidate_group = dict(state.finding_groups[0])
        assert prepare_component(runner, runner.repo_root) == first
    assert len(calls) == 2  # Code still needs quick checks, review and expanded proof.


@pytest.mark.parametrize('failure', ['malformed', 'mutated', 'provider'])
def test_invalid_reviewer_cannot_authorize_generation(setup, failure):
    runner, state, plan, calls = setup
    original = runner.target_orchestrator._call_with_failover
    def bad(request):
        result = original(request)
        if request.stage == 'self_repair_plan_review':
            if failure == 'malformed':
                result.summary = '{'
            elif failure == 'mutated':
                (runner.repo_root / 'source.py').write_text('unexpected write\n')
            else:
                result.ok = False
        return result
    runner.target_orchestrator._call_with_failover = bad
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        with pytest.raises(PlanningBlocked):
            prepare_component(runner, runner.repo_root)
    assert not runner._candidate_group.get('planning_receipt')
    assert not any(v.get('decision') == 'APPROVE' for v in state.planning_receipts.values())


@pytest.mark.parametrize('verdict', ['required', 'follow_up', 'unknown'])
def test_scope_needs_independent_evidence_not_only_an_obligation_id(setup, verdict):
    runner, state, plan, calls = setup
    finding = SelfRepairFinding('observation', status='confirmed', disposition='contract_violation',
        causal_obligation_id=state.contract_obligation_ids[0], counterexample='retained child loses scope',
        required_test=plan['quick_checks'][0], evidence=['source.py:1'])
    state.findings[finding.finding_id] = finding
    def scope(request):
        return AgentResult(True, [], request.output_path, summary=json.dumps({'decisions': [dict(
            finding_id=finding.finding_id, verdict=verdict, obligation_id=finding.causal_obligation_id,
            reason='reviewed original supported behavior', trigger='resume the retained child', consequence='ownership lost',
            support_basis='original request', evidence=['source.py:1'])]}))
    runner.target_orchestrator._call_with_failover = scope
    if verdict == 'unknown':
        with pytest.raises(PlanningBlocked, match='insufficient'):
            review_scope(runner, runner.repo_root, [finding])
    else:
        review_scope(runner, runner.repo_root, [finding])
        with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('unchanged scope was re-audited')):
            review_scope(runner, runner.repo_root, [finding])
    assert nonblocking_scope(state, finding) == (verdict == 'follow_up')
    assert not state.progress_credits
    assert finding.status == 'confirmed'  # Reclassification does not rewrite history.
    finding.counterexample = 'different observed behavior'
    assert not nonblocking_scope(state, finding)


@pytest.mark.parametrize('disposition,obligation', [('candidate_regression', 'root'), ('contract_violation', 'safety:target_untouched')])
def test_scope_cannot_defer_proven_regression_or_safety(setup, disposition, obligation):
    runner, state, plan, calls = setup
    if obligation == 'root':
        obligation = state.contract_obligation_ids[0]
    finding = dict(finding_id='damage', disposition=disposition, causal_obligation_id=obligation,
                   reason='foreign file overwritten', counterexample='writer changes shared source')
    runner.target_orchestrator._call_with_failover = lambda req: AgentResult(True, [], req.output_path,
        summary=json.dumps({'decisions': [dict(finding_id='damage', verdict='follow_up',
            reason='unlikely input', evidence=['source.py:1'])]}))
    with pytest.raises(PlanningBlocked, match='cannot defer'):
        review_scope(runner, runner.repo_root, [finding])
    assert not state.scope_decisions


def test_probe_cannot_mutate_original_or_claim_changed_source_as_evidence(setup):
    runner, state, plan, calls = setup
    result = _probe(runner, runner.repo_root, dict(command='python -B -c "from pathlib import Path; Path(\'source.py\').write_text(\'changed\')"',
                                                expected='pass', purpose='mutation fixture'))
    assert result['outcome'] == 'inconclusive' and not result['matches']
    assert (runner.repo_root / 'source.py').read_text() == 'value = 1\n'
    assert not state.progress_credits


@pytest.mark.parametrize('quick_ok,review_ok', [(False, True), (True, False), (True, True)])
def test_small_check_precedes_review_and_does_not_run_expanded_suite(setup, quick_ok, review_ok):
    runner, state, plan, calls = setup
    runner._candidate_group.update(plan, planning_receipt='reviewed')
    sequence = []
    runner._run_verification_commands = lambda *a, **kw: (sequence.append('quick') or _VerificationResult(quick_ok, 'quick'))
    runner._review_candidate = lambda *a, **kw: (sequence.append('review') or _VerificationResult(review_ok, 'review'))
    runner._run_active_group_verification = lambda *a: pytest.fail('expanded verification ran early')
    quick, review = runner._early_candidate_checks(runner.repo_root, state.base_commit)
    assert sequence == (['quick', 'review'] if quick_ok else ['quick'])
    assert quick.ok == quick_ok
    assert review.ok == (quick_ok and review_ok)


def test_command_dedup_preserves_cohort_flags_and_original_request_mapping():
    commands = ['python -m pytest -q tests/a.py::test_a',
                'python  -m pytest -q "tests/a.py::test_a"',
                'python -m pytest -q tests/a.py::test_a tests/b.py::test_b',
                'python -m pytest -x tests/a.py::test_a',
                'echo x && python -m pytest -q tests/a.py::test_a']
    selected, requests = canonical_commands(commands)
    assert len(selected) == 4 and len(requests) == 5
    assert requests[0]['execution_index'] == requests[1]['execution_index']
    assert requests[2]['execution_index'] != requests[0]['execution_index']


@pytest.mark.parametrize('stage', ['plan', 'quick', 'review', 'expanded'])
def test_actual_candidate_pipeline_stops_before_later_stages(setup, stage):
    runner, state, plan, calls = setup
    runner._candidate_is_final_group = False
    sequence = []
    def prepare(root):
        sequence.append('plan')
        if stage == 'plan':
            raise PlanningBlocked('independent review rejected the plan')
        runner._candidate_group.update(plan, planning_receipt='independent')
    def writer(request):
        sequence.append('writer')
        (request.cwd / 'source.py').write_text('value = 2\n')
        return AgentResult(True, [], request.output_path, summary='updated code\nCOMMIT_MESSAGE: preserve custody')
    def quick(*args, **kwargs):
        sequence.append('quick')
        return _VerificationResult(stage != 'quick', 'small checks', returncodes=[1 if stage == 'quick' else 0])
    def review(*args, **kwargs):
        sequence.append('review')
        return _VerificationResult(stage != 'review', 'semantic review', payload={'findings': []})
    def expanded(*args):
        sequence.append('expanded')
        return _VerificationResult(False, 'expanded regression failed', returncodes=[1])
    runner.target_orchestrator._call_with_failover = writer
    with (patch.object(runner, '_prepare_component_plan', side_effect=prepare),
          patch.object(runner, '_build_prompt', return_value='repair this candidate'),
          patch.object(runner, '_candidate_deterministic_issues', return_value=[]),
          patch.object(runner, '_verification_environment_blocker', return_value=None),
          patch.object(runner, '_run_verification_commands', side_effect=quick),
          patch.object(runner, '_review_candidate', side_effect=review),
          patch.object(runner, '_run_active_group_verification', side_effect=expanded),
          patch.object(runner, '_candidate_boundary_checks', side_effect=AssertionError('boundary ran too early')),
          patch.object(runner, '_full_suite_differential', side_effect=AssertionError('full suite ran too early'))):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=1,
                                      deadline=None, prior_failures=[], seen_fingerprints=set())
    expected = ['plan', 'writer', 'quick', 'review', 'expanded']
    stop = {'plan': 1, 'quick': 3, 'review': 4, 'expanded': 5}[stage]
    assert sequence == expected[:stop]
    assert not result.ok and result.status != 'candidate_group_completed'
    assert 'validation:focused' not in result.passed_obligations


def test_unknown_scope_can_use_bounded_probe_without_claiming_repair(setup):
    runner, state, plan, calls = setup
    finding = dict(finding_id='uncertain', disposition='contract_violation',
                   causal_obligation_id=state.contract_obligation_ids[0], reason='needs a native observation')
    count = 0
    def scope(request):
        nonlocal count
        count += 1
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        answer = {'decisions': [dict(finding_id='uncertain', verdict='unknown' if count == 1 else 'required',
            obligation_id=finding['causal_obligation_id'], trigger='original resume', consequence='wrong owner',
            support_basis='existing route', evidence=['native parser observation'], reason='checked') ]}
        if count == 1:
            answer['probes'] = [{'command': 'python -B -c "print(1)"', 'expected': 'pass', 'purpose': 'read-only observation'}]
        else:
            assert context['probe_results'][0]['outcome'] == 'pass'
        return AgentResult(True, [], request.output_path, summary=json.dumps(answer))
    runner.target_orchestrator._call_with_failover = scope
    review_scope(runner, runner.repo_root, [finding])
    assert count == 2 and state.scope_decisions['uncertain']['verdict'] == 'required'
    assert not state.progress_credits


def test_v5_migration_preserves_history_and_requires_new_audit(setup):
    runner, state, plan, calls = setup
    group = state.finding_groups[0]
    group.update(status='completed', completed_by='prior')
    state.consecutive_non_improvements = 2
    state.progress_credits['existing'] = 'prior'
    payload = state.to_dict()
    payload['schema_version'] = 5
    for name in ('scope_decisions', 'planning_receipts', 'planning_attempts', 'historical_completed_groups'):
        payload.pop(name, None)
    path = runner._experiment_store.path
    path.write_text(json.dumps(payload))
    loaded = runner._experiment_store.load()
    runner._experiment_store.save(loaded)
    assert json.loads(path.with_name('experiment.v5.json').read_text()) == payload
    assert loaded.historical_completed_groups[group['group_id']]['completed_by'] == 'prior'
    assert not loaded.planning_receipts
    assert loaded.progress_credits == state.progress_credits
    assert loaded.consecutive_non_improvements == 2
    report = history_report(loaded)
    assert report['historical_progress_count'] == 1


@pytest.mark.parametrize('damage', ['missing', 'incomplete'])
def test_serialized_approval_without_independent_artifact_is_not_reused(setup, damage):
    runner, state, plan, calls = setup
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        first = prepare_component(runner, runner.repo_root)
        result_path = runner._experiment_store.root / 'planning' / first['request_id'] / 'result.json'
        if damage == 'missing':
            result_path.unlink()
        else:
            result_path.write_text(json.dumps({'decision': 'APPROVE', 'issues': [], 'scenario_ids': []}))
        runner._candidate_group = dict(state.finding_groups[0])
        second = prepare_component(runner, runner.repo_root)
    assert second['request_id'] != first['request_id'] and len(calls) == 3
    assert [request.stage for request, _ in calls].count('self_repair_component_plan') == 1


def test_verified_existing_candidate_uses_acceptance_without_a_writer(setup):
    runner, state, plan, calls = setup
    runner._candidate_is_final_group = False
    runner.target_orchestrator._call_with_failover = lambda *_: pytest.fail('writer invoked for retained code')
    def prepare(root):
        runner._candidate_group.update(plan, planning_receipt='independent', mode='verify_existing')
    ok = _VerificationResult(True, 'trusted check passed', returncodes=[0])
    with (patch.object(runner, '_prepare_component_plan', side_effect=prepare),
          patch.object(runner, '_build_prompt', return_value='revalidate'),
          patch.object(runner, '_candidate_deterministic_issues', return_value=[]),
          patch.object(runner, '_verification_environment_blocker', return_value=None),
          patch.object(runner, '_run_verification_commands', return_value=ok) as quick,
          patch.object(runner, '_review_candidate', return_value=_VerificationResult(True, 'reviewed')),
          patch.object(runner, '_run_active_group_verification', return_value=ok) as expanded,
          patch.object(runner, '_candidate_boundary_checks', return_value=(ok, ok))):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=1,
                                      deadline=None, prior_failures=[], seen_fingerprints=set())
    assert result.status == 'candidate_group_completed'
    assert result.candidate_commit == state.base_commit and result.diff_line_count == 0
    quick.assert_called_once()
    expanded.assert_called_once()


def test_scope_reclassification_never_earns_a_repair_resolution_credit(setup):
    from auto_agents.repair_progress import achievements
    from auto_agents.self_repair_search import SelfRepairCandidateRecord
    runner, state, plan, calls = setup
    finding = SelfRepairFinding('unrelated', disposition='contract_violation',
        causal_obligation_id=state.contract_obligation_ids[0], required_test=plan['quick_checks'][0])
    state.findings[finding.finding_id] = finding
    state.scope_decisions[finding.finding_id] = dict(policy=POLICY_VERSION, contract=state.contract_fingerprint,
        engine_base=state.base_commit, finding_key=finding_key(finding), verdict='follow_up', request_id='reviewed')
    record = SelfRepairCandidateRecord('candidate', review_completed=True,
        resolved_finding_ids=[finding.finding_id], verified_check_ids=['tests/test_contract.py::test_contract'])
    assert not any(key.startswith('finding:') for key in achievements(state, record))


def test_source_mutation_invalidates_quick_success_before_review(setup):
    runner, state, plan, calls = setup
    runner._candidate_group.update(plan, planning_receipt='independent')
    runner._candidate_verified_check_ids = {'previously-proven'}
    def mutate(*args, **kwargs):
        (runner.repo_root / 'source.py').write_text('changed during test\n')
        runner._candidate_verified_check_ids.add('tests/test_contract.py::test_contract')
        return _VerificationResult(True, 'runner reported pass')
    with patch.object(runner, '_run_verification_commands', side_effect=mutate), \
         patch.object(runner, '_review_candidate', side_effect=AssertionError('review ran on invalid proof')):
        quick, review = runner._early_candidate_checks(runner.repo_root, state.base_commit)
    assert not quick.ok and quick.payload['outcome'] == 'invalid'
    assert not review.ok
    assert runner._candidate_verified_check_ids == {'previously-proven'}


@pytest.mark.parametrize('malformed', ['null_command', 'object_id', 'missing_requirement', 'invalid_probe'])
def test_malformed_plan_fields_are_rejected_without_running_commands(setup, malformed):
    runner, state, plan, calls = setup
    plan = deepcopy(plan)
    if malformed == 'null_command':
        plan['quick_checks'] = [None]
    elif malformed == 'object_id':
        plan['scenarios'][0]['scenario_id'] = {}
    elif malformed == 'missing_requirement':
        plan['scenarios'][0].pop('obligation_ids')
    else:
        plan['probes'][0]['command'] = None
    with pytest.raises(PlanningBlocked):
        validate_plan(plan, runner._candidate_group, set(state.contract_obligation_ids))


def test_large_planning_input_has_complete_artifact_and_explicit_unread_index(setup):
    from auto_agents.repair_planning import _invoke
    runner, state, plan, calls = setup
    context = {'source': 'retained', 'workspace': str(runner.repo_root), 'history': 'evidence' * 10000}
    requests = []
    def provider(request):
        requests.append(request)
        assert json.loads((request.output_path.parent / 'input.json').read_text()) == context
        return AgentResult(True, [], request.output_path, summary='{"decision":"REVISE"}')
    runner.target_orchestrator._call_with_failover = provider
    _invoke(runner, runner.repo_root, 'self_repair_plan_review', 'review', context)
    assert len(requests[0].prompt) < 32000
    assert 'unread_sections' in requests[0].prompt and 'complete_input' in requests[0].prompt
