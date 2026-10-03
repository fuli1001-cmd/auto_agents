"""Real failover selection with separately charged, replayable provider commands."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from auto_agents.models import AgentRequest, AgentResult, AgentTermination, ProviderConfig
from auto_agents.provider_usage import invoke_provider
from auto_agents.recovery.convergence import scope
from auto_agents.recovery.model import KernelError
from auto_agents.recovery.native import provider
from test_acceptance_recovery_budget import scene
from test_recovery_provider_quota import result as quota_result


class Adapter:
    def __init__(self, reply):
        self.reply, self.requests, self.ready = reply, [], True

    def available(self):
        return self.ready

    def run(self, request):
        self.requests.append(request)
        return self.reply(request)


@pytest.fixture
def pool(scene, monkeypatch, tmp_path):
    root, state, store, stream, owner, _ = scene
    primary = Adapter(lambda r: quota_result(r.output_path))
    backup = Adapter(lambda r: AgentResult(True, [], r.output_path, summary='Observed the original goal'))
    adapters = {'codex-fuli0110': primary, 'codex': backup}
    owner.config.providers = {name: ProviderConfig(kind='codex') for name in adapters}
    owner.config.active_provider = 'codex-fuli0110'
    owner.adapter = primary
    owner._last_successful_provider = None
    monkeypatch.setattr(owner, '_build_adapter_for_provider', lambda name: adapters[name])
    monkeypatch.setattr(owner, '_run_provider_with_smart_recovery',
                        lambda adapter, request, name: invoke_provider(adapter, request, name))
    request = AgentRequest('implement', 'deep', 'Inspect the existing goal without replanning',
        root, tmp_path / 'reply', purpose='acceptance_execute', attempt_id='acceptance-original',
        logical_call_id='original-logical-call', sandbox_mode='workspace-write',
        usage_context={'project_root': str(root), 'workflow_kind': 'collab', 'subject_id': state.session_id})
    def call():
        return provider(owner, request, owner._call_with_failover_owned)
    return root, state, store, stream, owner, adapters, request, call


def test_quota_is_sealed_before_switch_and_both_attempts_are_counted_and_replayed(pool):
    root, state, store, stream, owner, adapters, request, call = pool
    before = store.load(stream)
    routes = deepcopy(scope(before, 'collab:' + state.session_id)['routes'])
    def backup(bound):
        commands = list(store.load(stream)['commands'].values())
        prior = next(c for c in commands if (c.get('outcome') or {}).get('details', {}).get('provider_quota'))
        assert prior['status'] == 'finished' and prior['outcome']['kind'] == 'environment_blocked'
        assert not prior['outcome']['evidence']
        assert bound.usage_context['quota_predecessor'] == prior['command_id']
        assert bound.usage_context['kernel_provider'] == 'codex'
        assert not bound.resume_session_id and not bound.resume_provider
        assert 'Inspect the existing goal without replanning' in bound.prompt
        assert state.goal in bound.prompt
        return AgentResult(True, [], bound.output_path, summary='Observed the original goal')
    adapters['codex'].reply = backup
    returned = call()
    assert returned.ok and owner._last_successful_provider == 'codex'
    assert [r['provider'] for r in returned.usage_attempts] == ['codex-fuli0110', 'codex']
    after = store.replay(stream)
    assert after['budget']['model_calls'] == before['budget']['model_calls'] + 2
    assert scope(after, 'collab:' + state.session_id)['routes'] == routes
    fallback = max(after['commands'].values(), key=lambda row: row['sequence'])
    assert fallback['outcome']['details']['provider'] == 'codex'
    assert fallback['outcome']['details']['quota_predecessor']
    assert call().ok
    assert len(adapters['codex-fuli0110'].requests) == len(adapters['codex'].requests) == 1
    assert store.replay(stream) == after


@pytest.mark.parametrize('failure', ['connection', 'tool_activity', 'source_changed'])
def test_unknown_or_effectful_calls_never_start_another_provider(pool, failure):
    root, state, store, stream, owner, adapters, request, call = pool
    before = store.load(stream)['budget']['model_calls']
    def refusal(bound):
        reply = quota_result(bound.output_path)
        if failure == 'connection': return replace(reply, stderr='Connection lost')
        if failure == 'tool_activity':
            return replace(reply, stderr=reply.stderr + '\n{"type":"item.completed","item":{"type":"command_execution"}}')
        (root / 'value.py').write_text('CHANGED = 2\n')
        return reply
    adapters['codex-fuli0110'].reply = refusal
    with pytest.raises(KernelError) as error: call()
    assert error.value.code == 'outcome_unknown'
    assert len(adapters['codex-fuli0110'].requests) == 1 and not adapters['codex'].requests
    assert store.replay(stream)['budget']['model_calls'] == before + 1


def test_all_providers_quota_blocked_are_tried_once_and_replay_without_repeating_requests(pool):
    root, state, store, stream, owner, adapters, request, call = pool
    adapters['codex'].reply = lambda r: quota_result(r.output_path)
    before = store.load(stream)['budget']['model_calls']
    with pytest.raises(KernelError, match='quota exhausted'): call()
    after = store.replay(stream)
    assert after['budget']['model_calls'] == before + 2
    assert all(c['status'] == 'finished' for c in after['commands'].values())
    with pytest.raises(KernelError, match='quota exhausted'): call()
    assert len(adapters['codex-fuli0110'].requests) == len(adapters['codex'].requests) == 1
    assert store.replay(stream) == after


def test_interruption_after_refusal_resumes_at_backup_without_repeating_primary(pool):
    root, state, store, stream, owner, adapters, request, call = pool
    def interrupted():
        raise KeyboardInterrupt
    adapters['codex'].available = interrupted
    with pytest.raises(KeyboardInterrupt): call()
    assert len(adapters['codex-fuli0110'].requests) == 1 and not adapters['codex'].requests
    assert all(c['status'] == 'finished' for c in store.load(stream)['commands'].values())
    adapters['codex'].available = lambda: True
    assert call().ok
    assert len(adapters['codex-fuli0110'].requests) == len(adapters['codex'].requests) == 1


def test_backup_unknown_outcome_blocks_replay_and_a_third_provider(pool):
    root, state, store, stream, owner, adapters, request, call = pool
    third = Adapter(lambda r: AgentResult(True, [], r.output_path, summary='Unexpected extra request'))
    adapters['codex-third'] = third
    owner.config.providers['codex-third'] = ProviderConfig(kind='codex')
    adapters['codex'].reply = lambda r: AgentResult(False, [], r.output_path, returncode=-1,
        stderr='Connection lost', termination=AgentTermination(reason='provider_error'))
    with pytest.raises(KernelError) as error: call()
    assert error.value.code == 'outcome_unknown'
    after = store.replay(stream)
    with pytest.raises(KernelError): call()
    assert not third.requests
    assert len(adapters['codex-fuli0110'].requests) == len(adapters['codex'].requests) == 1
    assert store.replay(stream) == after


def test_unavailable_backup_is_skipped_without_spending_a_call(pool):
    root, state, store, stream, owner, adapters, request, call = pool
    adapters['codex'].ready = False
    third = Adapter(lambda r: AgentResult(True, [], r.output_path, summary='Original goal observed'))
    adapters['codex-third'] = third
    owner.config.providers['codex-third'] = ProviderConfig(kind='codex')
    before = store.load(stream)['budget']['model_calls']
    assert call().ok
    assert len(third.requests) == 1 and not adapters['codex'].requests
    assert store.replay(stream)['budget']['model_calls'] == before + 2


@pytest.mark.parametrize('code', ['user_budget_exhausted', 'no_progress'])
def test_backup_still_requires_its_own_kernel_budget_and_phase_permit(pool, monkeypatch, code):
    from auto_agents.recovery import policy
    root, state, store, stream, owner, adapters, request, call = pool
    before = store.load(stream)['budget']['model_calls']
    reserve = policy.reserve
    def limited(store, stream, command):
        if store.load(stream)['budget']['model_calls'] > before:
            raise KernelError(code, 'Original kernel budget or phase allowance exhausted')
        return reserve(store, stream, command)
    monkeypatch.setattr(policy, 'reserve', limited)
    with pytest.raises(KernelError) as error: call()
    assert error.value.code == code
    assert not adapters['codex'].requests
    assert len(adapters['codex-fuli0110'].requests) == 1
    after = store.replay(stream)
    assert after['budget']['model_calls'] == before + 1
    assert all(c['status'] == 'finished' for c in after['commands'].values())


def test_same_session_finishes_acceptance_after_quota_failover_without_replanning(pool, monkeypatch):
    from auto_agents.config import load_run_state, load_session_state
    from auto_agents.session import Session
    from auto_agents.session_acceptance import prepare
    from auto_agents.workflow_runtime import WorkflowCoordinator
    root, state, store, stream, owner, adapters, request, call = pool
    state = load_session_state(root, state.session_id)
    before = store.load(stream)['budget']['model_calls']
    run = load_run_state(root).to_dict()
    WorkflowCoordinator(owner, auto_approve=True)._apply_authorization_policy(state)
    prepare(Session(owner, mode='collab', auto_approve=True), state,
            {'spec_seed': {'scope': 'existing_behavior_real_acceptance_only'}})
    def reply(bound):
        if bound.purpose == 'acceptance_execute':
            directory = root / '.auto-agents/state/sessions' / state.session_id / 'acceptance'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'observed.txt').write_text((root / 'value.py').read_text())
            text = json.dumps({'status': 'passed', 'summary': 'Observed the existing value', 'evidence': ['observed.txt']})
        else:
            assert bound.purpose == 'acceptance_review'
            text = json.dumps({'approved': True, 'reason': 'Checked original goal and actual file observation'})
        return AgentResult(True, [], bound.output_path, summary=text)
    adapters['codex'].reply = reply
    monkeypatch.setattr(owner, '_call_with_failover', lambda req: provider(owner, req, owner._call_with_failover_owned))
    completed = Session(owner, mode='collab', auto_approve=True).resume(state.session_id)
    assert completed.status == 'completed' and completed.session_id == state.session_id
    assert completed.acceptance_execution['phase'] == 'completed'
    assert not completed.active_handoff_id
    assert len(adapters['codex-fuli0110'].requests) == 1
    assert [r.purpose for r in adapters['codex'].requests] == ['acceptance_execute', 'acceptance_review']
    assert store.replay(stream)['budget']['model_calls'] == before + 3
    assert load_run_state(root).to_dict() == run
