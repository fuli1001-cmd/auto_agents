"""Pending decisions are visible, addressable and actually reach the model."""

import json
import pytest
from auto_agents.control import Store, ControlError
from auto_agents.control.engine import Engine
from auto_agents.control.cli import main
from auto_agents.control.interaction import pending_decisions, pending_questions, resolved_answers
from auto_agents.operator_inputs import OperatorInputStore, UserInputRequest
from auto_agents.config import save_project_config
from auto_agents.models import ProjectConfig
from test_control_engine import project, Worker


def test_parent_displays_child_question_and_resume_without_new_calls(tmp_path,monkeypatch,capsys):
    from auto_agents.control import cli
    root=project(tmp_path)
    config=ProjectConfig('fixture')
    save_project_config(root,config)
    store=Store(root)
    class Question(Worker):
        def run(self,ctx,phase,*a,**kw):
            if phase=='classify':
                return {'text':json.dumps({'decision':'need_user','question':'Confirm whether to add the recovery button'})}
            return super().run(ctx,phase,*a,**kw)
    actual=Engine
    worker=Question()
    monkeypatch.setattr(cli,'Engine',lambda *a,**kw:actual(*a,**kw,transport=worker))
    waiting=actual(root,store,config,transport=worker,auto_approve=True,print_fn=lambda *a:None).start('collab','Set value')
    child=store.work(waiting['context']['child'])
    used=store.workflow(waiting['workflow'])['calls']
    assert main(['collab','--project',str(root),'--session',waiting['id'],'--auto-approve'])==0
    output=capsys.readouterr().out
    assert 'Confirm whether to add the recovery button' in output
    assert 'auto-agents answer' in output and '--request-id' in output and '--no-resume' in output
    assert child['id'] in output and '--session '+waiting['id'] in output
    assert store.workflow(waiting['workflow'])['calls']==used


def test_all_parallel_decisions_are_displayed_and_secret_input_uses_environment(tmp_path):
    store=Store(tmp_path)
    parent=store.create_workflow('collab','Goal','source')
    children=[]
    for _ in range(2):
        children.append(store.child(parent,'fix','Child','source'))
        parent=store.work(parent['id'])
    for index,child in enumerate(children):
        child=store.transition(child,'RUNNING')
        request={'key':'choice.'+str(index),'kind':'secret' if index else 'text',
            'question':'Enter credential' if index else 'Confirm choice','purpose':'test','why_required':'needed'}
        requests,_=pending_questions(tmp_path,child,request)
        store.transition(child,'WAITING',context={'waiting_for':'user','input_requests':requests})
    parent=store.transition(parent,'WAITING',context={'waiting_for':'children','children':[c['id'] for c in children]})
    decisions=pending_decisions(store,parent)
    assert len(decisions)==2
    assert {row['work_id'] for row in decisions}=={c['id'] for c in children}
    assert '--from-env AUTO_AGENTS_ANSWER' in next(row['command'] for row in decisions if 'credential' in row['question'])


def test_plain_answer_is_projected_but_secret_and_changed_revision_are_not(tmp_path):
    inputs=OperatorInputStore(tmp_path)
    answers=[]
    for key,kind,value in [('choice.answer','text','Use the approved prototype'),('account.key','secret','fixture-secret-value')]:
        request=UserInputRequest.from_dict({'key':key,'kind':kind,'question':'Input','purpose':'test','why_required':'test'})
        record=inputs.save_answer(request,value)
        answers.append({'key':key,'revision':record['revision'],'answer_ref':key+':'+str(record['revision'])})
    projected=resolved_answers(tmp_path,answers)
    assert projected[0]['value']=='Use the approved prototype'
    assert projected[1]['available'] is True and 'value' not in projected[1]
    assert 'fixture-secret-value' not in json.dumps(projected)
    request=UserInputRequest.from_dict({'key':'choice.answer','kind':'text','question':'Input','purpose':'test','why_required':'test'})
    inputs.save_answer(request,'Changed answer')
    with pytest.raises(ControlError,match='Recorded operator answer changed'):
        resolved_answers(tmp_path,answers)


