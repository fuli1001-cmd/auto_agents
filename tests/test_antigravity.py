import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_agents.adapters.antigravity import (
    INLINE_PROMPT_MAX_BYTES,
    AntigravityAdapter,
    SETTINGS_PATH,
)
from auto_agents.models import AgentRequest, AgentResult, ProviderConfig


def test_antigravity_available():
    config = ProviderConfig(kind="antigravity", binary="agy")
    adapter = AntigravityAdapter(config)

    with patch("shutil.which", return_value="/usr/local/bin/agy"):
        assert adapter.available() is True

    with patch("shutil.which", return_value=None):
        assert adapter.available() is False


def test_antigravity_config_normalizes_legacy_stdin_transport():
    config = ProviderConfig.from_dict(
        {
            "kind": "antigravity",
            "binary": "agy",
            "prompt_via_stdin": True,
        }
    )

    assert config.prompt_via_stdin is False


def test_antigravity_build_command():
    config = ProviderConfig(
        kind="antigravity",
        binary="agy",
        timeout_seconds=600,
        extra_args=["--sandbox"],
        profile_map={},
    )
    adapter = AntigravityAdapter(config)
    request = AgentRequest(
        stage="implement",
        effort="deep",
        prompt="Write a python script",
        cwd=Path("/tmp/myproject"),
        output_path=Path("/tmp/out"),
    )

    command = adapter._build_command(request)
    assert command == [
        "agy",
        "--dangerously-skip-permissions",
        "--add-dir",
        "/tmp/myproject",
        "--print-timeout",
        "600s",
        "--sandbox",
        "--print",
        "Write a python script",
    ]


def test_antigravity_build_command_stages_oversized_prompt(tmp_path):
    config = ProviderConfig(kind="antigravity", binary="agy")
    adapter = AntigravityAdapter(config)
    prompt = "x" * (INLINE_PROMPT_MAX_BYTES + 1)
    request = AgentRequest(
        stage="review",
        effort="deep",
        prompt=prompt,
        cwd=tmp_path,
        output_path=tmp_path / "out.md",
    )

    command = adapter._build_command(request)

    assert command[-2] == "--print"
    assert prompt not in command
    staged_files = list(
        (tmp_path / ".auto-agents" / "runs" / "provider-prompts").glob("*.txt")
    )
    assert len(staged_files) == 1
    assert staged_files[0].read_text(encoding="utf-8") == prompt
    assert str(staged_files[0]) in command[-1]


def test_antigravity_run_settings_override_and_restoration(tmp_path):
    # Setup paths
    settings_file = tmp_path / "settings.json"
    initial_settings = {"colorScheme": "dark", "model": "old-model"}
    settings_file.write_text(json.dumps(initial_settings), encoding="utf-8")

    config = ProviderConfig(
        kind="antigravity",
        binary="agy",
        profile_map={
            "balanced": "Gemini 3.5 Flash (High)",
            "deep": "Gemini 3.5 Flash (High)",
        },
    )
    adapter = AntigravityAdapter(config)
    
    # We patch SETTINGS_PATH and SETTINGS_PATH.parent
    parent_mock = MagicMock()
    parent_mock.exists.return_value = True

    # Build request
    output_file = tmp_path / "out.txt"
    request = AgentRequest(
        stage="implement",
        effort="deep",
        prompt="Write code",
        cwd=tmp_path,
        output_path=output_file,
    )

    # Mock subprocess execution
    mock_run = MagicMock(return_value=("output from agy", "", 0, True, False))

    with patch("auto_agents.adapters.antigravity.SETTINGS_PATH", settings_file), \
         patch("auto_agents.adapters.antigravity.run_subprocess_with_optional_streaming", mock_run):
        
        # Before run, settings has old-model
        assert json.loads(settings_file.read_text(encoding="utf-8"))["model"] == "old-model"

        # Under the hood during run, settings is temporarily overwritten with "Gemini 3.5 Flash (High)"
        # and restored afterwards. Let's verify that run succeeds and settings is restored.
        result = adapter.run(request)

        assert result.ok is True
        assert result.summary == "output from agy"
        assert result.model == "Gemini 3.5 Flash (High)"
        
        # After run, settings is restored back to old-model
        assert json.loads(settings_file.read_text(encoding="utf-8"))["model"] == "old-model"

        command = mock_run.call_args.args[0]
        assert command[-2:] == ["--print", "Write code"]
        assert mock_run.call_args.kwargs["stdin_input"] == ""


def test_antigravity_rejects_response_to_its_own_flag(tmp_path):
    config = ProviderConfig(kind="antigravity", binary="agy", profile_map={})
    adapter = AntigravityAdapter(config)
    request = AgentRequest(
        stage="review",
        effort="deep",
        prompt="Review this task",
        cwd=tmp_path,
        output_path=tmp_path / "out.md",
    )
    bad_response = (
        "It looks like you've entered `--dangerously-skip-permissions`, which is a "
        "CLI startup flag rather than a task."
    )

    with patch(
        "auto_agents.adapters.antigravity.run_subprocess_with_optional_streaming",
        return_value=(bad_response, "", 0, False, False),
    ):
        result = adapter.run(request)

    assert result.ok is False
    assert result.returncode == 0
    assert result.summary == bad_response
    assert "provider protocol error" in result.stderr
