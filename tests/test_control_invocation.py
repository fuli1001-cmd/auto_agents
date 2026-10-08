"""The business kernel defines launch intent for both CLI and supervision."""

import json
from pathlib import Path
import pytest
from auto_agents.control import Store, ControlError
from auto_agents.control.invocation import request
from auto_agents.control.cli import main


@pytest.mark.parametrize("command", ["collab", "fix", "provider-resolve", "provider-research"])
def test_no_selector_is_new_even_with_identical_incomplete_roots(tmp_path, command):
    store=Store(tmp_path)
    mode="provider_resolve" if command.startswith("provider-") else command
    previous=store.create_workflow(mode,"Original goal","source",max_calls=5)
    before=store.works()
    for _ in range(2):
        result=request(tmp_path,[command,'--project',str(tmp_path),'--goal','Original goal'])
        assert result['intent']=='start' and result['root_id'] is None
    assert store.works()==before and store.work(previous['id'])['calls']==0


@pytest.mark.parametrize("command,selector", [('collab','--session'),('resume','--workflow')])
def test_explicit_work_and_workflow_select_same_root(tmp_path, command, selector):
    store=Store(tmp_path)
    work=store.create_workflow('collab','Original goal','source')
    identity=work['id'] if selector=='--session' else work['workflow']
    result=request(tmp_path,[command,'--project',str(tmp_path),selector+'='+identity])
    assert result['intent']=='resume' and result['root_id']==work['id'] and result['work_id']==work['id']


def test_run_auto_selection_excludes_auxiliary_roots_and_refuses_ambiguity(tmp_path):
    store=Store(tmp_path)
    store.create_workflow('run','Prototype variant','source',inputs={'variant_only':'variant'})
    store.create_workflow('run','Release','source',inputs={'release_only':True})
    assert request(tmp_path,['run','--project',str(tmp_path)])['intent']=='start'
    run=store.create_workflow('run','Original run','source')
    result=request(tmp_path,['run','--project',str(tmp_path)])
    assert result['intent']=='resume' and result['root_id']==run['id']
    store.create_workflow('run','Another run','source')
    with pytest.raises(ControlError,match='Choose --session'):
        request(tmp_path,['run','--project',str(tmp_path)])


def test_conflicting_selectors_and_wrong_mode_fail_before_any_work(tmp_path):
    store=Store(tmp_path)
    one=store.create_workflow('collab','One','source')
    two=store.create_workflow('collab','Two','source')
    before=store.works()
    with pytest.raises(ControlError,match='different work items'):
        request(tmp_path,['resume','--project',str(tmp_path),'--session',one['id'],'--workflow',two['workflow']])
    with pytest.raises(ControlError,match='Requested mode differs'):
        request(tmp_path,['fix','--project',str(tmp_path),'--session',one['id']])
    assert store.works()==before


def test_public_execution_request_is_read_only_and_resolves_restart(tmp_path,capsys):
    store=Store(tmp_path)
    work=store.create_workflow('run','Original run','source',max_calls=5)
    work=store.transition(work,'RUNNING')
    store.transition(work,'BLOCKED',failure={'category':'model'})
    before=store.works()
    invocation=tmp_path/'argv.json'
    invocation.write_text(json.dumps(['run','--project',str(tmp_path),'--restart-blocked']))
    assert main(['execution-request','--project',str(tmp_path),'--invocation',str(invocation)])==0
    value=json.loads(capsys.readouterr().out)
    assert value['intent']=='start' and value['root_id'] is None
    assert store.works()==before


def test_unknown_selector_is_structured_and_does_not_create_state(tmp_path,capsys):
    invocation=tmp_path/'argv.json'
    invocation.write_text(json.dumps(['collab','--project',str(tmp_path),'--session','missing']))
    assert main(['execution-request','--project',str(tmp_path),'--invocation',str(invocation)])==3
    assert json.loads(capsys.readouterr().out)['error']['code']=='unknown_work'
    assert not (tmp_path/'.auto-agents/state/business.sqlite3').exists()


def test_failure_before_new_root_does_not_block_previous_goal(tmp_path,monkeypatch,capsys):
    from auto_agents.control import cli
    from auto_agents.config import save_project_config
    from auto_agents.models import ProjectConfig

    store=Store(tmp_path)
    old=store.create_workflow('collab','Previous goal','source')
    store.set_meta('active_root',old['id'])
    save_project_config(tmp_path,ProjectConfig('fixture'))
    before=store.work(old['id'])
    def unavailable_engine(*args,**kwargs):
        raise TypeError('Engine constructor defect before root creation')
    monkeypatch.setattr(cli,'Engine',unavailable_engine)
    assert main(['collab','--project',str(tmp_path),'--goal','New goal','--json'])==3
    assert json.loads(capsys.readouterr().out)['error']['category']=='engine'
    assert store.work(old['id'])==before and len(store.works())==1
    assert store.meta('active_root')==''
    assert not list((tmp_path/'.auto-agents/state/resume-checkpoints').glob('*.json'))


def test_new_goal_prompt_reports_waiting_without_claiming_previous_root(tmp_path,monkeypatch):
    import sys
    from auto_agents.config import save_project_config
    from auto_agents.models import ProjectConfig

    store=Store(tmp_path)
    old=store.create_workflow('collab','Previous goal','source')
    store.set_meta('active_root',old['id'])
    save_project_config(tmp_path,ProjectConfig('fixture'))
    before=store.work(old['id'])
    observation=tmp_path/'observation.json'
    monkeypatch.setenv('AUTO_AGENTS_OBSERVATION_FILE',str(observation))
    monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    def interrupted_input(prompt):
        value=json.loads(observation.read_text())
        assert value['status']=='waiting' and value['waiting_for']=='user'
        assert value['phase']=='goal' and not value['root_subject']
        assert value['heartbeat_at'] and value['steps']==[]
        raise KeyboardInterrupt
    monkeypatch.setattr('builtins.input',interrupted_input)
    with pytest.raises(KeyboardInterrupt):
        main(['collab','--project',str(tmp_path)])
    assert store.work(old['id'])==before and len(store.works())==1
