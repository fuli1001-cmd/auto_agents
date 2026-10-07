"""Exact command custody survives conflicting guidance and rejected replies."""
import json
from pathlib import Path

import pytest

from auto_agents.config import load_session_state,save_session_state
from auto_agents.models import AgentResult,SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.session_candidate import execution_checkout
from auto_agents.session_issue import validate_classification,recover_classification_command
from auto_agents.session_verification import bind_session,SessionOwnershipError
from auto_agents.workflow_chain import WorkflowStore,IssueBriefBuilder
from test_seeded_fix_issue_custody import private_seeded_fix
from workflow_support import ObservationBoundary

COMMANDS=[
    './.conda/bin/python -m pytest -q tests/test_owned.py::test_owned',
    'conda run -p ./.conda python -m pytest -q tests/test_owned.py::test_owned',
    'PYTHONDONTWRITEBYTECODE=1 ./.conda/bin/python -m pytest -q -p no:cacheprovider tests/test_owned.py::test_owned',
]

def seeded_command(tmp_path,monkeypatch,command=COMMANDS[-1]):
    root,child,_=private_seeded_fix(tmp_path,monkeypatch)
    assert not child.verification_binding
    store=WorkflowStore(root);handoff=store.load_handoff(child.parent_handoff_id)
    handoff.payload['issue_seed']['verification_command']=command
    handoff.payload['verification_command']=command
    store.save_handoff(handoff)
    IssueBriefBuilder(root,child.session_id).materialize({**handoff.payload['issue_seed'],
        'source_handoff_id':handoff.handoff_id,'reported_goal':child.goal})
    child.fix_verify_command=command;save_session_state(root,child)
    return root,child,command


@pytest.mark.parametrize('command',COMMANDS)
@pytest.mark.parametrize('reply_kind',['exact','omit','conflicting'])
def test_bound_private_classification_has_one_command_contract_after_restart(tmp_path,monkeypatch,command,reply_kind):
    root,child,command=seeded_command(tmp_path,monkeypatch,command)
    issue=root/'.auto-agents/state/sessions'/child.session_id/'issue.json'
    original=issue.read_bytes();calls=[];executed=[]
    def agent(self,request):
        if request.purpose=='collab':raise ObservationBoundary()
        assert request.cwd!=root and command in request.prompt
        assert 'every Python-oriented verification_command must run inside it' not in request.prompt
        assert 'when choosing verification_command' not in request.prompt
        assert 'Current repository gate commands' not in request.prompt
        assert 'Omit verification_command from the disposition or copy it verbatim' in request.prompt
        assert 'Startup incorrectly queues a historical project' in request.prompt
        calls.append(request.cwd)
        disposition={'decision':'fix','summary':'original isolation defect'}
        if reply_kind=='exact':disposition['verification_command']=command
        elif reply_kind=='conflicting':disposition['verification_command']=command+' tests/foreign.py'
        reply='FIX_DISPOSITION v1: '+json.dumps(disposition)
        request.output_path.parent.mkdir(parents=True,exist_ok=True);request.output_path.write_text(reply)
        return AgentResult(True,['fixture'],request.output_path,summary=reply,stdout=reply,returncode=0)
    def execute(session,state):
        assert command in session._build_fix_prompt(state,'')
        executed.append(True);state.status='paused';session._save(state);return state
    monkeypatch.setattr(Orchestrator,'_call_with_failover',agent)
    monkeypatch.setattr(Session,'_phase_fix_execute',execute)
    for attempt in range(1 if reply_kind=='conflicting' else 2):
        try:
            Session(Orchestrator(root),mode='fix',auto_approve=True).resume(child.session_id)
        except ObservationBoundary:
            pass
        result=load_session_state(root,child.session_id)
        assert result.fix_verify_command==command and issue.read_bytes()==original
        if reply_kind=='conflicting':
            assert result.status=='blocked' and not executed
            assert result.execution_log[-1]['diagnostic']['proposed_command']==command+' tests/foreign.py'
        else:
            assert result.status=='paused' and executed
            if attempt==0:result.status='conversing';save_session_state(root,result)
    assert len(calls)==(1 if reply_kind=='conflicting' else 2)


def test_unbound_fix_still_chooses_repository_environment_and_gate_command(tmp_path,monkeypatch):
    from test_session import _make_project
    root=_make_project(str(tmp_path));orch=Orchestrator(root)
    orch.config.gates.commands=['conda run -p ./.conda python -m pytest -q tests']
    prompt=Session(orch,mode='fix')._build_converse_prompt(SessionState(session_id='unbound',mode='fix',goal='repair'))
    assert 'every Python-oriented verification_command must run inside it' in prompt
    assert 'when choosing verification_command' in prompt
    assert 'Current repository gate commands' in prompt


def rejected_child(tmp_path,monkeypatch):
    root,child,command=seeded_command(tmp_path,monkeypatch)
    session=Session(Orchestrator(root),mode='fix',auto_approve=True)
    bind_session(session,child)
    with execution_checkout(session,child):pass
    proposed=command+' tests/foreign.py'
    child.conversation.append({'role':'agent','content':'FIX_VERIFY: '+proposed})
    try:
        validate_classification(session,child,{'verification_command':proposed})
    except SessionOwnershipError as error:
        session._block_execution_binding(child,error,'verification_ownership')
    else:raise AssertionError('conflicting proposal was accepted')
    return root,child,command,session


