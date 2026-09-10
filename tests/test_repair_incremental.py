"""Public controller regressions for retained plans, bounded repair and proof reuse."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_control import atomic_json
from auto_agents.repair_memory import (
    compact_context, component_key, dependencies_match, dependency_manifest,
    latest_revision, read_record, remember_review,
)
from auto_agents.repair_planning import (
    PlanFormatError, PlanningBlocked, _materialize_draft, prepare_component, review_scope, validate_plan,
)
from auto_agents.repair_schedule import quick_verification_plan, verification_plan
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_planning import setup
from test_repair_routing import git


def answer(request, value):
    return AgentResult(True, [], request.output_path, summary=json.dumps(value))


def test_nine_commands_remain_atomic_and_do_not_consume_revisions(setup):
    runner, state, plan, calls = setup
    plan['quick_checks'] += [f'python -m pytest -q tests/test_contract.py::test_case{i}' for i in range(8)]
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    assert receipt['decision'] == 'APPROVE'
    assert len(receipt['plan']['quick_checks']) == 9
    assert [request.stage for request, _ in calls] == ['self_repair_component_plan', 'self_repair_plan_review']
    quick = quick_verification_plan(state, runner._candidate_group)
    expanded = verification_plan(state, runner._candidate_group)
    assert quick['commands'] == [plan['quick_checks'][0]]
    assert set(plan['quick_checks']).issubset(expanded['commands'])
    assert not state.attempt_count and not state.candidates.keys() - {'base'}


def test_format_error_reports_field_and_repairs_original_before_independent_review(setup):
    runner, state, plan, calls = setup
    provider = runner.target_orchestrator._call_with_failover
    stages = []
    def respond(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_component_plan':
            return answer(request, {**plan, 'quick_checks': [None]})
        if request.stage == 'self_repair_plan_format':
            incoming = json.loads((request.output_path.parent / 'input.json').read_text())
            assert incoming['previous_revision']['draft']['quick_checks'] == [None]
            assert incoming['feedback'][0]['retry_kind'] == 'format'
            return answer(request, plan)
        return provider(request)
    runner.target_orchestrator._call_with_failover = respond
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
        assert prepare_component(runner, runner.repo_root) == receipt
    assert stages == ['self_repair_component_plan', 'self_repair_plan_format', 'self_repair_plan_review']
    assert max(state.planning_attempts.values()) == 1
    assert latest_revision(runner, runner._candidate_group)['review']['decision'] == 'APPROVE'
    bad = {**plan, 'quick_checks': ['python -m pytest tests/test_contract.py']}
    with pytest.raises(PlanFormatError) as error:
        validate_plan(bad, runner._candidate_group, set(state.contract_obligation_ids))
    assert error.value.detail['field'] == 'quick_checks[0]'
    assert '::node' in error.value.detail['constraint']


def test_format_exhaustion_and_restart_create_no_extra_calls_or_candidates(setup):
    runner, state, plan, calls = setup
    stages = []
    def respond(request):
        stages.append(request.stage)
        return answer(request, {**plan, 'quick_checks': [None]})
    runner.target_orchestrator._call_with_failover = respond
    with pytest.raises(PlanningBlocked, match='format corrections exhausted'):
        prepare_component(runner, runner.repo_root)
    assert stages == ['self_repair_component_plan'] + ['self_repair_plan_format'] * 2
    runner._experiment = runner._experiment_store.load()
    with pytest.raises(PlanningBlocked, match='episode exhausted'):
        prepare_component(runner, runner.repo_root)
    assert len(stages) == 3 and not state.attempt_count and not state.progress_credits
    assert state.candidates.keys() == {'base'}


def test_interrupted_format_correction_retains_its_round_and_budget(setup):
    runner, state, plan, calls = setup
    provider = runner.target_orchestrator._call_with_failover
    stages = []
    def interrupted(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_plan_format':
            raise KeyboardInterrupt
        return answer(request, {**plan, 'quick_checks': [None]})
    runner.target_orchestrator._call_with_failover = interrupted
    with pytest.raises(KeyboardInterrupt):
        prepare_component(runner, runner.repo_root)
    runner._experiment = runner._experiment_store.load()
    def resumed(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_plan_format':
            return answer(request, plan)
        return provider(request)
    runner.target_orchestrator._call_with_failover = resumed
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        prepare_component(runner, runner.repo_root)
    assert stages == ['self_repair_component_plan', 'self_repair_plan_format',
                      'self_repair_plan_format', 'self_repair_plan_review']
    assert max(runner._experiment.planning_attempts.values()) == 1


def test_revision_keeps_previous_plan_and_delta_instead_of_rebuilding_context(setup):
    runner, state, plan, calls = setup
    initial = deepcopy(plan)
    provider = runner.target_orchestrator._call_with_failover
    drafts = []
    reviews = []
    def respond(request):
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        if request.stage == 'self_repair_component_plan':
            drafts.append(context)
            if len(drafts) == 2:
                previous = context['previous_revision']
                assert previous['draft'] == initial
                assert previous['review']['decision'] == 'REVISE'
                return answer(request, {'amendment': {'parent_revision': previous['id'],
                    'replace_steps': {previous['step_ids'][0]: 'preserve custody through actual resume'}}})
        if request.stage == 'self_repair_plan_review':
            reviews.append(context)
            if len(reviews) == 1:
                return answer(request, {'decision': 'REVISE', 'reason': 'missing concrete recovery',
                    'issues': ['decide recovery transition'], 'scenario_ids': [s['scenario_id'] for s in plan['scenarios']]})
        return provider(request)
    runner.target_orchestrator._call_with_failover = respond
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    assert receipt['plan']['scenarios'] == initial['scenarios']
    assert receipt['plan']['implementation_steps'] != initial['implementation_steps']
    assert len(drafts) == len(reviews) == 2
    latest = latest_revision(runner, runner._candidate_group)
    assert latest['parent_revision'] and len(state.plan_revisions) == 4
    assert latest['step_ids'] == drafts[1]['previous_revision']['step_ids']
    with pytest.raises(PlanningBlocked, match='stale'):
        _materialize_draft({'amendment': {'parent_revision': 'wrong', 'set': {'mode': 'verify_existing'}}}, latest)
    with pytest.raises(PlanningBlocked, match='deletes'):
        _materialize_draft({'amendment': {'parent_revision': latest['id'], 'set': {'scenarios': []}}}, latest)


def test_covered_implementation_regression_reuses_approved_plan(setup):
    runner, state, plan, calls = setup
    plan['touched_paths'].append('source.py')
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        first = prepare_component(runner, runner.repo_root)
        group = deepcopy(state.finding_groups[0])
        finding = SelfRepairFinding('implementation-error', status='confirmed', disposition='candidate_regression',
            causal_obligation_id=group['contract_obligation_ids'][0],
            affected_paths=['source.py'], required_test=plan['quick_checks'][0],
            reason='implementation violated the approved recovery scenario',
            counterexample='resume loses owned context', evidence=['source.py:1'])
        state.findings[finding.finding_id] = finding
        group['finding_ids'] = [finding.finding_id]
        state.finding_groups[0] = group
        runner._candidate_group = deepcopy(group)
        remember_review(runner, runner.repo_root, group, {'decision': 'REJECT',
            'findings': [{**finding.to_dict(), 'repair_kind': 'implementation', 'scenario_ids': ['failure']}]})
        (runner.repo_root / 'source.py').write_text('value = 2\n')
        with patch('auto_agents.repair_planning.review_scope'):
            second = prepare_component(runner, runner.repo_root)
        assert second['request_id'] == first['request_id']
        assert len(calls) == 2
        assert runner._candidate_group['finding_scenario_bindings'] == {finding.finding_id: ['failure']}


def test_scope_dependency_manifest_invalidates_imports_configuration_and_unknown_inputs(tmp_path):
    from test_repair_routing import git
    git(tmp_path, 'init', '-q')
    (tmp_path / 'owned.py').write_text('from helper import VALUE\nVALUE = VALUE + 1\n')
    (tmp_path / 'helper.py').write_text('VALUE = 3\n')
    (tmp_path / 'unrelated.txt').write_text('first\n')
    manifest = dependency_manifest(tmp_path, ['owned.py'])
    assert manifest['complete']
    (tmp_path / 'unrelated.txt').write_text('second\n')
    assert dependencies_match(tmp_path, manifest)
    (tmp_path / 'helper.py').write_text('VALUE = 4\n')
    assert not dependencies_match(tmp_path, manifest)
    manifest = dependency_manifest(tmp_path, ['owned.py'])
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    assert not dependencies_match(tmp_path, manifest)
    (tmp_path / 'owned.py').write_text('VALUE = open("/some/runtime/input").read()\n')
    opaque = dependency_manifest(tmp_path, ['owned.py'])
    assert not opaque['complete'] and not dependencies_match(tmp_path, opaque)


def test_compact_context_preserves_complete_background_without_requiring_full_history(setup):
    runner, state, plan, calls = setup
    history = {'open_contract_findings': [{'finding_id': 'other', 'evidence': ['old data' * 10000]}],
               'sticky_verification_commands': ['python -m pytest -q tests/all.py'], 'secret_history': 'kept'}
    context = compact_context(runner, {'history': history, 'component': runner._candidate_group})
    assert len(json.dumps(context)) < len(json.dumps(history)) / 4
    assert json.loads(Path(context['background_ref']).read_text())['history'] == history
    assert 'other' not in json.dumps(context['history'])


def test_migration_recovers_unreviewed_legacy_draft_without_resetting_history(setup):
    runner, state, plan, calls = setup
    from auto_agents.repair_planning import _context
    context = _context(runner, runner.repo_root)
    context.update(revision=3, findings=[], feedback=[{'reason': 'local fix'}])
    identity = 'a' * 32
    root = runner._experiment_store.root / 'planning' / identity
    atomic_json(root / 'request.json', {'request_id': identity, 'stage': 'self_repair_component_plan', 'policy': 1})
    atomic_json(root / 'input.json', context)
    atomic_json(root / 'result.json', plan)
    state.attempt_count = 66
    state.progress_credits = {'check:historical': 'c63'}
    state.planning_attempts['legacy'] = 3
    state.component_memory = {}
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    assert receipt['decision'] == 'APPROVE'
    assert [r.stage for r, _ in calls] == ['self_repair_plan_review']
    assert state.attempt_count == 66 and state.progress_credits == {'check:historical': 'c63'}
    assert state.planning_attempts['legacy'] == 3
    assert next(iter(state.repair_episodes.values()))['semantic_attempts'] == 3


def test_schema_seven_records_and_original_six_backup_survive_store_reload(setup):
    runner, state, plan, calls = setup
    raw = state.to_dict()
    raw['schema_version'] = 6
    atomic_json(runner._experiment_store.path, raw)
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        prepare_component(runner, runner.repo_root)
    assert json.loads(runner._experiment_store.path.with_name('experiment.v6.json').read_text()) == raw
    restored = runner._experiment_store.load()
    assert restored.to_dict()['schema_version'] == 7
    assert restored.plan_revisions == state.plan_revisions and restored.repair_episodes == state.repair_episodes
    assert all(read_record(runner, ref) for ref in restored.plan_revisions.values())


def test_actual_search_does_not_register_a_planning_failure_as_a_candidate(setup):
    runner, state, plan, calls = setup
    with (patch.object(runner, '_load_or_create_experiment', return_value=(runner._experiment_store, state)),
          patch.object(runner, '_ensure_approved_repair_design', return_value=True),
          patch.object(runner, '_migrate_recoverable_candidate_to_pending'),
          patch.object(runner, '_resume_pending_validation_candidate', return_value=None),
          patch.object(runner, '_prepare_component_plan', side_effect=PlanningBlocked('format exhausted', code='format_exhausted')),
          patch.object(runner, '_register_search_result', side_effect=AssertionError('empty candidate registered')),
          patch.object(runner, '_automatic_contract_reanalysis', side_effect=AssertionError('unnecessary global replan'))):
        result = runner._run_search()
    assert result.status == 'planning_blocked' and not result.candidate_id
    assert state.attempt_count == 0 and state.candidates.keys() == {'base'}
    assert result.next_action['planning_failure']['code'] == 'format_exhausted'


def test_interrupted_format_calls_cannot_reset_the_two_correction_limit(setup):
    runner, state, plan, calls = setup
    stages = []
    def respond(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_plan_format':
            raise KeyboardInterrupt
        return answer(request, {**plan, 'quick_checks': [None]})
    runner.target_orchestrator._call_with_failover = respond
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            prepare_component(runner, runner.repo_root)
        runner._experiment = runner._experiment_store.load()
    with pytest.raises(PlanningBlocked, match='format corrections exhausted'):
        prepare_component(runner, runner.repo_root)
    assert stages == ['self_repair_component_plan'] + ['self_repair_plan_format'] * 2
    assert next(iter(runner._experiment.repair_episodes.values()))['format_corrections'] == 2


def test_specific_quick_parameter_never_replaces_complete_acceptance(setup):
    runner, state, plan, calls = setup
    base = 'python -m pytest -q tests/test_contract.py::test_contract'
    for row in plan['scenarios']:
        row['quick_check'] = base + '[retained]'
    accepted = validate_plan(plan, runner._candidate_group, set(state.contract_obligation_ids))
    active = {**runner._candidate_group, **accepted}
    quick = quick_verification_plan(state, active)
    import shlex
    assert [shlex.split(command) for command in quick['commands']] == [shlex.split(base + '[retained]')]
    assert base in verification_plan(state, active)['commands']
    plan['scenarios'][0]['quick_check'] = 'python -m pytest -q tests/unrelated.py::test_other'
    with pytest.raises(PlanningBlocked, match='not a selection'):
        validate_plan(plan, runner._candidate_group, set(state.contract_obligation_ids))


def test_scope_reuses_only_complete_dependencies_and_keeps_original_verdict(setup):
    runner, state, plan, calls = setup
    finding = SelfRepairFinding('static', status='confirmed', disposition='contract_violation',
        causal_obligation_id=state.contract_obligation_ids[0], affected_paths=['source.py'],
        reason='old counterexample', counterexample='prior source lacks constant', evidence=['source.py:1'])
    requests = []
    def scope(request):
        requests.append(request)
        return answer(request, {'decisions': [{'finding_id': 'static', 'verdict': 'not_applicable',
            'obligation_id': finding.causal_obligation_id, 'reason': 'current source has the constant',
            'evidence': ['source.py:1'], 'disproof': 'constant is present'}]})
    runner.target_orchestrator._call_with_failover = scope
    review_scope(runner, runner.repo_root, [finding])
    (runner.repo_root / 'notes.md').write_text('unrelated\n')
    review_scope(runner, runner.repo_root, [finding])
    assert len(requests) == 1
    (runner.repo_root / 'source.py').write_text('value = 4\n')
    review_scope(runner, runner.repo_root, [finding])
    assert len(requests) == 2 and not state.progress_credits


def test_corrupt_episode_counter_cannot_grant_more_attempts(setup):
    from auto_agents.self_repair_search import SelfRepairExperiment
    runner, state, plan, calls = setup
    value = state.to_dict()
    value['repair_episodes'] = {'episode': {'semantic_attempts': -1}}
    with pytest.raises(ValueError, match='nonnegative'):
        SelfRepairExperiment.from_dict(value)


def test_count_exhaustion_without_evidence_does_not_request_global_redesign(setup):
    from auto_agents.repair_actions import stalled_correction
    from auto_agents.self_repair import SelfRepairResult
    runner, state, plan, calls = setup
    result = stalled_correction(runner, state, SelfRepairResult(False, 'candidate_review_rejected', 'no evidence'))
    assert result['kind'] == 'blocked' and not calls


def test_plan_reference_errors_are_corrected_without_changing_scenarios(setup):
    runner, state, plan, calls = setup
    stages = []
    original = runner.target_orchestrator._call_with_failover
    invalid = deepcopy(plan)
    invalid['scenarios'][0]['finding_ids'] = ['old-resolved-finding', 'planning-review-note']
    def respond(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_component_plan':
            return answer(request, invalid)
        if request.stage == 'self_repair_plan_format':
            incoming = json.loads((request.output_path.parent / 'input.json').read_text())
            assert incoming['feedback'][0]['field'] == 'scenarios.finding_ids'
            fixed = deepcopy(plan)
            fixed['scenarios'][0]['reference_ids'] = invalid['scenarios'][0]['finding_ids']
            return answer(request, fixed)
        return original(request)
    runner.target_orchestrator._call_with_failover = respond
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    assert receipt['plan']['scenarios'][0]['check'] == plan['scenarios'][0]['check']
    assert receipt['decision'] == 'APPROVE'
    assert stages == ['self_repair_component_plan', 'self_repair_plan_format', 'self_repair_plan_review']


def test_completed_draft_output_survives_cancel_before_provider_returns(setup):
    runner, state, plan, calls = setup
    provider = runner.target_orchestrator._call_with_failover
    def interrupted(request):
        request.output_path.write_text(json.dumps(plan))
        raise KeyboardInterrupt
    runner.target_orchestrator._call_with_failover = interrupted
    with pytest.raises(KeyboardInterrupt):
        prepare_component(runner, runner.repo_root)
    runner._experiment = runner._experiment_store.load()
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    assert receipt['decision'] == 'APPROVE'
    assert [request.stage for request, _ in calls] == ['self_repair_plan_review']
    assert not state.progress_credits and not state.attempt_count


def test_quick_command_parser_rejects_shell_chaining_but_keeps_quoted_parameters():
    from auto_agents.repair_schedule import pytest_parts
    assert pytest_parts('python -m pytest tests/a.py::test_a;pwd') is None
    assert pytest_parts("python -m pytest 'tests/a.py::test_a[x;y]'")[1] == ['tests/a.py::test_a[x;y]']


def test_renaming_component_does_not_reopen_an_exhausted_episode(setup):
    runner, state, plan, calls = setup
    runner.target_orchestrator._call_with_failover = lambda request: answer(request, {**plan, 'quick_checks': [None]})
    with pytest.raises(PlanningBlocked):
        prepare_component(runner, runner.repo_root)
    state.finding_groups[0]['group_id'] = 'same-work-new-label'
    runner._candidate_group = deepcopy(state.finding_groups[0])
    with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('budget reset')):
        with pytest.raises(PlanningBlocked, match='episode exhausted'):
            prepare_component(runner, runner.repo_root)
    assert len(state.repair_episodes) == 1


def test_local_diagnosis_requires_evidence_and_invalidates_when_source_changes(setup):
    from contextlib import nullcontext
    from auto_agents.repair_actions import stalled_correction
    from auto_agents.self_repair import SelfRepairResult
    from auto_agents.self_repair_search import SelfRepairCandidateRecord
    runner, state, plan, calls = setup
    finding = SelfRepairFinding('failure', status='confirmed', disposition='contract_violation',
        causal_obligation_id=state.contract_obligation_ids[0], required_test=plan['quick_checks'][0],
        reason='a concrete failed check', counterexample='resume loses ownership', evidence=['source.py:1'])
    state.findings[finding.finding_id] = finding
    runner._candidate_group['finding_ids'] = [finding.finding_id]
    state.candidates['bad'] = SelfRepairCandidateRecord('bad', candidate_commit=state.base_commit)
    candidate = SelfRepairResult(False, 'candidate_review_rejected', 'failed', candidate_id='bad', candidate_commit=state.base_commit)
    responses = []
    def respond(request):
        responses.append(request)
        return answer(request, {'kind': 'global_redesign' if len(responses) == 1 else 'local_repair',
            'reason': 'correct the already identified failure', 'evidence_ids': ['failure']})
    runner.target_orchestrator._call_with_failover = respond
    with patch('auto_agents.repair_actions.diagnosis_workspace', side_effect=lambda *args: nullcontext(runner.repo_root)):
        assert stalled_correction(runner, state, candidate)['kind'] == 'blocked'
        assert stalled_correction(runner, state, candidate)['kind'] == 'local_repair'
        assert stalled_correction(runner, state, candidate)['kind'] == 'local_repair'
        assert len(responses) == 2
        (runner.repo_root / 'source.py').write_text('value = 9\n')
        assert stalled_correction(runner, state, candidate)['kind'] == 'local_repair'
        assert len(responses) == 3
