"""New collab goals come from the user; project specs remain reference material."""

import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import pytest
from auto_agents.control import Store
from auto_agents.control.cli import main
from auto_agents.control.engine import Engine
from auto_agents.control.workspace import git
from auto_agents.config import save_project_config
from auto_agents.models import ProjectConfig
from test_control_engine import project, Worker
from test_watch_invocation import native_fixture, ENGINE


@pytest.mark.parametrize('option',['--goal','--spec-file'])
def test_explicit_goal_bypasses_prompt_and_preserves_project_reference(tmp_path,monkeypatch,capsys,option):
    from auto_agents.control import cli
    root=project(tmp_path)
    (root/'spec.md').write_text('Unrelated project reference')
    git(root,'add','spec.md')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','project reference')
    save_project_config(root,ProjectConfig('fixture'))
    class Question(Worker):
        def run(self,ctx,phase,*a,**kw):
            assert ctx.contract.goal=='User-selected goal'
            assert (ctx.workspace_root/'spec.md').read_text()=='Unrelated project reference'
            return {'text':json.dumps({'action':'need_user','question':'Confirm the selected goal'})}
    actual=Engine
    monkeypatch.setattr(cli,'Engine',lambda *a,**kw:actual(*a,**kw,transport=Question()))
    monkeypatch.setattr('builtins.input',lambda *a:pytest.fail('Explicit goal prompted'))
    value='User-selected goal'
    if option=='--spec-file':
        file=tmp_path/'goal.txt';file.write_text(value);value=str(file)
    assert main(['collab','--project',str(root),option,value,'--json'])==0
    work=json.loads(capsys.readouterr().out)['work']
    assert Store(root).contract(work['contract']).goal=='User-selected goal'


def test_noninteractive_collab_requires_goal_even_when_project_spec_exists(tmp_path,monkeypatch,capsys):
    root=project(tmp_path)
    (root/'spec.md').write_text('Project reference')
    save_project_config(root,ProjectConfig('fixture'))
    monkeypatch.setattr(sys.stdin,'isatty',lambda:False)
    assert main(['collab','--project',str(root),'--json'])==3
    assert json.loads(capsys.readouterr().out)['error']['code']=='goal'
    assert Store(root).works()==[]


@pytest.mark.parametrize('contents',[None,'   '])
def test_explicit_missing_or_empty_goal_file_does_not_fall_back(tmp_path,monkeypatch,capsys,contents):
    root=project(tmp_path)
    (root/'spec.md').write_text('Project reference')
    save_project_config(root,ProjectConfig('fixture'))
    file=tmp_path/'goal.txt'
    if contents is not None:file.write_text(contents)
    monkeypatch.setattr('builtins.input',lambda *a:pytest.fail('Invalid explicit file prompted'))
    assert main(['collab','--project',str(root),'--spec-file',str(file),'--json'])==3
    assert json.loads(capsys.readouterr().out)['error']['code']=='goal'
    assert Store(root).works()==[]


def test_real_terminal_goal_passes_through_bootstrap_and_watcher(tmp_path):
    root=native_fixture(tmp_path)
    (root/'spec.md').write_text('Unrelated project reference, not the requested collab goal')
    git(root,'add','spec.md')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','project reference')
    binary=tmp_path/'bin';binary.mkdir()
    watcher=binary/'auto-agents-watch'
    watcher.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(ENGINE / "supervisor/src")!r})\n'
        'from auto_agents_watch.cli import main\nraise SystemExit(main())\n')
    watcher.chmod(0o700)
    environment={**os.environ,'PYTHONPATH':str(ENGINE/'src'),'PATH':str(binary)+os.pathsep+os.environ['PATH'],
        'AUTO_AGENTS_WATCH_ROOT':str(tmp_path/'watch')}
    environment.pop('AUTO_AGENTS_NO_SUPERVISOR',None)
    environment.pop('AUTO_AGENTS_ACCEPTANCE_BUDGET',None)
    master,slave=pty.openpty()
    child=None
    try:
        child=subprocess.Popen([sys.executable,'-m','auto_agents','collab','--project',str(root),
            '--provider','fixture','--auto-approve'],cwd=ENGINE,env=environment,stdin=slave,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        os.close(slave);slave=None
        os.write(master,'用户输入的本次协作目标\n\n保留第二段内容\n'.encode()+b'\x04')
        output,error=child.communicate(timeout=30)
        assert child.returncode==0,output+error
        assert '请输入目标或问题' in error and 'Ctrl+D' in error and '监督任务已暂停' in output
        store=Store(root)
        works=store.works()
        assert len(works)==1 and store.contract(works[0]['contract']).goal=='用户输入的本次协作目标\n\n保留第二段内容'
        assert works[0]['calls']==1 and works[0]['status']=='WAITING'
    finally:
        if child and child.poll() is None:child.kill();child.communicate()
        os.close(master)
        if slave is not None:os.close(slave)
