import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.config import (
    config_path,
    load_project_config,
    load_run_state,
    migrate_project_config,
)
from auto_agents.io_utils import read_json, write_json, write_text
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.run_lock import ProjectRunLock
from auto_agents.workflow_health import WorkflowHealthRuntime


def _restore_legacy_execution_config(project_root: Path) -> bytes:
    path = config_path(project_root)
    payload = read_json(path, default={})
    payload["execution"].pop("acceleration", None)
    payload["execution"]["parallel_tasks"]["enabled"] = False
    write_json(path, payload)
    return path.read_bytes()


class ConfigReadingProviderAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.calls = 0
        self.config_at_call = b""

    def run(self, request):
        self.calls += 1
        self.config_at_call = config_path(self.project_root).read_bytes()
        load_project_config(self.project_root)
        write_text(request.output_path, "provider research complete\n")
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary="provider research complete",
            returncode=0,
        )


class ConfigEditingProviderAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        path = config_path(self.project_root)
        payload = read_json(path, default={})
        payload["project_name"] = "changed-by-provider"
        write_json(path, payload)
        write_text(request.output_path, "provider research complete\n")
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary="provider research complete",
            returncode=0,
        )


def test_ordinary_config_read_does_not_persist_migration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        legacy_bytes = _restore_legacy_execution_config(project_root)

        config = load_project_config(project_root)

        assert config.execution.acceleration.enabled
        assert config.execution.parallel_tasks.enabled
        assert config_path(project_root).read_bytes() == legacy_bytes

        observer = Orchestrator(project_root)
        assert observer.config.execution.acceleration.enabled
        assert config_path(project_root).read_bytes() == legacy_bytes


def test_explicit_config_migration_requires_lifecycle_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        legacy_bytes = _restore_legacy_execution_config(project_root)

        with pytest.raises(
            RuntimeError,
            match="migration requires an acquired ProjectRunLock",
        ):
            migrate_project_config(project_root)

        assert config_path(project_root).read_bytes() == legacy_bytes
        with ProjectRunLock(project_root, environ={}):
            assert migrate_project_config(project_root)
        assert "acceleration" in read_json(config_path(project_root), default={})[
            "execution"
        ]


def test_engine_config_upgrade_precedes_provider_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        orchestrator = Orchestrator(project_root)
        legacy_bytes = _restore_legacy_execution_config(project_root)
        adapter = ConfigReadingProviderAdapter(project_root)
        orchestrator.adapter = adapter
        orchestrator._build_adapter = lambda _config: adapter
        state = load_run_state(project_root)

        with ProjectRunLock(project_root, environ={}):
            result = orchestrator._run_agent_with_retries(
                state=state,
                stage="provider_research",
                stage_key="provider-research-regression",
                prompt="Refresh provider references.",
            )

        persisted = read_json(config_path(project_root), default={})
        assert result.ok
        assert adapter.calls == 1
        assert adapter.config_at_call != legacy_bytes
        assert "acceleration" in persisted["execution"]
        assert persisted["execution"]["parallel_tasks"]["enabled"]


def test_engine_config_upgrade_is_completed_before_provider_snapshot() -> None:
    """Stable proof node retained by persisted root-cause diagnoses."""

    test_engine_config_upgrade_precedes_provider_snapshot()


def test_health_sidecar_starts_after_explicit_config_migration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        orchestrator = Orchestrator(project_root)
        _restore_legacy_execution_config(project_root)
        runtime = WorkflowHealthRuntime(
            project_root,
            workflow_kind="run",
            run_token="test-token",
            enabled=True,
            auto_agents_entry=project_root / "auto_agents.py",
            orchestrator=orchestrator,
        )
        config_seen_at_launch = []

        def observe_launch(**_kwargs):
            config_seen_at_launch.append(
                read_json(config_path(project_root), default={})
            )
            return None

        with ProjectRunLock(project_root, environ={}):
            with patch(
                "auto_agents.workflow_health.start_health_sidecar",
                side_effect=observe_launch,
            ):
                runtime.start(load_run_state(project_root).run_id)

        assert len(config_seen_at_launch) == 1
        execution = config_seen_at_launch[0]["execution"]
        assert "acceleration" in execution
        assert execution["parallel_tasks"]["enabled"]


def test_provider_config_edit_remains_a_scope_violation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        orchestrator = Orchestrator(project_root)
        orchestrator.adapter = ConfigEditingProviderAdapter(project_root)
        state = load_run_state(project_root)

        with ProjectRunLock(project_root, environ={}):
            with pytest.raises(
                RuntimeError,
                match=(
                    r"stage provider_research modified files outside its ownership"
                    r".*config\.json"
                ),
            ):
                orchestrator._run_agent_with_retries(
                    state=state,
                    stage="provider_research",
                    stage_key="provider-research-config-edit",
                    prompt="Refresh provider references.",
                )


def test_restore_point_excludes_transient_sqlite_sidecars() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project_root = root / "demo"
        restore_root = root / "restore"
        Orchestrator.init_project(project_root, "demo", "mock")
        state_root = project_root / ".auto-agents" / "state"
        write_text(state_root / "gate_baseline_cache.sqlite3-wal", "volatile\n")
        write_text(state_root / "gate_baseline_cache.sqlite3-shm", "volatile\n")

        Orchestrator(project_root)._capture_auto_agents_restore_point(restore_root)

        assert not (restore_root / ".auto-agents/state/gate_baseline_cache.sqlite3-wal").exists()
        assert not (restore_root / ".auto-agents/state/gate_baseline_cache.sqlite3-shm").exists()
