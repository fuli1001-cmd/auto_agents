from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.adapters.base import run_subprocess_with_optional_streaming
from auto_agents.cli import build_parser
from auto_agents.diagnostic_output import diagnostic_attachments, copy_diagnostic_attachments
from auto_agents.health_watch import build_progress_vector, _activity_payload
from auto_agents.logging_utils import build_run_logger, attach_run_file_logger, read_diagnostic_log
from auto_agents.models import AgentRequest, RunState, TaskSpec
from auto_agents.process_supervision import run_supervised_shell_command
from auto_agents.reporting import Reporter, ReportingRuntime


@pytest.fixture
def report(tmp_path):
    stream = io.StringIO()
    reporter = Reporter(tmp_path, stream, language="zh")
    reporter.bind("run", "example")
    yield reporter, stream
    reporter.close()


def task(identifier="T1", status="pending", title="权限检查"):
    return TaskSpec(identifier, title, "description", ["works"], status=status)


def events(reporter):
    return [json.loads(line) for line in (reporter.root / "events.jsonl").read_text().splitlines()]


def test_progress_replans_rewinds_and_does_not_count_attempts(report):
    reporter, stream = report
    state = RunState("example", current_stage="implement", tasks=[task(status="done"), task("T2")])
    reporter.observe_run(state)
    assert reporter.snapshot.done == 1
    original_plan = reporter.snapshot.plan_id
    state.tasks[1].review_history.append({"attempt": 3})
    reporter.observe_run(state)
    assert reporter.snapshot.plan_id == original_plan
    reporter.task("T2", "验证", "retry", 4)
    assert len(reporter.snapshot.tasks) == 2
    state.tasks[1:] = [task("T2a"), task("T2b")]
    reporter.observe_run(state)
    assert "计划调整：2 → 3" in stream.getvalue()
    assert reporter.snapshot.done == 1
    state.tasks[0].status = "pending"
    reporter.rewind("plan")
    reporter.observe_run(state)
    assert reporter.snapshot.done == 0
    assert "返回计划" in stream.getvalue()
    assert not any("\x1b" in str(record) for record in events(reporter))


def test_same_count_replacement_is_a_plan_change(report):
    reporter, stream = report
    state = RunState("example", tasks=[task()])
    reporter.observe_run(state)
    state.tasks = [task("replacement")]
    reporter.observe_run(state)
    assert "计划调整：1 → 1" in stream.getvalue()


def test_worker_done_waits_for_main_integration(report, tmp_path):
    reporter, stream = report
    state = RunState("example", current_stage="implement", tasks=[task()])
    reporter.observe_run(state)
    worker = reporter.child(tmp_path / "worker", "T1")
    worker.task("T1", "权限检查", "implement", 1)
    worker.observe_run(RunState("example", tasks=[task(status="done")]))
    assert reporter.active_tasks["T1"] == "waiting_integration"
    assert reporter.snapshot.done == 0
    state.tasks[0].status = "done"
    reporter.observe_run(state)
    assert reporter.snapshot.done == 1
    assert "T1" not in reporter.active_tasks
    worker.close()


def test_simple_console_keeps_diagnostics_and_normalizes_old_evidence(report):
    reporter, stream = report
    logger = build_run_logger(stream, reporter)
    log_path = attach_run_file_logger(logger, reporter.root / "run.log")
    logger.info("[agent:implement] provider=fake model=deep\nsecond line")
    logger.debug("debug-only evidence")
    assert "model=deep" not in stream.getvalue()
    assert "debug-only evidence" not in log_path.read_text()
    assert read_diagnostic_log(log_path) == "[agent:implement] provider=fake model=deep\nsecond line\n"
    assert all(line.startswith("[aa-log ") for line in log_path.read_text().splitlines())
    assert any(event["message"] == "debug-only evidence" for event in events(reporter))
    reporter.presenter.configure("debug", False)
    logger.info("visible detail")
    assert "visible detail" in stream.getvalue()
    legacy = reporter.root / "legacy.log"
    legacy.write_text("old diagnostic\n")
    assert read_diagnostic_log(legacy) == "old diagnostic\n"


def test_log_handlers_do_not_leak_across_runs(report):
    reporter, stream = report
    logger = build_run_logger(stream, reporter)
    first = attach_run_file_logger(logger, reporter.root / "run.log")
    logger.info("first subject")
    reporter.bind("run", "second")
    logger.info("second subject")
    assert "second subject" not in first.read_text()
    assert "second subject" in (reporter.root / "run.log").read_text()


