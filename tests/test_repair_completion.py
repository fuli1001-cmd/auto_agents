"""A completed component is reusable evidence, not a permanent boolean."""
from copy import deepcopy
from contextlib import nullcontext
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_completion import assess, refresh, seal, _memory
from auto_agents.repair_work import memory as work_memory
from auto_agents.repair_memory import component_key, read_record, remember_review, remember_check_timings, remember_revision
from auto_agents.self_repair import _VerificationResult
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_planning import setup
from test_repair_routing import git
from test_repair_restart import restart, _new_job


@pytest.fixture
def completed(setup):
    runner, state, _, _ = setup
    for name in ('a', 'b'):
        (runner.repo_root / f'tests/test_{name}.py').write_text(f'def test_{name}():\n    assert 1 == 1\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'independent component checks')
    commands = [f'python -m pytest -q tests/test_{name}.py::test_{name}' for name in ('a', 'b')]
    group = state.finding_groups[0]
    group.update(touched_paths=['tests/test_a.py', 'tests/test_b.py'], focused_tests=commands)
    runner._candidate_group = {**group, 'planning_receipt': 'approved', 'quick_checks': commands[:1]}
    runner._candidate_id = 'completed-candidate'
    runner._candidate_is_final_group = False
    remember_revision(runner, group, {'component': deepcopy(group), 'draft': {
        'implementation_steps': group['implementation_steps'], 'touched_paths': group['touched_paths'],
        'quick_checks': commands[:1], 'scenarios': []}, 'status': 'APPROVE'})
    review = _VerificationResult(True, 'approved', payload={'decision': 'APPROVE', 'findings': [], 'resolved_finding_ids': []})
    verification = _VerificationResult(True, 'passed', commands=tuple(commands), returncodes=(0, 0),
        payload={'source_commands': commands, 'executed_tests': [f'tests/test_{name}.py::test_{name}' for name in ('a', 'b')],
                 'command_timings': [{'command': c, 'seconds': 1, 'cache_hit': False} for c in commands]})
    remember_review(runner, runner.repo_root, runner._candidate_group, review.payload)
    remember_check_timings(runner, runner.repo_root, runner._candidate_group, {'commands': commands}, verification, phase='expanded')
    reference = seal(runner, runner.repo_root, review, verification)
    assert reference
    group.update(status='completed', completed_by=runner._candidate_id)
    state.planning_receipts['approved'] = {'decision': 'APPROVE'}
    runner._experiment_store.save(state)
    return runner, state, group, commands, reference


def test_restart_rechecks_receipt_and_skips_valid_completed_group_without_credit(completed):
    runner, state, group, commands, reference = completed
    group['status'] = 'needs_revalidation'
    before = deepcopy(state.progress_credits)
    with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('model invoked')):
        refresh(runner, runner.repo_root)
        assert group['status'] == 'completed'
        assert state.next_finding_group()['group_id'] == state.finding_groups[1]['group_id']
        assert assess(runner, runner.repo_root, group)['reusable_checks'] == commands
        refresh(runner, runner.repo_root)
    assert state.progress_credits == before and state.attempt_count == 0
    assert runner._experiment_store.load().finding_groups[0]['status'] == 'completed'


def test_proven_unrelated_change_does_not_reopen_component(completed):
    runner, state, group, _, reference = completed
    assert read_record(runner, reference)['dependencies']['complete']
    (runner.repo_root / 'README.md').write_text('new documentation')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'unrelated documentation')
    refresh(runner, runner.repo_root)
    assert group['status'] == 'completed'


