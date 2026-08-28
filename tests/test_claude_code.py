import json
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.adapters.claude_code import (
    ClaudeCodeAdapter,
    ClaudeProgressDecoder,
)
from auto_agents.config import load_project_config, save_project_config
from auto_agents.models import AgentRequest, ProviderConfig
from auto_agents.orchestrator import Orchestrator


def _request(tmp_path: Path, **overrides) -> AgentRequest:
    defaults = {
        "stage": "implement",
        "effort": "deep",
        "prompt": "Write a python script",
        "cwd": tmp_path,
        "output_path": tmp_path / "agent.md",
    }
    defaults.update(overrides)
    return AgentRequest(**defaults)


def _stream_jsonl(events: list) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


SUCCESS_STREAM = _stream_jsonl(
    [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-1",
            "model": "claude-opus-5",
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Working on it."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "pytest"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "3 passed"}
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "sess-1",
            "result": "Done. All tests pass.",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10,
            },
        },
    ]
)


def test_claude_code_available():
    config = ProviderConfig(kind="claude-code", binary="claude")
    adapter = ClaudeCodeAdapter(config)

    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        assert adapter.available() is True

    with patch("shutil.which", return_value=None):
        assert adapter.available() is False

    assert adapter.supports_image_attachments() is True


def test_claude_code_config_defaults_for_kind():
    config = ProviderConfig.from_dict({"kind": "claude-code"})

    assert config.binary == "claude"
    assert config.cwd_flag == ""
    assert config.output_flag == ""
    assert config.prompt_via_stdin is True
    assert config.profile_map == {"balanced": "sonnet", "deep": "opus", "max": "opus"}
    assert config.timeout_seconds == 3600


def test_runtime_provider_override_adds_claude_to_legacy_config(tmp_path):
    project_root = tmp_path / "legacy-project"
    Orchestrator.init_project(project_root, "legacy-project")
    config = load_project_config(project_root)
    config.providers.pop("claude-code")
    save_project_config(project_root, config)

    orchestrator = Orchestrator(project_root)
    orchestrator._set_active_provider("claude-code")

    persisted = load_project_config(project_root)
    assert persisted.active_provider == "claude-code"
    assert persisted.providers["claude-code"].binary == "claude"
    assert persisted.providers["claude-code"].profile_map["deep"] == "opus"
    assert isinstance(orchestrator.adapter, ClaudeCodeAdapter)

    orchestrator.config.provider.extra_args = ["--model=claude-haiku-4-5"]
    assert (
        orchestrator._model_label_for_agent_stage("implement", "deep")
        == "claude-haiku-4-5"
    )


def test_claude_code_build_command_uses_stdin_transport(tmp_path):
    config = ProviderConfig(
        kind="claude-code",
        binary="claude",
        profile_map={"balanced": "sonnet", "deep": "opus"},
    )
    adapter = ClaudeCodeAdapter(config)

    command = adapter._build_command(_request(tmp_path))

    assert command == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "opus",
        "--dangerously-skip-permissions",
    ]
    # The prompt travels over stdin, never as a command-line argument.
    assert "Write a python script" not in command


def test_claude_code_build_command_inline_prompt(tmp_path):
    config = ProviderConfig(kind="claude-code", binary="claude", prompt_via_stdin=False)
    adapter = ClaudeCodeAdapter(config)

    command = adapter._build_command(_request(tmp_path))

    assert command[-1] == "Write a python script"


def test_claude_code_build_command_resume_and_attachments(tmp_path):
    config = ProviderConfig(kind="claude-code", binary="claude", profile_map={})
    adapter = ClaudeCodeAdapter(config)
    request = _request(
        tmp_path,
        resume_session_id="session-123",
        attachments=[tmp_path / "comparison.png"],
    )

    command = adapter._build_command(request)

    assert command[command.index("--resume") + 1] == "session-123"
    assert str(tmp_path / "comparison.png") not in command
    assert "Attached image files" in adapter._effective_prompt(request)
    assert str(tmp_path / "comparison.png") in adapter._effective_prompt(request)


