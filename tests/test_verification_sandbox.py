import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from auto_agents.verification_sandbox import verification_argv


def test_missing_sandbox_blocks_instead_of_running_unrestricted(tmp_path):
    with patch("auto_agents.verification_sandbox.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="needs a local Codex sandbox"):
            with verification_argv(["true"], tmp_path / "candidate", tmp_path / "project"):
                raise AssertionError("unrestricted fallback")


def test_verification_cannot_grant_write_access_to_live_project(tmp_path):
    with pytest.raises(RuntimeError, match="overlaps"):
        with verification_argv(["true"], tmp_path, tmp_path / "project"):
            pass


@pytest.mark.skipif(shutil.which("codex") is None, reason="local Codex sandbox not installed")
def test_actual_sandbox_protects_live_inputs_and_keeps_candidate_writable(tmp_path):
    project, candidate = tmp_path / "project", tmp_path / "candidate"
    project.mkdir()
    candidate.mkdir()
    live = project / "input.txt"
    live.write_text("original")
    code = (
        "from pathlib import Path; Path('result.txt').write_text('allowed'); "
        f"p=Path({str(live)!r}); "
        "\ntry: p.write_text('bad'); blocked=False\n"
        "except OSError: blocked=True\n"
        "assert blocked"
    )
    with verification_argv([sys.executable, "-c", code], candidate, project) as command:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert live.read_text() == "original"
    assert (candidate / "result.txt").read_text() == "allowed"
