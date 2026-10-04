"""Foreground kernel repair uses the established terminal presenter."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from auto_agents.recovery.progress import RepairProgress
from auto_agents.process_supervision import RunInterruptedError
from test_execution_display import report, screen, frame, history
from test_recovery_engine import runner
from test_recovery_kernel import scene


def test_kernel_stages_and_periodic_plain_counts_follow_existing_style(report, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('time.time', lambda: clock[0])
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    with RepairProgress(report, {}) as progress:
        progress.command({'phase': 'plan'}, {'attempts': 0})
        progress.command({'phase': 'implement'}, {'attempts': 1})
        progress.phase('artifact')
        progress.command({'phase': 'verify'}, {'attempts': 1})
        progress.phase('full_suite')
        progress.check('checks_started', {'completed': 0, 'total': 292})
        progress.check('check_finished', {'completed': 202, 'total': 292, 'failed_count': 0,
            'command': 'internal command', 'excerpt': 'private output'})
        before = history(report)
        clock[0] += 59
        progress.refresh()
        assert history(report) == before
        clock[0] += 1
        progress.refresh()
        assert '202/292' in history(report) and '最近输出 60 秒前' in history(report)
        periodic = history(report)
        progress.refresh()
        assert history(report) == periodic
        progress.phase('boundary')
        progress.rejected([{'unit': 'original-boundary'}])
        progress.command({'phase': 'implement'}, {'attempts': 2, 'failure': {'kind': 'candidate_rejected'}})
        progress.blocked({'kind': 'environment_blocked',
            'reason': "input_too_large: /private/path; actual_chars=4166739"}, 'private-incident')
    assert progress._thread is not None and not progress._thread.is_alive()
    assert frame(report) == '' and report.snapshot.repair == ''
    text = history(report)
    for message in ('[自修复] 准备环境', '[自修复] 制定修复方案', '[自修复] 第1轮 · 修复中',
                    '准备验证环境', '检查测试完整性', '完整验证', '验证任务恢复',
                    '验收未通过：原任务恢复检查未通过（1项）',
                    '第2轮 · 继续修复上次检查发现的问题', '请求内容超出模型限制；进度已保留'):
        assert message in text
    assert all(line.startswith('[') and '] [自修复] ' in line
               for line in report.presenter.stream.getvalue().splitlines())
    assert all(secret not in text for secret in ('internal command', 'private output', '/private', 'private-incident', 'actual_chars'))
    assert 'private-incident' in (report.root / 'events.jsonl').read_text()


def test_live_provider_output_and_parallel_checks_clear_between_phases(report, screen, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('time.time', lambda: clock[0])
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    progress = RepairProgress(report, {})
    progress.command({'phase': 'implement'}, {'attempts': 1})
    progress.agent({'event': 'thread/status/changed'})
    progress.agent({'event': 'account/rateLimits/updated'})
    assert '最近输出' not in frame(report)
    progress.agent({'event': 'item/agentMessage/delta'})
    clock[0] += 5
    progress.agent({'event': 'thread/tokenUsage/updated'})
    progress.refresh()
    assert '最近输出 5 秒前' in frame(report)
    progress.phase('full_suite')
    assert '最近输出' not in frame(report)
    progress.check('checks_started', {'completed': 0, 'total': 4})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: progress.check('check_output', {}), range(8)))
    progress.check('check_finished', {'completed': 3, 'total': 4, 'failed_count': 1, 'cancelled_count': 1})
    assert '3/4' in frame(report) and '取消 1' in frame(report)
    before = history(report)
    clock[0] += 60
    progress.refresh()
    assert history(report) == before
    assert history(report).count('完整验证') == 1
    progress.phase('boundary')
    assert '3/4' not in frame(report) and '最近输出' not in frame(report)
    progress.command({'phase': 'review'}, {'attempts': 1})
    assert '[自修复] 第1轮 · 审查' in frame(report)
    progress.phase('deliver')
    progress.handoff()
    assert progress._stop.is_set() and report._handed_off
    assert '恢复原任务' in history(report)
    assert '已恢复原任务' not in history(report)
    old = history(report)
    progress.agent({'event': 'item/completed'})
    progress.check('check_finished', {'completed': 4, 'total': 4})
    progress.refresh()
    assert history(report) == old


@pytest.mark.parametrize('failure', [KeyboardInterrupt(), RunInterruptedError(15), RuntimeError('private internal failure')])
def test_exit_stops_refresh_and_finalizes_the_action(report, failure):
    with pytest.raises(type(failure)):
        with RepairProgress(report, {}) as progress:
            progress.phase('full_suite')
            raise failure
    assert not progress._thread.is_alive()
    assert frame(report) == '' and report.snapshot.repair == ''
    assert ('修复已停止' if isinstance(failure, (KeyboardInterrupt, RunInterruptedError)) else '修复受阻') in history(report)
    assert 'private internal failure' not in history(report)


def test_kernel_runner_drives_display_without_changing_execution_or_journal(scene, tmp_path, report):
    repair, effects = runner(scene, tmp_path)
    with RepairProgress(report, {}) as progress:
        repair.progress = progress
        result = repair.run()
    assert result['status'] == 'completed'
    assert effects.calls == ['plan', 'implement', 'verify', 'review']
    state = repair.store.replay('workflow')
    assert state['budget']['model_calls'] == 3
    assert state['tasks']['engine']['attempts'] == 1
    before = repair.store.load('workflow')
    progress.refresh()
    assert repair.store.load('workflow') == before
    text = history(report)
    assert text.index('制定修复方案') < text.index('修复中') < text.index('检查测试完整性') < text.index('审查')


def test_submit_connects_reporter_and_hides_failure_json(report, monkeypatch):
    from auto_agents.recovery import engine
    def stopped(store, project, orchestrator, payload, args, lock, progress):
        assert progress.reporter is report
        progress.phase('full_suite')
        progress.blocked({'kind': 'environment_blocked', 'reason': 'private details'}, 'private-incident')
        return 3
    monkeypatch.setattr(engine, '_submit', stopped)
    assert engine.submit(None, report.project_root, SimpleNamespace(reporter=report), {}, None, None) == 3
    assert '修复受阻' in history(report)
    assert 'private-incident' not in report.presenter.stream.getvalue()


def test_english_repair_uses_existing_translations(report):
    report.language = 'en'
    with RepairProgress(report, {}) as progress:
        progress.phase('full_suite')
        progress.blocked({'reason': 'input_too_large'})
    assert '[Self-repair] Preparing environment' in history(report)
    assert '[Self-repair] Full verification' in history(report)
    assert 'Request exceeds the model input limit' in history(report)
