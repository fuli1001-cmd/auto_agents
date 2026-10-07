"""Interactive failures remain readable while machine diagnostics stay complete."""
import json
from pathlib import Path

import pytest

from auto_agents import bootstrap
from auto_agents.engine_fault import EngineFault, request_engine_repair
from auto_agents_watch import cli as watch_cli
from auto_agents_watch.store import Store


def test_business_exception_prints_summary_and_saves_full_diagnostics(tmp_path, monkeypatch, capsys):
    from auto_agents import cli_impl
    evidence = {'issue_seed': {'required_behavior': ['retained evidence'] * 500}}
    def failed(*args):
        raise EngineFault('修复交付检查要求执行不存在的历史测试', evidence=evidence)
    monkeypatch.setattr(bootstrap, 'select_runtime', lambda *args: None)
    monkeypatch.setattr(cli_impl, 'main', failed)
    assert bootstrap.main(['collab', '--project', str(tmp_path), '--session', 'parent', '--no-supervisor']) == 3
    output = capsys.readouterr()
    assert output.out == ''
    assert '执行已停止：修复交付检查要求执行不存在的历史测试' in output.err
    assert '详细诊断：' in output.err and len(output.err) < 1000
    assert 'Traceback' not in output.err and 'required_behavior' not in output.err
    diagnostics = next((tmp_path / '.auto-agents/state/resume-checkpoints').glob('*.diagnostics.json'))
    value = json.loads(diagnostics.read_text())
    assert value['evidence'] == evidence
    assert 'Traceback' in value['traceback']
    checkpoint = json.loads(Path(value['resume_token']).read_text())
    assert checkpoint['argv'][-2:] == ['--session', 'parent']


@pytest.mark.parametrize('machine', [False, True])
def test_supervisor_terminal_summary_honors_explicit_json(tmp_path, monkeypatch, capsys, machine):
    store = Store(tmp_path / 'watch')
    job = store.create(['auto-agents', 'collab'], tmp_path / 'project', tmp_path / 'engine')
    store.save(job, 'STOPPED', reason='Engine source has uncommitted changes; candidate admission preserves user work',
               fault={'message': '验证要求执行不存在的历史测试', 'evidence': {'large': ['detail'] * 500}})
    monkeypatch.setattr(watch_cli.Runner, 'start', lambda *args: store.get(job['id']))
    args = ['run', '--root', str(store.root), '--engine', str(tmp_path / 'engine')]
    if machine:
        args.append('--json')
    assert watch_cli.main([*args, '--', 'auto-agents', 'collab']) == 3
    output = capsys.readouterr().out
    if machine:
        assert json.loads(output)['fault']['evidence']['large'] == ['detail'] * 500
    else:
        assert '监督任务已停止。' in output
        assert '引擎仓库有未提交改动' in output
        assert '验证要求执行不存在的历史测试' in output
        assert str(store.root / 'jobs' / job['id'] / 'business.log') in output
        assert '--json' in output and len(output) < 1000
        assert '"evidence"' not in output


def test_supervisor_non_json_error_is_readable(tmp_path, monkeypatch, capsys):
    def failed(*args, **kwargs):
        raise RuntimeError('无法读取恢复检查点')
    monkeypatch.setattr(watch_cli.Runner, 'resume', failed)
    assert watch_cli.main(['resume', '--root', str(tmp_path), '--job', 'retained']) == 3
    assert capsys.readouterr().err == '监督任务已停止：无法读取恢复检查点\n'


def test_nested_engine_route_uses_user_visible_problem(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_ENGINE_SOURCE_ROOT', str(tmp_path))
    payload = {'issue_seed': {'target_repository': str(tmp_path),
                             'user_summary': '修复被不存在的历史测试阻塞', 'reason': 'technical detail'}}
    with pytest.raises(EngineFault, match='修复被不存在的历史测试阻塞') as result:
        request_engine_repair(type('Owner', (), {'project_root': tmp_path})(), payload)
    assert result.value.evidence == payload
