"""Native entrypoints use the durable authority, with no remote model calls."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from auto_agents.config import save_session_state, load_session_state
from auto_agents.models import AgentRequest, AgentResult, SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.recovery import KernelStore, KernelError
from auto_agents.recovery.authority import activate_project, read_projection, write_projection, export_snapshot
from auto_agents.recovery.migration import apply_project, inspect_project
from auto_agents.recovery.native import provider
from test_session_verification_ownership import project


def activate(root, control, monkeypatch):
    store = KernelStore(control)
    apply_project(store, inspect_project(root))
    store.set_meta('mode', 'active')
    store.set_meta('activation', {'projects': [str(root.resolve())]})
    store.set_meta('active_runtime', {'source': 'f'*64, 'commit': 'fixture'})
    activate_project(store, root)
    monkeypatch.setenv('AUTO_AGENTS_RECOVERY_CONTROL', str(control))
    return store


def test_parallel_root_cause_roles_do_not_claim_ambient_business_operations(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from auto_agents.config import load_run_state
    root, child = project(tmp_path)
    store = activate(root, tmp_path/'control', monkeypatch)
    orch = Orchestrator(root)
    orch._invocation_context = {'command': 'collab', 'session_id': child.session_id}
    before = store.status()
    unrelated = load_run_state(root).to_dict()
    barrier = Barrier(2)
    def execute(request):
        barrier.wait(timeout=10)
        assert request.purpose == 'diagnosis'
        assert 'Controller task contract' not in request.prompt
        return AgentResult(True, ['fixture'], request.output_path, summary='Evidence inspected')
    monkeypatch.setattr(orch, '_call_with_failover_owned', execute)
    def diagnose(role):
        request = AgentRequest('self_repair_' + role, 'medium', 'Inspect the stopped session',
            root, tmp_path/role, purpose='diagnosis', attempt_id='root-cause-' + role,
            sandbox_mode='read-only', record_execution_incidents=False)
        return orch._call_with_failover(request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(result.ok for result in pool.map(diagnose, ['investigator', 'reviewer']))
    assert store.status() == before
    assert load_run_state(root).to_dict() == unrelated


def test_amendment_review_does_not_become_a_parent_business_review(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    store = activate(root, tmp_path/'control', monkeypatch)
    before = store.status()
    request = AgentRequest('proof_review', 'medium', 'Review amended assertions', root, tmp_path/'reply',
        purpose='proof_review', sandbox_mode='read-only', logical_call_id='proof-review:sealed:1',
        usage_context={'workflow_kind': 'proof_review', 'subject_id': child.session_id})
    seen = []
    def execute(bound):
        seen.append(bound)
        return AgentResult(True, ['fixture-review'], bound.output_path, summary='reviewed')
    assert provider(Orchestrator(root), request, execute).ok
    assert seen == [request]
    assert store.status() == before


@pytest.mark.parametrize('code', ['no_progress', 'outcome_unknown', 'stale_transition'])
def test_collab_kernel_stop_preserves_reason_without_agent_error_retries(tmp_path, monkeypatch, code):
    from auto_agents.session import Session
    from test_session import _confirm_collab_state
    root, state = project(tmp_path)
    state.mode, state.status = 'collab', 'executing'
    _confirm_collab_state(state)
    save_session_state(root, state)
    store = activate(root, tmp_path/'control', monkeypatch)
    state = load_session_state(root, state.session_id)
    session = Session(Orchestrator(root), mode='collab', auto_approve=True)
    calls = []
    def stopped(*args):
        calls.append(True)
        raise KernelError(code, 'Retain the original candidate', command_id='retained-command')
    monkeypatch.setattr(session, '_call_agent', stopped)
    monkeypatch.setattr(session, '_restore_collab_mutations',
                        lambda *a: pytest.fail('uncertain kernel effects must not be rolled back'))
    result = session._phase_collab_loop(state)
    assert result.status == 'blocked' and result.resolution == 'kernel_' + code
    assert len(calls) == 1 and result.consecutive_agent_errors == 0
    assert not any(row['action'] == 'agent_error' for row in result.execution_log)
    saved = load_session_state(root, state.session_id)
    assert saved.execution_log[-1]['diagnostic']['command_id'] == 'retained-command'
    from auto_agents.controlled_failure import capture
    assert capture(saved).evidence['reason'] == 'Retain the original candidate'


def test_provider_resolve_kernel_stop_does_not_retry_or_restore_unknown_effects(tmp_path, monkeypatch):
    from auto_agents.session import Session
    from test_session import _make_provider_blocked_project
    root, _ = _make_provider_blocked_project(str(tmp_path))
    state = SessionState('provider-stop', mode='provider_resolve', status='executing',
                         goal='Resolve the retained provider reference')
    save_session_state(root, state)
    activate(root, tmp_path/'control', monkeypatch)
    state = load_session_state(root, state.session_id)
    session = Session(Orchestrator(root), mode='provider_resolve')
    calls = []
    def stopped(*args):
        calls.append(True)
        raise KernelError('outcome_unknown', 'Original operation requires reconciliation')
    monkeypatch.setattr(session, '_call_agent', stopped)
    monkeypatch.setattr(session, '_restore_provider_artifacts',
                        lambda *a: pytest.fail('unknown effects must remain available for reconciliation'))
    result = session._phase_provider_resolve_execute(state)
    assert result.status == 'blocked' and result.resolution == 'kernel_outcome_unknown'
    assert len(calls) == 1 and result.consecutive_agent_errors == 0


@pytest.mark.parametrize('kind,purpose', [('collab','collab'), ('fix','fix'), ('provider_resolve','provider_resolve'), ('run','implement')])
def test_all_native_provider_modes_reserve_once_and_replay(tmp_path, monkeypatch, kind, purpose):
    root, child = project(tmp_path)
    if kind == 'run':
        from auto_agents.models import RunState
        from auto_agents.config import save_run_state
        (root/'spec.md').write_text('Repair the existing value')
        save_run_state(root, RunState(run_id='run-one', resume_context={'spec_file': 'spec.md'}))
        native = 'run-one'
    else:
        child.mode = kind
        save_session_state(root, child)
        native = child.session_id
    store = activate(root, tmp_path/'control', monkeypatch)
    orch = Orchestrator(root)
    request = AgentRequest('implement','medium','Original request',root,tmp_path/'reply',
        purpose=purpose,attempt_id='call-one',usage_context={'workflow_kind': kind, 'subject_id': native})
    calls = []
    def execute(bound):
        calls.append(bound)
        assert 'Controller task contract' in bound.prompt
        assert 'Repair the existing value' in bound.prompt
        return AgentResult(True, ['fixture'], bound.output_path, summary='Done')
    assert provider(orch, request, execute).ok
    assert provider(Orchestrator(root), request, execute).ok
    assert len(calls) == 1
    stream = store.binding(root, ('run:' if kind == 'run' else 'session:') + native)
    state = store.replay(stream)
    assert state['budget']['model_calls'] == 1
    assert all(c['status'] == 'finished' for c in state['commands'].values())


def test_projection_authority_survives_missing_view_and_exports_hydrated_snapshot(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    child.execution_log = [{'preimages': {'large': 'x'*100000}}]
    save_session_state(root, child)
    store = activate(root, tmp_path/'control', monkeypatch)
    path = root/'.auto-agents/state/sessions/owned-child/session_state.json'
    state = load_session_state(root, child.session_id)
    state.current_attempt += 1
    save_session_state(root, state)
    assert path.stat().st_size < 1000
    path.unlink()
    assert load_session_state(root, child.session_id).current_attempt == 1
    # Snapshot export must enumerate authoritative records even if a view was lost.
    export_snapshot(root, tmp_path/'snapshot')
    target = tmp_path/'snapshot/.auto-agents/state/sessions/owned-child/session_state.json'
    assert json.loads(target.read_text())['execution_log'] == child.execution_log


def test_real_fix_resumes_and_delivers_under_kernel(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    store = activate(root, tmp_path/'control', monkeypatch)
    calls = []
    def local(self, request):
        calls.append(request)
        if request.purpose == 'review':
            props = request.response_schema['properties']['change_coverage']['items']['properties']
            checks = props['requirement']['enum'][:-1]
            text = json.dumps({'decision':'APPROVE','findings':[],
                'coverage':[{'requirement':check,'nodes':['tests/test_owned.py::test_owned']} for check in checks],
                'change_coverage':[{'change':key,'requirement':checks[0],'reason':'Original task','evidence':'verified test'}
                                   for key in props['change'].get('enum',[])]})
            request.output_path.write_text(text)
            return AgentResult(True,['fixture-reviewer'],request.output_path,summary=text)
        (request.cwd/'value.py').write_text('VALUE = 1\n')
        text = 'Fixed\nCOMMIT_MESSAGE: Repair value'
        request.output_path.write_text(text)
        return AgentResult(True, ['fixture'], request.output_path, summary=text, stdout=text)
    monkeypatch.setattr(Orchestrator, '_call_with_failover_owned', local)
    from auto_agents.session import Session
    result = Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
    assert result.status == 'completed', result.to_dict()
    assert result.candidate_custody['delivered_revision']
    assert len(calls) == 2
    again = Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
    assert again.status == 'completed'
    assert len(calls) == 2
    stream = store.binding(root, 'session:' + child.session_id)
    state = store.replay(stream)
    assert {'implement','verify','review','deliver'} <= {c['phase'] for c in state['commands'].values()}


def test_stale_session_object_cannot_borrow_a_later_read_version(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    activate(root, tmp_path/'control', monkeypatch)
    first = load_session_state(root, child.session_id)
    second = load_session_state(root, child.session_id)
    second.current_attempt = 1
    save_session_state(root, second)
    load_session_state(root, child.session_id)
    first.current_attempt = 2
    with pytest.raises(KernelError, match='changed since'): save_session_state(root, first)
    assert load_session_state(root, child.session_id).current_attempt == 1


def test_native_restart_reuses_an_undispatched_reservation(tmp_path,monkeypatch):
    from auto_agents.recovery.executor import Executor
    root, child = project(tmp_path)
    store = activate(root,tmp_path/'control',monkeypatch)
    request = AgentRequest('implement','medium','Repair existing value',root,tmp_path/'first',purpose='fix',
        attempt_id='first',usage_context={'workflow_kind':'fix','subject_id':child.session_id})
    calls = []
    def execute(bound):
        calls.append(bound)
        return AgentResult(True,['fixture'],bound.output_path,summary='Done')
    with monkeypatch.context() as patch:
        def crash(*args): raise SystemExit('before dispatch')
        patch.setattr(Executor,'execute',crash)
        with pytest.raises(SystemExit): provider(Orchestrator(root),request,execute)
    assert not calls
    retry = replace(request,attempt_id='second',output_path=tmp_path/'second')
    assert provider(Orchestrator(root),retry,execute).ok
    stream = store.binding(root,'session:' + child.session_id)
    assert store.replay(stream)['budget']['model_calls'] == 1
    assert len(calls) == 1


def test_runtime_change_reverifies_but_never_repeats_a_completed_model_effect(tmp_path,monkeypatch):
    from auto_agents.recovery.native import perform
    from auto_agents.recovery import OutcomeKind
    root, child = project(tmp_path)
    store = activate(root,tmp_path/'control',monkeypatch)
    orch = Orchestrator(root)
    usage = {'workflow_kind':'fix','subject_id':child.session_id}
    request = AgentRequest('implement','medium','Repair value',root,tmp_path/'reply',purpose='fix',
                           attempt_id='fixed-operation',usage_context=usage)
    calls, checks = [], []
    def execute(bound):
        calls.append(bound)
        return AgentResult(True,['fixture'],bound.output_path,summary='Done')
    def verify():
        checks.append(True)
        return {'ok':True}
    for source in ('f'*64,'a'*64):
        store.set_meta('active_runtime',{'source':source})
        assert provider(orch,request,execute).ok
        perform(orch,'verify','same-candidate',verify,lambda r:(OutcomeKind.SUCCESS,'Checked'),usage=usage)
    assert len(calls) == 1
    assert len(checks) == 2


def test_new_engine_route_uses_kernel_authority_without_legacy_registration(tmp_path,monkeypatch):
    from auto_agents import repair_client
    root, child = project(tmp_path)
    store = activate(root,tmp_path/'control',monkeypatch)
    engine_root = Path(repair_client.__file__).resolve().parents[2]
    (store.root/'operator.json').write_text(json.dumps({'source_root':str(engine_root)}))
    monkeypatch.delenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED',raising=False)
    orch = Orchestrator(root)
    orch._invocation_context = {'session_id':child.session_id,'auto_approve':True,'command':'fix'}
    route = {'target_repository':str(engine_root),'issue_seed':{'summary':'Observed engine defect'}}
    with pytest.raises(repair_client.EngineRepairRequired) as captured:
        repair_client.engine_route(orch,route)
    triage = repair_client.triage_engine_request(orch,root,captured.value)
    assert triage.decision.eligible, triage.reason
    assert store.load(store.binding(root,'session:' + child.session_id))['budget']['model_calls'] == 0


def test_control_witness_survives_hydrated_diagnostic_export(tmp_path,monkeypatch):
    from auto_agents.repair_v2.scope import witnesses
    root, child = project(tmp_path)
    activate(root,tmp_path/'control',monkeypatch)
    state = load_session_state(root,child.session_id)
    state.execution_log = [{'preimages':{'large':'x'*100000}}]
    save_session_state(root,state)
    relative = '.auto-agents/state/sessions/' + child.session_id + '/session_state.json'
    refs = witnesses([{'origin':'target','path':relative}],root,tmp_path/'engine')
    export_snapshot(root,tmp_path/'snapshot')
    assert witnesses(refs,tmp_path/'snapshot',tmp_path/'engine') == refs


def test_child_creation_keeps_goal_and_identity_after_interruption(tmp_path,monkeypatch):
    from auto_agents.workflow_chain import WorkflowStore, WorkflowRef
    from auto_agents.workflow_runtime import WorkflowCoordinator
    from auto_agents.session import Session
    import auto_agents.workflow_runtime as workflow_runtime
    root, parent = project(tmp_path)
    store = activate(root,tmp_path/'control',monkeypatch)
    graph = WorkflowStore(root)
    snapshot = graph.create_root(WorkflowRef('fix',parent.session_id))
    handoff = graph.prepare_handoff(snapshot,parent=snapshot.root,target='fix',goal='Repair the existing value',
        reason='Owned child',payload={'auto_approve':True,'issue_seed':{'summary':'Classified child defect'}})
    coordinator = WorkflowCoordinator(Orchestrator(root),auto_approve=True)
    session = Session(coordinator.orch,mode='fix',auto_approve=True,coordinator=coordinator)
    monkeypatch.setattr(coordinator,'_drive_session',lambda session,state,*a,**kw:state)
    with monkeypatch.context() as patch:
        def interrupted(*a,**kw): raise SystemExit('after retained child identity, before state publication')
        patch.setattr(workflow_runtime,'save_session_state',interrupted)
        with pytest.raises(SystemExit): coordinator.start_seeded_session(session,snapshot=snapshot,handoff=handoff)
    saved = graph.load_handoff(handoff.handoff_id)
    identity = saved.payload['child_session_id']
    child = coordinator.start_seeded_session(session,snapshot=snapshot,handoff=saved)
    assert child.session_id == identity
    assert child.parent_handoff_id == saved.handoff_id
    assert store.binding(root,'session:' + identity) == store.binding(root,'session:' + parent.session_id)


def test_kernel_provider_turn_does_not_retry_inside_legacy_smart_recovery(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from auto_agents.models import AgentTermination
    root, child = project(tmp_path)
    store = activate(root,tmp_path/'control',monkeypatch)
    orch = Orchestrator(root)
    calls = []
    def run(request):
        calls.append(request)
        return AgentResult(False,['fixture'],request.output_path,stderr='transport disconnected',
            returncode=1,termination=AgentTermination('tool_stalled'))
    orch.adapter = SimpleNamespace(available=lambda:True,run=run)
    request = AgentRequest('implement','medium','Repair value',root,tmp_path/'reply',purpose='fix',attempt_id='one',
        usage_context={'workflow_kind':'fix','subject_id':child.session_id})
    with pytest.raises(KernelError) as error: orch._call_with_failover(request)
    assert error.value.code == 'outcome_unknown'
    assert len(calls) == 1
    stream = store.binding(root,'session:' + child.session_id)
    assert store.load(stream)['budget']['model_calls'] == 1


def test_workflow_event_and_snapshot_commit_before_optional_views(tmp_path,monkeypatch):
    from auto_agents.workflow_chain import WorkflowStore, WorkflowRef
    root, child = project(tmp_path)
    activate(root,tmp_path/'control',monkeypatch)
    graph = WorkflowStore(root)
    snapshot = graph.create_root(WorkflowRef('fix',child.session_id))
    original = KernelStore.bind
    def interrupted(self,project,name,stream):
        if name.startswith('event:'): raise SystemExit('after atomic journal commit')
        return original(self,project,name,stream)
    with monkeypatch.context() as patch:
        patch.setattr(KernelStore,'bind',interrupted)
        with pytest.raises(SystemExit): graph.append_event(snapshot,'operation_intent',operation_id='one')
    assert graph.load(snapshot.workflow_id).event_sequence == 2
    assert [row['operation_id'] for row in graph.events(snapshot.workflow_id)][-1] == 'one'
    export_snapshot(root,tmp_path/'snapshot')
    exported = WorkflowStore(tmp_path/'snapshot')
    assert exported.load(snapshot.workflow_id).event_sequence == 2
    assert exported.events(snapshot.workflow_id)[-1]['operation_id'] == 'one'