def test_parent_view_is_restored_after_nested_session(report):
    reporter, stream = report
    parent_root = reporter.root
    state = RunState("example", current_stage="implement", tasks=[task()])
    reporter.observe_run(state)
    with reporter.preserve_subject():
        reporter.bind("fix", "child", goal="repair child")
        reporter.text("child event")
    assert reporter.root == parent_root
    assert reporter.snapshot.subject == "example"
    assert reporter.snapshot.tasks["T1"]["status"] == "pending"
    assert any(item.get("kind") == "index" for item in reporter._artifacts.values())


def test_user_heartbeat_does_not_change_health_inputs(report):
    reporter, stream = report
    state = RunState("example", tasks=[task()])
    logger = build_run_logger(stream, reporter)
    log_path = attach_run_file_logger(logger, reporter.root / "run.log")
    logger.info("real diagnostic event")
    before_stat = log_path.stat()
    before_vector = build_progress_vector(state)
    before_activity = _activity_payload(reporter.project_root, state)
    reporter.emit("heartbeat", stage="实现", elapsed="00:10:00")
    after_activity = _activity_payload(reporter.project_root, state)
    assert log_path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert log_path.stat().st_size == before_stat.st_size
    assert build_progress_vector(state) == before_vector
    assert before_activity == after_activity


def test_capture_redacts_split_secrets_and_is_available_in_private_snapshot(report, tmp_path):
    reporter, stream = report
    capture = reporter.capture(attempt_id="try-one", kind="provider")
    capture.start(["provider"], {"OPENAI_API_KEY": "private-key-value"})
    capture("stdout", "api_key=sec")
    capture("stdout", "ret123\nAuthorization: Bear")
    capture("stdout", "er abcdefg\nprivate-")
    capture("stdout", "key-value\n")
    capture.finish(returncode=1)
    output = (capture.root / "stdout.txt").read_text()
    assert all(secret not in output for secret in ("secret123", "abcdefg", "private-key-value"))
    attachments = diagnostic_attachments(tmp_path, "example")
    assert attachments
    copied = copy_diagnostic_attachments(attachments, tmp_path / "snapshot")
    assert len(copied) == len(attachments)
    for artifact in copied:
        assert Path(artifact["path"]).is_file()
    assert not str(capture.root) in str([item["path"] for item in copied])


@pytest.mark.parametrize("stream_transport", [False, True])
def test_diagnostic_capture_does_not_mark_output_as_visible(report, tmp_path, stream_transport):
    reporter, stream = report
    request = AgentRequest("implement", "deep", "", tmp_path, tmp_path / "out",
                           stream_transport=stream_transport)
    result = run_subprocess_with_optional_streaming(
        [sys.executable, "-c", "import sys; print('raw evidence'); print('failure detail', file=sys.stderr)"],
        request, dict(os.environ), timeout=5,
    )
    assert result.returncode == 0
    assert not result.streamed_stdout and not result.streamed_stderr
    assert "raw evidence" not in stream.getvalue()
    output_files = list((reporter.root / "diagnostic-output").glob("*/stdout.txt"))
    assert any("raw evidence" in path.read_text() for path in output_files)


def test_full_gate_output_is_saved_before_returned_tail_is_bounded(report, tmp_path, monkeypatch):
    import auto_agents.process_supervision as supervision
    original = supervision._bounded_output
    monkeypatch.setattr(supervision, "_bounded_output", lambda source: original(source, limit=64))
    reporter, stream = report
    result = run_supervised_shell_command(
        f"{sys.executable} -c \"print('EARLY FAILURE'); print('x' * 1000); print('END')\"",
        cwd=tmp_path, timeout_seconds=5,
    )
    assert "EARLY FAILURE" not in result.stdout
    outputs = list((reporter.root / "diagnostic-output").glob("*/stdout.txt"))
    assert any("EARLY FAILURE" in path.read_text() for path in outputs)


def test_cli_options_and_saved_mode_override(tmp_path):
    parser = build_parser()
    args = parser.parse_args(["run", "--project", str(tmp_path), "--log-mode", "plain"])
    root = tmp_path / ".auto-agents"
    (root / "state").mkdir(parents=True)
    (root / "state/run_state.json").write_text(json.dumps({"run_id": "saved"}))
    (root / "runs/saved").mkdir(parents=True)
    (root / "runs/saved/diagnostics.json").write_text(json.dumps({
        "presentation": {"log_mode": "debug", "print_agent_output": True},
    }))
    runtime = ReportingRuntime(args)
    assert runtime.presenter.mode == "plain"
    assert args.print_agent_output is True
    runtime.close()
    args = parser.parse_args(["run", "--project", str(tmp_path)])
    runtime = ReportingRuntime(args)
    assert runtime.presenter.mode == "debug"
    runtime.close()