def test_claude_code_permission_mapping(tmp_path):
    request = _request(tmp_path)

    skipping = ClaudeCodeAdapter(ProviderConfig(kind="claude-code", binary="claude"))
    assert skipping._permission_args(request) == ["--dangerously-skip-permissions"]

    read_only = ClaudeCodeAdapter(ProviderConfig(kind="claude-code", binary="claude"))
    assert read_only._permission_args(_request(tmp_path, sandbox_mode="read-only")) == [
        "--permission-mode",
        "dontAsk",
    ]

    overridden = ClaudeCodeAdapter(
        ProviderConfig(
            kind="claude-code",
            binary="claude",
            extra_args=["--permission-mode", "acceptEdits"],
        )
    )
    assert overridden._permission_args(request) == []


def test_claude_code_explicit_model_flag_wins(tmp_path):
    config = ProviderConfig(
        kind="claude-code",
        binary="claude",
        profile_map={"deep": "opus"},
        extra_args=["--model", "claude-haiku-4-5"],
    )
    adapter = ClaudeCodeAdapter(config)

    command = adapter._build_command(_request(tmp_path))

    assert command.count("--model") == 1
    assert command[command.index("--model") + 1] == "claude-haiku-4-5"
    assert adapter._model_label(_request(tmp_path)) == "claude-haiku-4-5"


def test_claude_code_equals_style_overrides_win(tmp_path):
    config = ProviderConfig(
        kind="claude-code",
        binary="claude",
        profile_map={"deep": "opus"},
        extra_args=["--model=claude-haiku-4-5", "--permission-mode=dontAsk"],
    )
    adapter = ClaudeCodeAdapter(config)

    command = adapter._build_command(_request(tmp_path))

    assert "--model" not in command
    assert "--dangerously-skip-permissions" not in command
    assert "--model=claude-haiku-4-5" in command
    assert "--permission-mode=dontAsk" in command
    assert adapter._model_label(_request(tmp_path)) == "claude-haiku-4-5"


def test_claude_code_run_parses_stream_and_writes_summary(tmp_path):
    config = ProviderConfig(kind="claude-code", binary="claude", profile_map={})
    adapter = ClaudeCodeAdapter(config)
    request = _request(tmp_path)

    with patch(
        "auto_agents.adapters.claude_code.run_subprocess_with_optional_streaming",
        return_value=(SUCCESS_STREAM, "", 0, False, False),
    ) as run_mock:
        result = adapter.run(request)

    assert result.ok is True
    assert result.returncode == 0
    assert result.summary == "Done. All tests pass."
    assert (tmp_path / "agent.md").read_text(encoding="utf-8").strip() == (
        "Done. All tests pass."
    )
    assert result.stdout == "Working on it.\n"
    assert result.provider_session_id == "sess-1"
    assert result.usage is not None
    # AgentUsage.input_tokens follows the Codex convention of total input,
    # while Claude reports uncached, cache-created, and cache-read input separately.
    assert result.usage.input_tokens == 150
    assert result.usage.cached_input_tokens == 40
    assert result.usage.output_tokens == 50
    assert result.model == "default"
    assert run_mock.call_args.kwargs["stdin_input"] == "Write a python script"
    assert run_mock.call_args.kwargs["provider"] == "claude-code"
    assert run_mock.call_args.args[0][0] == "claude"


def test_claude_code_run_routes_error_results(tmp_path):
    error_stream = _stream_jsonl(
        [
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "session_id": "sess-2",
                "result": "Exceeded maximum turns.",
            }
        ]
    )
    adapter = ClaudeCodeAdapter(ProviderConfig(kind="claude-code", binary="claude"))
    request = _request(tmp_path)

    with patch(
        "auto_agents.adapters.claude_code.run_subprocess_with_optional_streaming",
        return_value=(error_stream, "", 0, False, False),
    ):
        result = adapter.run(request)

    assert result.ok is False
    assert "Exceeded maximum turns." in result.stderr
    assert result.summary != "Exceeded maximum turns."


def test_claude_code_run_routes_errors_array_when_result_is_empty(tmp_path):
    error_stream = _stream_jsonl(
        [
            {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "errors": ["Budget limit reached."],
            }
        ]
    )
    adapter = ClaudeCodeAdapter(ProviderConfig(kind="claude-code", binary="claude"))

    with patch(
        "auto_agents.adapters.claude_code.run_subprocess_with_optional_streaming",
        return_value=(error_stream, "", 0, False, False),
    ):
        result = adapter.run(_request(tmp_path))

    assert result.ok is False
    assert result.stderr == "Budget limit reached."


