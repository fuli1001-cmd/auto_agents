import copy
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.adapters.antigravity import (
    AntigravityAdapter,
    AntigravityProgressDecoder,
)
from auto_agents.adapters.codex import CodexAdapter, CodexProgressDecoder
from auto_agents.adapters.copilot_cli import CopilotCliAdapter, CopilotProgressDecoder
from auto_agents.adapters.shell import ShellProgressDecoder
from auto_agents.adapters.base import run_subprocess_with_optional_streaming
from auto_agents.config import DEFAULT_CONFIG
from auto_agents.models import (
    SMART_TIMEOUT_PROGRESS_PROTOCOL,
    AgentProgressEvent,
    AgentRequest,
    ProviderConfig,
    SmartTimeoutConfig,
)
from auto_agents.supervision import ProgressDecoder, ProgressSupervisor
from auto_agents.validation import validate_project_config_payload


def _request(tmp_path: Path) -> AgentRequest:
    return AgentRequest(
        stage="implement",
        effort="deep",
        prompt="implement the task",
        cwd=tmp_path,
        output_path=tmp_path / "agent.md",
        attempt_id="task-1",
        progress_report_path=tmp_path / "attempt.json",
    )


def _supervisor(
    tmp_path: Path,
    clock: list[float],
    config: SmartTimeoutConfig,
    decoder: Optional[ProgressDecoder] = None,
) -> ProgressSupervisor:
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        return ProgressSupervisor(
            config=config,
            request=_request(tmp_path),
            provider="test",
            process_pid=99999999,
            decoder=decoder,
        )


def test_provider_idle_uses_process_and_protocol_activity_lease(tmp_path):
    clock = [0.0]
    supervisor = _supervisor(
        tmp_path,
        clock,
        SmartTimeoutConfig(provider_idle_seconds=60),
    )

    clock[0] = 61.0
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        assert supervisor.poll() == "provider_idle"


def test_tool_stall_and_semantic_stall_are_distinct(tmp_path):
    clock = [0.0]
    config = SmartTimeoutConfig(
        provider_idle_seconds=600,
        tool_idle_seconds=60,
        semantic_stall_seconds=120,
    )
    supervisor = _supervisor(tmp_path, clock, config, ProgressDecoder())
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        supervisor.observe_events(
            [AgentProgressEvent(kind="tool_started", tool_id="tool-1", detail="pytest")]
        )
        clock[0] = 61.0
        assert supervisor.poll() == "tool_stalled"

    clock = [0.0]
    supervisor = _supervisor(tmp_path, clock, config, ProgressDecoder())
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        supervisor.observe_events([AgentProgressEvent(kind="activity", detail="heartbeat")])
        clock[0] = 100.0
        supervisor.observe_events([AgentProgressEvent(kind="activity", detail="heartbeat")])
        clock[0] = 121.0
        assert supervisor.poll() == "semantic_stall"


def test_active_tool_extends_only_the_safety_ceiling(tmp_path):
    clock = [0.0]
    supervisor = _supervisor(
        tmp_path,
        clock,
        SmartTimeoutConfig(
            provider_idle_seconds=600,
            tool_idle_seconds=600,
            semantic_stall_seconds=600,
            safety_ceiling_seconds=60,
            active_tool_grace_seconds=30,
        ),
        ProgressDecoder(),
    )
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        supervisor.observe_events(
            [AgentProgressEvent(kind="tool_started", tool_id="tool-1", detail="pytest")]
        )
        clock[0] = 61.0
        assert supervisor.poll() is None
        clock[0] = 91.0
        assert supervisor.poll() == "safety_ceiling"


def test_repeated_completed_tool_fingerprint_detects_loop(tmp_path):
    clock = [0.0]
    supervisor = _supervisor(
        tmp_path,
        clock,
        SmartTimeoutConfig(loop_repeat_limit=3),
        ProgressDecoder(),
    )
    event = AgentProgressEvent(
        kind="tool_completed",
        tool_id="tool-1",
        fingerprint="same-command-and-result",
        detail="pytest same_test.py",
        semantic=True,
    )
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        supervisor.observe_events([event, event, event])
        assert supervisor.poll() == "loop_detected"
        assert supervisor.termination("loop_detected").repeat_count == 3


def test_explicit_provider_error_terminates_without_resume_lease(tmp_path):
    clock = [0.0]
    supervisor = _supervisor(tmp_path, clock, SmartTimeoutConfig(), ProgressDecoder())
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        supervisor.observe_events(
            [AgentProgressEvent(kind="error", detail="provider rejected request")]
        )
        assert supervisor.poll() == "provider_error"


