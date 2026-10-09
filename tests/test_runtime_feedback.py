"""Live progress and inherited app configuration cross the watcher boundary."""

import json
import os
from pathlib import Path
import pytest
from auto_agents.control import Store
from auto_agents.control.cli import main
from auto_agents.control.engine import Engine
from auto_agents.config import save_project_config
from auto_agents.models import ProjectConfig
from auto_agents.control.workspace import git
from test_control_engine import project, Worker
from test_terminal_submission import launch_terminal, read_until, finish


@pytest.mark.parametrize('process_value',[None,'explicit-process-value'])
def test_project_dotenv_precedes_launcher_defaults_but_not_explicit_environment(tmp_path,monkeypatch,capsys,process_value):
    from auto_agents.control import cli
    root=project(tmp_path)
    launcher=tmp_path/'launcher';launcher.mkdir()
    (launcher/'.env').write_text('APP_TEST_ENDPOINT=launcher-default\nLAUNCHER_TEST_EXTRA=present\n')
    (root/'.env').write_text('APP_TEST_ENDPOINT=project-default\nAPP_TEST_SECRET=fixture-secret\n')
    for key in ('APP_TEST_ENDPOINT','LAUNCHER_TEST_EXTRA','APP_TEST_SECRET'):monkeypatch.delenv(key,raising=False)
    if process_value is not None:monkeypatch.setenv('APP_TEST_ENDPOINT',process_value)
    monkeypatch.chdir(launcher)
    save_project_config(root,ProjectConfig('fixture'))
    class Configured(Worker):
        def run(self,ctx,phase,prompt,*a,**kw):
            assert ctx.environment['APP_TEST_ENDPOINT']==(process_value or 'project-default')
            assert ctx.environment['APP_TEST_SECRET']=='fixture-secret'
            assert ctx.environment['LAUNCHER_TEST_EXTRA']=='present'
            assert not (ctx.workspace_root/'.env').exists()
            assert 'fixture-secret' not in prompt
            return {'text':json.dumps({'action':'need_user','question':'A legitimate fixture question'})}
    actual=Engine
    monkeypatch.setattr(cli,'Engine',lambda *a,**kw:actual(*a,**kw,transport=Configured()))
    assert main(['collab','--project',str(root),'--goal','Inspect configured app','--json'])==0
    output=capsys.readouterr()
    assert 'fixture-secret' not in output.out+output.err
    assert '目标已接收' in output.err
    store=Store(root)
    with store.connect() as db:
        rows=db.execute('SELECT inputs,result FROM operations').fetchall()
        assert all('fixture-secret' not in str(tuple(row)) for row in rows)


def test_receipt_and_phase_are_visible_before_blocked_provider_finishes(tmp_path,monkeypatch):
    for key in ('APP_TEST_ENDPOINT','APP_TEST_SECRET'):monkeypatch.delenv(key,raising=False)
    release=tmp_path/'release-provider'
    def prepare(root):
        (root/'.env').write_text('APP_TEST_ENDPOINT=https://fixture.invalid\nAPP_TEST_SECRET=fixture-private-value\n')
        (root/'fixture_provider.py').write_text(f'''import json,os,sys,time
from pathlib import Path
prompt=sys.stdin.read()
assert os.environ['APP_TEST_ENDPOINT']=='https://fixture.invalid'
assert os.environ['APP_TEST_SECRET']=='fixture-private-value'
assert not Path('.env').exists()
assert 'fixture-private-value' not in prompt
assert '"project_dotenv_present": true' in prompt
release=Path({str(release)!r})
while not release.exists():time.sleep(0.02)
print(json.dumps({{'action':'need_user','question':'A legitimate fixture question'}}))
''')
        git(root,'add','fixture_provider.py')
        git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','blocked provider fixture')
    root,master,child=launch_terminal(tmp_path,prepare=prepare)
    try:
        read_until(child.stderr,b'Ctrl+D')
        os.write(master,b'Inspect configured app\n\x04')
        receipt=read_until(child.stderr,'目标已接收'.encode())
        phase=receipt
        if b'[collab] diagnose' not in phase:phase+=read_until(child.stderr,b'[collab] diagnose')
        assert child.poll() is None and not release.exists()
        release.touch()
        output,error=child.communicate(timeout=25)
        assert child.returncode==0,output+error
        assert b'[collab] diagnose' in phase
        assert (output+phase+error).count('待处理：'.encode())==1
        assert b'fixture-private-value' not in output+phase+error
        store=Store(root)
        assert not (root/'.auto-agents/state/owned/workspaces'/store.works()[0]['id']/'.env').exists()
        with store.connect() as db:
            assert all('fixture-private-value' not in str(tuple(row)) for row in db.execute('SELECT inputs,result FROM operations'))
    finally:finish(child,master)