def test_capture_failure_does_not_escape_or_repeat(report, monkeypatch):
    reporter, stream = report
    import auto_agents.diagnostic_output as output
    monkeypatch.setattr(output, "atomic_json", lambda *a: (_ for _ in ()).throw(OSError("disk unavailable")))
    capture = reporter.capture()
    capture("stdout", "some output\n")
    capture.finish(returncode=1)
    capture.finish()
    assert stream.getvalue().count("诊断记录不完整") == 1


def test_jsonl_event_ids_remain_unique_across_resume(tmp_path):
    stream = io.StringIO()
    first = Reporter(tmp_path, stream)
    first.bind("run", "example")
    first.text("before restart")
    first.close()
    second = Reporter(tmp_path, stream)
    second.bind("run", "example")
    second.text("after restart")
    ids = [event["event_id"] for event in events(second)]
    assert len(ids) == len(set(ids))
    second.close()


def test_late_worker_does_not_replace_current_task_or_clobber_index(report, tmp_path):
    reporter, stream = report
    reporter.observe_run(RunState("example", tasks=[task()]))
    old = reporter.child(tmp_path / "old", "T1")
    current = reporter.child(tmp_path / "new", "T1")
    current.task("T1", "权限检查", "review", 2)
    old.task("T1", "权限检查", "implement", 1)
    old.observe_run(RunState("example", tasks=[task(status="done")]))
    assert reporter.active_tasks["T1"] == "review"
    capture = current.capture()
    capture("stdout", "worker diagnostic\n")
    capture.finish()
    before = json.loads((reporter.root / "diagnostics.json").read_text())["artifacts"]
    old.close()
    current.close()
    after = json.loads((reporter.root / "diagnostics.json").read_text())["artifacts"]
    assert before == after


def test_parallel_legacy_logs_reach_owning_run_once(report, tmp_path):
    reporter, stream = report
    logger = build_run_logger(stream, reporter)
    attach_run_file_logger(logger, reporter.root / "run.log")
    child = reporter.child(tmp_path / "worker", "T1")
    worker_logger = build_run_logger(stream, child)
    worker_logger.info("[task:T1] diagnostic from worker")
    assert read_diagnostic_log(reporter.root / "run.log").count("diagnostic from worker") == 1
    assert "diagnostic from worker" not in stream.getvalue()
    child.close()
    logger.info("parent still has its file handler")
    assert "parent still" in read_diagnostic_log(reporter.root / "run.log")


def test_presentation_options_wait_for_session_selection(tmp_path):
    args = build_parser().parse_args(["collab", "--project", str(tmp_path)])
    runtime = ReportingRuntime(args)
    reporter = Reporter(tmp_path, io.StringIO(), presenter=runtime.presenter, runtime=runtime)
    index = tmp_path / ".auto-agents/state/sessions/chosen/logs/diagnostics.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"presentation": {"log_mode": "plain", "print_agent_output": True}}))
    reporter.text("select a session")
    reporter.bind("collab", "chosen")
    assert runtime.presenter.mode == "plain"
    assert args.print_agent_output is True
    runtime.close()


def test_live_panel_does_not_redirect_streams_and_cleans_up(tmp_path, monkeypatch):
    pytest.importorskip("rich")
    from rich.text import Text
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    monkeypatch.setenv("TERM", "xterm")
    reporter = Reporter(tmp_path, stream, language="zh")
    reporter.bind("run", "tty")
    reporter.observe_run(RunState("tty", current_stage="implement",
                                 tasks=[task(str(n), "done" if n == 0 else "pending") for n in range(5)]))
    for n in range(1, 5):
        reporter.task(str(n), "权限检查", "implement", 1)
    frame = reporter.presenter._frame(reporter)
    assert "1/5" in frame and "+1" in frame and "处理中 4" in frame
    assert reporter.presenter._live is not None
    reporter.presenter._live.update(Text(frame), refresh=True)
    with reporter.presenter.input():
        reporter.text("请确认下一步")
        assert reporter.presenter._suspended == 1
        assert "\x1b[?25h" in stream.getvalue()
    assert sys.stdout is original_stdout and sys.stderr is original_stderr
    reporter.close()
    assert reporter.presenter._live is None
    assert "\x1b" in stream.getvalue()
    assert "\x1b" not in (reporter.root / "user.log").read_text()


