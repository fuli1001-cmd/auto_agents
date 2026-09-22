from __future__ import annotations

import io
import pytest

from auto_agents.models import CommandResult, GateResult, RunState, TaskSpec
from auto_agents.reporting import GateObservation, Reporter


@pytest.fixture
def report(tmp_path):
    reporter = Reporter(tmp_path, io.StringIO(), language="zh")
    reporter.bind("run", "progress")
    yield reporter
    reporter.close()


def setup_tasks(reporter):
    state = RunState("progress", current_stage="implement", tasks=[
        TaskSpec("T1", "恢复策略", "description", ["works"]),
        TaskSpec("T2", "状态显示", "description", ["works"]),
    ])
    reporter.observe_run(state)
    reporter.stage("implement")
    return state


def frame(reporter):
    return reporter.presenter._frame(reporter)


def result(command="pytest tests/test_recovery.py", ok=True, **kwargs):
    return CommandResult(command=command, ok=ok, returncode=0 if ok else 1, **kwargs)


def test_pending_save_does_not_hide_live_task_and_frames_do_not_append_logs(report):
    state = setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 2)
    report.observe_run(state)
    before = (report.root / "user.log").read_text()
    for _ in range(3):
        value = frame(report)
        assert "实现 → 任务验证" in value
        assert "0/2" in value and "T1「恢复策略」" in value and "第 2 次尝试" in value
        assert "等待执行" not in value
        assert "本次运行" not in value and "阶段 00:" not in value
    assert (report.root / "user.log").read_text() == before
    assert state.status == "pending"  # Presentation never changes business state.


def test_action_and_task_changes_update_panel_and_leave_history(report):
    state = setup_tasks(report)
    report.task("T1", "恢复策略", "implement", 1)
    assert "编写代码" in frame(report)
    report.task("T1", "恢复策略", "review", 1)
    assert "代码审查" in frame(report) and "编写代码" not in frame(report)
    state.tasks[0].status = "done"
    report.observe_run(state)
    report.task("T2", "状态显示", "implement", 1)
    value = frame(report)
    assert "1/2" in value and "T2「状态显示」" in value and "T1「" not in value
    history = (report.root / "user.log").read_text()
    assert "[实现/T1]" in history and "审查" in history and "已完成「恢复策略」" in history
    assert "[实现/T2]" in history


def test_current_check_duration_updates_but_total_duration_is_absent(report, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("auto_agents.execution_display.time.monotonic", lambda: clock[0])
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 2)
    observation = GateObservation(None, report, 18)
    observation("start", "pytest tests/test_recovery.py --password private", 0)
    clock[0] += 258
    value = frame(report)
    assert "验证进度 0/18" in value and "检查 1 (tests/test_recovery.py)" in value
    assert "已执行 00:04:18" in value and "private" not in value and "password" not in value
    assert "本次运行" not in value and "阶段 00:" not in value
    observation.result(result())
    assert "验证进度 1/18" in frame(report)
    observation.finish(GateResult(ok=True, commands=[result()], summary="partial"))
    assert "已执行" not in frame(report) and "验证结束 1/18" in frame(report)


