import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_agents.adapters.antigravity import AntigravityAdapter, SETTINGS_PATH
from auto_agents.models import AgentRequest, AgentResult, ProviderConfig


def test_antigravity_available():
    config = ProviderConfig(kind="antigravity", binary="agy-proxy")
    adapter = AntigravityAdapter(config)

    with patch("shutil.which", return_value="/usr/local/bin/agy-proxy"):
        assert adapter.available() is True

    with patch("shutil.which", return_value=None):
        assert adapter.available() is False


def test_antigravity_build_command():
    config = ProviderConfig(
        kind="antigravity",
        binary="agy-proxy",
        timeout_seconds=600,
        extra_args=["--sandbox"],
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
        "agy-proxy",
        "-p",
        "--dangerously-skip-permissions",
        "--add-dir",
        "/tmp/myproject",
        "--print-timeout",
        "600s",
        "--sandbox",
    ]


def test_antigravity_run_settings_override_and_restoration(tmp_path):
    # Setup paths
    settings_file = tmp_path / "settings.json"
    initial_settings = {"colorScheme": "dark", "model": "old-model"}
    settings_file.write_text(json.dumps(initial_settings), encoding="utf-8")

    config = ProviderConfig(
        kind="antigravity",
        binary="agy-proxy",
        profile_map={
            "balanced": "gemini-2.5-flash",
            "deep": "Claude Opus 4.6 (Thinking)",
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

        # Under the hood during run, settings is temporarily overwritten with "Claude Opus 4.6 (Thinking)"
        # and restored afterwards. Let's verify that run succeeds and settings is restored.
        result = adapter.run(request)

        assert result.ok is True
        assert result.summary == "output from agy"
        assert result.model == "Claude Opus 4.6 (Thinking)"
        
        # After run, settings is restored back to old-model
        assert json.loads(settings_file.read_text(encoding="utf-8"))["model"] == "old-model"