def test_renderer_failure_falls_back_to_plain_without_losing_question(tmp_path, monkeypatch):
    live = pytest.importorskip("rich.live")
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    class BrokenLive:
        def __init__(self, *args, **kwargs):
            raise OSError("terminal unavailable")
    monkeypatch.setattr(live, "Live", BrokenLive)
    stream = Terminal()
    reporter = Reporter(tmp_path, stream)
    reporter.bind("run", "failed-renderer")
    reporter.text("Question for the user")
    assert "Question for the user" in stream.getvalue()
    assert reporter.presenter._live is None
    reporter.close()


def test_controls_are_removed_from_logs_without_mutating_diagnostic_records(report):
    reporter, stream = report
    reporter.text("\x1b[31muser message\x1b[0m")
    logger = build_run_logger(stream, reporter)
    attach_run_file_logger(logger, reporter.root / "run.log")
    logger.info("\x1b[31mtechnical diagnostic\x1b[0m")
    assert "\x1b" not in stream.getvalue()
    assert "\x1b" not in (reporter.root / "run.log").read_text()
    assert "technical diagnostic" in read_diagnostic_log(reporter.root / "run.log")


def test_certificate_uses_output_content_and_not_capture_paths(tmp_path):
    from auto_agents.root_cause import RootCauseCoordinator
    coordinator = RootCauseCoordinator.__new__(RootCauseCoordinator)
    coordinator.auto_agents_root = tmp_path / "engine"
    coordinator.target_root = tmp_path / "project"
    coordinator.diagnostic_auto_root = tmp_path / "engine-snapshot"
    coordinator.diagnostic_target_root = tmp_path / "project-snapshot"
    first = {"diagnostic_attachments": [{"path": "/tmp/first/stdout.txt", "kind": "stdout", "sha256": "a" * 64}]}
    second = {"diagnostic_attachments": [{"path": "/tmp/second/stdout.txt", "kind": "stdout", "sha256": "a" * 64}]}
    assert coordinator._canonical_certificate_evidence(first) == coordinator._canonical_certificate_evidence(second)
    second["diagnostic_attachments"][0]["sha256"] = "b" * 64
    assert coordinator._canonical_certificate_evidence(first) != coordinator._canonical_certificate_evidence(second)


def test_visible_shell_output_without_final_newline_is_flushed(tmp_path):
    from auto_agents.orchestrator import Orchestrator
    project = tmp_path / "project"
    Orchestrator.init_project(project, "demo", "mock")
    stream = io.StringIO()
    orchestrator = Orchestrator(project, agent_output_stream=stream)
    callback = orchestrator._stream_agent_output_callback("implement")
    request = AgentRequest("implement", "deep", "", project, project / "out", stream_output=callback)
    result = run_subprocess_with_optional_streaming(
        [sys.executable, "-c", "print('last partial line', end='')"], request, dict(os.environ), timeout=5,
    )
    assert result.returncode == 0
    assert stream.getvalue().count("last partial line") == 1
    orchestrator.reporter.close()


def test_late_worker_diagnostics_keep_their_original_run(report, tmp_path):
    reporter, stream = report
    logger = build_run_logger(stream, reporter)
    attach_run_file_logger(logger, reporter.root / "run.log")
    original = reporter.root
    child = reporter.child(tmp_path / "worker", "T1")
    child_logger = build_run_logger(stream, child)
    reporter.bind("run", "next-run")
    child_logger.info("[task:T1] late result")
    assert "late result" in read_diagnostic_log(original / "run.log")
    assert "late result" not in read_diagnostic_log(reporter.root / "run.log")
    assert all(event["subject_id"] != "example" for event in events(reporter))
    child.close()


def test_malformed_optional_logging_metadata_cannot_block_a_run(tmp_path):
    root = tmp_path / ".auto-agents/runs/example"
    root.mkdir(parents=True)
    (root / "diagnostics.json").write_text(json.dumps({
        "artifacts": ["invalid old index"], "progress": "invalid",
        "presentation": {"log_mode": ["invalid"], "print_agent_output": "false"},
    }))
    stream = io.StringIO()
    reporter = Reporter(tmp_path, stream)
    reporter.bind("run", "example")
    reporter.observe_run(RunState("example", tasks=[task()]))
    assert reporter.snapshot.subject == "example"
    assert "Current plan" in stream.getvalue()
    reporter.close()


def test_live_stage_is_not_replaced_by_an_unchanged_persisted_stage(report):
    reporter, stream = report
    state = RunState("example", workflow_version=2, current_stage="verify", tasks=[task(status="done")])
    reporter.observe_run(state)
    reporter.stage("readme")
    state.agent_attempts["readme"] = 1
    reporter.observe_run(state)
    assert reporter.snapshot.stage == "readme"
    state.current_stage = "plan"
    reporter.observe_run(state)
    assert reporter.snapshot.stage == "plan"