def test_output_age_uses_execution_output_not_render_or_heartbeat(report, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("auto_agents.execution_display.time.monotonic", lambda: clock[0])
    setup_tasks(report)
    report.task("T1", "恢复策略", "implement", 1)
    capture = report.capture(kind="provider")
    capture.start(["provider"], {}, provider="codex", capture_mode="live")
    assert "codex" in frame(report)
    capture("stdout", "first output")
    clock[0] += 12
    report.heartbeat()
    assert "最近输出 12 秒前" in frame(report)
    clock[0] += 1
    assert "最近输出 13 秒前" in frame(report)
    capture.finish(returncode=0)
    assert "处理执行结果" in frame(report) and "服务调用：" not in frame(report)
    assert "服务调用结束，处理执行结果" in (report.root / "user.log").read_text()


def test_refresh_each_second_and_context_heartbeat_every_minute(report, monkeypatch):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    clock = [0.0]
    monkeypatch.setattr("auto_agents.reporting.time.monotonic", lambda: clock[0])
    presenter = report.presenter
    presenter._last_event = presenter._last_heartbeat = 0.0
    monkeypatch.setattr(presenter, "_ensure_started", lambda: None)
    waits = []
    class Stop:
        def set(self):
            pass

        def wait(self, seconds):
            waits.append(seconds)
            clock[0] += seconds
            return clock[0] > 180
    monkeypatch.setattr(presenter, "_stop", Stop())
    presenter._refresh_loop()
    history = (report.root / "user.log").read_text()
    assert waits == [1.0] * 181
    assert history.count("实现 → 任务验证") == 3
    assert history.count("第 1 次尝试") == 4  # Start event plus three heartbeats.
    assert report.display.last_output is None


def test_finished_failed_cancelled_checks_keep_distinct_results(report):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    observation = GateObservation(None, report, 3)
    commands = [result(), result("pytest tests/test_failed.py", False),
                result("pytest tests/test_cancelled.py", False, termination_reason="cancelled")]
    observation.result(commands[0])
    observation.finish(GateResult(ok=False, commands=commands, summary="failed"))
    value = frame(report)
    assert "验证结束 2/3，通过 1，失败 1，取消 1" in value
    assert "→ 任务验证" not in value
    history = (report.root / "user.log").read_text()
    assert "正在验证" in history and "验证结束：" in history
    assert "test_failed.py" in history and "未通过" in history
    assert "test_cancelled.py" in history and "已取消" in history
    assert observation.counts["passed"] == 1


def test_parallel_task_checks_and_late_retry_results_are_isolated(report, tmp_path):
    setup_tasks(report)
    first = report.child(tmp_path / "first", "T1")
    second = report.child(tmp_path / "second", "T2")
    try:
        first.task("T1", "恢复策略", "verify", 1)
        second.task("T2", "状态显示", "verify", 1)
        one, two = GateObservation(None, first, 2), GateObservation(None, second, 5)
        one.result(result())
        two.result(result(ok=False))
        value = frame(report)
        assert "T1 验证进度 1/2，通过 1，失败 0" in value
        assert "T2 验证进度 1/5，通过 0，失败 1" in value
        first.task("T1", "恢复策略", "verify", 2)
        current = GateObservation(None, first, 4)
        one.finish(GateResult(ok=True, commands=[result()], summary="late"))
        assert "T1 验证进度 0/4" in frame(report)
        assert "T2 验证进度 1/5" in frame(report)
        assert current.view.finished is False
    finally:
        first.close()
        second.close()


def test_rewind_invalidates_old_workers_checks_and_output(report, tmp_path):
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
        capture("stdout", "late output\n")
        assert "计划" in frame(report)
        assert "T1「" not in frame(report) and "验证进度" not in frame(report)
        assert report.display.last_output is None
        assert "返回计划" in (report.root / "user.log").read_text()
    finally:
        worker.close()


@pytest.mark.parametrize("status", ["blocked", "completed", "waiting_user", "paused", "failed"])
def test_terminal_state_wins_over_active_checks(report, status):
    state = setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    observation = GateObservation(None, report, 2)
    state.status = status
    report.observe_run(state)
    observation("start", "pytest tests/test_old.py", 0)
    assert "任务验证" not in frame(report) and "已执行" not in frame(report)
    assert not report.active_tasks and not report.display.checks


def test_gate_exception_closes_current_check(report, monkeypatch):
    from auto_agents.gates import run_gate_plan
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    def fail(*args, **kwargs):
        kwargs["progress"]("start", "pytest tests/test_recovery.py", 0)
        raise RuntimeError("execution failed")
    monkeypatch.setattr("auto_agents.gates._run_phased_gate_plan", fail)
    with pytest.raises(RuntimeError, match="execution failed"):
        run_gate_plan(["pytest tests/test_recovery.py"], [], report.project_root, collect_all=True)
    assert "验证中断" in frame(report) and "已执行" not in frame(report)
    assert "验证中断" in (report.root / "user.log").read_text()


def test_resume_does_not_invent_live_actions(report):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    other = Reporter(report.project_root, io.StringIO(), language="zh")
    try:
        other.bind("run", "progress")
        other.observe_run(RunState("progress", current_stage="implement"))
        assert "实现 → 当前动作未知" in frame(other)
        assert "任务验证" not in frame(other)
    finally:
        other.close()


def test_english_narrow_terminal_and_title_control_characters(report):
    from rich.console import Console
    from rich.cells import cell_len
    setup_tasks(report)
    report.language = "en"
    report.task("T1", "unsafe\n\x1b[31mtitle" * 10, "verify", 2)
    report.presenter._console = Console(file=io.StringIO(), width=45)
    value = frame(report)
    assert "Implementation → Task" in value
    assert "\x1b" not in value
    assert all(cell_len(line) <= 45 for line in value.splitlines())
    assert len(value.splitlines()) == 3


def test_submitted_checks_and_scheduler_messages_do_not_invent_execution(report):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    observation = GateObservation(None, report, 1)
    observation("scheduler_start", "mode=overlap slots=2", 0)
    observation("dispatch_parallel", "pytest tests/test_recovery.py", 900)
    value = frame(report)
    assert "检查 1 (tests/test_recovery.py)" in value
    assert "已提交，等待执行结果" in value and "已执行" not in value
    observation("start", "pytest tests/test_recovery.py", 0)
    assert "已执行 00:00:00" in frame(report)


def test_buffered_capture_does_not_claim_live_output(report):
    setup_tasks(report)
    capture = report.capture(kind="gate")
    capture.start("pytest", {}, capture_mode="file")
    assert "输出在结束后收集" in frame(report)
    assert "尚未收到执行输出" not in frame(report)
    capture.finish(returncode=0)
    assert "输出在结束后收集" not in frame(report)


def test_recovery_keeps_business_stage_and_shows_recovery_step(report):
    state = setup_tasks(report)
    state.status = "blocked"
    report.observe_run(state)
    report.repair("diagnosing")
    value = frame(report)
    assert "实现 → 正在修复 auto_agents：" in value
    assert "实现 → 受阻" not in value


def test_real_gate_results_reach_panel_without_raw_command_arguments(report):
    import sys
    from auto_agents.gates import run_gate_plan
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 1)
    commands = [f'{sys.executable} -c "print(123)"', f'{sys.executable} -c "raise SystemExit(1)"']
    gate = run_gate_plan(commands, [], report.project_root, collect_all=True)
    assert not gate.ok
    assert "验证结束 2/2，通过 1，失败 1" in frame(report)
    assert "验证结束：已结束 2/2" in (report.root / "user.log").read_text()
    assert "raise SystemExit" not in (report.root / "user.log").read_text()


def test_blocked_task_keeps_its_identity_and_stops_checks(report):
    setup_tasks(report)
    report.task("T1", "恢复策略", "verify", 2)
    observation = GateObservation(None, report, 1)
    observation("start", "pytest tests/test_recovery.py", 0)
    report.emit("task.blocked", task_id="T1", title="恢复策略")
    assert "实现 → 受阻" in frame(report)
    assert "T1「恢复策略」" in frame(report) and "已执行" not in frame(report)


def test_final_acceptance_results_are_not_rewritten_as_running(report):
    report.stage("verify")
    report.emit("verify.passed")
    report.emit("verify.failed", route="实现")
    history = (report.root / "user.log").read_text()
    assert "最终验收通过" in history
    assert "最终验收未通过；下一步：实现" in history
    assert "正在验证" not in history
