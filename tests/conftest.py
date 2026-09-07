import pytest


@pytest.fixture(autouse=True)
def isolate_repair_control(monkeypatch):
    """Unit tests never enroll real workflows in the installation daemon.

    Control-plane tests explicitly opt in with private state and local remotes.
    """
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", "1")