def test_checkpoint_persists_session_and_diagnostics(tmp_path):
    clock = [0.0]
    supervisor = _supervisor(tmp_path, clock, SmartTimeoutConfig(), ProgressDecoder())
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        supervisor.observe_events(
            [
                AgentProgressEvent(
                    kind="activity",
                    session_id="session-123",
                    detail="session started",
                )
            ]
        )

    payload = json.loads((tmp_path / "attempt.json").read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert payload["session_id"] == "session-123"
    assert payload["workspace_fingerprint"] == supervisor.workspace_fingerprint
    assert payload["events"][-1]["kind"] == "activity"


def test_smart_runner_terminates_process_group_and_writes_report(tmp_path):
    request = _request(tmp_path)

    result = run_subprocess_with_optional_streaming(
        ["/bin/sh", "-c", "sleep 10"],
        request,
        dict(os.environ),
        smart_timeout=SmartTimeoutConfig(
            provider_idle_seconds=0,
            tool_idle_seconds=60,
            semantic_stall_seconds=60,
            safety_ceiling_seconds=60,
        ),
        provider="shell-test",
    )

    assert result.returncode == -1
    assert result.termination is not None
    assert result.termination.reason == "provider_idle"
    assert "smart timeout: provider idle" in result.stderr
    payload = json.loads((tmp_path / "attempt.json").read_text(encoding="utf-8"))
    assert payload["status"] == "terminated"
    assert payload["reason"] == "provider_idle"


def test_custom_shell_provider_requires_smart_progress_protocol():
    payload = copy.deepcopy(DEFAULT_CONFIG)
    payload["providers"]["wrapper"] = {
        "kind": "shell-wrapper",
        "binary": "wrapper",
        "profile_map": {},
        "extra_args": [],
        "cwd_flag": "",
        "prompt_via_stdin": True,
        "output_flag": "",
    }
    payload["active_provider"] = "wrapper"

    errors = validate_project_config_payload(payload)
    assert any("providers.wrapper.progress_protocol" in error for error in errors)

    payload["providers"]["wrapper"][
        "progress_protocol"
    ] = SMART_TIMEOUT_PROGRESS_PROTOCOL
    errors = validate_project_config_payload(payload)
    assert not any("providers.wrapper.progress_protocol" in error for error in errors)


def test_codex_decoder_extracts_session_and_tool_events():
    decoder = CodexProgressDecoder()
    session = list(
        decoder.feed("stdout", '{"type":"thread.started","thread_id":"thread-1"}\n')
    )
    tool = list(
        decoder.feed(
            "stdout",
            '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"pytest"}}\n',
        )
    )

    assert session[0].session_id == "thread-1"
    assert tool[0].kind == "tool_started"
    with pytest.raises(ValueError, match="invalid JSON"):
        list(decoder.feed("stdout", "not-json\n"))


def test_copilot_decoder_extracts_session_and_tool_events():
    decoder = CopilotProgressDecoder()
    session = list(
        decoder.feed(
            "stdout",
            '{"type":"session.start","data":{"sessionId":"session-1"}}\n',
        )
    )
    tool = list(
        decoder.feed(
            "stdout",
            '{"type":"tool.execution_start","data":{"toolCallId":"t1","toolName":"shell","arguments":{"command":"pytest"}}}\n',
        )
    )

    assert session[0].session_id == "session-1"
    assert tool[0].kind == "tool_started"


def test_native_provider_resume_commands_use_exact_session(tmp_path):
    request = AgentRequest(
        stage="implement",
        effort="deep",
        prompt="continue",
        cwd=tmp_path,
        output_path=tmp_path / "agent.md",
        resume_session_id="session-123",
        attachments=[tmp_path / "comparison.png"],
    )
    codex = CodexAdapter(
        ProviderConfig(kind="codex", binary="codex", extra_args=["--model", "gpt-x"])
    )
    with patch(
        "auto_agents.adapters.codex.run_subprocess_with_optional_streaming",
        return_value=("", "", 0, False, False),
    ) as run_mock:
        codex.run(request)
    codex_command = run_mock.call_args.args[0]
    assert codex_command[:3] == ["codex", "exec", "resume"]
    assert codex_command[-2:] == ["session-123", "-"]
    assert codex_command.index("--model") < codex_command.index("session-123")
    assert codex_command[codex_command.index("--image") + 1] == str(
        tmp_path / "comparison.png"
    )

    copilot = CopilotCliAdapter(
        ProviderConfig(kind="copilot-cli", binary="copilot", profile_map={})
    )
    assert "--resume=session-123" in copilot._build_command(request)

    antigravity = AntigravityAdapter(
        ProviderConfig(kind="antigravity", binary="agy")
    )
    antigravity_command = antigravity._build_command(request)
    conversation_index = antigravity_command.index("--conversation")
    assert antigravity_command[conversation_index + 1] == "session-123"


def test_shell_decoder_requires_versioned_sidecar_protocol(tmp_path):
    progress_path = tmp_path / "progress.jsonl"
    progress_path.write_text(
        json.dumps(
            {
                "protocol": SMART_TIMEOUT_PROGRESS_PROTOCOL,
                "type": "tool.completed",
                "tool_id": "tool-1",
                "fingerprint": "result-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decoder = ShellProgressDecoder(progress_path)

    events = list(decoder.poll())

    assert events[0].kind == "tool_completed"
    assert events[0].semantic is True

    progress_path.write_text(
        json.dumps(
            {
                "protocol": SMART_TIMEOUT_PROGRESS_PROTOCOL,
                "type": "tool.finished",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    invalid_decoder = ShellProgressDecoder(progress_path)
    with pytest.raises(ValueError, match="unsupported shell progress event"):
        list(invalid_decoder.poll())


def test_antigravity_decoder_reads_log_session_and_sqlite_steps(tmp_path):
    settings_path = tmp_path / "settings.json"
    conversations = tmp_path / "conversations"
    conversations.mkdir()
    session_id = "12345678-1234-1234-1234-123456789abc"
    log_path = tmp_path / "agy.log"
    log_path.write_text(f"Created conversation {session_id}\n", encoding="utf-8")
    database = conversations / f"{session_id}.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "create table steps (idx integer, step_type integer, status integer, step_payload blob)"
        )
        connection.execute(
            "insert into steps values (?, ?, ?, ?)",
            (1, 8, 3, sqlite3.Binary(b"tool result")),
        )
        connection.commit()
    finally:
        connection.close()

    decoder = AntigravityProgressDecoder(log_path)
    with patch("auto_agents.adapters.antigravity.SETTINGS_PATH", settings_path):
        events = list(decoder.poll())

    assert any(event.session_id == session_id for event in events)
    assert any(event.kind == "tool_completed" for event in events)