def test_empty_or_already_answered_questions_do_not_create_an_unanswerable_wait(tmp_path):
    store=Store(tmp_path)
    work=store.create_workflow('fix','Goal','source')
    for question in (None,[], ''):
        with pytest.raises(ControlError,match='question is empty'):
            pending_questions(tmp_path,work,question)
    request=UserInputRequest.from_dict({'key':'choice.ready','kind':'text','question':'Input','purpose':'test','why_required':'test'})
    OperatorInputStore(tmp_path).save_answer(request,'Provided')
    engine=Engine(tmp_path,store,ProjectConfig('test'),transport=Worker(),print_fn=lambda *a:None)
    with pytest.raises(ControlError,match='already available'):
        engine.wait_user(work,request.to_dict(),None)
    assert store.work(work['id'])['status']=='READY'


def test_native_answer_roundtrip_reaches_same_root_and_preserves_call_limit(tmp_path,monkeypatch):
    import os,subprocess,sys
    from test_watch_invocation import native_fixture, ENGINE
    from auto_agents.control.workspace import git
    from auto_agents_watch.runner import Runner
    from auto_agents_watch.store import Store as WatchStore

    root=native_fixture(tmp_path)
    (root/'fixture_provider.py').write_text('''import json,os,sys
prompt=sys.stdin.read()
owned=json.loads(prompt.split('Owned inputs:\\n',1)[1].split('\\n\\n',1)[0])
answers=owned.get('operator_answers',[])
if answers:
 assert answers[-1]['value']=='Use the approved prototype'
 result={'decision':'pass','test_changes_valid':True,'reason':'The fixture decision is verified'} if os.environ['AUTO_AGENTS_STAGE']=='review' else {'decision':'not_bug','reason':'The explicit fixture decision was received'}
else:result={'decision':'need_user','question':'Which design should be used?'}
print(json.dumps(result))
''')
    git(root,'add','fixture_provider.py')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','answer fixture')
    monkeypatch.delenv('AUTO_AGENTS_ACCEPTANCE_BUDGET',raising=False)
    watch=WatchStore(tmp_path/'watch')
    job=Runner(watch).start([sys.executable,'-m','auto_agents','fix','--project',str(root),
        '--provider','fixture','--goal','Confirm the fixture decision','--max-provider-calls','4'],ENGINE)
    assert job['waiting_for']=='user' and job['model_calls']==0
    business=Store(root)
    before=business.work(job['root_id'])
    request=before['context']['input_requests'][0]
    environment={**os.environ,'PYTHONPATH':str(ENGINE/'src'),'AUTO_AGENTS_NO_SUPERVISOR':'1'}
    answer=subprocess.run([sys.executable,'-m','auto_agents','answer','--project',str(root),
        '--session',before['id'],'--request-id',request['request_id'],
        '--value','Use the approved prototype','--no-resume'],env=environment,cwd=ENGINE,
        capture_output=True,text=True,timeout=30)
    assert answer.returncode==0,answer.stdout+answer.stderr
    assert business.work(before['id'])['calls']==before['calls']==1
    result=Runner(watch).start([sys.executable,'-m','auto_agents','fix','--project',str(root),
        '--provider','fixture','--session',before['id']],ENGINE)
    assert result['id']==job['id'] and result['state']=='DONE',result.get('reason')
    assert not result.get('waiting_for') and result['model_calls']==0
    assert business.work(before['id'])['calls']==3
    assert business.work(before['id'])['max_calls']==before['max_calls']
    assert business.workflow(before['workflow'])['max_calls']==4


