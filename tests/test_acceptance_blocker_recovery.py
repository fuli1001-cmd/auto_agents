"""Sealed browser failures permit bounded diagnosis without progress credit."""
from copy import deepcopy

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.recovery.convergence import decision, scope
from auto_agents.recovery.model import Event, KernelError
from auto_agents.recovery.native import acceptance, _source
from auto_agents.session import Session
from auto_agents.session_acceptance import begin_recovery
from test_acceptance_recovery_budget import scene


def blocker(scene, *, evidence=True):
    root, initial, store, stream, owner, call = scene
    state = load_session_state(root, initial.session_id)
    directory = root / '.auto-agents/state/sessions' / state.session_id / 'acceptance'
    directory.mkdir(parents=True, exist_ok=True)
    if evidence: (directory / 'route-404.txt').write_text('Browser route /projects/existing returns 404\n')
    state.status, state.resolution = 'blocked', 'acceptance_blocked'
    state.acceptance_execution = {'phase': 'blocked', 'result': {'status': 'blocked',
        'summary': 'The existing browser recovery route returns 404', 'evidence': ['route-404.txt']}}
    save_session_state(root, state)
    return state, directory


def test_blocker_allows_diagnosis_after_old_route_exhaustion_without_credit_or_budget_reset(scene):
    root, initial, store, stream, owner, call = scene
    state, directory = blocker(scene)
    before = store.replay(stream)
    source = _source(owner, state)
    task = 'collab:' + state.session_id
    assert not decision(before, task, 'route', source)['allowed']
    acceptance(owner, state)
    sealed = store.replay(stream)
    assert sealed['budget'] == before['budget']
    assert scope(sealed, task) == scope(before, task)
    assert sealed['recovery']['frontier'] == before['recovery']['frontier']
    assert sealed['tasks'][task]['status'] != 'completed'
    assert decision(sealed, task, 'route', source)['allowed']
    observation = max(sealed['commands'].values(), key=lambda row: row['sequence'])
    assert not observation['model_call'] and observation['outcome']['kind'] == 'environment_blocked'
    assert not observation['outcome']['evidence']
    artifact = observation['outcome']['details']['acceptance_observation']['evidence']['route-404.txt']
    assert store.read_bytes(artifact['blob']) == (directory / 'route-404.txt').read_bytes()
    assert call('collab', 'focused-diagnosis-one').ok
    assert call('collab', 'focused-diagnosis-two').ok
    final = store.replay(stream)
    assert final['budget']['model_calls'] == before['budget']['model_calls'] + 2
    assert not decision(final, task, 'route', source)['allowed']
    assert all(scope(final, task)['routes'][key] == count for key, count in scope(before, task)['routes'].items())
    # Different screenshots, timestamps, summaries and re-entry cannot mint
    # a third diagnosis for the same original goal and code version.
    (directory / 'route-404.txt').write_text('Different screenshot timestamp; still 404\n')
    state.acceptance_execution['result']['summary'] = 'The same route remains blocked'
    acceptance(owner, state)
    assert store.replay(stream) == final
    assert not decision(store.load(stream), task, 'route', source)['allowed']


def test_a_blocked_model_claim_without_artifact_does_not_renew_diagnosis(scene):
    root, initial, store, stream, owner, call = scene
    state, _ = blocker(scene, evidence=False)
    before = store.replay(stream)
    acceptance(owner, state)
    assert store.replay(stream) == before
    assert not decision(before, 'collab:' + state.session_id, 'route', _source(owner, state))['allowed']


def test_forged_artifact_checksum_is_rejected_before_observation_is_published(scene):
    root, initial, store, stream, owner, call = scene
    state, directory = blocker(scene)
    acceptance(owner, state)
    before = store.replay(stream)
    command = max(before['commands'].values(), key=lambda row: row['sequence'])
    outcome = deepcopy(command['outcome'])
    observed = outcome['details']['acceptance_observation']
    observed['evidence']['route-404.txt']['blob'] = store.put_bytes(b'Forged browser evidence')
    outcome['details']['observation_ref'] = store.put({'blocker_observation': observed})
    with pytest.raises(KernelError) as error:
        store.apply(stream, before['revision'], Event('forged-blocker', 'command_finished',
            {'command_id': command['command_id'], 'outcome': outcome}))
    assert error.value.code == 'acceptance_observation'
    assert store.replay(stream) == before


def test_legacy_no_progress_blocker_returns_to_focused_diagnosis_once_without_ambient_run(scene):
    root, initial, store, stream, owner, call = scene
    state, directory = blocker(scene)
    state.resolution = 'kernel_no_progress'
    save_session_state(root, state)
    before = deepcopy(store.load(stream)['budget'])
    session = Session(owner, mode='collab', auto_approve=True)
    assert begin_recovery(session, state, automatic=True)
    assert state.status == 'executing' and state.resolution == ''
    assert state.acceptance_execution['recovery_started'] is True
    assert not begin_recovery(session, state, automatic=True)
    assert store.replay(stream)['budget'] == before
    observed = max(store.load(stream)['commands'].values(), key=lambda row: row['sequence'])
    assert observed['outcome']['details']['acceptance_observation']['domain'] == 'collab:' + state.session_id
