import pytest
import json
import tempfile
from pathlib import Path


@pytest.fixture(autouse=True)
def isolate_optional_supervision(monkeypatch):
    """Tests use explicit private watcher instances instead of operator state."""
    monkeypatch.setenv("AUTO_AGENTS_NO_SUPERVISOR", "1")


@pytest.fixture(autouse=True)
def isolate_verification_state(tmp_path, monkeypatch):
    """Tests never publish certificates or consume slots in operator state."""
    monkeypatch.setenv("AUTO_AGENTS_VERIFICATION_ROOT", str(tmp_path / "verification-state"))
    monkeypatch.setenv("AUTO_AGENTS_WORKER_ROOT", str(tmp_path / "worker-state"))
    monkeypatch.setenv("AUTO_AGENTS_CLUSTER_HOME", str(tmp_path / "cluster-state"))
    monkeypatch.setenv("AUTO_AGENTS_STORAGE_ROOT", str(tmp_path / "storage-state"))
    monkeypatch.setenv("AUTO_AGENTS_STORAGE_MAINTENANCE", "off")
    monkeypatch.setenv("AUTO_AGENTS_STORAGE_EPHEMERAL", "1")
    from auto_agents import artifact_runtime
    token = artifact_runtime._context.set(None)
    try:
        yield
    finally:
        artifact_runtime.release_owned()
        artifact_runtime._context.reset(token)


@pytest.fixture(autouse=True)
def isolate_short_verification_runtime(monkeypatch):
    """Fixed fixture job IDs must never collide with operator /tmp runtimes."""
    from auto_agents.verification_input_trace import file_identity
    with tempfile.TemporaryDirectory(prefix='aagt-',dir='/tmp') as directory:
        monkeypatch.setenv('AUTO_AGENTS_VERIFICATION_RUNTIME_ROOT', directory)
        monkeypatch.setenv('AUTO_AGENTS_VERIFICATION_RUNTIME_ID',
                           json.dumps(file_identity(Path(directory).stat())))
        yield
