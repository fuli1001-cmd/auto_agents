"""Launch and ownership checks across the installed process boundary, without accounts."""

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from auto_agents.control import Store as BusinessStore
from auto_agents.config import save_project_config
from auto_agents.models import ProjectConfig, ProviderConfig, SMART_TIMEOUT_PROGRESS_PROTOCOL
from auto_agents.control.workspace import git
from auto_agents_watch.runner import Runner, ProjectBusyError, checkpoint_arguments
from auto_agents_watch.store import Store
from test_control_engine import project


ENGINE=Path(__file__).resolve().parents[1]


def native_fixture(tmp_path):
    root=project(tmp_path)
    (root/'fixture_provider.py').write_text('''import json,os,sys
prompt=sys.stdin.read()
phase=os.environ['AUTO_AGENTS_STAGE']
if phase in {'classify','diagnose','implement','review'}:
 assert 'repository_map' in prompt
if phase=='diagnose':result={'action':'need_user','question':'Confirm the fixture goal'}
elif phase=='classify':result={'decision':'need_user','question':'Confirm the fixture issue'}
else:result={'questions':['Confirm the fixture goal']}
print(json.dumps(result))
''')
    git(root,'add','fixture_provider.py')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','local provider fixture')
    config=ProjectConfig('fixture')
    config.repo_map.enabled=True
    config.providers={'fixture':ProviderConfig(kind='shell',binary=sys.executable,
        extra_args=['./fixture_provider.py'],progress_protocol=SMART_TIMEOUT_PROGRESS_PROTOCOL)}
    config.active_provider='fixture'
    save_project_config(root,config)
    return root


@pytest.mark.parametrize('command',['collab','fix','provider-resolve','provider-research','run'])
def test_native_launches_use_kernel_intent_and_preserve_waiting_budget(tmp_path,monkeypatch,command):
    monkeypatch.delenv('AUTO_AGENTS_ACCEPTANCE_BUDGET',raising=False)
    root=native_fixture(tmp_path)
    store=Store(tmp_path/'watch')
    argv=[sys.executable,'-m','auto_agents',command,'--project',str(root),'--provider','fixture',
        '--goal','Confirm the fixture goal','--auto-approve']
    runner=Runner(store,sandbox_factory=lambda *a,**k:pytest.fail('A waiting user triggered Docker'))
    first=runner.start(argv,ENGINE)
    assert first['state']=='STOPPED' and not first.get('fault'),first.get('reason')
    business=BusinessStore(root)
    first_work=business.work(first['root_id'])
    assert first_work['status']=='WAITING' and first_work['calls']==1
    second=Runner(store).start(argv,ENGINE)
    if command=='run':
        assert second['id']==first['id'] and second['root_id']==first['root_id']
        assert len(business.works())==1
    else:
        assert second['id']!=first['id'] and second['root_id']!=first['root_id']
        assert len(business.works())==2
    assert business.work(first['root_id'])['calls']==1
    # Explicit resume finds the original job by root even with different flags.
    resume=[sys.executable,'-m','auto_agents',command,'--project',str(root),
        '--session='+first['root_id'],'--provider=fixture','--log-mode','plain']
    retained=Runner(store).start(resume,ENGINE)
    assert retained['id']==first['id'] and business.work(first['root_id'])['calls']==1
    assert not list(store.root.glob('.invocation-*'))


def test_root_identity_preserves_unknown_call_and_maintenance_budget(tmp_path):
    root=project(tmp_path)
    work=BusinessStore(root).create_workflow('collab','Original goal','source')
    store=Store(tmp_path/'watch')
    argv=['auto-agents','collab','--project',str(root),'--session',work['id'],'--provider','first']
    job=store.create(argv,root,ENGINE)
    store.reserve(job,'implement')
    store.save(job,'STOPPED',root_id=work['id'])
    changed=[sys.executable,'-m','auto_agents','resume','--project',str(root),'--workflow',work['workflow'],
        '--provider','replacement','--log-mode','debug']
    with pytest.raises(RuntimeError,match='Unconfirmed model operation'):
        Runner(store).start(changed,ENGINE)
    assert len(store.list())==1 and store.get(job['id'])['model_calls']==1


