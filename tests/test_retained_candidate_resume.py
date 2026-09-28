"""Stopped searches retain a verifiable continuation, never fresh writer credit."""
from copy import deepcopy
import json

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.models import AgentResult, CommandResult, GateResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.recovery import KernelError, OutcomeKind
from auto_agents.recovery.native import perform
from auto_agents.session import Session
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_engine_child_recovery import parent_workflow, ObservationBoundary
from test_recovery_native import activate
from test_session_verification_ownership import project


def test_targeted_feedback_keeps_pytest_cause_hidden_by_conda_wrapper():
    from auto_agents.verification_failure import details
    gate = GateResult(False, [CommandResult('conda run python -m pytest', False, 1,
        stdout='traceback\nE   AssertionError: budget is 1, expected 2\n'
               'FAILED tests/test_patch.py::test_boundary - AssertionError: budget is 1\n',
        stderr='ERROR conda.cli.main_run: Subprocess command failed ' + 'wrapper ' * 200)])
    reason, diagnostic = details(gate)
    assert 'budget is 1, expected 2' in reason
    assert 'tests/test_patch.py::test_boundary' in reason
    assert diagnostic['comparable'] and diagnostic['command_failures'][0]['stdout_tail']


def stopped_candidate(tmp_path, monkeypatch, *, engine_return=False):
    from auto_agents import session_candidate
    root, child = project(tmp_path)
    graph, snapshot, handoff = parent_workflow(root, child)
    (root/'.auto-agents/state/sessions'/child.session_id/'issue.json').write_text('{"task_id":"task-owned"}')
    store = activate(root, tmp_path/'control', monkeypatch)
    calls = []
    def agent(self, request):
        calls.append(request.purpose)
        if request.purpose == 'collab':
            raise ObservationBoundary()
        if request.purpose == 'review':
            props = request.response_schema['properties']['change_coverage']['items']['properties']
            checks = props['requirement']['enum'][:-1]
            reply = json.dumps({'decision': 'APPROVE', 'findings': [],
                'coverage': [{'requirement': check, 'nodes': ['tests/test_owned.py::test_owned']} for check in checks],
                'change_coverage': [{'change': key, 'requirement': checks[0], 'reason': 'Owned fix',
                                    'evidence': 'tests/test_owned.py::test_owned'} for key in props['change']['enum']]})
        else:
            (request.cwd/'value.py').write_text('VALUE = 1\n')
            reply = 'Fixed\nCOMMIT_MESSAGE: Repair owned value'
        return AgentResult(True, ['local-test-transport'], request.output_path, summary=reply, stdout=reply)
    monkeypatch.setattr(Orchestrator, '_call_with_failover_owned', agent)
    original = session_candidate.record_receipt
    def interrupt(session, state):
        original(session, state)
        raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr(session_candidate, 'record_receipt', interrupt)
        session = Session(Orchestrator(root), mode='fix', auto_approve=True)
        with pytest.raises(KeyboardInterrupt):
            session._phase_fix_execute(load_session_state(root, child.session_id))
    child = load_session_state(root, child.session_id)
    session._current_state = child
    # The old verifier rejects four candidates around one bounded diagnosis.
    # These are real kernel transitions; no direct budget mutation is used.
    for i in range(4):
        perform(session, 'verify', 'old-' + str(i), lambda: {'ok': False},
                lambda r: (OutcomeKind.CANDIDATE_REJECTED, 'old verifier rejected'))
        if i == 1:
            perform(session, 'diagnose', 'bounded', lambda: {'cause': 'old verifier'},
                    lambda r: (OutcomeKind.SUCCESS, 'diagnosed'), model=True)
    session._block_execution_binding(child, KernelError('no_progress', 'No verified progress'), 'kernel_no_progress')
    snapshot = graph.load(snapshot.workflow_id)
    handoff = graph.load_handoff(handoff.handoff_id)
    graph.record_result(snapshot, handoff, status='blocked', result={'status': 'blocked',
        'resolution': 'kernel_no_progress', 'session_id': child.session_id})
    graph.consume_result(snapshot, handoff, operation_id='retained-return')
    if engine_return:
        handoff = graph.prepare_handoff(snapshot, parent=handoff.parent, target='fix', goal=child.goal,
            reason='Completed engine repair returned the original stopped child',
            payload={'target_repository': str(tmp_path/'engine'),
                     'issue_seed': {'failed_handoff_id': handoff.handoff_id}})
        graph.record_result(snapshot, handoff, status='blocked', result={'status': 'blocked',
            'resolution': 'kernel_no_progress', 'session_id': child.session_id})
        graph.consume_result(snapshot, handoff, operation_id='engine-return')
    parent = load_session_state(root, 'parent')
    coordinator = WorkflowCoordinator(Orchestrator(root), auto_approve=True)
    coordinator._preserve_engine_resume_budget = True
    coordinator._apply_child_result(parent, handoff)
    # Match the old release's already-consumed return and five empty retries.
    parent.status, parent.resolution = 'failed', 'agent_errors_exhausted'
    parent.consecutive_agent_errors = 5
    save_session_state(root, parent)
    return root, store, child, calls


