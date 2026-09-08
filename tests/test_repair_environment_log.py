import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import patch

import pytest

from auto_agents.repair_environment_log import EnvironmentSetupLog, failure_result, sanitize


@pytest.fixture
def config(tmp_path):
    return {"root": str(tmp_path / "controller"), "python": sys.executable}


def engine(tmp_path):
    from auto_agents.repair_runtime import RUNTIME_CAPABILITIES
    root = tmp_path / "engine"
    (root / "src/auto_agents").mkdir(parents=True)
    (root / "src/auto_agents/repair_control.py").write_text("VERSION = 1\n")
    (root / "src/auto_agents/repair_client.py").write_text("")
    (root / "src/auto_agents/repair_runtime.py").write_text(f"RUNTIME_CAPABILITIES = {RUNTIME_CAPABILITIES!r}\n")
    (root / "pyproject.toml").write_text('[project]\nname="example"\n')
    return root


def test_real_failed_command_preserves_redacted_stdout_stderr(config, monkeypatch):
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_JOB", "job-one")
    log = EnvironmentSetupLog(config)
    environment = {**os.environ, "DIAG_TOKEN": "private-token-123",
                   "PIP_INDEX_URL": "https://reader-name:p%40ss-word@mirror.example/simple?token=query-secret"}
    script = (
        "import os,sys; print('install attempt: '+os.environ['DIAG_TOKEN']); "
        "print('Looking in indexes: '+os.environ['PIP_INDEX_URL']); "
        "print('Authorization: Basic secret-base64', file=sys.stderr); "
        "print('ERROR: No matching distribution found for example', file=sys.stderr); sys.exit(7)"
    )
    with pytest.raises(subprocess.CalledProcessError) as caught:
        log.run([sys.executable, "-c", script], env=environment, timeout=5)
    result = failure_result(caught.value)
    assert caught.value.returncode == 7
    paths = result["environment_diagnostics"]
    assert Path(paths["directory"]).is_relative_to(Path(config["root"]) / "jobs/job-one")
    stdout = Path(paths["stdout"]).read_text()
    stderr = Path(paths["stderr"]).read_text()
    metadata = json.loads(Path(paths["metadata"]).read_text())
    assert "install attempt: <redacted>" in stdout
    assert "mirror.example/simple" in stdout
    assert "No matching distribution found" in stderr
    assert metadata["returncode"] == 7
    assert metadata["status"] == "failed"
    all_saved = "\n".join(p.read_text() for p in log.root.rglob("*") if p.is_file())
    for secret in ("private-token-123", "reader-name", "p%40ss-word", "query-secret", "secret-base64"):
        assert secret not in all_saved
    for path in Path(paths["directory"]).iterdir():
        assert path.stat().st_mode & 0o077 == 0


def test_timeout_keeps_partial_bytes_and_original_exception(config):
    log = EnvironmentSetupLog(config)
    script = "import time,sys; print('before timeout',flush=True); print('password=hidden-pass',file=sys.stderr,flush=True); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        log.run([sys.executable, "-c", script], timeout=0.2)
    paths = failure_result(caught.value)["environment_diagnostics"]
    assert "before timeout" in Path(paths["stdout"]).read_text()
    assert "hidden-pass" not in Path(paths["stderr"]).read_text()
    assert json.loads(Path(paths["metadata"]).read_text())["status"] == "timeout"


def test_sanitizes_encoded_environment_secrets_and_all_url_queries():
    environment = {"ACCESS_TOKEN": "abc+def secret"}
    text = "abc+def secret abc%2Bdef%20secret abc%2Bdef+secret https://user:pw@example.org/a?X-Amz-Signature=private&other=secret#fragment"
    saved = sanitize(text, environment)
    for secret in ("abc+def secret", "abc%2Bdef%20secret", "abc%2Bdef+secret", "user:pw", "private", "fragment"):
        assert secret not in saved
    assert "example.org/a" in saved


def test_quoted_password_and_multiline_token_are_fully_redacted():
    text = 'password="two word password"\naccess_token=\'first line\nsecond line\'\nERROR: permission denied'
    saved = sanitize(text, {})
    assert "word password" not in saved
    assert "first line" not in saved and "second line" not in saved
    assert "ERROR: permission denied" in saved


