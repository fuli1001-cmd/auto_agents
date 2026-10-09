"""Interactive failures remain readable while machine diagnostics stay complete."""

import json
from pathlib import Path

import pytest

from auto_agents import bootstrap
from auto_agents.engine_fault import EngineFault, request_engine_repair
from auto_agents_watch import cli as watch_cli
from auto_agents_watch.store import Store


def test_business_exception_prints_summary_and_saves_full_diagnostics(
    tmp_path, monkeypatch, capsys
):
    from auto_agents.control import Store as BusinessStore
    from auto_agents.control.engine import Engine
    from auto_agents.control.workspace import Workspaces
    from auto_agents.config import save_project_config
    from auto_agents.models import ProjectConfig
    from test_control_engine import project

    root = project(tmp_path)
    save_project_config(root, ProjectConfig("failure"))
    store = BusinessStore(root)
    source = Workspaces(root, store).snapshot()
    work = store.create_workflow("collab", "Continue original goal", source)
    evidence = {"issue_seed": {"required_behavior": ["retained evidence"] * 500}}

    def failed(*args):
        raise EngineFault("修复交付检查要求执行不存在的历史测试", evidence=evidence)

    monkeypatch.setattr(bootstrap, "select_runtime", lambda *args: None)
    monkeypatch.setattr(Engine, "resume", failed)
    assert (
        bootstrap.main(
            [
                "collab",
                "--project",
                str(root),
                "--session",
                work["id"],
                "--no-supervisor",
            ]
        )
        == 3
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "执行已停止：修复交付检查要求执行不存在的历史测试" in output.err
    assert "详细诊断：" in output.err and len(output.err) < 1000
    assert "Traceback" not in output.err and "required_behavior" not in output.err
    diagnostics = next(
        (root / ".auto-agents/state/resume-checkpoints").glob("*.diagnostics.json")
    )
    value = json.loads(diagnostics.read_text())
    assert value["evidence"]["exception_type"] == "EngineFault"
    assert "Traceback" in value["evidence"]["traceback"]
    assert value["evidence"]["cause"] == evidence
    checkpoint = json.loads(Path(value["resume_token"]).read_text())
    assert checkpoint["argv"][-2:] == ["--session", work["id"]]


@pytest.mark.parametrize("machine", [False, True])
def test_supervisor_terminal_summary_honors_explicit_json(
    tmp_path, monkeypatch, capsys, machine
):
    store = Store(tmp_path / "watch")
    job = store.create(
        ["auto-agents", "collab"], tmp_path / "project", tmp_path / "engine"
    )
    store.save(
        job,
        "STOPPED",
        reason="Engine source has uncommitted changes; candidate admission preserves user work",
        fault={
            "message": "验证要求执行不存在的历史测试",
            "evidence": {"large": ["detail"] * 500},
        },
    )
    monkeypatch.setattr(watch_cli.Runner, "start", lambda *args: store.get(job["id"]))
    args = ["run", "--root", str(store.root), "--engine", str(tmp_path / "engine")]
    if machine:
        args.append("--json")
    assert watch_cli.main([*args, "--", "auto-agents", "collab"]) == 3
    output = capsys.readouterr().out
    if machine:
        assert json.loads(output)["fault"]["evidence"]["large"] == ["detail"] * 500
    else:
        assert "监督任务已停止。" in output
        assert "引擎仓库有未提交改动" in output
        assert "验证要求执行不存在的历史测试" in output
        assert str(store.root / "jobs" / job["id"] / "business.log") in output
        assert "--json" in output and len(output) < 1000
        assert '"evidence"' not in output


def test_supervisor_non_json_error_is_readable(tmp_path, monkeypatch, capsys):
    def failed(*args, **kwargs):
        raise RuntimeError("无法读取恢复检查点")

    monkeypatch.setattr(watch_cli.Runner, "resume", failed)
    assert watch_cli.main(["resume", "--root", str(tmp_path), "--job", "retained"]) == 3
    assert capsys.readouterr().err == "监督任务已停止：无法读取恢复检查点\n"


def test_nested_engine_route_uses_user_visible_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_AGENTS_ENGINE_SOURCE_ROOT", str(tmp_path))
    payload = {
        "issue_seed": {
            "target_repository": str(tmp_path),
            "user_summary": "修复被不存在的历史测试阻塞",
            "reason": "technical detail",
        }
    }
    with pytest.raises(EngineFault, match="修复被不存在的历史测试阻塞") as result:
        request_engine_repair(type("Owner", (), {"project_root": tmp_path})(), payload)
    assert result.value.evidence == payload


@pytest.mark.parametrize('machine',[False,True])
def test_waiting_operator_is_a_successful_pause_with_actionable_question(tmp_path,monkeypatch,capsys,machine):
    store=Store(tmp_path/'watch')
    job=store.create(['auto-agents','collab'],tmp_path/'project',tmp_path/'engine')
    store.save(job,'STOPPED',waiting_for='user',reason='Business task is waiting for operator input',
        observation={'status':'waiting','pending_decisions':[{'question':'是否支持继续生成按钮？',
            'command':'auto-agents answer --session original --value "<你的回答>"'}]})
    monkeypatch.setattr(watch_cli.Runner,'start',lambda *a:store.get(job['id']))
    args=['run','--root',str(store.root),'--engine',str(tmp_path/'engine')]
    if machine:args+=['--json']
    assert watch_cli.main([*args,'--','auto-agents','collab'])==0
    output=capsys.readouterr().out
    if machine:
        value=json.loads(output)
        assert value['state']=='STOPPED' and value['waiting_for']=='user'
    else:
        assert '监督任务已暂停' in output and '是否支持继续生成按钮' in output
        assert '处理命令' in output and '未检测到引擎异常' not in output


def test_streamed_waiting_question_is_not_repeated_but_status_keeps_details(tmp_path,monkeypatch,capsys):
    store=Store(tmp_path/'watch')
    job=store.create(['auto-agents','collab'],tmp_path/'project',tmp_path/'engine')
    store.save(job,'STOPPED',waiting_for='user',business_output_streamed=True,
        reason='Business task is waiting for operator input',observation={'pending_decisions':[
            {'question':'A previously streamed question','command':'auto-agents answer --session original'}]})
    monkeypatch.setattr(watch_cli.Runner,'start',lambda *a:store.get(job['id']))
    assert watch_cli.main(['run','--root',str(store.root),'--engine',job['engine'],'--','auto-agents','collab'])==0
    output=capsys.readouterr().out
    assert '监督任务已暂停' in output and 'A previously streamed question' not in output
    assert watch_cli.main(['status','--root',str(store.root),'--job',job['id']])==0
    assert 'A previously streamed question' in capsys.readouterr().out


def test_business_blocker_has_bounded_summary_and_complete_resume_command(tmp_path,monkeypatch,capsys):
    import shlex
    from auto_agents.control import Store as BusinessStore
    from auto_agents.control import cli
    from auto_agents.control.engine import Engine
    from auto_agents.control.workspace import Workspaces
    from auto_agents.config import save_project_config
    from auto_agents.models import ProjectConfig
    from test_control_engine import project

    base=tmp_path/'directory with spaces';base.mkdir()
    root=project(base)
    config=ProjectConfig('failure');save_project_config(root,config)
    store=BusinessStore(root)
    source=Workspaces(root,store).snapshot()
    work=store.create_workflow('collab','Observe existing app',source)
    reason='图片供应商限流，验收停止。'+('完整业务证据。'*100)
    store.transition(work,'BLOCKED',failure={'code':'acceptance','category':'verification',
        'message':reason,'details':{}})
    monkeypatch.setattr(Engine,'resume',lambda self,identity:self.store.work(identity))
    assert cli.main(['collab','--project',str(root),'--session',work['id'],
        '--provider',config.active_provider,'--auto-approve'])==3
    output=capsys.readouterr()
    assert '任务已停止；会话：'+work['id'] in output.out
    line=next(line for line in output.out.splitlines() if line.startswith('恢复命令：'))
    assert shlex.split(line.removeprefix('恢复命令：'))==[
        'auto-agents','collab','--project',str(root),'--session',work['id'],
        '--provider',config.active_provider,'--auto-approve']
    assert '执行已停止：图片供应商限流' in output.err
    assert len(output.err.splitlines()[0])<=410 and reason not in output.err
    diagnostics=next((root/'.auto-agents/state/resume-checkpoints').glob('*.diagnostics.json'))
    assert json.loads(diagnostics.read_text())['message']==reason


@pytest.mark.parametrize('failure_streamed', [False, True])
def test_streamed_business_failure_is_not_repeated_but_status_retains_it(tmp_path,monkeypatch,capsys,failure_streamed):
    store=Store(tmp_path/'watch')
    job=store.create(['auto-agents','collab'],tmp_path/'project',tmp_path/'engine')
    reason='图片供应商返回 429，验收停止。'
    store.save(job,'STOPPED',reason=reason,business_output_streamed=True,business_failure_streamed=failure_streamed,
        fault={'category':'verification','message':reason})
    monkeypatch.setattr(watch_cli.Runner,'start',lambda *a:store.get(job['id']))
    assert watch_cli.main(['run','--root',str(store.root),'--engine',job['engine'],
        '--','auto-agents','collab'])==3
    output=capsys.readouterr().out
    assert (reason in output) is (not failure_streamed)
    assert '监督任务已停止' in output
    assert 'business.log' in output and '--json' in output
    assert watch_cli.main(['status','--root',str(store.root),'--job',job['id']])==0
    assert reason in capsys.readouterr().out
    assert watch_cli.main(['status','--root',str(store.root),'--job',job['id'],'--json'])==0
    assert json.loads(capsys.readouterr().out)['fault']['message']==reason


@pytest.mark.parametrize('stream,failed', [(False,True),(True,False),(True,True)])
def test_failure_deduplication_requires_an_actually_streamed_summary(tmp_path,capsys,stream,failed):
    import os
    import sys
    from auto_agents_watch.process import run

    message='执行已停止：图片供应商返回 429' if failed else '正在执行验收'
    result=run([sys.executable,'-c',f'print({message!r}, flush=True)'],
        cwd=tmp_path,env=dict(os.environ),log=tmp_path/'business.log',stream=stream)
    assert result['returncode']==0
    assert result['failure_summary_streamed'] is (stream and failed)
    assert (message in capsys.readouterr().err) is stream
    assert message in (tmp_path/'business.log').read_text()