@pytest.mark.parametrize('change',['valid','writer','product','command','source','other_failure'])
def test_rejected_classification_resume_keeps_command_and_requires_unchanged_preimplementation_authority(tmp_path,monkeypatch,change):
    root,child,command,session=rejected_child(tmp_path,monkeypatch)
    if change=='writer':child.execution_log.insert(0,{'action':'fix','attempt':1})
    elif change=='product':(Path(child.candidate_custody['checkout'])/'value.py').write_text('unreceipted product edit')
    elif change=='command':child.execution_log[-1]['diagnostic']['original_command']='unrelated-command'
    elif change=='source':child.source_descriptor['source_id']='changed-source'
    elif change=='other_failure':child.execution_log[-1]['result']='unrelated ownership blocker'
    save_session_state(root,child);before=child.to_dict()
    if change=='valid':
        assert recover_classification_command(session,child,check_only=True)
        assert child.to_dict()==before
        assert recover_classification_command(session,child)
        assert child.status=='conversing' and child.fix_verify_command==command
        assert child.current_attempt==before['current_attempt'] and child.max_attempts==before['max_attempts']
        assert child.execution_log[-1]['rejected_classification']['diagnostic']['proposed_command']==command+' tests/foreign.py'
    elif change in {'command','other_failure'}:
        assert not recover_classification_command(session,child)
        assert child.to_dict()==before
    else:
        with pytest.raises(SessionOwnershipError):recover_classification_command(session,child)
        assert child.to_dict()==before


def test_rejected_classification_through_resume_wrapper_returns_to_original_child_without_parent_model_retry(tmp_path,monkeypatch):
    from auto_agents.engine_fault import engine_root
    root,child,command,_=rejected_child(tmp_path,monkeypatch)
    store=WorkflowStore(root);snapshot=store.load(child.workflow_id)
    original=store.load_handoff(child.parent_handoff_id)
    store.record_result(snapshot,original,status='blocked',result={'status':'blocked','resolution':'verification_ownership'})
    store.consume_result(snapshot,original,operation_id='first-blocked-return')
    wrapper=store.prepare_handoff(snapshot,parent=original.parent,target='resume',goal=original.goal,
        reason='already resumed child',payload={'resume_handoff_id':original.handoff_id})
    store.bind_child(snapshot,wrapper,original.child)
    store.record_result(snapshot,wrapper,status='blocked',result={'status':'blocked','resolution':'verification_ownership'})
    store.consume_result(snapshot,wrapper,operation_id='classification-rejected-return')
    parent=load_session_state(root,'parent');parent.status='executing';parent.active_handoff_id=''
    parent.last_child_result_ref=str(store.handoff_path(wrapper.handoff_id))
    parent.conversation.append({'role':'agent','content':'ROUTE_WORKFLOW v1: '+json.dumps({
        'target':'fix','target_repository':str(engine_root()),'issue_seed':{'summary':'conflicting classification prompt'}})})
    save_session_state(root,parent);before_attempt=parent.current_attempt;calls=[]
    def agent(self,request):
        if request.purpose=='collab':
            if not calls:raise AssertionError('recovery consumed another parent diagnostic call')
            assert (request.cwd/'classification-recovery.txt').read_text()=='original repair'
            raise ObservationBoundary()
        calls.append(request.purpose)
        if request.purpose.endswith('_converse'):
            assert command in request.prompt
            assert 'every Python-oriented verification_command must run inside it' not in request.prompt
            reply='FIX_DISPOSITION v1: {"decision":"fix","summary":"original repair; command owned by controller"}'
        else:
            assert request.purpose=='fix'
            (request.cwd/'classification-recovery.txt').write_text('original repair')
            reply='Fixed\nCOMMIT_MESSAGE: Complete the original classification recovery'
        request.output_path.parent.mkdir(parents=True,exist_ok=True);request.output_path.write_text(reply)
        return AgentResult(True,['fixture'],request.output_path,summary=reply,stdout=reply,returncode=0)
    monkeypatch.setattr(Orchestrator,'_call_with_failover',agent)
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root),mode='collab',auto_approve=True).resume('parent')
    result=load_session_state(root,child.session_id)
    assert result.status=='completed' and result.fix_verify_command==command and result.current_attempt==1
    assert result.parent_handoff_id==original.handoff_id
    parent=load_session_state(root,'parent')
    assert parent.current_attempt==before_attempt+1  # Normal diagnosis after the delivered repair.
    assert calls==['fix_converse','fix']


def test_bound_command_rejects_launcher_target_argument_and_type_changes_before_mutation(tmp_path,monkeypatch):
    root,child,command,_=rejected_child(tmp_path,monkeypatch)
    session=Session(Orchestrator(root),mode='fix',auto_approve=True)
    before=child.to_dict()
    proposals=[command.replace('./.conda/bin/python','python'),
               'conda run -p ./.conda '+command,
               command.replace(' tests/test_owned.py::test_owned',''),
               command.replace(' -q ',' -vv '),None,0,False,[]]
    for proposed in proposals:
        with pytest.raises(SessionOwnershipError):
            validate_classification(session,child,{'verification_command':proposed})
        assert child.to_dict()==before
