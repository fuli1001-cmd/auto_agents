from __future__ import annotations

import io
import sys

import pytest

from auto_agents.models import CommandResult, GateResult, RunState, TaskSpec
from auto_agents.reporting import GateObservation, Reporter


@pytest.fixture
def report(tmp_path):
    reporter = Reporter(tmp_path, io.StringIO(), language="zh")
    reporter.bind("run", "progress")
    yield reporter
    reporter.close()


def setup_tasks(reporter, total=2, done=0):
    state = RunState("progress", current_stage="implement", tasks=[
        TaskSpec(f"T{i + 1}", f"任务{i + 1}", "description", ["works"], status="done" if i < done else "pending")
        for i in range(total)
    ])
    reporter.observe_run(state)
    reporter.stage("implement")
    return state


def frame(reporter):
    return reporter.presenter._frame(reporter)


def history(reporter):
    path = reporter.root / "user.log"
    return path.read_text() if path.exists() else ""


def result(command="pytest tests/test_recovery.py", ok=True, **kwargs):
    return CommandResult(command=command, ok=ok, returncode=0 if ok else 1, **kwargs)


@pytest.fixture
def screen(report, monkeypatch):
    class Screen:
        width = 120
        frame = ""
        history = []

        def update(self, value, **kwargs):
            self.frame = value.plain

        def print(self, value):
            self.history.append(value.plain)

        def stop(self):
            self.frame = ""

    screen = Screen()
    report.presenter._live = screen
    report.presenter._console = screen
    monkeypatch.setattr(report.presenter, "_ensure_started", lambda: None)
    return screen


def test_plain_log_matches_requested_task_and_action_transcript(report):
    state = setup_tasks(report, total=19, done=4)
    report.task("T5", "按历史最佳纠正规划", "implement", 1)
    report.observe_run(state)
    report.heartbeat()
    report.task("T5", "按历史最佳纠正规划", "verify", 1)
    checks = GateObservation(None, report, 3)
    checks.finish(GateResult(ok=True, commands=[result(str(i)) for i in range(3)], summary="passed"))
    report.task_result("T5", "按历史最佳纠正规划", "verify", "pass")
    report.task("T5", "按历史最佳纠正规划", "review", 1)
    report.task_result("T5", "按历史最佳纠正规划", "review", "fail")
    report.task("T5", "按历史最佳纠正规划", "implement", 2)
    messages = [line.split(" ", 1)[1] for line in history(report).splitlines()]
    assert messages == [
        "（实现阶段：5/19）T5：按历史最佳纠正规划", "    编码", "    验证",
        "    审查", "    审查未通过", "    编码",
    ]
    assert state.status == "pending"
    assert "最近输出" not in report.presenter.stream.getvalue()
    assert report.presenter._thread is None