def test_changed_check_only_reexecutes_affected_command_cohort(completed):
    runner, state, group, commands, _ = completed
    (runner.repo_root / 'tests/test_a.py').write_text('def test_a():\n    assert 2 == 2\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'changed first dependency')
    decision = assess(runner, runner.repo_root, group)
    assert decision['state'] == 'needs_revalidation'
    assert decision['affected_checks'] == commands[:1] and decision['reusable_checks'] == commands[1:]
    seen = []
    def execute(selected, root, **kwargs):
        seen.extend(selected)
        return _VerificationResult(True, 'fresh pass', commands=tuple(selected), returncodes=(0,),
            payload={'source_commands': selected, 'executed_tests': ['tests/test_a.py::test_a']})
    with patch.object(runner, '_guarded_component_checks', side_effect=execute):
        result = runner._run_active_group_verification(runner.repo_root)
    assert result.ok and seen == commands[:1]
    assert result.payload['retained_commands'] == commands[1:]
    assert set(result.payload['executed_tests']) == {'tests/test_a.py::test_a', 'tests/test_b.py::test_b'}
    for request in result.payload['requests']:
        assert result.payload['source_commands'][request['execution_index']] == request['command']


@pytest.mark.parametrize('change', ['environment', 'policy', 'contract', 'acceptance', 'plan', 'target', 'fresh', 'mode', 'revoked'])
def test_bound_inputs_invalidate_completion(completed, change):
    runner, state, group, commands, _ = completed
    manager = patch('auto_agents.repair_completion.policy', return_value='changed') if change == 'policy' else nullcontext()
    if change == 'environment':
        runner._full_suite_environment_fingerprint = lambda: ('changed',)
    elif change == 'contract':
        state.contract_fingerprint = 'different contract'
    elif change == 'acceptance':
        group['focused_tests'] = [*commands, 'python -m pytest -q tests/test_contract.py::test_contract']
    elif change == 'plan':
        work_memory(runner, group)['latest_revision'] = {'id': 'new-plan'}
    elif change == 'target':
        (runner.target_project_root / 'source.py').write_text('changed project inputs')
    elif change == 'fresh':
        runner._verification_fresh = True
    elif change == 'mode':
        (runner.repo_root / 'tests/test_a.py').chmod(0o700)
    elif change == 'revoked':
        manager = patch('auto_agents.repair_completion._ledger_state', return_value=['same namespace', 'revoked'])
    with manager:
        assert assess(runner, runner.repo_root, group)['state'] == 'needs_revalidation'


def test_unknown_dependencies_cannot_be_treated_as_independent(completed):
    runner, _, group, _, reference = completed
    proof = read_record(runner, reference)
    # Exercise the real dependency analyzer before resealing the controlled fixture.
    from auto_agents.repair_memory import dependency_manifest, save_record
    (runner.repo_root / 'tests/test_a.py').write_text('import os\ndef test_a():\n    assert os.getenv("VALUE")\n')
    proof['dependencies'] = dependency_manifest(runner.repo_root, ['tests/test_a.py'])
    assert not proof['dependencies']['complete']
    proof['source'] = 'previous exact source'
    copied = save_record(runner, 'component_completion', {k: v for k, v in proof.items() if k not in {'id', 'kind'}})
    _memory(runner, group)['completion'] = copied
    assert assess(runner, runner.repo_root, group)['state'] == 'needs_revalidation'


@pytest.mark.parametrize('failure', ['missing_review', 'tampered_completion', 'new_finding', 'reopened_finding'])
def test_invalid_or_disproved_evidence_never_keeps_completed_flag(completed, failure):
    runner, state, group, _, reference = completed
    proof = read_record(runner, reference)
    if failure == 'missing_review':
        (runner._experiment_store.root / 'planning' / proof['review']['id'] / 'memory.json').unlink()
    elif failure == 'tampered_completion':
        path = runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json'
        value = json.loads(path.read_text()); value['contract'] = 'forged'
        path.write_text(json.dumps(value))
    else:
        state.findings['counterexample'] = SelfRepairFinding('counterexample',
            status='reopened' if failure.startswith('reopened') else 'confirmed',
            disposition='contract_violation', causal_obligation_id=group['contract_obligation_ids'][0],
            repair_group_id=group['group_id'], affected_paths=['tests/test_a.py'], reason='new failing behavior')
    refresh(runner, runner.repo_root)
    assert group['status'] == ('pending' if 'finding' in failure else 'needs_revalidation')


def test_final_integration_and_legacy_missing_receipts_cannot_be_skipped(completed):
    runner, state, group, _, _ = completed
    state.finding_groups = [group]
    refresh(runner, runner.repo_root)
    assert group['status'] == 'needs_revalidation'
    assert 'final integration' in _memory(runner, group)['completion_assessment']['reason']
    _memory(runner, group).pop('completion')
    refresh(runner, runner.repo_root)
    assert group['status'] == 'needs_revalidation'


def test_display_labels_do_not_renew_or_invalidate_evidence(completed):
    runner, _, group, _, _ = completed
    group['title'] = 'clearer display title'
    group['group_id'] = 'same-component-new-label'
    assert assess(runner, runner.repo_root, group)['state'] == 'completed'


def test_real_candidate_completion_seals_evidence_and_next_loop_skips_it(setup, tmp_path, monkeypatch):
    from auto_agents.models import AgentResult
    runner, state, plan, _ = setup
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    runner._continuous_workspace = tmp_path / 'continuous'
    runner._candidate_is_final_group = False
    def prepare(root):
        runner._candidate_group.update(plan, planning_receipt='approved', mode='verify_existing')
    def provider(request):
        assert request.stage == 'self_repair_candidate_review', 'writer was invoked for completed implementation'
        return AgentResult(True, [], request.output_path, summary=json.dumps({
            'decision': 'APPROVE', 'reason': 'checked independent implementation', 'findings': [], 'resolved_finding_ids': []}))
    runner.target_orchestrator._call_with_failover = provider
    with patch.object(runner, '_prepare_component_plan', side_effect=prepare), \
         patch.object(runner, '_build_prompt', return_value='verify existing'), \
         patch.object(runner, '_verification_environment_blocker', return_value=None):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=1, deadline=None,
                                       prior_failures=[], seen_fingerprints=set())
    assert result.status == 'candidate_group_completed'
    group = state.finding_groups[0]
    assert _memory(runner, group)['completion']
    state.mark_finding_group_completed(group['group_id'], candidate_id=result.candidate_id)
    refresh(runner, runner._continuous_workspace / 'repair')
    assert group['status'] == 'completed'
    assert state.next_finding_group()['group_id'] != group['group_id']


