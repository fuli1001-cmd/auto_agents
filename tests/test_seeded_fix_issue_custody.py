"""Routed fix scope survives a real private clone and a process restart."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.session_source import register_source
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_session_verification_ownership import project
from workflow_support import ObservationBoundary, parent_workflow


def private_seeded_fix(tmp_path, monkeypatch):
    root, first = project(tmp_path)
    store, snapshot, _ = parent_workflow(root, first)
    def first_agent(self, request):
        if request.purpose.startswith('collab'):
            raise ObservationBoundary()
        (request.cwd/'value.py').write_text('VALUE = 1\n')
        reply='Fixed\nCOMMIT_MESSAGE: Repair original value'
        request.output_path.parent.mkdir(parents=True,exist_ok=True)
        request.output_path.write_text(reply)
        return AgentResult(True,['fixture'],request.output_path,summary=reply,stdout=reply,returncode=0)
    monkeypatch.setattr(Orchestrator,'_call_with_failover',first_agent)
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root),mode='collab',auto_approve=True).resume('parent')
    parent=load_session_state(root,'parent')
    snapshot=store.load(parent.workflow_id)
    assert parent.candidate_custody
    command='python -m pytest -q tests/test_owned.py::test_owned'
    seed={'summary':'Repair the current startup isolation defect',
          'reproduction':['Startup incorrectly queues a historical project'],
          'constraints':['Keep paid receipts and original project identity'],
          'required_behavior':['Never enqueue unrelated historical work'],
          'verification_command':command,'verification_scope':{'mode':'focused_fix'}}
    handoff=store.prepare_handoff(snapshot,parent=snapshot.root,target='fix',goal=parent.goal,
        reason='current isolated startup repair',payload={'issue_seed':seed,
            'authorization_policy':parent.authorization_policy,
            'goal_execution_environment':parent.goal_execution_environment,'auto_approve':parent.auto_approve})
    register_source(root,parent,handoff);store.save_handoff(handoff)
    parent.active_handoff_id=handoff.handoff_id;parent.status='waiting_child';save_session_state(root,parent)
    coordinator=WorkflowCoordinator(Orchestrator(root),auto_approve=True)
    session=Session(coordinator.orch,mode='fix',auto_approve=True,coordinator=coordinator)
    with patch.object(coordinator,'_drive_session',side_effect=lambda _session,state,*_args,**_kw:state):
        child=coordinator.start_seeded_session(session,snapshot=snapshot,handoff=handoff)
    assert child.source_descriptor and child.fix_verify_command==command
    return root,child,command


@pytest.mark.parametrize('reply_kind',['correct','conflicting','legacy_conflicting'])
def test_private_classification_preserves_control_issue_and_command(tmp_path,monkeypatch,reply_kind):
    root,child,command=private_seeded_fix(tmp_path,monkeypatch)
    issue=root/'.auto-agents/state/sessions'/child.session_id/'issue.json'
    original=issue.read_bytes()
    observed=[]
    def agent(self,request):
        if request.purpose.startswith('collab'):
            raise ObservationBoundary()
        observed.append(request.cwd)
        assert request.cwd!=root
        assert 'Authoritative Routed Issue Brief' in request.prompt
        assert 'Startup incorrectly queues a historical project' in request.prompt
        assert 'Never enqueue unrelated historical work' in request.prompt
        assert command in request.prompt
        assert issue.read_bytes()==original
        if reply_kind=='legacy_conflicting':reply='GOAL_CLEAR\nFIX_VERIFY: npm test'
        else:reply='FIX_DISPOSITION v1: '+json.dumps({'decision':'fix','summary':'classified current defect',
            'verification_command':command if reply_kind=='correct' else 'npm test'})
        request.output_path.parent.mkdir(parents=True,exist_ok=True);request.output_path.write_text(reply)
        return AgentResult(True,['fixture'],request.output_path,summary=reply,stdout=reply,returncode=0)
    monkeypatch.setattr(Orchestrator,'_call_with_failover',agent)
    executed=[]
    def execute(session,state):
        prompt=session._build_fix_prompt(state,'')
        assert 'current startup isolation defect' in prompt
        executed.append(True);state.status='paused';session._save(state);return state
    monkeypatch.setattr(Session,'_phase_fix_execute',execute)
    try:
        Session(Orchestrator(root),mode='fix',auto_approve=True).resume(child.session_id)
    except ObservationBoundary:
        pass
    result=load_session_state(root,child.session_id)
    assert observed and issue.read_bytes()==original
    assert result.fix_verify_command==command
    if reply_kind=='correct':
        assert result.status=='paused' and executed
    else:
        assert result.status=='blocked' and not executed
        assert result.resolution=='verification_ownership'
    # A new Session instance must still read control authority after cloning.
    if reply_kind=='correct':
        result.status='conversing';save_session_state(root,result)
        try:
            Session(Orchestrator(root),mode='fix',auto_approve=True).resume(child.session_id)
        except ObservationBoundary:
            pass
        assert len(observed)==2 and observed[0]==observed[1]
        assert issue.read_bytes()==original


@pytest.mark.parametrize('change',['valid','canonical_issue','canonical_and_projection','handoff','writer','product','committed_product','receipt'])
def test_preimplementation_command_recovery_requires_three_consistent_sources(tmp_path,monkeypatch,change):
    from auto_agents.session_candidate import execution_checkout
    from auto_agents.session_issue import recover_classification_command
    from auto_agents.session_verification import bind_session,SessionOwnershipError
    from auto_agents.workflow_chain import WorkflowStore
    root,child,command=private_seeded_fix(tmp_path,monkeypatch)
    session=Session(Orchestrator(root),mode='fix',auto_approve=True)
    bind_session(session,child)
    with execution_checkout(session,child):pass
    child.fix_verify_command='npm test'
    child.status,child.resolution='blocked','verification_ownership'
    child.execution_log.append({'action':'fix_disposition','result':'fix'})
    if change=='handoff':
        store=WorkflowStore(root);handoff=store.load_handoff(child.parent_handoff_id)
        handoff.payload['issue_seed']['verification_command']='python -m pytest tests/other.py'
        store.save_handoff(handoff)
    elif change=='writer':child.execution_log.append({'action':'fix','attempt':1})
    elif change=='product':(Path(child.candidate_custody['checkout'])/'value.py').write_text('changed product')
    elif change=='committed_product':
        from auto_agents.session_candidate import _git
        checkout=Path(child.candidate_custody['checkout'])
        (checkout/'value.py').write_text('changed product')
        _git(checkout,'add','value.py')
        _git(checkout,'-c','user.name=Test','-c','user.email=test@example.com','commit','-qm','unrecorded implementation')
    elif change=='receipt':child.candidate_custody['receipt']={'fingerprint':'not eligible'}
    save_session_state(root,child)
    issue_path=root/'.auto-agents/state/sessions'/child.session_id/'issue.json'
    original_issue=issue_path.read_bytes()
    if change in {'canonical_issue','canonical_and_projection'}:
        from auto_agents.business_state import BusinessStore
        store=BusinessStore(root);relative='sessions/'+child.session_id+'/issue.json'
        old=store.get(relative)
        broken={**old,'decision':'fix','summary':'historical frontend issue',
                'verification_command':'npm test'}
        broken.pop('verification_scope',None)
        store.save(relative,broken,old.reference)
        if change=='canonical_and_projection':issue_path.write_text(json.dumps(broken))
    if change in {'valid','canonical_issue'}:
        assert recover_classification_command(session,child,check_only=True)
        assert child.fix_verify_command=='npm test' and child.status=='blocked'
        assert recover_classification_command(session,child)
        assert child.fix_verify_command==command and child.status=='conversing'
        assert child.execution_log[-1]['previous_command']=='npm test'
        assert not recover_classification_command(session,child)
        if change=='canonical_issue':
            from auto_agents.io_utils import read_json
            assert read_json(issue_path)['verification_command']==command
            assert issue_path.read_bytes()==original_issue
            evidence=next(row for row in child.execution_log if row['action']=='routed_issue_projection_recovery')
            assert evidence['previous_issue']['summary']=='historical frontend issue'
    else:
        with pytest.raises(SessionOwnershipError):recover_classification_command(session,child)
        assert child.fix_verify_command=='npm test' and child.status=='blocked'


@pytest.mark.parametrize('change',['valid','writer','canonical_and_projection'])
def test_parent_resume_rechecks_saved_engine_request_only_for_recoverable_child(tmp_path,monkeypatch,change):
    from auto_agents.business_state import BusinessStore
    from auto_agents.engine_fault import EngineFault,engine_root
    from auto_agents.session_candidate import execution_checkout
    from auto_agents.session_verification import bind_session
    from auto_agents.workflow_chain import WorkflowStore
    root,child,command=private_seeded_fix(tmp_path,monkeypatch)
    session=Session(Orchestrator(root),mode='fix',auto_approve=True)
    bind_session(session,child)
    with execution_checkout(session,child):pass
    child.fix_verify_command='npm test'
    child.status,child.resolution='blocked','verification_ownership'
    if change=='writer':child.execution_log.append({'action':'fix','attempt':1})
    save_session_state(root,child)
    issue_path=root/'.auto-agents/state/sessions'/child.session_id/'issue.json'
    business=BusinessStore(root);relative='sessions/'+child.session_id+'/issue.json'
    old=business.get(relative)
    broken={**old,'decision':'fix','verification_command':'npm test'}
    business.save(relative,broken,old.reference)
    if change=='canonical_and_projection':issue_path.write_text(json.dumps(broken))
    store=WorkflowStore(root);handoff=store.load_handoff(child.parent_handoff_id)
    snapshot=store.load(child.workflow_id)
    store.record_result(snapshot,handoff,status='blocked',result={'status':'blocked','resolution':'verification_ownership'})
    store.consume_result(snapshot,handoff,operation_id='blocked-return')
    parent=load_session_state(root,'parent')
    parent.status='executing';parent.active_handoff_id=''
    parent.last_child_result_ref=str(store.handoff_path(handoff.handoff_id))
    reply='ROUTE_WORKFLOW v1: '+json.dumps({'target':'fix','target_repository':str(engine_root()),
        'issue_seed':{'summary':'repair classification overwrite','verification_command':'python -m pytest'}})
    parent.conversation.append({'role':'agent','content':reply});save_session_state(root,parent)
    observed=[]
    def agent(self,request):
        assert request.purpose=='collab'
        assert 'Recovery eligibility was checked without changing the child' in request.prompt
        assert handoff.handoff_id in request.prompt
        observed.append(request.cwd)
        raise ObservationBoundary()
    monkeypatch.setattr(Orchestrator,'_call_with_failover',agent)
    if change=='valid':
        with pytest.raises(ObservationBoundary):
            Session(Orchestrator(root),mode='collab',auto_approve=True).resume('parent')
        assert observed and observed[0]!=root
        retained=load_session_state(root,'parent')
        assert any(row.get('action')=='engine_route_revalidated' for row in retained.execution_log)
        assert any(row.get('content')==reply for row in retained.conversation)
    else:
        with pytest.raises(EngineFault):
            Session(Orchestrator(root),mode='collab',auto_approve=True).resume('parent')
        assert not observed
    retained_child=load_session_state(root,child.session_id)
    assert retained_child.fix_verify_command=='npm test' and retained_child.status=='blocked'


@pytest.mark.parametrize('legacy_wrapper',[False,True])
def test_parent_resume_preserves_original_source_through_child_delivery_and_restart(tmp_path,monkeypatch,legacy_wrapper):
    """Parent route, resumed private writer, real checks, delivery, then restart."""
    from auto_agents.business_state import BusinessStore
    from auto_agents.local_io import atomic_json
    from auto_agents.session_candidate import execution_checkout
    from auto_agents.session_source import _identity
    from auto_agents.session_verification import bind_session,fingerprint
    from auto_agents.workflow_chain import WorkflowStore
    root,child,command=private_seeded_fix(tmp_path,monkeypatch)
    session=Session(Orchestrator(root),mode='fix',auto_approve=True)
    bind_session(session,child)
    with execution_checkout(session,child):pass
    original_source=dict(child.source_descriptor)
    child.fix_verify_command='npm test';child.status,child.resolution='blocked','verification_ownership'
    save_session_state(root,child)
    business=BusinessStore(root);relative='sessions/'+child.session_id+'/issue.json'
    old=business.get(relative)
    business.save(relative,{**old,'decision':'fix','verification_command':'npm test'},old.reference)
    store=WorkflowStore(root);original=store.load_handoff(child.parent_handoff_id)
    snapshot=store.load(child.workflow_id)
    store.record_result(snapshot,original,status='blocked',result={'status':'blocked','resolution':'verification_ownership'})
    store.consume_result(snapshot,original,operation_id='old-blocked-return')
    parent=load_session_state(root,'parent')
    parent.active_handoff_id='';parent.status='executing';parent.resolution=''
    parent.last_child_result_ref=str(store.handoff_path(original.handoff_id))
    parent.conversation.append({'role':'orchestrator','content':'Resume the same retained product repair.'})
    save_session_state(root,parent)
    if legacy_wrapper:
        wrapper=store.prepare_handoff(snapshot,parent=WorkflowRef('collab','parent'),target='resume',
            goal=parent.goal,reason='resume the original product repair',
            payload={'resume_handoff_id':original.handoff_id,'authorization_policy':parent.authorization_policy,
                'goal_execution_environment':parent.goal_execution_environment,'auto_approve':parent.auto_approve})
        minted=_identity(root,parent,wrapper);minted['source_id']=fingerprint(minted)
        atomic_json(root/'.auto-agents/state/sources'/(minted['source_id']+'.json'),minted)
        wrapper.payload['source_descriptor']=minted;store.save_handoff(wrapper)
        parent.active_handoff_id=wrapper.handoff_id;parent.status='blocked';parent.resolution='verification_ownership'
        save_session_state(root,parent)
    classifications=[];writers=[];returned=[]
    def agent(self,request):
        if request.purpose=='collab':
            current=load_session_state(root,child.session_id)
            if current.status=='completed':
                returned.append(request.cwd)
                assert (request.cwd/'startup-isolation.txt').read_text()=='retained repair'
                raise ObservationBoundary()
            assert not legacy_wrapper
            reply='ROUTE_WORKFLOW v1: '+json.dumps({'target':'resume','resume_handoff_id':original.handoff_id})
        elif request.purpose.endswith('_converse'):
            classifications.append(request.cwd)
            assert 'Startup incorrectly queues a historical project' in request.prompt
            assert command in request.prompt
            reply='FIX_DISPOSITION v1: '+json.dumps({'decision':'fix','summary':'original startup isolation repair',
                'verification_command':command})
        else:
            assert request.purpose=='fix'
            writers.append(request.cwd)
            (request.cwd/'startup-isolation.txt').write_text('retained repair')
            reply='Fixed\nCOMMIT_MESSAGE: Preserve resumed startup isolation repair'
        request.output_path.parent.mkdir(parents=True,exist_ok=True);request.output_path.write_text(reply)
        return AgentResult(True,['fixture'],request.output_path,summary=reply,stdout=reply,returncode=0)
    monkeypatch.setattr(Orchestrator,'_call_with_failover',agent)
    shared=(root/'value.py').read_bytes()
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root),mode='collab',auto_approve=True).resume('parent')
    result=load_session_state(root,child.session_id)
    assert result.status=='completed' and result.fix_verify_command==command
    assert result.source_descriptor==original_source and result.parent_handoff_id==original.handoff_id
    assert result.candidate_custody.get('receipt') and result.candidate_custody.get('delivered_revision')
    assert len(classifications)==len(writers)==len(returned)==1
    assert (root/'value.py').read_bytes()==shared and not (root/'startup-isolation.txt').exists()
    parent=load_session_state(root,'parent')
    assert parent.candidate_custody['consumed_delivery']['revision']==result.candidate_custody['delivered_revision']
    wrapper=store.load_handoff(Path(parent.last_child_result_ref).stem)
    assert wrapper.target=='resume' and 'source_descriptor' not in wrapper.payload
    assert store.resolve_handoff_chain(wrapper,workflow_id=child.workflow_id)[-1].handoff_id==original.handoff_id
    if legacy_wrapper:
        evidence=next(row for row in parent.execution_log if row.get('action')=='resume_source_registration_recovered')
        assert evidence['previous_source_descriptor']==minted
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root),mode='collab',auto_approve=True).resume('parent')
    assert len(writers)==1 and len(returned)==2


def test_legacy_resume_source_recovery_rejects_unregistered_or_conflicting_authority(tmp_path,monkeypatch):
    from copy import deepcopy
    from auto_agents.local_io import atomic_json
    from auto_agents.session_source import _identity,recover_resume_source
    from auto_agents.session_verification import fingerprint
    from auto_agents.workflow_chain import WorkflowStore
    root,child,_=private_seeded_fix(tmp_path,monkeypatch)
    parent=load_session_state(root,'parent')
    store=WorkflowStore(root);snapshot=store.load(child.workflow_id)
    wrapper=store.prepare_handoff(snapshot,parent=WorkflowRef('collab','parent'),target='resume',goal=parent.goal,
        reason='legacy continuation',payload={'resume_handoff_id':child.parent_handoff_id,
            'authorization_policy':parent.authorization_policy})
    minted=_identity(root,parent,wrapper);minted['source_id']=fingerprint(minted)
    registration=root/'.auto-agents/state/sources'/(minted['source_id']+'.json')
    atomic_json(registration,minted);wrapper.payload['source_descriptor']=minted;store.save_handoff(wrapper)
    parent.active_handoff_id=wrapper.handoff_id;parent.status='blocked';parent.resolution='verification_ownership'
    save_session_state(root,parent)
    assert recover_resume_source(root,parent,wrapper,check_only=True)
    before=parent.to_dict()
    changed=deepcopy(wrapper);changed.payload['source_descriptor']['tree']='0'*40
    assert not recover_resume_source(root,parent,changed)
    changed=deepcopy(wrapper);changed.payload['authorization_policy']={'mode':'manual'}
    assert not recover_resume_source(root,parent,changed)
    registration.unlink()
    assert not recover_resume_source(root,parent,wrapper)
    registration.write_text('corrupt registration')
    assert not recover_resume_source(root,parent,wrapper)
    atomic_json(registration,minted)
    child.source_descriptor['source_id']='other-source';save_session_state(root,child)
    assert not recover_resume_source(root,parent,wrapper)
    assert parent.to_dict()==before and store.load_handoff(wrapper.handoff_id).payload['source_descriptor']==minted