def test_claude_code_run_falls_back_to_visible_stdout(tmp_path):
    adapter = ClaudeCodeAdapter(ProviderConfig(kind="claude-code", binary="claude"))
    request = _request(tmp_path)
    stream = _stream_jsonl(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Partial answer."}]},
            }
        ]
    )

    with patch(
        "auto_agents.adapters.claude_code.run_subprocess_with_optional_streaming",
        return_value=(stream, "", 0, False, False),
    ):
        result = adapter.run(request)

    assert result.ok is True
    assert result.summary == "Partial answer."
    assert (tmp_path / "agent.md").read_text(encoding="utf-8").strip() == "Partial answer."


def test_claude_decoder_extracts_session_tools_and_completion():
    decoder = ClaudeProgressDecoder()

    init = list(
        decoder.feed(
            "stdout",
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}) + "\n",
        )
    )
    tool = list(
        decoder.feed(
            "stdout",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "Bash",
                                "input": {"command": "pytest"},
                            }
                        ]
                    },
                }
            )
            + "\n",
        )
    )
    completed = list(
        decoder.feed(
            "stdout",
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}
                        ]
                    },
                }
            )
            + "\n",
        )
    )
    done = list(decoder.feed("stdout", json.dumps({"type": "result", "subtype": "success"}) + "\n"))

    assert init[0].kind == "activity"
    assert init[0].session_id == "sess-1"
    assert tool[0].kind == "tool_started"
    assert tool[0].tool_id == "tool-1"
    assert tool[0].detail == "Bash"
    assert tool[0].fingerprint
    assert completed[0].kind == "tool_completed"
    assert completed[0].tool_id == "tool-1"
    assert completed[0].semantic is True
    assert done[0].kind == "completed"


def test_claude_decoder_tracks_server_tool_blocks():
    decoder = ClaudeProgressDecoder()

    started = list(
        decoder.feed(
            "stdout",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "server_tool_use",
                                "id": "server-tool-1",
                                "name": "web_search",
                                "input": {"query": "official docs"},
                            }
                        ]
                    },
                }
            )
            + "\n",
        )
    )
    completed = list(
        decoder.feed(
            "stdout",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "web_search_tool_result",
                                "tool_use_id": "server-tool-1",
                                "content": {"type": "web_search_result", "results": []},
                            }
                        ]
                    },
                }
            )
            + "\n",
        )
    )

    assert started[0].kind == "tool_started"
    assert started[0].tool_id == "server-tool-1"
    assert completed[0].kind == "tool_completed"
    assert completed[0].tool_id == "server-tool-1"
    assert completed[0].semantic is True


def test_claude_decoder_error_result_terminates():
    decoder = ClaudeProgressDecoder()

    events = list(
        decoder.feed(
            "stdout",
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "tool crashed",
                }
            )
            + "\n",
        )
    )

    assert events[0].kind == "error"
    assert events[0].detail == "tool crashed"


def test_claude_decoder_rejects_invalid_json():
    decoder = ClaudeProgressDecoder()

    with pytest.raises(ValueError, match="invalid JSON"):
        decoder.feed("stdout", "not json\n")

    assert decoder.feed("stdout", "\n") == ()


def test_claude_stream_filter_forwards_text_and_errors():
    forwarded: list = []

    def callback(stream_name: str, chunk: str) -> None:
        forwarded.append((stream_name, chunk))

    filtered = ClaudeCodeAdapter._make_json_stream_filter(callback)

    filtered("stdout", json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
    ) + "\n")
    filtered("stdout", json.dumps({"type": "user", "message": {"content": []}}) + "\n")
    filtered("stdout", json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "boom",
        }
    ) + "\n")
    filtered("stderr", "raw stderr\n")
    filtered("stdout", "not json\n")

    assert ("stdout", "hello\n") in forwarded
    assert ("stderr", "boom\n") in forwarded
    assert ("stderr", "raw stderr\n") in forwarded
    assert ("stdout", "not json\n") in forwarded
    # Exactly the four visible chunks above; the protocol-only user event is
    # consumed silently.
    assert len(forwarded) == 4
