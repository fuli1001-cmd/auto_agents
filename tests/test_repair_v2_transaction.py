from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_v2.store import Store, atomic_json, digest
from auto_agents.repair_v2.transaction import frozen_request, intent, transaction_root
from auto_agents.repair_v2.types import Acceptance, RepairBlocked, RepairRequest


def test_controller_upgrade_uses_committed_operator_source_without_resetting_transaction(tmp_path):
    from auto_agents.repair_v2.transaction import bind_controller
    from auto_agents.repair_v2.workspace import git
    old, new, candidate = [tmp_path / name for name in ('old-controller', 'new-controller', 'candidate')]
    for directory, content in ((old, 'clearance only'), (new, 'real verification and review'), (candidate, 'untrusted report')):
        directory.mkdir()
        git(directory, 'init', '-q')
        (directory / 'boundary_driver.py').write_text(content)
        git(directory, 'add', '.'); git(directory, 'commit', '-qm', content)
    root = tmp_path / 'transaction'
    store = Store(root)
    store.save({'status': 'blocked', 'attempts': 4, 'calls': 9, 'plan': 'retained-plan',
                'sessions': {'implement': 'retained-session'}, 'blocker': {'code': 'no_progress'}})
    before = (root / 'state.json').read_bytes()
    first = bind_controller(root, {'implementation_root': str(old), 'source_root': str(candidate)}, 'job:1')
    upgraded = {'implementation_root': str(new), 'source_root': str(candidate)}
    assert bind_controller(root, upgraded, 'job:1') == first
    second = bind_controller(root, upgraded, 'job:2')
    assert second['root'] == str(new) and second['commit'] == git(new, 'rev-parse', 'HEAD')
    assert second['source'] != first['source']
    assert (root / 'state.json').read_bytes() == before


@pytest.fixture
def legacy(tmp_path):
    config = {'root': str(tmp_path / 'control')}
    payload = {'project': str(tmp_path / 'project'), 'base': 'old-engine',
               'fingerprint': 'same-failure', 'autonomy': 'max',
               'contract': {'expected_postconditions': ['repair original behavior']},
               'invocation': {'command': 'collab', 'session_id': 'session', 'workflow_id': ''}}
    root = transaction_root(config, payload)
    request = RepairRequest('frozen', 'old-engine', 'repair original behavior',
                            (Acceptance('behavior', 'repair original behavior'),), 'codex')
    frozen_request(root, payload, lambda: request)
    atomic_json(root / 'target-evidence/.auto-agents/state/sessions/session/session_state.json',
                {'session_id': 'session', 'workflow_id': 'wf-original', 'mode': 'collab'})
    Store(root).save({'status': 'blocked', 'phase': 'validate', 'attempts': 3, 'calls': 8,
                     'stagnant': 1, 'replans': 1, 'plan': 'original-plan',
                     'sessions': {'implement': 'original-writer', 'review': 'original-reviewer'},
                     'blocker': {'code': 'provider_failed', 'message': 'usageLimitExceeded'}})
    (root / 'candidate.patch').write_text('retained changes\n')
    current = {**payload, 'base': 'new-engine', 'provider': 'codex',
               'invocation': {**payload['invocation'], 'workflow_id': 'wf-original'}}
    return config, payload, root, request, current


@pytest.mark.parametrize('duplicate', [False, True])
def test_completed_workflow_identity_selects_original_quota_stopped_transaction(legacy, duplicate):
    config, payload, root, request, current = legacy
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    accidental = Path(config['root']) / 'v2-transactions' / digest(intent(current))
    if duplicate:
        frozen_request(accidental, current, lambda: request)
        Store(accidental).save({'status': 'stopped', 'phase': 'plan', 'attempts': 0})
        duplicate_bytes = (accidental / 'state.json').read_bytes()
    assert transaction_root(config, current) == root
    restored = frozen_request(root, current, lambda: pytest.fail('must reuse frozen contract'))
    assert restored.to_dict() == request.to_dict()
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    if duplicate:
        assert (accidental / 'state.json').read_bytes() == duplicate_bytes
    assert transaction_root(config, payload) == root


@pytest.mark.parametrize('change', ['workflow', 'session', 'project', 'contract', 'route', 'symptom', 'autonomy', 'command'])
def test_distinct_repair_scope_never_reuses_legacy_transaction(legacy, change):
    config, payload, root, request, current = legacy
    changed = deepcopy(current)
    if change in ('workflow', 'session', 'command'):
        key = {'workflow': 'workflow_id', 'session': 'session_id', 'command': 'command'}[change]
        changed['invocation'][key] = 'other'
    elif change == 'route':
        changed['invocation']['engine_route'] = {'issue_seed': {'scope': 'different'}}
    elif change == 'contract':
        changed['contract'] = {'expected_postconditions': ['different']}
    elif change == 'symptom':
        changed['fingerprint'] = 'different'
    else:
        changed[change] = 'different'
    assert transaction_root(config, changed) != root
    with pytest.raises(RepairBlocked, match='repair intent differs'):
        frozen_request(root, changed, lambda: request)


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'empty_workflow', 'marker'])
def test_unproved_legacy_identity_does_not_silently_get_fresh_budget(legacy, damage):
    config, payload, root, request, current = legacy
    evidence = root / 'target-evidence/.auto-agents/state/sessions/session/session_state.json'
    if damage == 'missing': evidence.unlink()
    elif damage == 'corrupt': evidence.write_text('{')
    elif damage == 'empty_workflow': atomic_json(evidence, {'session_id': 'session', 'workflow_id': ''})
    else: atomic_json(root / 'intent.json', {'digest': 'incorrect'})
    with pytest.raises(RepairBlocked) as error:
        transaction_root(config, current)
    assert error.value.code == 'transaction_identity_unresolved'
    assert Store(root).load()['attempts'] == 3


