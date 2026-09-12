"""A non-ready writer output cannot be mistaken for a finished repair."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_candidate_admission import admission_blocker, declared_not_ready
from auto_agents.repair_capability_checks import namespace_observation, production_capabilities
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairFinding
from auto_agents.repair_actions import stalled_correction, prepare_action
from auto_agents.repair_planning import PlanningBlocked, prepare_component
from test_repair_planning import setup


def declaration(group='custody'):
    return 'Stage declaration:\n```json\n' + json.dumps({'component': group, 'candidate_ready': False,
        'status': 'capability_blocked', 'capability': 'nested_user_mount_namespace',
        'reason': 'nested namespace unavailable', 'retry_fix': False}) + '\n```'


@pytest.mark.parametrize('text', ['capability_blocked', '{"candidate_ready":false}',
                                declaration('foreign'), '{"component":"custody","candidate_ready":true}'])
def test_only_explicit_current_component_handoff_is_recognized(text):
    assert declared_not_ready(text, 'custody') is None


def test_unavailable_plan_preserves_precise_blocker_and_does_not_run_format_retries(setup):
    runner, state, _, _ = setup
    runner._real_project_root = runner.target_project_root
    calls = []
    def provider(request):
        calls.append(request.stage)
        return AgentResult(True, [], request.output_path, summary=json.dumps({
            'decision': 'BLOCKED', 'reason': 'production namespace unavailable: EPERM',
            'capability': 'nested_user_mount_namespace'}))
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_capability_checks.production_capabilities', return_value={'supported': False}):
        for _ in range(2):
            with pytest.raises(PlanningBlocked, match='EPERM') as error:
                prepare_component(runner, runner.repo_root)
            assert error.value.detail['code'] == 'capability_unavailable'
            runner._experiment = runner._experiment_store.load()
    assert calls == ['self_repair_component_plan'] and not state.progress_credits


@pytest.mark.parametrize('supported', [False, True, None])
def test_provider_claim_is_checked_and_never_becomes_acceptance(setup, supported):
    runner, state, _, _ = setup
    runner._candidate_id = 'not-ready'
    observation = {'supported': supported, 'status': 'observed', 'reason': 'namespace unavailable', 'acceptance_proof': False}
    with patch('auto_agents.repair_capability_checks.namespace_observation', return_value=observation) as probe:
        blocker = admission_blocker(runner, runner.repo_root, declaration(runner._candidate_group['group_id']))
    assert blocker['capability_verified'] == (supported is False)
    assert blocker['candidate_ready'] is False and blocker['kind'] == 'blocked'
    assert not state.progress_credits and not state.completed_contract_obligation_ids
    probe.assert_called_once()
    evidence = runner._candidate_failure_evidence[-1]
    assert evidence['next_action'] == 'blocked'
    receipt = json.loads(Path(evidence['artifacts']['result']).read_text())
    assert receipt['observation']['acceptance_proof'] is False


def test_unrelated_capability_claim_is_not_confirmed_by_a_namespace_failure(setup):
    runner, _, _, _ = setup
    runner._candidate_id = 'not-ready'
    text = declaration(runner._candidate_group['group_id']).replace('nested_user_mount_namespace', 'missing_database')
    with patch('auto_agents.repair_capability_checks.namespace_observation', side_effect=AssertionError('wrong capability')):
        assert admission_blocker(runner, runner.repo_root, text)['capability_verified'] is False


@pytest.mark.parametrize('target_changed', [False, True])
def test_not_ready_writer_stops_before_prechecks_review_and_acceptance(setup, target_changed):
    runner, state, plan, _ = setup
    def prepare(root):
        runner._candidate_group.update(plan, planning_receipt='approved')
    def provider(request):
        if target_changed:
            (runner.target_project_root / 'source.py').write_text('unexpected write')
        return AgentResult(True, [], request.output_path, summary=declaration(runner._candidate_group['group_id']))
    runner.target_orchestrator._call_with_failover = provider
    with patch.object(runner, '_prepare_component_plan', side_effect=prepare), \
         patch.object(runner, '_build_prompt', return_value='repair'), \
         patch.object(runner, '_verification_environment_blocker', return_value=None), \
         patch('auto_agents.repair_capability_checks.namespace_observation', return_value={
             'supported': False, 'reason': 'EPERM', 'acceptance_proof': False}), \
         patch.object(runner, '_candidate_deterministic_issues', side_effect=AssertionError('preflight ran')), \
         patch.object(runner, '_early_candidate_checks', side_effect=AssertionError('review/acceptance ran')):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=1,
                                      deadline=None, prior_failures=[], seen_fingerprints=set())
    if target_changed:
        assert result.status == 'candidate_rejected' and result.fatal_candidate
        assert 'live target changed' in result.reason
        return
    assert result.status == 'candidate_not_ready' and result.recoverable_validation
    assert result.next_action['capability_verified'] is True and 'EPERM' in result.reason
    runner._register_search_result(result)
    assert result.next_action['capability_verified'] is True
    assert not result.review_completed and not state.progress_credits
    assert all(g['status'] == 'pending' for g in state.finding_groups)
    with patch('auto_agents.repair_capability_checks.namespace_observation', return_value={'supported': False}):
        action = prepare_action(runner, state)
    assert action['kind'] == 'blocked' and 'EPERM' in action['cause']
    with patch('auto_agents.repair_capability_checks.namespace_observation', return_value={'supported': True}):
        assert prepare_action(runner, state)['kind'] == 'repair_code'


@pytest.mark.parametrize('legacy', [False, True])
def test_confirmed_current_review_is_visible_to_stall_recovery(setup, legacy):
    runner, state, _, _ = setup
    group = state.finding_groups[0]
    fid = 'private-metadata-denied'
    finding = SelfRepairFinding(fid, status='confirmed', disposition='contract_violation',
        causal_obligation_id=group['contract_obligation_ids'][0], reason='private chmod denied',
        counterexample='real writer gets EPERM', required_test='tests/test_contract.py::test_contract',
        evidence=['verified independent review'], repair_group_id='untrusted-foreign-owner')
    record = SelfRepairCandidateRecord('rejected', finding_group_id=group['group_id'],
        candidate_commit=state.base_commit, review_completed=True)
    state.register_candidate(record, findings=[finding])
    assert state.findings[fid].repair_group_id == group['group_id']
    assert fid in group['finding_ids']
    if legacy:
        group['finding_ids'].remove(fid)
        state.findings[fid].repair_group_id = ''
    runner._candidate_group = deepcopy(group)
    result = SimpleNamespace(candidate_id='rejected', candidate_commit=state.base_commit,
        patch_fingerprint='', review_findings=[finding.to_dict()], failure_evidence=[],
        finding_ids=[fid], finding_group_id=group['group_id'])
    calls = []
    def provider(request):
        calls.append(request)
        assert fid in request.prompt
        return AgentResult(True, [], request.output_path, summary=json.dumps({
            'kind': 'local_repair', 'reason': 'repair the confirmed private metadata incompatibility',
            'evidence_ids': [fid]}))
    runner.target_orchestrator._call_with_failover = provider
    action = stalled_correction(runner, state, result)
    assert action['kind'] == 'local_repair' and len(calls) == 1
    assert stalled_correction(runner, state, result) == action and len(calls) == 1
    assert not state.progress_credits


def test_namespace_probe_uses_production_wrapper_and_distinguishes_unknown(setup):
    runner, _, _, _ = setup
    observed = []
    @contextmanager
    def wrapper(argv, cwd):
        observed.append(argv)
        yield ['actual-wrapper', *argv]
    runner._verification_argv = wrapper
    process = SimpleNamespace(returncode=1, stderr='unshare: EPERM', stdout='',
                              termination_reason='', cleanup_incomplete=False)
    with patch('auto_agents.repair_capability_checks.shutil.which', return_value='/usr/bin/unshare'), \
         patch('auto_agents.process_supervision.run_supervised_shell_command', return_value=process) as execute:
        result = namespace_observation(runner, runner.repo_root)
        assert result['supported'] is False and 'EPERM' in result['reason']
        assert execute.call_args.args[0].startswith('exec actual-wrapper ')
        assert '--mount' in observed[0] and '--map-root-user' in observed[0]
        process.termination_reason = 'timeout'
        assert namespace_observation(runner, runner.repo_root)['supported'] is None


def test_capability_observation_is_cached_only_for_same_source_and_environment(setup):
    runner, _, _, _ = setup
    with patch('auto_agents.repair_capability_checks.namespace_observation', return_value={'supported': False}) as probe, \
         patch('auto_agents.repair_capability_checks.metadata_observation', return_value={'supported': False}):
        production_capabilities(runner, runner.repo_root)
        production_capabilities(runner, runner.repo_root)
        assert probe.call_count == 1
        (runner.repo_root / 'source.py').write_text('changed = True\n')
        production_capabilities(runner, runner.repo_root)
        assert probe.call_count == 2


def test_candidate_entry_probe_cannot_mutate_retained_source(setup):
    from auto_agents.repair_capability_checks import metadata_observation
    runner, _, _, _ = setup
    original = (runner.repo_root / 'source.py').read_bytes()
    def mutate(_runner, snapshot):
        assert snapshot != runner.repo_root
        (snapshot / 'source.py').write_text('mutated by candidate launcher')
        return {'supported': True, 'acceptance_proof': False}
    with patch('auto_agents.repair_capability_checks._metadata_observation_in_snapshot', side_effect=mutate):
        result = metadata_observation(runner, runner.repo_root)
    assert result['supported'] is None and result['status'] == 'inconclusive'
    assert (runner.repo_root / 'source.py').read_bytes() == original


@pytest.mark.skipif(not sys.platform.startswith('linux') or not shutil.which('codex'), reason='requires Linux verification wrapper')
def test_real_production_probe_exposes_private_metadata_denial(tmp_path):
    from auto_agents.managed_verification import engine_runner
    from auto_agents.repair_capability_checks import metadata_observation
    from auto_agents.verification_sandbox import landlock_abi
    from test_repair_routing import git
    if landlock_abi() < 3:
        pytest.skip('Landlock unavailable')
    root, target = tmp_path / 'source', tmp_path / 'target'
    root.mkdir(); target.mkdir()
    (root / 'retained').write_text('original')
    # A production repair checkout contains the engine package. Include that
    # source explicitly, rather than depending on this interpreter's pip install.
    (root / 'src').symlink_to(Path(__file__).resolve().parents[1] / 'src', target_is_directory=True)
    git(root, 'init', '-q'); git(root, 'add', '.'); git(root, 'commit', '-qm', 'retained source')
    runner = engine_runner(root, target, sys.executable, repository=root)
    observed = metadata_observation(runner, root)
    assert observed['status'] == 'observed', observed
    assert observed['checks']['shared_read'] and observed['shared_unchanged']
    assert observed['checks']['shared_chmod'] is False
    assert observed['checks']['private_chmod'] is False and observed['checks']['private_fchmod'] is False
    assert observed['supported'] is False and observed['acceptance_proof'] is False
    assert (root / 'retained').read_text() == 'original' and not list(target.iterdir())
