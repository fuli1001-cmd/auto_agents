import pytest


@pytest.fixture(autouse=True)
def isolate_repair_control(monkeypatch):
    """Unit tests never enroll real workflows in the installation daemon.

    Control-plane tests explicitly opt in with private state and local remotes.
    """
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", "1")


@pytest.fixture(autouse=True)
def isolate_verification_state(tmp_path, monkeypatch):
    """Tests never publish certificates or consume slots in operator state."""
    monkeypatch.setenv("AUTO_AGENTS_VERIFICATION_ROOT", str(tmp_path / "verification-state"))
    monkeypatch.setenv("AUTO_AGENTS_WORKER_ROOT", str(tmp_path / "worker-state"))
    monkeypatch.setenv("AUTO_AGENTS_CLUSTER_HOME", str(tmp_path / "cluster-state"))
    monkeypatch.setenv("AUTO_AGENTS_STORAGE_ROOT", str(tmp_path / "storage-state"))
    monkeypatch.setenv("AUTO_AGENTS_STORAGE_MAINTENANCE", "off")
    from auto_agents import artifact_runtime
    token = artifact_runtime._context.set(None)
    try:
        yield
    finally:
        artifact_runtime.release_owned()
        artifact_runtime._context.reset(token)