def test_legacy_prototype_skip_is_not_displayed_as_remaining_work(report):
    reporter, stream = report
    state = RunState("example", current_stage="implement", stage_summaries={"design": "done"})
    reporter.observe_run(state)
    assert reporter.snapshot.stages["prototype"] == "skipped"


def test_repair_validation_capture_does_not_pollute_original_incident_evidence(report):
    from auto_agents.gates import run_commands
    reporter, stream = report
    def progress(*args):
        pass
    progress.reporter = reporter
    progress.context = "self_repair"
    progress.stage = "self_repair_validation"
    result = run_commands(
        [f"{sys.executable} -c \"print('repair verification output')\""],
        reporter.project_root, progress=progress,
    )
    assert result.ok
    outputs = list((reporter.root / "diagnostic-output").glob("*/stdout.txt"))
    assert any("repair verification output" in path.read_text() for path in outputs)
    assert diagnostic_attachments(reporter.project_root, "example") == []


def test_repair_handoff_parent_does_not_overwrite_resumed_diagnostic_index(report):
    parent, stream = report
    parent.handoff()
    child = Reporter(parent.project_root, stream)
    child.bind("run", "example")
    capture = child.capture(attempt_id="after-restart")
    capture("stdout", "new process evidence\n")
    capture.finish()
    child.close()
    before = (child.root / "diagnostics.json").read_bytes()
    parent.close()
    assert (child.root / "diagnostics.json").read_bytes() == before
    assert "after-restart" in before.decode()
    assert parent.presenter.external_owner


def test_repair_launcher_hands_off_display_and_preserves_process_contract(report, monkeypatch):
    import auto_agents.cli as cli
    from unittest.mock import Mock
    reporter, stream = report
    process = SimpleNamespace(pid=12345, returncode=0, poll=lambda: 0)
    popen = Mock(return_value=process)
    registry = Mock()
    registry.register.return_value = SimpleNamespace(pgid=12345)
    monkeypatch.setattr(cli, "find_reporter", lambda: reporter)
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(cli, "ACTIVE_PROCESSES", registry)
    monkeypatch.setattr(cli, "process_group_exists", lambda group: False)
    code = cli._run_self_repair_resume_process(["repaired-cli"], cwd=reporter.project_root, env={}, pass_fd=7)
    assert code == 0 and reporter._handed_off
    assert reporter.presenter.external_owner
    assert popen.call_args.kwargs["pass_fds"] == (7,)
    assert popen.call_args.kwargs["start_new_session"] is True
    registry.unregister.assert_called_once_with(12345, preserve_if_alive=False)


def test_failed_repair_launch_returns_display_ownership(report, monkeypatch):
    import auto_agents.cli as cli
    reporter, stream = report
    monkeypatch.setattr(cli, "find_reporter", lambda: reporter)
    def failed(*args, **kwargs):
        raise OSError("could not launch")
    monkeypatch.setattr(cli.subprocess, "Popen", failed)
    with pytest.raises(OSError, match="could not launch"):
        cli._run_self_repair_resume_process(["repaired-cli"], cwd=reporter.project_root, env={}, pass_fd=7)
    assert not reporter._handed_off and not reporter.presenter.external_owner


def test_closed_reporter_cannot_recreate_a_removed_artifact_directory(report):
    import shutil
    reporter, stream = report
    capture = reporter.capture()
    capture("stdout", "partial")
    logger = build_run_logger(stream, reporter)
    attach_run_file_logger(logger, reporter.root / "run.log")
    child = reporter.child(reporter.project_root / "worker", "T1")
    worker_logger = build_run_logger(stream, child)
    queued_handlers = list(worker_logger.handlers)
    reporter.close()
    child.close()
    shutil.rmtree(reporter.root)
    reporter.text("late event")
    capture.start("late command", {})
    capture("stdout", "late bytes\n")
    reporter.capture()("stderr", "late capture\n")
    for handler in queued_handlers:
        handler.handle(logging.LogRecord("late.worker", logging.INFO, "", 1, "late record", (), None))
    assert not reporter.root.exists()


def test_output_callback_waiting_for_lock_cannot_write_after_finish(report):
    import shutil
    import threading
    reporter, stream = report
    capture = reporter.capture()
    ready = threading.Event()
    def late():
        ready.set()
        capture("stdout", "late callback\n")
    with capture._lock:
        thread = threading.Thread(target=late)
        thread.start()
        assert ready.wait(2)
        capture.finish()
        shutil.rmtree(capture.root)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not capture.root.exists()