def test_legacy_identity_uses_frozen_evidence_not_current_project_state(legacy):
    config, payload, root, request, current = legacy
    path = Path(payload['project']) / '.auto-agents/state/sessions/session/session_state.json'
    atomic_json(path, {'session_id': 'session', 'workflow_id': 'wf-unrelated', 'mode': 'collab'})
    assert transaction_root(config, current) == root
    different = {**current, 'invocation': {**current['invocation'], 'workflow_id': 'wf-unrelated'}}
    assert transaction_root(config, different) != root


@pytest.mark.parametrize('initial_workflow', ['', None, 'wf-original', 'wf-wrong'])
def test_submit_fills_first_invocation_from_durable_session(tmp_path, initial_workflow):
    from auto_agents.config import save_session_state
    from auto_agents.models import SessionState
    from auto_agents.repair_client import EngineRepairRequired, submit_and_wait
    from auto_agents.self_repair import SelfRepairDecision
    project = tmp_path / 'project'; project.mkdir()
    save_session_state(project, SessionState(session_id='session', mode='collab', workflow_id='wf-original'))
    invocation = {'command': 'collab', 'session_id': '', 'workflow_id': initial_workflow}
    orch = SimpleNamespace(_invocation_context=invocation, _repair_registration={'config': {}, 'subscriber': 'owner'},
        config=SimpleNamespace(active_provider='codex', execution=SimpleNamespace(
            autonomy=SimpleNamespace(mode='max', to_dict=lambda: {}))))
    args = SimpleNamespace(command='collab', provider='codex', autonomy=None)
    submitted = []
    class Submitted(BaseException): pass
    def transport(config, request):
        assert request['op'] == 'submit'
        submitted.append(request['payload'])
        raise Submitted()
    argv = ['python', 'auto_agents.py', 'collab', '--project', str(project), '--session', 'session']
    with patch('auto_agents.cli._run_command_for_self_repair_resume', return_value=argv), \
         patch('auto_agents.repair_client.git', return_value='engine'), \
         patch('auto_agents.repair_client.rpc', side_effect=transport), \
         patch('auto_agents.process_supervision.ACTIVE_PROCESSES.terminate_all'), \
         patch('auto_agents.process_supervision.ACTIVE_PROCESSES.snapshot', return_value=[]):
        with pytest.raises(RuntimeError if initial_workflow == 'wf-wrong' else Submitted):
            submit_and_wait(project, orch, EngineRepairRequired({'target_repository': '/engine'}),
                            SelfRepairDecision(True), args, SimpleNamespace())
    if initial_workflow == 'wf-wrong':
        assert submitted == []
    else:
        assert submitted[0]['invocation']['workflow_id'] == 'wf-original'
        assert submitted[0]['invocation']['session_id'] == 'session'


def test_legacy_quota_resume_reuses_candidate_plan_sessions_and_attempts(tmp_path):
    from auto_agents.repair_v2.workspace import Workspace
    from test_repair_v2_controller import controller, Driver, Verifier
    from auto_agents.repair_v2.workspace import git
    source = tmp_path / 'engine'; source.mkdir()
    git(source, 'init', '-q')
    (source / 'source.py').write_text('value = 0\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'baseline')
    config = {'root': str(tmp_path / 'control')}
    payload = {'project': str(tmp_path / 'project'), 'fingerprint': 'failure', 'autonomy': 'max',
               'contract': {}, 'invocation': {'command': 'collab', 'session_id': 'session', 'workflow_id': ''}}
    root = transaction_root(config, payload)
    request = RepairRequest('repair', git(source, 'rev-parse', 'HEAD'), 'restore value',
                            (Acceptance('value', 'value must be 1'),), 'fake')
    frozen_request(root, payload, lambda: request)
    atomic_json(root / 'target-evidence/.auto-agents/state/sessions/session/session_state.json',
                {'session_id': 'session', 'workflow_id': 'wf-original', 'mode': 'collab'})
    workspace = Workspace(root / 'workspace', source, request.engine_base)
    driver, verifier = Driver(), Verifier()
    def quota(identity, *args): raise RepairBlocked('provider_failed', 'usageLimitExceeded')
    first = controller((request, Store(root), workspace), driver, verifier)
    first.review = quota
    first.resume_token = 'first'
    stopped = first.run()
    assert stopped['status'] == 'blocked' and stopped['phase'] == 'validate'
    calls = list(driver.calls)
    current = {**payload, 'invocation': {**payload['invocation'], 'workflow_id': 'wf-original'}}
    restored_root = transaction_root(config, current)
    restored_request = frozen_request(restored_root, current, lambda: pytest.fail('new contract'))
    resumed = controller((restored_request, Store(restored_root),
                          Workspace(restored_root / 'workspace', source, request.engine_base)), driver, verifier)
    resumed.resume_token = 'new-invocation'
    final = resumed.run()
    assert final['status'] == 'ready' and final['attempts'] == stopped['attempts'] == 1
    assert final['plan'] == stopped['plan'] and final['sessions']['implement'] == 'writer'
    assert driver.calls[len(calls):] == [('review', '')]
    assert (workspace.candidate / 'source.py').read_text() == 'value = 1\n'