@pytest.mark.parametrize('change', ['none', 'dependency', 'runtime', 'legacy'])
def test_cancel_import_preserves_conditional_completion_after_source_integration(restart, change):
    import sys
    from copy import copy
    from auto_agents.repair_restart import import_cancelled_repair
    from auto_agents.self_repair_search import SelfRepairExperiment, SelfRepairExperimentStore
    from test_self_repair_performance import _runner
    source = restart.source
    (source / 'tests').mkdir(exist_ok=True)
    (source / 'tests/test_done.py').write_text('def test_done():\n    assert 1 == 1\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'freeze completed component')
    state = restart.evidence.load()
    obligation = next(key for key in state.contract_obligation_ids if key.startswith('root:'))
    command = 'python -m pytest -q tests/test_done.py::test_done'
    group = {'group_id': 'done', 'status': 'completed', 'depends_on': [], 'finding_ids': [],
             'contract_obligation_ids': [obligation], 'touched_paths': ['tests/test_done.py'], 'focused_tests': [command]}
    state.finding_groups = [group, {**group, 'group_id': 'remaining', 'status': 'pending',
                                   'depends_on': ['done'], 'touched_paths': ['bug.py'], 'focused_tests': ['test -f bug.py']}]
    state.repair_design = {'contract_fingerprint': state.contract_fingerprint, 'components': deepcopy(state.finding_groups)}
    state.planning_receipts['review'] = {'decision': 'APPROVE'}
    old = _runner(source)
    old.target_project_root = restart.project
    old._experiment, old._experiment_store = state, restart.evidence
    old._candidate_group, old._candidate_id = group, 'completed'
    old._verification_python_cache = sys.executable
    old._full_suite_environment_fingerprint = lambda: ('same-runtime',)
    review = _VerificationResult(True, 'approved', payload={'decision': 'APPROVE', 'findings': [], 'resolved_finding_ids': []})
    verification = _VerificationResult(True, 'passed', commands=(command,), returncodes=(0,),
        payload={'source_commands': [command], 'executed_tests': ['tests/test_done.py::test_done']})
    remember_review(old, source, group, review.payload)
    remember_check_timings(old, source, group, {'commands': [command]}, verification, phase='expanded')
    assert seal(old, source, review, verification)
    if change == 'legacy':
        _memory(old, group).pop('completion')
    restart.evidence.save(state)
    job, working = _new_job(restart, restart.payload)
    destination = SelfRepairExperimentStore(working, 'session-session', 'root')
    fresh = SelfRepairExperiment.create(run_id='session-session', root_fingerprint='root', category='engine',
        base_commit=restart.payload['base'], expected_postconditions=['resume the retained child'])
    destination.save(fresh)
    assert import_cancelled_repair(restart.store, job, working, restart.repository)
    new = copy(old)
    new._experiment, new._experiment_store = destination.load(), destination
    root = working.parent / 'continuous/repair'
    assert new._experiment.finding_groups[0]['status'] == 'needs_revalidation'
    if change == 'dependency':
        (root / 'tests/test_done.py').write_text('def test_done():\n    assert False\n')
    if change == 'runtime':
        new._full_suite_environment_fingerprint = lambda: ('changed-runtime',)
    credits = deepcopy(new._experiment.progress_credits)
    refresh(new, root)
    assert new._experiment.finding_groups[0]['status'] == ('completed' if change == 'none' else 'needs_revalidation')
    assert new._experiment.progress_credits == credits
    assert new._experiment.next_finding_group()['group_id'] == ('remaining' if change == 'none' else 'done')


def test_another_group_with_identical_paths_cannot_borrow_completion(completed):
    runner, state, group, _, _ = completed
    peer = {**deepcopy(group), 'group_id': 'different-owner', 'status': 'needs_revalidation'}
    state.finding_groups[1] = peer
    assert assess(runner, runner.repo_root, group)['state'] == 'completed'
    assert assess(runner, runner.repo_root, peer)['state'] == 'needs_revalidation'


def test_opaque_checks_require_revalidation_in_a_new_execution_context(completed):
    from auto_agents.repair_memory import save_record
    runner, _, group, commands, reference = completed
    proof = read_record(runner, reference)
    proof['checks'][0]['dependencies']['complete'] = False
    copied = save_record(runner, 'component_completion', {k: v for k, v in proof.items() if k not in {'kind', 'id'}})
    _memory(runner, group)['completion'] = copied
    assert assess(runner, runner.repo_root, group)['state'] == 'completed'
    runner._repair_control_binding = {'job': 'new-invocation', 'generation': 1}
    result = assess(runner, runner.repo_root, group)
    assert result['state'] == 'needs_revalidation'
    assert result['affected_checks'] == commands[:1] and result['reusable_checks'] == commands[1:]


def test_observed_metadata_is_replayed_even_for_identical_source(completed):
    from auto_agents.repair_memory import save_record
    runner, _, group, commands, reference = completed
    proof = read_record(runner, reference)
    proof['checks'][0]['inputs'] = {'manifest': {'metadata': 'old-value'}, 'root': str(runner.repo_root)}
    copied = save_record(runner, 'component_completion', {k: v for k, v in proof.items() if k not in {'kind', 'id'}})
    _memory(runner, group)['completion'] = copied
    with patch.object(runner, '_revalidate_verification_manifest', return_value=False) as replay:
        result = assess(runner, runner.repo_root, group)
    assert result['state'] == 'needs_revalidation' and result['affected_checks'] == commands[:1]
    replay.assert_called_once()


def test_reopened_prerequisite_invalidates_dependent_completion(completed):
    runner, state, group, commands, _ = completed
    child = {**deepcopy(group), 'group_id': 'dependent', 'depends_on': [group['group_id']],
             'touched_paths': ['tests/test_b.py'], 'focused_tests': commands[1:]}
    state.finding_groups = [group, child, {**child, 'group_id': 'integration', 'status': 'pending', 'depends_on': ['dependent']}]
    runner._candidate_group, runner._candidate_id = child, 'dependent-candidate'
    review = _VerificationResult(True, 'approved', payload={'decision': 'APPROVE', 'findings': [], 'resolved_finding_ids': []})
    checks = _VerificationResult(True, 'passed', returncodes=(0,), payload={
        'source_commands': commands[1:], 'executed_tests': ['tests/test_b.py::test_b']})
    remember_review(runner, runner.repo_root, child, review.payload)
    remember_check_timings(runner, runner.repo_root, child, {'commands': commands[1:]}, checks, phase='expanded')
    assert seal(runner, runner.repo_root, review, checks)
    state.findings['upstream'] = SelfRepairFinding('upstream', status='confirmed', disposition='contract_violation',
        repair_group_id=group['group_id'], causal_obligation_id=group['contract_obligation_ids'][0], reason='upstream violation')
    refresh(runner, runner.repo_root)
    assert group['status'] == 'pending' and child['status'] == 'needs_revalidation'
    assert 'prerequisite' in _memory(runner, child)['completion_assessment']['reason']


def test_unportable_fresh_success_does_not_loop_on_an_obsolete_receipt(completed):
    runner, _, group, _, _ = completed
    assert seal(runner, runner.repo_root, _VerificationResult(True, 'legacy review'),
                _VerificationResult(True, 'fresh checks')) is None
    assert not _memory(runner, group).get('completion')
    refresh(runner, runner.repo_root)
    assert group['status'] == 'completed'
    (runner.repo_root / 'tests/test_a.py').write_text('def test_a():\n    assert False\n')
    refresh(runner, runner.repo_root)
    assert group['status'] == 'needs_revalidation'


def test_partial_reuse_keeps_shell_preparation_barriers(completed):
    from auto_agents.repair_completion import retained_checks
    runner, _, group, commands, _ = completed
    assert retained_checks(runner, runner.repo_root, group, ['python scripts/prepare.py', *commands]) == []


def test_revalidated_unchanged_plan_keeps_independent_quick_check(completed):
    from auto_agents.repair_completion import execute_checks
    runner, state, group, commands, _ = completed
    previous = read_record(runner, work_memory(runner, group)['latest_revision'])
    remember_revision(runner, group, {'component': deepcopy(group), 'parent_revision': previous['id'],
        'draft': {**previous['draft'], 'mode': 'verify_existing'}, 'status': 'APPROVE'})
    (runner.repo_root / 'tests/test_a.py').write_text('def test_a():\n    assert 2 == 2\n')
    with patch.object(runner, '_guarded_component_checks', side_effect=AssertionError('unchanged quick check executed')):
        result = execute_checks(runner, runner.repo_root, commands[1:])
    assert result.ok and result.payload['retained_commands'] == commands[1:]
    assert result.payload['executed_tests'] == ['tests/test_b.py::test_b']