def test_live_action_is_finalized_without_output_age_when_next_action_starts(report, screen, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("auto_agents.execution_display.time.monotonic", lambda: clock[0])
    setup_tasks(report)
    report.task("T1", "恢复策略", "implement", 1)
    capture = report.capture(kind="provider")
    capture.start(["provider"], {}, provider="codex", capture_mode="live")
    capture("stdout", "output")
    clock[0] += 3
    assert "编码 | 最近输出 3 秒前" in frame(report)
    assert all("编码" not in line for line in screen.history)
    capture.finish(returncode=0)
    report.task("T1", "恢复策略", "verify", 1)
    assert screen.history[-1].endswith("    编码")
    assert "最近输出" not in screen.history[-1]
    assert "验证" in screen.frame and "编码" not in screen.frame
    assert "最近输出" not in screen.frame
    assert "服务调用" not in history(report)
    report.presenter.close()
    assert report.presenter.stream.getvalue().endswith("    验证\n")


def test_checks_use_progress_bar_and_passing_does_not_add_result_log(report, screen):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    observation = GateObservation(None, report, 3)
    observation("start", "pytest tests/test_recovery.py --password private", 0)
    assert "验证 | [░░░░░░░░░░░░] 0/3" in frame(report)
    command = result()
    observation.result(command)
    assert "[████░░░░░░░░] 1/3" in frame(report)
    assert "private" not in frame(report) and "已执行" not in frame(report)
    observation.finish(GateResult(ok=True, commands=[command, result("2"), result("3")], summary="passed"))
    report.task_result("T1", "恢复策略", "verify", "pass")
    assert frame(report) == ""
    assert screen.history[-1].endswith("    验证")
    assert "通过" not in history(report) and "3/3" not in history(report)


def test_verification_failure_printed_once_and_cancelled_checks_settle_progress(report):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    observation = GateObservation(None, report, 3)
    commands = [result(), result("failed", False), result("cancelled", False, termination_reason="cancelled")]
    for command in commands:
        observation.result(command)
    assert "3/3" in frame(report) and "失败 1" in frame(report) and "取消 1" in frame(report)
    observation.finish(GateResult(ok=False, commands=commands, summary="failed"))
    report.task_result("T1", "恢复策略", "verify", "fail")
    assert history(report).count("验证未通过") == 1
    assert frame(report) == ""
    assert "检查" not in history(report)


def test_parallel_ordinals_progress_and_output_ages_are_independent(report, tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("auto_agents.execution_display.time.monotonic", lambda: clock[0])
    setup_tasks(report, total=19, done=4)
    first = report.child(tmp_path / "first", "T5")
    second = report.child(tmp_path / "second", "T6")
    try:
        first.task("T5", "恢复策略", "verify", 1)
        second.task("T6", "状态显示", "verify", 1)
        one, two = GateObservation(None, first, 2), GateObservation(None, second, 5)
        first.capture()("stdout", "first")
        clock[0] += 10
        second.capture()("stdout", "second")
        clock[0] += 2
        one.result(result())
        value = frame(report).splitlines()
        assert "T5 · 验证" in value[0] and "1/2" in value[0] and "最近输出 12 秒前" in value[0]
        assert "T6 · 验证" in value[1] and "0/5" in value[1] and "最近输出 2 秒前" in value[1]
        assert "（实现阶段：5/19）T5" in history(report)
        assert "（实现阶段：6/19）T6" in history(report)
        first.task("T5", "恢复策略", "verify", 2)
        current = GateObservation(None, first, 4)
        one.finish(GateResult(ok=True, commands=[result()], summary="late"))
        assert "0/4" in frame(report) and "0/5" in frame(report)
        assert not current.view.finished
        assert history(report).count("（实现阶段：5/19）") == 1
    finally:
        first.close()
        second.close()


def test_plan_reorder_changes_ordinal_without_using_completed_count(report):
    state = setup_tasks(report)
    report.task("T2", "任务2", "implement", 1)
    assert "（实现阶段：2/2）T2" in history(report)
    state.tasks.reverse()
    report.observe_run(state)
    report.task("T2", "任务2", "review", 1)
    assert "（实现阶段：1/2）T2" in history(report)


def test_rewind_invalidates_old_workers_actions_and_output(report, tmp_path):
    setup_tasks(report)
    worker = report.child(tmp_path / "worker", "T1")
    try:
        worker.task("T1", "恢复策略", "verify", 1)
        observation = GateObservation(None, worker, 2)
        capture = worker.capture(kind="provider")
        capture.start(["provider"], {}, capture_mode="live")
        report.rewind("plan", reason="needs a new plan")
        worker.task("T1", "恢复策略", "implement", 2)
        observation("start", "pytest tests/test_old.py", 0)
        observation.finish(GateResult(ok=True, commands=[result()], summary="late"))
        capture("stdout", "late output")
        assert frame(report) == ""
        assert report.display.last_output is None
        assert "返回计划" in history(report)
    finally:
        worker.close()


@pytest.mark.parametrize("status", ["blocked", "completed", "waiting_user", "paused", "failed"])
def test_terminal_state_finishes_current_action(report, status):
    state = setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    observation = GateObservation(None, report, 2)
    state.status = status
    report.observe_run(state)
    observation("start", "pytest tests/test_old.py", 0)
    assert frame(report) == ""
    assert not report.active_tasks and not report.display.checks


def test_gate_exception_closes_current_action(report, monkeypatch):
    from auto_agents.gates import run_gate_plan
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    def fail(*args, **kwargs):
        kwargs["progress"]("start", "pytest tests/test_recovery.py", 0)
        raise RuntimeError("execution failed")
    monkeypatch.setattr("auto_agents.gates._run_phased_gate_plan", fail)
    with pytest.raises(RuntimeError, match="execution failed"):
        run_gate_plan(["pytest tests/test_recovery.py"], [], report.project_root, collect_all=True)
    assert frame(report) == ""
    assert "验证中断" in history(report)


def test_resume_waits_for_actual_action_without_unknown_status_noise(report):
    setup_tasks(report)
    other = Reporter(report.project_root, io.StringIO(), language="zh")
    try:
        other.bind("run", "progress")
        other.observe_run(RunState("progress", current_stage="implement"))
        assert frame(other) == "" and other.presenter.stream.getvalue() == ""
    finally:
        other.close()


def test_refresh_updates_action_without_adding_heartbeat_history(report, screen, monkeypatch):
    setup_tasks(report)
    report.task("T1", "恢复策略", "implement", 1)
    clock = [0.0]
    monkeypatch.setattr("auto_agents.reporting.time.monotonic", lambda: clock[0])
    report.capture()("stdout", "output")
    before = history(report)
    waits = []
    class Stop:
        def set(self):
            pass
        def wait(self, seconds):
            waits.append(seconds)
            clock[0] += seconds
            return clock[0] > 180
    monkeypatch.setattr(report.presenter, "_stop", Stop())
    report.presenter._refresh_loop()
    assert waits == [1.0] * 181
    assert "最近输出 180 秒前" in screen.frame
    assert history(report) == before
    assert len(screen.history) == 1  # Only the task heading is committed so far.


def test_english_narrow_terminal_and_title_controls(report, screen):
    from rich.cells import cell_len
    setup_tasks(report)
    report.language = "en"
    report.task("T1", "unsafe\n\x1b[31mtitle", "verify", 2)
    screen.width = 32
    report.capture()("stdout", "output")
    value = frame(report)
    assert "Verification" in value and "\x1b" not in value
    assert all(cell_len(line) <= 32 for line in value.splitlines())
    assert "\x1b" not in history(report)


def test_buffered_call_has_no_extra_status_line(report):
    setup_tasks(report)
    report.task("T1", "任务1", "verify", 1)
    capture = report.capture(kind="gate")
    capture.start("pytest", {}, capture_mode="file")
    assert frame(report).endswith("    验证")
    assert "尚未收到" not in history(report)
    capture.finish(returncode=0)


def test_recovery_uses_an_action_line(report):
    state = setup_tasks(report)
    state.status = "blocked"
    report.observe_run(state)
    report.repair("diagnosing")
    assert "[引擎自修复] 定位故障原因" in frame(report)
    assert "正在修复 auto_agents" not in frame(report)


def test_real_gate_failure_has_only_one_concise_result(report):
    from auto_agents.gates import run_gate_plan
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    commands = [f'{sys.executable} -c "print(123)"', f'{sys.executable} -c "raise SystemExit(1)"']
    gate = run_gate_plan(commands, [], report.project_root, collect_all=True)
    report.task_result("T1", "恢复策略", "verify", "fail")
    assert not gate.ok and frame(report) == ""
    assert history(report).count("验证未通过") == 1
    assert "raise SystemExit" not in history(report)


def test_blocked_task_finishes_action_and_keeps_notice(report):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 2)
    report.emit("task.blocked", task_id="T1", title="恢复策略")
    assert frame(report) == "" and "受阻" in history(report)


def test_final_acceptance_success_is_silent_but_failure_keeps_route(report):
    report.stage("verify")
    report.emit("verify.passed")
    assert frame(report) == "" and "通过" not in history(report)
    report.emit("verify.failed", route="实现")
    assert "最终验收未通过；下一步：实现" in history(report)


def test_nested_subject_finalizes_child_and_resumes_parent_action(report, screen):
    setup_tasks(report)
    report.task("T1", "任务1", "implement", 1)
    with report.preserve_subject():
        report.bind("fix", "child")
        report.stage("verify")
        assert "验收" in screen.frame
    assert "编码" in screen.frame and "验收" not in screen.frame
    assert report.snapshot.subject == "progress"
    assert sum(line.endswith("    编码") for line in screen.history) == 1
    assert sum(line.endswith("    验收") for line in screen.history) == 1


def test_recovery_replaces_task_action_instead_of_leaving_it_active(report):
    setup_tasks(report)
    report.task("T1", "任务1", "implement", 1)
    report.repair("diagnosing")
    assert "[引擎自修复] 定位故障原因" in frame(report) and "编码" not in frame(report)


@pytest.mark.parametrize('eligible', [False, True])
def test_diagnosis_decision_finalizes_live_action_before_cli_summary(report, screen, eligible):
    report.repair('diagnosing')
    report.display.output_times[''] = 0
    report.emit('repair.eligible' if eligible else 'repair.not_eligible')
    assert frame(report) == ''
    assert report.snapshot.repair == ''
    assert screen.history[-2].endswith('[引擎自修复] 定位故障原因')
    assert '最近输出' not in screen.history[-2]
    assert ('准备修复' if eligible else '本次未获准自动修复') in screen.history[-1]
    assert '正在恢复任务' not in history(report)
    before = list(screen.history)
    report.presenter.close()
    assert screen.history == before


def repair_job(phase='implement', **display):
    return {'id': 'private-job', 'generation': 1, 'state': 'repairing',
            'display': {'phase': phase, 'sequence': 1, **display}}


def test_repair_live_output_ages_without_renewing_on_poll_and_finalizes(report, screen, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('auto_agents.reporting.time.time', lambda: clock[0])
    monkeypatch.setattr('auto_agents.reporting.time.monotonic', lambda: clock[0])
    subscriber = {'state': 'waiting'}
    job = repair_job(last_output_at=1000)
    report.repair_update(job, subscriber)
    clock[0] += 3
    report.repair_update(job, subscriber)
    assert '[引擎自修复] 编码 | 最近输出 3 秒前' in frame(report)
    clock[0] += 7
    report.repair_update(job, subscriber)
    assert '最近输出 10 秒前' in frame(report)
    assert history(report).count('[引擎自修复] 编码') == 1
    report.repair_update(repair_job('validate', sequence=2, review_running=True, checks={'completed': 1, 'total': 4}), subscriber)
    assert screen.history[-1].endswith('[引擎自修复] 编码')
    assert '最近输出' not in screen.history[-1]
    assert '[引擎自修复] 验证与审查 | [███░░░░░░░░░] 1/4' in frame(report)
    assert '最近输出' not in frame(report)
    assert 'private-job' not in history(report)


def test_repair_parallel_completion_and_terminal_reason(report, screen):
    subscriber = {'state': 'waiting'}
    report.repair_update(repair_job('validate', checks={'completed': 2, 'total': 4}, review_running=True), subscriber)
    report.repair_update(repair_job('validate', checks={'completed': 2, 'total': 4}, review_finished=True), subscriber)
    assert '[引擎自修复] 验证 |' in frame(report)
    assert '验证与审查' in screen.history[-1]
    report.repair_update(repair_job('validate', checks_finished=True, review_running=True), subscriber)
    assert '[引擎自修复] 审查' in frame(report) and '/4' not in frame(report)
    job = {**repair_job(), 'state': 'blocked', 'result': {
        'error': 'Insufficient disk space at /private/path: password=hidden'}}
    report.repair_update(job, {'state': 'blocked'})
    assert frame(report) == ''
    assert '磁盘空间不足；清理空间后重新运行以继续' in screen.history[-1]
    assert 'hidden' not in history(report) and '/private' not in history(report)


def test_repair_plain_progress_is_periodic_and_detailed_control_stays_in_events(report, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('auto_agents.reporting.time.time', lambda: clock[0])
    monkeypatch.setattr('auto_agents.reporting.time.monotonic', lambda: clock[0])
    job = repair_job('validate', last_output_at=1000, checks={'completed': 1, 'total': 3})
    report.repair_update(job, {'state': 'waiting'})
    report.event('repair.control', {}, audience='user', message='Self-repair private-id: command internals')
    for _ in range(59):
        clock[0] += 1
        report.repair_update(job, {'state': 'waiting'})
    assert len(history(report).splitlines()) == 1
    clock[0] += 1
    report.repair_update(job, {'state': 'waiting'})
    assert '1/3 | 最近输出 60 秒前' in history(report)
    assert '\x1b' not in report.presenter.stream.getvalue()
    assert 'command internals' not in history(report)
    assert 'command internals' in (report.root / 'events.jsonl').read_text()
    report.repair_update({**job, 'state': 'ready'}, {'state': 'resuming'})
    assert '最近输出' not in frame(report) and '1/3' not in frame(report)


def test_resumed_command_output_is_relayed_once_without_rewriting_user_log(report, tmp_path):
    from auto_agents.repair_client import _ResumeOutputRelay

    relay = _ResumeOutputRelay()
    path = tmp_path / 'resume.log'
    line = '[17:54:09] （实现阶段：7/19）继续验证'
    encoded = (line + '\n').encode()
    relay.drain(path, report)
    path.write_bytes(encoded[:25])
    relay.drain(path, report)
    assert report.presenter.stream.getvalue() == ''
    with path.open('ab') as output:
        output.write(encoded[25:])
    relay.drain(path, report)
    relay.drain(path, report)
    assert report.presenter.stream.getvalue().splitlines() == [line]
    assert line not in history(report)
    with path.open('ab') as output:
        output.write(b'last line without newline')
    relay.drain(path, report, final=True)
    assert report.presenter.stream.getvalue().splitlines()[-1] == 'last line without newline'


def test_resumed_command_output_uses_live_presenter(report, screen, tmp_path):
    from auto_agents.repair_client import _ResumeOutputRelay

    path = tmp_path / 'resume.log'
    path.write_text('[17:54:09] （实现阶段：7/19）继续验证\n')
    _ResumeOutputRelay().drain(path, report)
    assert screen.history == ['[17:54:09] （实现阶段：7/19）继续验证']
    assert report.presenter.stream.getvalue() == ''


def test_repair_diagnosis_uses_actual_provider_output(report, screen, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('auto_agents.execution_display.time.monotonic', lambda: clock[0])
    report.repair('diagnosing')
    capture = report.capture(kind='provider')
    capture.start(['provider'], {}, provider='codex', capture_mode='live')
    capture('stdout', 'actual diagnosis output')
    clock[0] += 4
    assert '[引擎自修复] 定位故障原因 | 最近输出 4 秒前' in frame(report)
    capture.finish(returncode=0)
    report.repair_update(repair_job('plan'), {'state': 'waiting'})
    assert screen.history[-1].endswith('[引擎自修复] 定位故障原因')
    assert '最近输出' not in frame(report)


def test_repair_short_actions_between_polls_are_not_lost(report):
    report.repair_update(repair_job('implement', sequence=10), {'state': 'waiting'})
    report.repair_update(repair_job('validate', sequence=13, transitions=[
        {'phase': 'implement', 'sequence': 10}, {'phase': 'audit', 'sequence': 11},
        {'phase': 'boundary_preflight', 'sequence': 12}, {'phase': 'validate', 'sequence': 13},
    ]), {'state': 'waiting'})
    assert [line.split('[引擎自修复] ')[1] for line in history(report).splitlines()] == [
        '编码', '检查测试完整性', '检查任务恢复', '验证']


def test_repair_display_can_resume_old_worker_and_keep_health_state_unchanged(report, tmp_path):
    from test_repair_control import registration, failure
    from auto_agents.repair_control import Store
    store = Store(tmp_path / 'control')
    subscriber = store.register(registration(tmp_path / 'project'))
    identity = store.submit(subscriber, failure(tmp_path / 'project'))
    store.transition(identity, 'repairing')
    store.event(identity, 'phase_started', {'phase': 'implement'})
    # Older retained workers supply item events rather than agent_output.
    store.event(identity, 'agent_progress', {'role': 'implement', 'event': 'item/completed'})
    report.repair_update(store.job(identity, include_progress=True), {'state': 'waiting'})
    assert '编码 | 最近输出' in frame(report)
    stamp = report.display.output_times['']
    store.event(identity, 'agent_progress', {'role': 'implement', 'event': 'thread/status/changed'})
    report.repair_update(store.job(identity, include_progress=True), {'state': 'waiting'})
    assert abs(report.display.output_times[''] - stamp) < .1
    assert not (report.project_root / '.auto-agents/state/run_state.json').exists()


def test_repair_english_and_confirmed_handoff_finalize_without_stale_checks(report, screen):
    report.language = 'en'
    report.repair_update(repair_job('plan'), {'state': 'waiting'})
    assert '[Engine repair] Planning' in frame(report)
    job = {**repair_job(), 'state': 'completed'}
    report.repair_update(job, {'state': 'resuming', 'payload': {'recovery_confirmed': {
        'job': job['id'], 'generation': job['generation']}}})
    assert frame(report) == ''
    assert screen.history[-1].endswith('[Engine repair] Original task resumed')


@pytest.mark.parametrize('error,expected', [
    ('repair made no verified progress after its bounded rediagnosis', '连续修正未取得新的验证进展'),
    ('恢复验证证据不完整；候选已保留，需要更新验证控制器后重新检查原任务。', '原任务恢复证据不完整'),
])
def test_repair_blocker_displays_actionable_reason_without_internal_review_text(report, error, expected):
    report.repair_update({**repair_job(), 'state': 'blocked', 'result': {'error': error}}, {'state': 'blocked'})
    assert expected in history(report)
    assert 'bounded rediagnosis' not in history(report)


def test_each_new_verification_round_can_report_failure(report):
    setup_tasks(report)
    report.task("T1", "任务1", "verify", 1)
    for _ in range(2):
        observation = GateObservation(None, report, 1)
        observation.finish(GateResult(ok=False, commands=[result(ok=False)], summary="failed"))
        report.task_result("T1", "任务1", "verify", "fail")
    assert history(report).count("验证未通过") == 2


def test_renderer_failure_falls_back_to_plain_action_without_losing_history(report, screen):
    setup_tasks(report)
    report.task("T1", "任务1", "implement", 1)
    def broken_update(*args, **kwargs):
        raise RuntimeError("terminal unavailable")
    screen.update = broken_update
    # Finish must tolerate rendering failure just like the periodic refresh.
    report.task("T1", "任务1", "verify", 1)
    assert report.presenter._live is None
    value = report.presenter.stream.getvalue()
    assert value.count("编码") == 1 and value.count("验证") == 1
    assert "最近输出" not in value


def test_terminated_worker_does_not_leave_a_live_action(report, tmp_path):
    setup_tasks(report)
    worker = report.child(tmp_path / "worker", "T1")
    try:
        worker.task("T1", "任务1", "implement", 1)
        worker.exception(RuntimeError("stopped"))
        assert frame(report) == ""
    finally:
        worker.close()


def test_repair_retries_and_terminal_failure_survive_one_poll(report, tmp_path):
    from test_repair_control import registration, failure
    from auto_agents.repair_control import Store
    store = Store(tmp_path / 'control')
    subscriber = store.register(registration(tmp_path / 'project'))
    identity = store.submit(subscriber, failure(tmp_path / 'project'))
    store.transition(identity, 'repairing')
    store.event(identity, 'phase_started', {'phase': 'implement', 'attempt': 0})
    report.repair_update(store.job(identity, include_progress=True), {'state': 'waiting'})
    finding = {'requirement': 'internal', 'reason': 'private verbose review',
               'summary_zh': '原任务恢复证据不足', 'summary_en': 'Task recovery evidence is incomplete'}
    store.event(identity, 'acceptance_failed', {'attempt': 1, 'failures': [finding]})
    store.event(identity, 'phase_started', {'phase': 'plan', 'attempt': 1, 'replans': 1})
    store.event(identity, 'phase_started', {'phase': 'implement', 'attempt': 1, 'correcting': True})
    store.event(identity, 'acceptance_failed', {'attempt': 2, 'failures': [finding]})
    store.transition(identity, 'blocked', {'error': 'repair made no verified progress after its bounded rediagnosis'})
    job = store.job(identity, include_progress=True)
    report.repair_update(job, {'state': 'blocked'})
    value = history(report)
    expected = ['第1轮 · 编码', '第1轮 · 验收未通过：原任务恢复证据不足',
                '重新规划：调整修复方案', '第2轮 · 继续编码：修正上一轮未通过项',
                '第2轮 · 验收未通过：原任务恢复证据不足', '连续修正未取得新的验证进展']
    positions = [value.index(label) for label in expected]
    assert positions == sorted(positions)
    assert '剩余：原任务恢复证据不足' in value
    assert '累计2轮' in value
    assert 'private verbose review' not in value
    report.repair_update(job, {'state': 'blocked'})
    assert history(report) == value
    assert frame(report) == ''


def test_repair_first_poll_marks_history_and_keeps_current_counts(report):
    report.repair_update(repair_job('validate', sequence=3, attempt=1, review_running=True,
        checks={'completed': 4, 'total': 169}, transitions=[
            {'phase': 'implement', 'sequence': 1, 'attempt': 0},
            {'phase': 'audit', 'sequence': 2, 'attempt': 1},
            {'phase': 'validate', 'sequence': 3, 'attempt': 1},
        ]), {'state': 'waiting'})
    assert '历史记录 · 第1轮 · 编码' in history(report)
    assert '第1轮 · 验证与审查 |' in frame(report)
    assert '4/169' in frame(report)
    assert '历史记录' not in frame(report)


def test_repair_summary_is_bounded_safe_and_supports_legacy_events():
    from auto_agents.repair_display import failure_summary
    assert failure_summary([{'unit': 'original-boundary'}]) == '原任务恢复检查未通过'
    assert failure_summary([{'requirement': 'old', 'reason': 'long private text'}]) == '审查发现问题'
    assert failure_summary([{'requirement': 'x', 'summary_zh': 'password=secret /private/path'}]) == '审查发现问题'
    assert failure_summary([{'requirement': 'x', 'summary_en': 'Task recovery evidence is incomplete'}], 'en') == 'Task recovery evidence is incomplete'
    assert failure_summary([{'requirement': 'x', 'summary_zh': '长' * 100}]) == '审查发现问题'