def test_changed_answer_can_be_explicitly_reconciled_without_stale_references(tmp_path,capsys):
    root=project(tmp_path)
    config=ProjectConfig('fixture');save_project_config(root,config)
    store=Store(root)
    class Question(Worker):
        def run(self,ctx,phase,*a,**kw):
            return {'text':json.dumps({'decision':'need_user','question':'Choose the design'})}
    engine=Engine(root,store,config,transport=Question(),print_fn=lambda *a:None)
    waiting=engine.start('fix','Choose design',max_calls=4)
    request=waiting['context']['input_requests'][0]
    args=['answer','--project',str(root),'--session',waiting['id'],'--request-id',request['request_id'],'--no-resume']
    assert main([*args,'--value','Original decision'])==0
    inputs=OperatorInputStore(root)
    typed=UserInputRequest.from_dict(request)
    inputs.save_answer(typed,'External replacement')
    blocked=engine.resume(waiting['id'])
    assert blocked['status']=='BLOCKED' and blocked['failure']['code']=='operator_input_changed'
    calls=blocked['calls']
    assert main([*args,'--value','Confirmed replacement'])==0
    capsys.readouterr()
    ready=store.work(waiting['id'])
    assert ready['status']=='READY' and not ready['failure'] and ready['calls']==calls
    assert len(ready['context']['operator_answers'])==1
    assert resolved_answers(root,ready['context']['operator_answers'])[0]['value']=='Confirmed replacement'


def test_reconciling_a_child_answer_through_root_selects_only_the_leaf(tmp_path,capsys):
    root=project(tmp_path)
    save_project_config(root,ProjectConfig('fixture'))
    store=Store(root)
    parent=store.create_workflow('collab','Original goal','source')
    child=store.child(parent,'fix','Child goal','source')
    parent=store.work(parent['id'])
    request=UserInputRequest.from_dict({'key':'decision.child','kind':'text','question':'Choose',
        'purpose':'test','why_required':'needed'})
    record=OperatorInputStore(root).save_answer(request,'Old value')
    child=store.transition(child,'RUNNING')
    failure={'code':'operator_input_changed','category':'operator','message':'changed','details':{'key':request.key}}
    child=store.transition(child,'BLOCKED',failure=failure,context={'question':[request.to_dict()],
        'operator_answers':[{'key':request.key,'revision':record['revision']}]})
    store.transition(parent,'BLOCKED',failure={**failure,'child_id':child['id']})
    assert main(['answer','--project',str(root),'--session',parent['id'],
        '--request-id',request.request_id,'--value','Confirmed value','--no-resume'])==0
    capsys.readouterr()
    assert store.work(child['id'])['status']=='READY'
    assert resolved_answers(root,store.work(child['id'])['context']['operator_answers'])[0]['value']=='Confirmed value'


def test_request_id_selects_one_parallel_question_through_parent(tmp_path,capsys):
    root=project(tmp_path);save_project_config(root,ProjectConfig('fixture'))
    store=Store(root);parent=store.create_workflow('collab','Parent goal','source')
    children=[]
    for _ in range(2):
        child=store.child(parent,'fix','Child','source');parent=store.work(parent['id'])
        request=UserInputRequest.from_dict({'key':'decision.'+child['id'],'kind':'text',
            'question':'Choose','purpose':'test','why_required':'needed'})
        child=store.transition(child,'RUNNING')
        child=store.transition(child,'WAITING',context={'waiting_for':'user','input_requests':[request.to_dict()]})
        children.append((child,request))
    store.transition(parent,'WAITING',context={'waiting_for':'children','children':[child['id'] for child,request in children]})
    chosen,request=children[0]
    assert main(['answer','--project',str(root),'--session',parent['id'],'--request-id',request.request_id,
        '--value','My decision','--no-resume'])==0
    capsys.readouterr()
    assert store.work(chosen['id'])['status']=='READY'
    assert store.work(children[1][0]['id'])['status']=='WAITING'
    assert store.workflow(parent['workflow'])['calls']==0
