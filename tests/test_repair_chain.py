from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_v2.chain import RepairChain
from auto_agents.repair_v2.diagnostic_evidence import prepare
from auto_agents.repair_v2.store import Store, atomic_json
from auto_agents.repair_v2.transaction import transaction_root
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.repair_control import Supervisor, digest, start_ticks
from test_repair_control import configuration, registration


def payload(tmp_path, symptom='first'):
    return {'project': str(tmp_path / 'product'), 'base': 'base', 'environment': 'env',
            'autonomy': 'max', 'fingerprint': symptom, 'contract': {},
            'invocation': {'command': 'collab', 'session_id': 'parent', 'workflow_id': 'wf-parent',
                           'engine_route': {'issue_seed': {'failed_handoff_id': 'hf-original'}}}}


def test_new_description_provider_and_job_cannot_reset_goal_budget(tmp_path):
    config = {'root': str(tmp_path / 'control'),
              'repair_chain_limits': {'transactions': 2, 'implementations': 2, 'model_calls': 4}}
    p = payload(tmp_path)
    root = transaction_root(config, p)
    first = RepairChain(config, p, root); first.admit()
    first.reserve('plan'); first.reserve('implement')
    changed = payload(tmp_path, 'reworded'); changed['provider'] = 'another-provider'
    changed['invocation']['command'] = 'resume'
    changed['invocation']['run_id'] = 'nested-run-metadata'
    changed['repair_chain_limits'] = {'model_calls': 1000}
    second = RepairChain(config, changed, transaction_root(config, changed)); second.admit()
    second.reserve('plan'); second.reserve('implement')
    with pytest.raises(RepairBlocked, match='model_calls'):
        RepairChain(config, p, root).reserve('review')
    third = payload(tmp_path, 'another-new-issue')
    with pytest.raises(RepairBlocked, match='new issue description'):
        RepairChain(config, third, transaction_root(config, third)).admit()
    assert second.context()['used'] == {'transactions': 2, 'implementations': 2, 'model_calls': 4}
    third['invocation']['session_id'] = 'explicit-new-user-session'
    independent = RepairChain(config, third, transaction_root(config, third))
    assert independent.admit()['model_calls'] == 0


def test_budget_imports_previous_calls_including_interrupted_implementation(tmp_path):
    config = {'root': str(tmp_path / 'control')}
    p = payload(tmp_path)
    root = transaction_root(config, p)
    atomic_json(root / 'original-payload.json', p)
    Store(root).save({'calls': 7, 'attempts': 2})
    (root / 'events.jsonl').write_text('\n'.join(json.dumps({'kind': 'agent_started', 'role': 'implement'})
                                                for _ in range(3)))
    chain = RepairChain(config, p, root)
    assert chain.admit() == {'transactions': 1, 'implementations': 3, 'model_calls': 7}
    chain.reserve('review')
    # Importing the older checkpoint or changing workflow_id cannot refund it.
    p['invocation']['workflow_id'] = ''
    resumed = RepairChain(config, p, root)
    assert resumed.admit()['model_calls'] == 8


def test_concurrent_reservations_never_overspend_or_refund_on_restart(tmp_path):
    config = {'root': str(tmp_path), 'repair_chain_limits': {'model_calls': 5}}
    p = payload(tmp_path); chain = RepairChain(config, p, transaction_root(config, p)); chain.admit()
    def reserve(_):
        try: chain.reserve('review'); return True
        except RepairBlocked: return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(12))) == 5
    assert chain.admit()['model_calls'] == 5


def test_saved_limits_and_implementation_budget_survive_controller_restart(tmp_path):
    config = {'root': str(tmp_path), 'repair_chain_limits': {'implementations': 1}}
    p = payload(tmp_path); root = transaction_root(config, p)
    chain = RepairChain(config, p, root); chain.admit(); chain.reserve('implement')
    resumed = RepairChain({'root': str(tmp_path)}, p, root); resumed.admit()
    with pytest.raises(RepairBlocked, match='implementations: 1/1'):
        resumed.reserve('implement')
    assert resumed.context()['used']['model_calls'] == 1


def test_new_invocation_reads_operator_limits_even_with_an_older_supervisor_config(tmp_path):
    config = {'root': str(tmp_path), 'identity': 'engine', 'remote': 'local.git', 'ref': 'refs/heads/main',
              'repair_chain_limits': {'model_calls': 1}}
    p = payload(tmp_path); root = transaction_root(config, p)
    chain = RepairChain(config, p, root); chain.admit(); chain.reserve('plan')
    atomic_json(tmp_path / 'operator.json', {**config, 'repair_chain_limits': {'model_calls': 2}})
    resumed = RepairChain(config, p, root); resumed.admit(); resumed.reserve('review')
    assert resumed.context()['used']['model_calls'] == 2
    with pytest.raises(RepairBlocked): resumed.reserve('review')