def test_output_is_redacted_before_truncation(config, monkeypatch):
    monkeypatch.setattr("auto_agents.repair_environment_log.MAX_OUTPUT_BYTES", 64)
    monkeypatch.setenv("API_KEY", "sensitive-boundary-value")
    output = b"start " + b"x" * 21 + b"sensitive-boundary-value" + b"x" * 200 + b" end"
    with patch("auto_agents.repair_environment_log.subprocess.run", return_value=subprocess.CompletedProcess(["cmd"], 0, output, b"")):
        log = EnvironmentSetupLog(config)
        log.run(["cmd"])
    data = (log.root / "01/stdout.txt").read_text()
    assert "sensitive" not in data
    assert data.startswith("start ") and data.endswith(" end")
    assert json.loads((log.root / "01/command.json").read_text())["stdout_truncated"]


def test_logging_failure_does_not_replace_install_failure(config):
    error = subprocess.CalledProcessError(9, ["pip"], output=b"out", stderr=b"error")
    with patch("auto_agents.repair_environment_log.subprocess.run", side_effect=error), \
         patch("auto_agents.repair_environment_log.private_directory", side_effect=OSError("disk full")):
        with pytest.raises(subprocess.CalledProcessError) as caught:
            EnvironmentSetupLog(config).run(["pip"])
    assert caught.value is error
    result = failure_result(error)
    assert "disk full" in result["environment_diagnostics_error"]
    assert "environment_diagnostics" not in result


def test_exception_command_uses_the_child_environment_for_redaction(config):
    command = ["pip", "child-only-secret"]
    error = subprocess.CalledProcessError(1, command, stderr=b"failed")
    with patch("auto_agents.repair_environment_log.subprocess.run", side_effect=error):
        with pytest.raises(subprocess.CalledProcessError):
            EnvironmentSetupLog(config).run(command, env={"API_KEY": "child-only-secret"})
    assert "child-only-secret" not in json.dumps(failure_result(error))


def test_all_environment_steps_are_logged_and_freeze_receipt_is_sanitized(config, tmp_path, monkeypatch):
    from auto_agents.repair_worker import engine_environment
    from auto_agents.repair_control import digest
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror-reader:mirror-password@mirror.example/simple")
    frozen = "example @ https://mirror-reader:mirror-password@mirror.example/example.whl?signature=private-signature\n"
    def execute(command, **kwargs):
        output = frozen if command[-1] == "freeze" else b"step passed"
        return subprocess.CompletedProcess(command, 0, output, "" if isinstance(output, str) else b"")
    with patch("auto_agents.repair_worker.subprocess.run", side_effect=execute) as run:
        python, fingerprint = engine_environment(config, engine(tmp_path))
    assert run.call_count == 4
    assert len(list(Path(config["root"]).glob("environment-setup/*/*/command.json"))) == 4
    receipt = json.loads((Path(python).parent.parent / "ready.json").read_text())
    assert receipt["fingerprint"] == fingerprint == digest(frozen)
    assert "mirror-password" not in receipt["dependencies"]
    assert "private-signature" not in receipt["dependencies"]


def test_worker_failure_receipt_points_to_pip_diagnostics(config, tmp_path, monkeypatch):
    from auto_agents import repair_worker
    checkout = engine(tmp_path)
    root = Path(config["root"]) / "jobs/job-one"
    root.mkdir(parents=True)
    request = root / "repair-g2-request.json"
    request.write_text(json.dumps({"config": config, "job": {"id": "job-one", "generation": 2}, "operation": "repair"}))
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_JOB", "job-one")
    monkeypatch.setattr(sys, "argv", ["repair_worker", str(request)])
    monkeypatch.setattr("auto_agents.artifact_runtime.activate", lambda **kwargs: None)
    monkeypatch.setattr("auto_agents.artifact_runtime.schedule", lambda: None)
    monkeypatch.setattr(repair_worker, "repair", lambda request: repair_worker.engine_environment(config, checkout))
    def execute(command, **kwargs):
        if "install" in command:
            raise subprocess.CalledProcessError(1, command, output=b"Looking in indexes: https://user:pw@mirror.example/simple",
                                                stderr=b"ERROR: Distribution not found\n")
        return subprocess.CompletedProcess(command, 0, b"venv created", b"")
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with patch("auto_agents.repair_worker.subprocess.run", side_effect=execute) as run:
            assert repair_worker.main() == 3
    finally:
        signal.signal(signal.SIGTERM, previous)
    result = json.loads((root / "repair-g2-result.json").read_text())
    assert result["generation"] == 2
    assert run.call_count == 2  # No silent retry after the install failed.
    assert "diagnostics:" in result["error"]
    paths = result["environment_diagnostics"]
    assert "Distribution not found" in Path(paths["stderr"]).read_text()
    assert "user:pw" not in Path(paths["stdout"]).read_text()
    assert not list(Path(config["root"]).glob("environments/*/ready.json"))