def test_duplicate_lock_refusal_keeps_original_job_state(tmp_path):
    store=Store(tmp_path/'watch')
    job=store.create([],tmp_path/'project',ENGINE)
    before=store.get(job['id'])
    owner=Runner(store)
    with owner.locked(job):
        with pytest.raises(ProjectBusyError,match='duplicate execution'):
            Runner(store).resume(job['id'])
        assert store.get(job['id'])==before


def test_inherited_lock_does_not_adopt_live_children_of_an_old_owner(tmp_path):
    from auto_agents.process_supervision import process_start_ticks
    from auto_agents.run_lock import ProjectRunLock, RunAlreadyActiveError

    store=Store(tmp_path/'watch')
    job=store.create([],tmp_path/'project',ENGINE)
    runner=Runner(store)
    runner.directory=store.root/'jobs'/job['id']
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    try:
        with runner.locked(job):
            control=Path(job['project'])/'.auto-agents/state/run.processes.json'
            control.write_text(json.dumps({'project':job['project'],'run_token':'old-owner','processes':[
                {'pid':child.pid,'pgid':child.pid,'start_ticks':process_start_ticks(child.pid)}]}))
            with pytest.raises(RunAlreadyActiveError,match='orphaned'):
                ProjectRunLock(Path(job['project']),environ=runner.env(job)).acquire()
            assert child.poll() is None
            assert json.loads(control.read_text())['run_token']=='old-owner'
    finally:
        child.terminate();child.wait(timeout=5)


def test_installed_repair_keeps_selected_provider_and_original_root():
    job={'project':'/project','argv':['auto-agents','collab','--project','/project',
        '--session','root','--provider','replacement'],
        'runtime_argv':['/accepted/bin/python','-m','auto_agents','collab','--project','/project',
            '--session','root','--provider','original'],
        'resume_token':{'schema':2,'project':'/project',
            'argv':['collab','--project','/project','--session','root','--provider','original']}}
    command=checkpoint_arguments(job)
    assert command[:3]==['/accepted/bin/python','-m','auto_agents']
    assert command[command.index('--session')+1]=='root'
    assert command[-2:]==['--provider','replacement']


def test_new_job_lock_refusal_stops_only_the_new_job(tmp_path):
    root=native_fixture(tmp_path)
    store=Store(tmp_path/'watch')
    old=store.create([],root,ENGINE)
    before=store.get(old['id'])
    owner=Runner(store)
    with owner.locked(old):
        with pytest.raises(ProjectBusyError):
            Runner(store).start(['auto-agents','collab','--project',str(root),'--goal','New goal'],ENGINE)
        assert store.get(old['id'])==before
        created=[row for row in store.list() if row['id']!=old['id']]
        assert len(created)==1 and created[0]['state']=='STOPPED'
        assert not created[0].get('process') and created[0]['model_calls']==0


@pytest.mark.parametrize('command',['collab','fix','provider-resolve','provider-research'])
def test_bootstrap_auto_supervision_creates_two_new_sessions(tmp_path,command):
    root=native_fixture(tmp_path)
    binary=tmp_path/'bin'
    binary.mkdir()
    watcher=binary/'auto-agents-watch'
    watcher.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(ENGINE / "supervisor/src")!r})\n'
        'from auto_agents_watch.cli import main\nraise SystemExit(main())\n')
    watcher.chmod(0o700)
    environment={**os.environ,'PYTHONPATH':str(ENGINE/'src'),
        'PATH':str(binary)+os.pathsep+os.environ['PATH'],'AUTO_AGENTS_WATCH_ROOT':str(tmp_path/'watch')}
    environment.pop('AUTO_AGENTS_NO_SUPERVISOR',None)
    environment.pop('AUTO_AGENTS_ACCEPTANCE_BUDGET',None)
    argv=[sys.executable,'-m','auto_agents',command,'--project',str(root),'--provider','fixture',
        '--goal','Confirm the fixture goal','--auto-approve']
    for _ in range(2):
        result=subprocess.run(argv,cwd=ENGINE,env=environment,capture_output=True,text=True,timeout=30)
        assert result.returncode==0 and '监督任务已暂停' in result.stdout,result.stdout+result.stderr
        assert 'Confirm the fixture' in result.stderr and '处理命令：' in result.stderr
        assert (result.stdout+result.stderr).count('待处理：')==1
    jobs=Store(tmp_path/'watch').list()
    assert len(jobs)==2 and len({row['root_id'] for row in jobs})==2
    assert all(row['model_calls']==0 and not row.get('fault') for row in jobs)
    assert len(BusinessStore(root).works())==2


