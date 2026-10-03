"""Explicit quota refusal is resumable without discarding uncertain effects."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_agents.models import AgentResult, AgentTermination
from auto_agents.recovery.model import Outcome, OutcomeKind
from auto_agents.recovery.native import provider, provider_outcome
from auto_agents.recovery.rejections import quota_rejection, quota_blocker, reconcile
from test_recovery_convergence_policy import scene, operation
from test_recovery_kernel import finish


def refused():
    message = 'You’ve hit your usage limit. Try again at 2:09 PM.'
    return {'ok': False, 'returncode': -1, 'cleanup_incomplete': False, 'stdout': '', 'summary': '',
        'usage': None, 'termination': {'reason': 'provider_error', 'active_tool': ''},
        'stderr': '\n'.join(json.dumps(event) for event in [
            {'type': 'thread.started', 'thread_id': 'new-thread'}, {'type': 'turn.started'},
            {'type': 'error', 'message': message}, {'type': 'turn.failed', 'error': {'message': message}}])}


def result(path):
    value = refused()
    return AgentResult(False, [], path, returncode=-1, stderr=value['stderr'],
                       termination=AgentTermination(reason='provider_error'))


@pytest.mark.parametrize('change', [
    {'summary': 'Partial reply'}, {'stdout': 'Partial stream'}, {'cleanup_incomplete': True},
    {'usage': {'output_tokens': 1}}, {'termination': {'reason': 'provider_error', 'active_tool': 'shell'}},
    {'termination': {'reason': 'timeout'}}, {'usage_attempts': [{}, {}]},
    {'prompt_metadata': {'resumed': True}}, {'prompt_metadata': 'malformed'}, {'usage_attempts': 1},
])
def test_effectful_or_uncertain_quota_errors_still_require_reconciliation(change):
    assert quota_rejection({**refused(), **change}) is None


@pytest.mark.parametrize('event', [
    {'type': 'item.completed', 'item': {'type': 'command_execution'}},
    {'type': 'item.started', 'item': {'type': 'reasoning'}}, {'type': 'turn.started'},
])
def test_quota_after_agent_activity_is_not_an_execution_refusal(event):
    value = refused()
    value['stderr'] += '\n' + json.dumps(event)
    assert quota_rejection(value) is None


def test_plain_text_quota_or_connection_error_does_not_prove_no_effects(tmp_path):
    value = refused()
    value['stderr'] = 'You’ve hit your usage limit. Try again later.'
    assert quota_rejection(value) is None
    reply = replace(result(tmp_path / 'reply'), stderr='Connection lost')
    assert provider_outcome(SimpleNamespace(_failover_error_category=lambda r: 'connection'), reply)[0] == OutcomeKind.OUTCOME_UNKNOWN


def test_complete_quota_receipt_reports_the_actual_external_blocker(tmp_path):
    kind, reason = provider_outcome(SimpleNamespace(), result(tmp_path / 'reply'))
    assert kind == OutcomeKind.ENVIRONMENT_BLOCKED
    assert 'quota exhausted before execution' in reason and '2:09 PM' in reason


@pytest.mark.parametrize('changed_source', [False, True])
def test_legacy_unknown_quota_is_settled_only_with_unchanged_source_and_no_budget_refund(scene, changed_source):
    store, contract = scene
    command = operation(store, contract, 'implement', 'quota-refusal', model=True)
    finish(store, command, Outcome(OutcomeKind.OUTCOME_UNKNOWN, 'Legacy classification',
        details={'native_result': store.put(refused()),
                 'post_source': 'f'*64 if changed_source else command.source}))
    before = deepcopy(store.load('workflow')['budget'])
    settled = reconcile(store, 'workflow')
    assert settled == ([] if changed_source else [command.command_id])
    after = store.replay('workflow')
    assert after['budget'] == before
    outcome = after['commands'][command.command_id]['outcome']
    assert outcome['kind'] == ('outcome_unknown' if changed_source else 'environment_blocked')
    assert not outcome['evidence']
    assert reconcile(store, 'workflow') == []


def test_acceptance_quota_never_routes_to_development_or_model_diagnosis_and_explicitly_resumes(tmp_path, monkeypatch):
    from auto_agents import cli
    from auto_agents.config import load_session_state, save_session_state
    from auto_agents.controlled_failure import capture
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.recovery.policy import enable
    from auto_agents.session import Session
    from auto_agents.session_acceptance import prepare
    from auto_agents.workflow_chain import WorkflowStore, WorkflowRef
    from auto_agents.workflow_runtime import WorkflowCoordinator
    from test_recovery_native import activate
    from test_session_acceptance import setup_acceptance

    root, state, calls, preserved = setup_acceptance(tmp_path, monkeypatch)
    accepted_provider = Orchestrator._call_with_failover
    state.workflow_id = WorkflowStore(root).create_root(WorkflowRef('collab', state.session_id)).workflow_id
    WorkflowCoordinator(Orchestrator(root), auto_approve=True)._apply_authorization_policy(state)
    save_session_state(root, state)
    prepare(Session(Orchestrator(root), mode='collab'), state,
            {'spec_seed': {'scope': 'existing_behavior_real_acceptance_only'}})
    store = activate(root, tmp_path / 'control', monkeypatch)
    stream = store.binding(root, 'session:' + state.session_id)
    enable(store, stream)
    dispatches = []
    def execute(self, request):
        dispatches.append(request.purpose)
        if len(dispatches) == 1: return result(request.output_path)
        return accepted_provider(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover_owned', execute)
    monkeypatch.setattr(Orchestrator, '_call_with_failover',
                        lambda self, request: provider(self, request, self._call_with_failover_owned))
    blocked = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert blocked.status == 'blocked' and blocked.resolution == 'kernel_environment_blocked'
    assert blocked.acceptance_execution['phase'] == 'executing'
    assert dispatches == ['acceptance_execute']
    snapshot = store.replay(stream)
    command = max(snapshot['commands'].values(), key=lambda row: row['sequence'])
    assert command['status'] == 'finished' and command['outcome']['kind'] == 'environment_blocked'
    assert quota_blocker(root, capture(blocked))['command_id'] == command['command_id']
    monkeypatch.setattr(cli, '_triage_terminal_run_error', lambda *a: pytest.fail('Quota started model diagnosis'))
    reporter = Mock(language='zh')
    cli._triage_controlled_workflow_result(root, SimpleNamespace(reporter=reporter), blocked,
        SimpleNamespace(command='collab', auto_approve=True, full_verify=False), None, Mock())
    assert '用量已耗尽' in '\n'.join(call.args[0] for call in reporter.text.call_args_list)
    record = json.loads((root / '.auto-agents/state/sessions/accept/terminal-triage.json').read_text())
    assert record['owner'] == 'external_provider' and record['triage']['source'] == 'provider_receipt'
    assert store.replay(stream) == snapshot
    resumed = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert resumed.status == 'completed'
    assert dispatches == ['acceptance_execute', 'acceptance_execute', 'acceptance_review']
    assert store.replay(stream)['budget']['model_calls'] == snapshot['budget']['model_calls'] + 2
    assert {p: (root / p).read_bytes() for p in preserved} == preserved
    assert load_session_state(root, state.session_id).status == 'completed'


def test_quota_prose_without_executor_receipt_cannot_skip_diagnosis(tmp_path, monkeypatch):
    from auto_agents.controlled_failure import capture
    from auto_agents.models import SessionState
    monkeypatch.setattr('auto_agents.recovery.authority.installed', lambda root: None)
    state = SessionState('unverified', mode='collab', status='blocked', resolution='kernel_environment_blocked',
                         execution_log=[{'action': 'error', 'result': 'Provider quota exhausted'}])
    assert quota_blocker(tmp_path, capture(state)) is None
