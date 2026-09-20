"""Control lifecycle regressions: real repositories and persistent receipts."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from auto_agents.repair_v2 import integration
from auto_agents.repair_v2.comparison import signatures
from auto_agents.repair_v2.proofs import once
from auto_agents.repair_v2.recovery import classify
from auto_agents.repair_v2.runtime_artifact import build, inspect, verify
from auto_agents.repair_v2.store import Store, digest
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.repair_v2.workspace import git, source_identity
from test_repair_v2_delivery import repair_request, components, Driver, Verifier


@pytest.mark.parametrize('origin', ['clone', 'worktree'])
def test_artifact_survives_removal_of_original_repository(tmp_path, origin):
    repository = tmp_path / 'repository'
    repository.mkdir()
    git(repository, 'init', '-q')
    (repository / 'source.py').write_text('value = 1\n')
    git(repository, 'add', '.'); git(repository, 'commit', '-qm', 'source')
    source = repository
    if origin == 'worktree':
        source = tmp_path / 'linked'
        git(repository, 'worktree', 'add', '--detach', str(source))
    artifact = build(tmp_path / 'control', source, source_identity(source), {'python': 'pinned'})
    shutil.rmtree(repository)
    moved = tmp_path / 'relocated'
    Path(artifact['path']).parent.rename(moved)
    artifact['path'] = str(moved / 'source')
    verify(artifact)
    assert git(artifact['path'], 'show', 'HEAD:source.py') == 'value = 1'
    assert (moved / 'source/.git').is_dir()


@pytest.mark.parametrize('damage', ['source', 'alternates', 'gitfile', 'manifest'])
def test_artifact_tampering_cannot_be_approved(tmp_path, damage):
    source = tmp_path / 'source'; source.mkdir()
    git(source, 'init', '-q')
    (source / 'app.py').write_text('value = 1\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'source')
    artifact = build(tmp_path / 'control', source, source_identity(source), {})
    runtime = Path(artifact['path'])
    if damage == 'source': (runtime / 'app.py').write_text('value = 0\n')
    if damage == 'alternates': (runtime / '.git/objects/info/alternates').write_text('/missing\n')
    if damage == 'gitfile':
        shutil.rmtree(runtime / '.git'); (runtime / '.git').write_text('gitdir: /missing\n')
    if damage == 'manifest': (runtime.parent / 'manifest.json').write_text('{}')
    with pytest.raises(RepairBlocked): verify(artifact)


def test_stage_reuse_invalidates_only_changed_dependencies(tmp_path):
    store, calls = Store(tmp_path), []
    def execute():
        calls.append(1)
        return {'ok': True, 'value': len(calls)}
    first = once(store, 'code', {'source': 'a'}, execute)
    assert once(store, 'code', {'source': 'a'}, execute) == first
    once(store, 'artifact', {'source': 'a', 'format': 1}, execute)
    once(store, 'artifact', {'source': 'a', 'format': 2}, execute)
    assert once(store, 'code', {'source': 'a'}, execute) == first
    assert len(calls) == 3


def test_controller_upgrade_revalidates_without_implementation_or_an_automatic_loop(repair_request):
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        approved = integration.repair_entry(repair_request)
    store = Store(approved['v2_transaction']); before = store.load()
    job = {**repair_request['job'], 'result': approved}
    subscriber = {'id': 'subscriber', 'project': job['payload']['project'], 'payload': {'repair': job['payload']}}
    verifier.runtime = 'upgraded-controller'
    with patch.object(integration, 'DockerVerifier', return_value=verifier):
        result = integration.validate_subscriber({**repair_request, 'job': job, 'subscriber': subscriber})
    assert result['revalidate'] and store.load()['phase'] == 'validate'
    assert store.load()['calls'] == before['calls'] and store.load()['attempts'] == before['attempts']
    # A second environment change before original-task progress is churn,
    # not permission to keep commissioning another acceptance indefinitely.
    verifier.runtime = 'changed-again'
    with patch.object(integration, 'DockerVerifier', return_value=verifier):
        repeated = integration.validate_subscriber({**repair_request, 'job': job, 'subscriber': subscriber})
    assert not repeated.get('revalidate') and store.load()['status'] == 'blocked'
    assert integration.verify_receipt(approved)
    assert driver.calls == ['plan', 'implement', 'review']


@pytest.mark.parametrize('domain', ['artifact', 'environment', 'unknown'])
def test_delivery_failure_preserves_code_acceptance_and_usage(repair_request, domain):
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        approved = integration.repair_entry(repair_request)
    job = {**repair_request['job'], 'result': approved}
    subscriber = {'id': 'subscriber', 'project': job['payload']['project'], 'payload': {'repair': job['payload']}}
    store = Store(approved['v2_transaction']); before = store.load()
    observed = {'error': 'unclassified exception'}
    if domain == 'artifact':
        observed = {'engine_runtime': {'ok': False, 'mismatches': ['commit_unavailable', 'commit']}}
    def boundary(identity, *args):
        return {'ok': False, 'snapshot': identity, 'infrastructure': domain == 'environment', 'observed': observed}
    verifier.boundary = boundary
    with patch.object(integration, 'DockerVerifier', return_value=verifier):
        result = integration.validate_subscriber({**repair_request, 'job': job, 'subscriber': subscriber})
    assert result['failure']['domain'] == domain
    assert not result['ok'] and store.load()['status'] == 'blocked'
    assert store.load()['calls'] == before['calls'] and store.load()['attempts'] == before['attempts']
    assert integration.verify_receipt(approved)
    assert not list((store.root / 'counterexamples').glob('*/payload.json'))
    assert driver.calls == ['plan', 'implement', 'review']


def test_acceptance_requires_current_owner_child_ack_and_is_idempotent(repair_request):
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        approved = integration.repair_entry(repair_request)
    assert approved['status'] == 'accepted' and not approved['engine_full_proof']['ok']
    job = {**repair_request['job'], 'result': approved}
    subscriber = {'id': 'subscriber', 'project': job['payload']['project'], 'payload': {'repair': job['payload']}}
    store = Store(approved['v2_transaction'])
    with patch.object(integration, 'DockerVerifier', return_value=verifier):
        result = integration.validate_subscriber({**repair_request, 'job': job, 'subscriber': subscriber})
    assert result['ok'] and store.load()['phase'] == 'activation'
    with pytest.raises(RepairBlocked, match='当前恢复操作'):
        integration.acknowledge_recovery({**job, 'generation': job['generation'] + 1}, subscriber, {})
    before = store.load()['calls']
    receipt = integration.acknowledge_recovery(job, subscriber, {'session_id': 'original-child'})
    assert integration.acknowledge_recovery(job, subscriber, receipt['boundary']) == receipt
    assert store.load()['status'] == 'complete' and store.load()['calls'] == before


def test_stale_checkpoint_cannot_overwrite_newer_phase(tmp_path):
    store = Store(tmp_path)
    state = {'status': 'ready', 'phase': 'activation'}
    store.save(state)
    stale = deepcopy(state)
    store.transition(state, status='complete', phase='recovered')
    with pytest.raises(RepairBlocked, match='过期进程'):
        store.transition(stale, phase='implement')


def test_critical_recovery_failure_cannot_be_waived_as_baseline():
    node = 'tests/test_repair_runtime.py::test_current_runtime_passes_controller_owned_behavior_checks'
    report = {'checks': [{'ok': False, 'returncode': 1, 'source_unchanged': True,
        'failed': [node], 'call_failed': [node], 'collected': [node],
        'failure_details': [{'nodeid': node, 'phase': 'call', 'message': 'assert loaded == expected'}]}]}
    assert signatures(report) is None


def test_budget_migration_restores_only_proved_restart_resets(tmp_path):
    from auto_agents.repair_v2.budget_recovery import reconcile, session_path
    from auto_agents.repair_v2.store import atomic_json
    original = {'session_id': 'parent', 'workflow_id': 'workflow', 'goal': 'original goal',
        'authorization_policy': {}, 'goal_execution_environment': {'mode': 'real'},
        'current_attempt': 2, 'attempt_epoch': 10, 'attempts_since_progress': 1,
        'max_attempts': 10, 'hard_ceiling': 25, 'execution_log': []}
    reset = {**original, 'current_attempt': 0, 'attempt_epoch': 16, 'attempts_since_progress': 0,
        'execution_log': [{'action': 'attempt_epoch_started', 'result': 'failed session resumed'}]}
    path = session_path(tmp_path, 'parent'); atomic_json(path, reset)
    assert reconcile(tmp_path, {'parent': original}, {'session_id': 'parent'}) == [str(path)]
    restored = json.loads(path.read_text())
    assert (restored['current_attempt'], restored['attempts_since_progress'], restored['attempt_epoch']) == (2, 1, 16)
    assert restored['hard_ceiling'] == 25 and restored['max_attempts'] == 10
    assert reconcile(tmp_path, {'parent': original}, {'session_id': 'parent'}) == []
    assert json.loads(path.read_text()) == restored
    reset['execution_log'].append({'action': 'provider_call', 'attempt': 1})
    atomic_json(path, reset)
    with pytest.raises(RepairBlocked, match='无法解释'):
        reconcile(tmp_path, {'parent': original}, {'session_id': 'parent'})
    assert json.loads(path.read_text()) == reset


def test_packaging_migration_preserves_revocation_and_does_not_implement(repair_request):
    from types import SimpleNamespace
    from auto_agents.repair_v2.store import atomic_json
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        approved = integration.repair_entry(repair_request)
    store = Store(approved['v2_transaction']); state = store.load()
    reference = state['receipt']
    observed = {'engine_runtime': {'ok': False, 'mismatches': ['commit_unavailable', 'commit']}}
    failure = {'ok': False, 'snapshot': state['snapshot'], 'observed': observed}
    case = store.root / 'counterexamples' / digest(observed)
    atomic_json(case / 'payload.json', repair_request['job']['payload'])
    atomic_json(store.root / 'subscriber-old.json', failure)
    atomic_json(store.root / 'revocations.json', [reference['digest']])
    state.pop('runtime_artifact')
    state.update(status='active', phase='implement', failures=[{'unit': 'subscriber-boundary', 'reason': 'old transport failure'}])
    store.save(state)
    candidate = store.root / 'workspace/candidate'
    controller = SimpleNamespace(store=store, workspace=SimpleNamespace(candidate=candidate))
    before = (state['calls'], state['attempts'])
    assert integration._migrate_artifact_failure(controller)
    assert store.load()['phase'] == 'validate' and store.load()['failures'] == []
    assert (store.load()['calls'], store.load()['attempts']) == before
    assert json.loads((store.root / 'revocations.json').read_text()) == [reference['digest']]
    assert integration._excluded_cases(store.root) == {digest(observed)}
    assert not integration._migrate_artifact_failure(controller)
