"""The original engine route remains the same fault boundary on receipt replay."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.business_state import BusinessStore
from auto_agents.engine_fault import EngineFault,request_engine_repair
from auto_agents.supervision_api import Observer,resume_check


@pytest.mark.parametrize('changed_payload',[False,True])
def test_engine_route_checkpoint_does_not_depend_on_last_model_operation(tmp_path,monkeypatch,changed_payload):
    from auto_agents import cli_impl,supervision_api
    engine=tmp_path/'engine';engine.mkdir()
    project=tmp_path/'project';project.mkdir()
    monkeypatch.setenv('AUTO_AGENTS_ENGINE_SOURCE_ROOT',str(engine))
    store=BusinessStore(project)
    store.save('sessions/parent/session_state.json',{'session_id':'parent','goal':'original goal','status':'executing'})
    payload={'issue_seed':{'target_repository':str(engine),'summary':'original engine defect',
                           'verification_command':'python -m pytest tests/test_current.py'}}
    live=SimpleNamespace(project_root=project,_current_state=SimpleNamespace(session_id='parent'))
    replay=SimpleNamespace(project_root=project,_kernel_subject='session:parent')
    args=['collab','--project',str(project),'--session','parent']
    with Observer(project,args) as observer:
        observer.step('route:last-paid-call','parent')
        with pytest.raises(EngineFault) as failure:request_engine_repair(live,payload)
        fault=observer.fault(failure.value)
    token=json.loads(Path(fault['resume_token']).read_text())
    assert token['step_id'].startswith('parent:engine-route:')
    changed={**payload,'issue_seed':{**payload['issue_seed'],'verification_command':'python -m pytest tests/test_other.py'}} if changed_payload else payload
    def dispatch(_args):
        request_engine_repair(replay,changed)
    monkeypatch.setenv('AUTO_AGENTS_OFFLINE_RESUME','1')
    monkeypatch.setattr(supervision_api,'offline_isolated',lambda:True)
    monkeypatch.setattr(cli_impl,'main',dispatch)
    report=resume_check(project,fault['resume_token'])
    assert report['category']=='engine' and report['type']=='EngineFault'
    assert report['reason']==fault['message'] and report['external_calls']==0
    assert (token['step_id'] in {row['step_id'] for row in report['steps']}) is (not changed_payload)
