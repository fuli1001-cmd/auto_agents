from __future__ import annotations

from auto_agents import health_control
from auto_agents.health_control import (
    HealthControlChannel,
    control_path,
    request_health_state,
)
from auto_agents.io_utils import read_json, write_json


def test_new_health_command_does_not_replay_a_previous_apply_error(tmp_path):
    enabled = []
    channel = HealthControlChannel(
        tmp_path, workflow_kind="run", run_token="test-run", enabled=False,
        on_enable=lambda payload: enabled.append(payload["generation"]),
        on_disable=lambda payload: None,
    )
    channel.start("run-1")
    try:
        path = control_path(tmp_path)
        payload = read_json(path)
        payload["apply_error"] = "previous sidecar launch failed"
        write_json(path, payload)

        result = request_health_state(tmp_path, enabled=True, timeout_seconds=3)

        assert result["ok"]
        assert result["applied_state"] == "enabled"
        assert result["apply_error"] == ""
        assert enabled == [result["generation"]]
    finally:
        channel.close()


def test_health_command_generation_advances_from_locked_state(tmp_path, monkeypatch):
    channel = HealthControlChannel(
        tmp_path, workflow_kind="run", run_token="test-run", enabled=False,
        on_enable=lambda payload: None, on_disable=lambda payload: None,
    )
    channel.start("run-1")
    mutate = health_control._mutate_control
    advanced = False

    def advance_then_mutate(project, update):
        nonlocal advanced
        if not advanced:
            advanced = True

            def concurrent_command(current):
                current.update(generation=7, applied_generation=7)
                return current

            mutate(project, concurrent_command)
        return mutate(project, update)

    monkeypatch.setattr(health_control, "_mutate_control", advance_then_mutate)
    try:
        result = request_health_state(tmp_path, enabled=True, timeout_seconds=3)

        assert result["generation"] == 8
        assert result["applied_generation"] == 8
    finally:
        channel.close()