def test_concurrent_resumes_admit_one_job_and_one_maintenance_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root=project(tmp_path)
    work=BusinessStore(root).create_workflow('collab','Original goal','source')
    store=Store(tmp_path/'watch')
    barrier=Barrier(2)
    class Admission(Runner):
        def execution_request(self,*args):
            result=super().execution_request(*args)
            barrier.wait(timeout=10)
            return result
        def resume(self,identity,**kwargs):
            return self.store.get(identity)
    argv=['auto-agents','collab','--project',str(root),'--session',work['id']]
    with ThreadPoolExecutor(max_workers=2) as executor:
        calls=[executor.submit(Admission(store).start,argv,ENGINE) for _ in range(2)]
        jobs=[call.result(timeout=15) for call in calls]
    assert jobs[0]['id']==jobs[1]['id'] and len(store.list())==1
    store.reserve(jobs[0],'implement',max_calls=1)
    with pytest.raises(RuntimeError,match='Reconcile the previous model call'):
        store.reserve(jobs[1],'implement',max_calls=1)
    assert store.get(jobs[0]['id'])['model_calls']==1


@pytest.mark.parametrize('category', ['model', 'verification', 'state', 'provider', 'environment'])
def test_native_resume_rechecks_cached_business_fault_without_maintenance(tmp_path, category):
    root=project(tmp_path)
    business=BusinessStore(root)
    work=business.create_workflow('collab','Original goal','source')
    store=Store(tmp_path/'watch')
    argv=[sys.executable,'-m','auto_agents','collab','--project',str(root),'--session',work['id']]
    job=store.create(argv,root,ENGINE)
    fault={'category':category,'message':'obsolete business fault'}
    store.save(job,'STOPPED',root_id=work['id'],fault=fault,reason=fault['message'],
               model_calls=2,attempts=1,no_progress=1,needs_maintenance=True)

    class Recheck(Runner):
        def business(self, retained):
            assert retained['fault'] is None and retained['needs_maintenance'] is False
            assert self.env(retained)['AUTO_AGENTS_MAINTENANCE_RESUME']=='1'
            self.store.save(retained,'STOPPED',reason='current business observed')
            return False
        def maintain(self, *_):
            pytest.fail('A cached business fault triggered engine maintenance')

    result=Recheck(store).start(argv,ENGINE)
    assert result['id']==job['id'] and len(store.list())==1
    assert result['reason']=='current business observed'
    assert result['retained_faults'][-1]['fault']==fault
    assert (result['model_calls'],result['attempts'],result['no_progress'])==(2,1,1)
    assert business.work(work['id'])['contract']==work['contract']


@pytest.mark.parametrize('blocker', ['cancelled', 'unconfirmed_call'])
def test_native_business_retry_keeps_cancellation_and_unknown_call_guards(tmp_path, blocker):
    root=project(tmp_path)
    work=BusinessStore(root).create_workflow('collab','Original goal','source')
    store=Store(tmp_path/'watch')
    argv=[sys.executable,'-m','auto_agents','collab','--project',str(root),'--session',work['id']]
    job=store.create(argv,root,ENGINE)
    if blocker=='unconfirmed_call':
        store.reserve(job,'implement')
    store.save(job,'STOPPED',root_id=work['id'],fault={'category':'model','message':'retained fault'},
               cancel_requested=blocker=='cancelled')

    class Refuse(Runner):
        def business(self, *_):
            pytest.fail('Business was dispatched before reconciliation or explicit uncancellation')
        def maintain(self, *_):
            pytest.fail('Maintenance was dispatched before reconciliation or explicit uncancellation')

    if blocker=='unconfirmed_call':
        with pytest.raises(RuntimeError,match='Unconfirmed model operation'):
            Refuse(store).start(argv,ENGINE)
        assert store.get(job['id'])['model_calls']==1
    else:
        assert Refuse(store).start(argv,ENGINE)['cancel_requested']
    assert len(store.list())==1 and not store.get(job['id']).get('retained_faults')