def test_only_explicit_operator_budget_increase_can_resume_exhausted_chain(tmp_path):
    from test_repair_v2_controller import Driver, Verifier, controller
    from auto_agents.repair_v2.types import RepairRequest, Acceptance
    from auto_agents.repair_v2.workspace import Workspace, git
    source = tmp_path / 'source'; source.mkdir(); git(source, 'init', '-q')
    (source / 'source.py').write_text('value = 0\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'base')
    request = RepairRequest('repair', git(source, 'rev-parse', 'HEAD'), 'repair',
                            (Acceptance('value', 'value is one'),), 'fake')
    p = payload(tmp_path); config = {'root': str(tmp_path / 'control'), 'repair_chain_limits': {'model_calls': 2}}
    root = transaction_root(config, p)
    chain = RepairChain(config, p, root); chain.admit()
    driver = Driver()
    runner = controller((request, Store(root), Workspace(tmp_path / 'work', source, request.engine_base)), driver, Verifier())
    runner.chain = chain; runner.resume_token = 'first'
    stopped = deepcopy(runner.run())
    assert stopped['blocker']['code'] == 'repair_chain_exhausted'
    runner.resume_token = 'same-budget-new-job'
    assert runner.run()['status'] == 'blocked'
    config['repair_chain_limits']['model_calls'] = 3
    runner = controller((request, Store(root), Workspace(tmp_path / 'work', source, request.engine_base)), driver, Verifier())
    runner.chain = RepairChain(config, p, root); runner.chain.admit()
    runner.resume_token = 'operator-increased-budget'
    assert runner.run()['status'] == 'ready'
    assert runner.state['attempts'] == stopped['attempts']
    assert runner.chain.context()['used']['model_calls'] == 3


def test_scene_bundle_contains_retained_chain_but_no_live_files_or_credentials(tmp_path, monkeypatch):
    root = tmp_path / 'transaction'; target = root / 'target-evidence'
    p = payload(tmp_path)
    state = target / '.auto-agents/state'
    atomic_json(state / 'sessions/parent/session_state.json',
                {'session_id': 'parent', 'goal': 'Generate one real video', 'active_handoff_id': 'hf-wrap'})
    atomic_json(state / 'handoffs/hf-wrap.json', {'payload': {'resume_handoff_id': 'hf-original'}})
    atomic_json(state / 'handoffs/hf-original.json', {'child': {'kind': 'fix', 'native_id': 'child'}})
    atomic_json(state / 'sessions/child/session_state.json', {'session_id': 'child',
                'parent_handoff_id': 'hf-original', 'diagnostic': 'api_key=private-value', 'payload': None})
    (target / '.env').write_text('SECRET=do-not-copy')
    p['invocation']['engine_route']['issue_seed']['evidence_refs'] = ['.env', '/outside/secret.txt']
    monkeypatch.setenv('TEST_SECRET', 'private-value')
    before = (state / 'sessions/child/session_state.json').read_bytes()
    folder, context = prepare(root, p)
    assert context['root'] == '/repair-evidence' and context['original_goal'] == 'Generate one real video'
    child = folder / 'original/.auto-agents/state/sessions/child/session_state.json'
    assert 'private-value' not in child.read_text() and '<redacted>' in child.read_text()
    assert json.loads(child.read_text())['session_id'] == 'child'
    assert not (folder / 'original/.env').exists()
    assert (state / 'sessions/child/session_state.json').read_bytes() == before
    assert prepare(root, p)[0] == folder


def test_agent_receives_only_read_only_sanitized_evidence(tmp_path):
    from auto_agents.repair_v2.providers import AgentSandbox
    candidate = tmp_path / 'candidate'; candidate.mkdir()
    evidence = tmp_path / 'safe-evidence'; evidence.mkdir()
    sandbox = AgentSandbox(tmp_path / 'home', 'pinned', evidence=evidence)
    with patch.object(sandbox, 'home', return_value=tmp_path / 'home'), \
         patch('auto_agents.repair_v2.docker.run', return_value=(0, '')):
        with sandbox.command('implement', candidate, ['tool']) as argv:
            assert f'type=bind,src={evidence},dst=/repair-evidence,readonly' in argv
            assert not any('target-evidence' in part for part in argv)


@pytest.fixture
def recovering(tmp_path):
    from auto_agents.run_lock import ProjectRunLock
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    project = tmp_path / 'product'; project.mkdir()
    with ProjectRunLock(project, environ={}) as lock:
        subscriber = supervisor.register({'payload': registration(project, lock.run_token)},
                                         [os.dup(lock.fileno)])['subscriber']
        p = payload(tmp_path)
        route = p['invocation']['engine_route']
        p['boundary'] = {'kind': 'engine_route', 'route_digest': digest(route)}
        job = supervisor.store.submit(subscriber, p)
        supervisor.store.transition(job, 'ready', {'engine': 'v2', 'ok': True, 'runtime': str(tmp_path),
                                                  'commit': 'verified', 'status': 'repaired'})
        with supervisor.store.connect() as db:
            db.execute("UPDATE subscribers SET state='resuming' WHERE id=?", (subscriber,))
        proof = {'ok': True, 'observed': {'recovery_observation': {'ok': True,
            'child_session_id': 'child', 'workflow_id': 'wf-parent', 'original_handoff_id': 'hf-original',
            'preflight_rechecked': True, 'boundary_kind': 'implementation'}}}
        atomic_json(Path(config['root']) / 'jobs' / job / f'validate-{subscriber}-g1-result.json',
                    {'ok': True, 'proof': json.dumps(proof)})
        try: yield supervisor, subscriber, job, p, tmp_path
        finally:
            for entry in supervisor.registrations.values():
                for fd in entry['fds']: os.close(fd)


def test_receipt_consumption_cannot_complete_or_publish_before_real_child_entry(recovering):
    supervisor, subscriber, job, p, root = recovering
    common = {'version': 1, '_peer_pid': os.getpid(), 'subscriber': subscriber}
    accepted = supervisor.dispatch({**common, 'op': 'consume-route',
                                   'route_digest': p['boundary']['route_digest']}, [])
    assert accepted['accepted'] and supervisor.store.job(job)['state'] == 'ready'
    assert supervisor.store.due_publish() == []
    with pytest.raises(RuntimeError, match='has not restored its bound child'):
        supervisor.dispatch({**common, 'op': 'submit', 'payload': {**p, 'fingerprint': 'new-words'}}, [])
    details = {'route_digest': p['boundary']['route_digest'], 'session_id': 'wrong',
               'workflow_id': 'wf-parent', 'original_handoff_id': 'hf-original', 'binding_fingerprint': 'bound'}
    request = {**common, 'op': 'boundary', 'kind': 'engine_child', 'runtime': str(root), 'details': details}
    assert not supervisor.dispatch(request, [])['accepted']
    details['session_id'] = 'child'
    assert supervisor.dispatch(request, [])['accepted']
    assert supervisor.store.job(job)['state'] == 'completed'


def test_successful_exit_without_child_entry_cannot_be_counted_as_recovery(recovering):
    supervisor, subscriber, job, p, root = recovering
    supervisor.resumes[subscriber] = SimpleNamespace(poll=lambda: 0, returncode=0)
    supervisor.tick()
    row = next(s for s in supervisor.store.subscriptions() if s['id'] == subscriber)
    assert row['state'] == 'blocked'
    assert 'implementation boundary' in row['payload']['repair_failure']['error']
    assert supervisor.store.job(job)['state'] != 'completed'


def test_verified_route_prepares_actual_child_reentry_without_parent_diagnosis(tmp_path, monkeypatch):
    from test_engine_reference_recovery import retained_recovery
    from auto_agents.config import load_session_state, save_session_state
    from auto_agents.session import Session
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.workflow_runtime import WorkflowCoordinator
    root, child, store, snapshot, original, failed, repair, _ = retained_recovery(tmp_path)
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = ''; parent.status = 'executing'
    save_session_state(root, parent)
    marker = tmp_path / 'verified.json'
    atomic_json(marker, {'route_digest': digest(repair.payload)})
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(marker))
    coordinator = WorkflowCoordinator(Orchestrator(root))
    session = Session(coordinator.orch, mode='collab', coordinator=coordinator)
    returned = session._prepare_workflow_handoff(parent, target='fix', reason='verified return', payload=repair.payload)
    assert returned.status == 'waiting_child'
    handoff = store.load_handoff(returned.active_handoff_id)
    assert handoff.payload == repair.payload
    seen = []
    class ChildBoundary(BaseException): pass
    def child_boundary(session, state, snapshot, *, root):
        seen.append(state)
        raise ChildBoundary()
    monkeypatch.setattr(coordinator, '_drive_session', child_boundary)
    with pytest.raises(ChildBoundary):
        coordinator._drive_handoff(session, returned, store.load(snapshot.workflow_id))
    assert seen[0].session_id == child.session_id and seen[0].status == 'executing'
    assert seen[0].verification_binding
    assert seen[0].parent_handoff_id == original.handoff_id


def test_recovery_environment_failure_stops_before_full_suite_or_review(tmp_path):
    from test_repair_v2_controller import Driver, Verifier, controller
    from auto_agents.repair_v2.types import RepairRequest, Acceptance
    from auto_agents.repair_v2.workspace import Workspace, git
    source = tmp_path / 'source'; source.mkdir(); git(source, 'init', '-q')
    (source / 'source.py').write_text('value = 0\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'base')
    request = RepairRequest('repair', git(source, 'rev-parse', 'HEAD'), 'repair',
                            (Acceptance('value', 'value is one'),), 'fake')
    verifier, driver = Verifier(), Driver()
    runner = controller((request, Store(tmp_path / 'state'), Workspace(tmp_path / 'work', source, request.engine_base)),
                        driver, verifier)
    runner.preflight_boundary = True
    runner.boundary = lambda identity, *args: {'ok': False, 'snapshot': identity, 'infrastructure': True,
        'reason': 'verification conda environment does not exist: /product/.conda'}
    runner.units = lambda _: pytest.fail('full suite must not start')
    state = runner.run()
    assert state['status'] == 'blocked' and state['blocker']['code'] == 'verification_infrastructure'
    assert verifier.calls == [] and all(role != 'review' for role, _ in driver.calls)