@pytest.mark.parametrize('entrypoint', ['session', 'workflow'])
@pytest.mark.parametrize('engine_return', [False, True])
def test_public_resume_reverifies_retained_child_before_parent_or_writer(tmp_path, monkeypatch, entrypoint, engine_return):
    root, store, child, calls = stopped_candidate(tmp_path, monkeypatch, engine_return=engine_return)
    stream = store.binding(root, 'session:' + child.session_id)
    before = deepcopy(store.load(stream)['budget'])
    assert before['stagnant'] == 2 and before['rediagnoses'] == 1
    receipt = deepcopy(child.candidate_custody['receipt'])
    parent_before = load_session_state(root, 'parent')
    store.set_meta('active_runtime', {'source': 'a'*64})
    with pytest.raises(ObservationBoundary):
        if entrypoint == 'session':
            Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
        else:
            WorkflowCoordinator(Orchestrator(root), auto_approve=True).resume_workflow(child.workflow_id)
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'completed', saved.resolution
    assert saved.candidate_custody['receipt'] == receipt
    assert calls == ['fix', 'review', 'collab']
    assert saved.current_attempt == child.current_attempt
    assert load_session_state(root, 'parent').consecutive_agent_errors == 0
    assert load_session_state(root, 'parent').attempt_epoch == parent_before.attempt_epoch
    assert saved.attempt_epoch == child.attempt_epoch
    assert store.replay(stream)['budget']['stagnant'] == 0
    assert store.load(stream)['budget']['implementations'] == before['implementations']


def test_failed_reverification_keeps_stagnation_and_never_calls_writer(tmp_path, monkeypatch):
    root, store, child, calls = stopped_candidate(tmp_path, monkeypatch)
    monkeypatch.setattr(Session, '_run_verify_owned', lambda *a: {
        'ok': False, 'retry_fix': True, 'reason': 'same real test still fails'})
    before = store.load(store.binding(root, 'session:' + child.session_id))['budget']
    for _ in range(2):
        state = Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
        assert state.status == 'blocked' and state.resolution == 'kernel_no_progress'
    after = store.load(store.binding(root, 'session:' + child.session_id))['budget']
    assert calls == ['fix']
    assert after['model_calls'] == before['model_calls']
    assert after['rediagnoses'] == 1 and after['stagnant'] >= 2


def test_retained_verification_is_invalidated_by_adopted_verifier(tmp_path, monkeypatch):
    from auto_agents import session_candidate
    root, store, child, _ = stopped_candidate(tmp_path, monkeypatch)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    session._current_state = child
    monkeypatch.setattr(session_candidate, 'recover_receipt', lambda *a: None)
    with session_candidate.execution_checkout(session, child):
        before = session_candidate.verification_identity(session, child)
        store.set_meta('active_runtime', {'source': 'a'*64})
        after = session_candidate.verification_identity(session, child)
        assert before != after
        assert after == session_candidate.verification_identity(session, child)