def test_bootstrap_resumes_recovered_acceptance_past_stale_supervisor_fault(tmp_path, monkeypatch):
    root=native_fixture(tmp_path)
    # A local provider fixture exercises the actual CLI, watcher and verifier.
    # No provider account, browser or media request is used by this test project.
    provider=root/'fixture_provider.py'
    provider.write_text('''import json,os,sys
from pathlib import Path
prompt=sys.stdin.read()
phase=os.environ['AUTO_AGENTS_STAGE']
report='.auto-agents/docs/acceptance/result.txt'
if phase=='diagnose':
 result={'action':'need_user','question':'Confirm the fixture goal'}
elif phase=='acceptance':
 path=Path(report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text('Observed existing value')
 test=Path('.tmp-tests/test_acceptance.py');test.parent.mkdir(exist_ok=True)
 test.write_text('import unittest\\nfrom value import VALUE\\nclass Acceptance(unittest.TestCase):\\n def test_value(self): self.assertEqual(VALUE, 0)\\n')
 result={'accepted':True,'evidence_refs':[report],
         'checks':[{'command':sys.executable+' -m unittest discover -s .tmp-tests -p test_acceptance.py -v'}]}
elif phase=='acceptance_review':
 result={'decision':'pass','reason':'Verified existing value','evidence_refs':[report]}
else:raise AssertionError(phase)
print(json.dumps(result))
''')
    git(root,'add','fixture_provider.py')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','local acceptance fixture')
    monkeypatch.delenv('AUTO_AGENTS_ACCEPTANCE_BUDGET',raising=False)
    store=Store(tmp_path/'watch')
    first=Runner(store).start([sys.executable,'-m','auto_agents','collab','--project',str(root),
        '--provider','fixture','--goal','Observe existing value, no new features','--auto-approve'],ENGINE)
    business=BusinessStore(root)
    work=business.work(first['root_id'])
    assert work['status']=='WAITING' and work['calls']==1
    recovered=business.transition(work,'READY',phase='acceptance',context={},failure={})
    fault={'category':'model','message':'Worker changed protected runtime or operator paths'}
    store.save(first,'STOPPED',fault=fault,reason=fault['message'],waiting_for=None,
               model_calls=2,attempts=1,no_progress=1)
    binary=tmp_path/'bin';binary.mkdir()
    watcher=binary/'auto-agents-watch'
    watcher.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(ENGINE / "supervisor/src")!r})\n'
        'from auto_agents_watch.cli import main\nraise SystemExit(main())\n')
    watcher.chmod(0o700)
    environment={**os.environ,'PYTHONPATH':str(ENGINE/'src'),
        'PATH':str(binary)+os.pathsep+os.environ['PATH'],'AUTO_AGENTS_WATCH_ROOT':str(store.root)}
    environment.pop('AUTO_AGENTS_NO_SUPERVISOR',None)
    result=subprocess.run([sys.executable,'-m','auto_agents','collab','--project',str(root),
        '--provider','fixture','--auto-approve','--session',work['id']],cwd=ENGINE,env=environment,
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert '正在执行验收' in result.stderr
    assert '会话：'+work['id'] in result.stderr
    assert '[run] clarify' not in result.stderr and '[run] prototype' not in result.stderr
    assert '正在明确需求' not in result.stderr and '正在制作界面原型' not in result.stderr
    final=store.get(first['id'])
    assert len(store.list())==1 and final['state']=='DONE' and not final.get('fault')
    assert final['retained_faults'][-1]['fault']==fault
    assert (final['model_calls'],final['attempts'],final['no_progress'])==(2,1,1)
    done=business.work(work['id'])
    assert done['status']=='COMPLETED' and done['calls']==3
    assert done['contract']==recovered['contract']
    assert done['result']['verification']['checks'][0]['executed']==1
    assert (root/'value.py').read_text()=='VALUE = 0\n'
