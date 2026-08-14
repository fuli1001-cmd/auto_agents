from __future__ import annotations

import subprocess
import tempfile
import io
import contextlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_agents.cli import main
from auto_agents.foreground_activity import ForegroundActivity, foreground_active
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.git_ops import add_worktree, list_worktrees
from auto_agents.models import GateConfig
from auto_agents.release_jobs import ReleaseJobStore
from auto_agents.release_worker import (
    _is_infrastructure_failure,
    _process_job,
    _recover_failure,
    ensure_release_worker,
)


def _repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
    (root / "value.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "value.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=root, check=True)


def test_release_jobs_coalesce_to_latest_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        first = store.enqueue(source="fix:first", affected_proof_ids=["affected.one"])

        (root / "value.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "add", "value.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "two"], cwd=root, check=True)
        second = store.enqueue(source="fix:second", affected_proof_ids=["affected.two"])

        assert first["job_id"] != second["job_id"]
        assert store.get(str(first["job_id"]))["status"] == "superseded"
        assert store.latest()["job_id"] == second["job_id"]
        claimed = store.claim_latest()
        assert claimed is not None
        assert claimed["status"] == "running"
        passed = store.complete(
            str(claimed["job_id"]),
            {"ok": True, "proof_ids": ["release.all"], "logical_commands": 1},
        )
        assert passed["status"] == "passed"


def test_same_passed_candidate_is_not_requeued() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        job = store.enqueue(source="run:first", affected_proof_ids=[])
        store.complete(str(job["job_id"]), {"ok": True})

        duplicate = store.enqueue(source="run:again", affected_proof_ids=[])

        assert duplicate["job_id"] == job["job_id"]
        assert duplicate["status"] == "passed"


def test_abandoned_active_job_is_requeued_after_worker_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        store.enqueue(source="run:first", affected_proof_ids=[])
        claimed = store.claim_latest()
        assert claimed is not None and claimed["status"] == "running"

        assert store.requeue_abandoned() == 1
        assert store.latest()["status"] == "pending"


def test_infrastructure_classification_requires_every_failure_to_be_infrastructure() -> None:
    assert _is_infrastructure_failure(
        {
            "commands": [
                {"ok": False, "infrastructure_failure": True},
                {"ok": False, "termination_reason": "timeout"},
            ]
        }
    )
    assert not _is_infrastructure_failure(
        {
            "commands": [
                {"ok": False, "infrastructure_failure": True},
                {"ok": False, "stderr": "assertion failed"},
            ]
        }
    )
    assert not _is_infrastructure_failure({"commands": []})


def test_release_recovery_runs_failed_proof_then_affected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        worker = Mock()
        worker.project_root = root
        worker.config.efforts = {"implement": "deep"}

        def edit_candidate(*args, **kwargs):
            (root / "value.txt").write_text("fixed\n", encoding="utf-8")

        worker._run_agent_with_retries.side_effect = edit_candidate
        targeted = Mock(ok=True)
        worker._run_gate_commands_for_commands.return_value = (targeted, "")
        worker.run_verification.return_value = {"ok": True}
        result = {
            "reason": "failed",
            "commands": [
                {
                    "command": "pytest -q tests/test_value.py::test_value",
                    "ok": False,
                    "stdout": "",
                    "stderr": "assertion failed",
                }
            ],
        }

        assert _recover_failure(worker, result, job_id="job-one", attempt=1)
        worker._run_gate_commands_for_commands.assert_called_once()
        assert worker.run_verification.call_args.kwargs["level"] == "affected"


def test_release_recovery_rejects_no_progress() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        worker = Mock()
        worker.project_root = root
        worker.config.efforts = {"implement": "deep"}

        with patch("auto_agents.release_worker.working_tree_clean", return_value=True):
            assert not _recover_failure(
                worker,
                {"reason": "failed", "commands": []},
                job_id="job-two",
                attempt=1,
            )


def test_release_worker_marks_isolated_candidate_passed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        root.mkdir()
        _repo(root)
        store = ReleaseJobStore(root)
        job = store.enqueue(source="run:test", affected_proof_ids=[])
        claimed = store.claim_latest()
        assert claimed is not None
        stale_path = (
            Path(tempfile.gettempdir())
            / "auto-agents-release-worktrees"
            / str(claimed["job_id"])
        )
        add_worktree(root, stale_path, ref=str(claimed["candidate_sha"]))
        shutil.rmtree(stale_path)
        assert str(stale_path) in list_worktrees(root)

        class FakeOrchestrator:
            def __init__(
                self,
                project_root,
                agent_output_stream=None,
                gate_cache_path=None,
                gate_preempt_requested=None,
            ):
                self.project_root = Path(project_root)
                self.config = SimpleNamespace(
                    gates=SimpleNamespace(
                        release_worker=SimpleNamespace(
                            background_parallel_workers=1,
                            max_recovery_attempts=2,
                            max_infrastructure_retries=2,
                        ),
                        parallel_workers="auto",
                        max_auto_workers="auto",
                    )
                )

            def run_verification(self, *, level):
                return {
                    "ok": True,
                    "reason": "passed",
                    "proof_ids": ["release.all"],
                    "logical_commands": 1,
                    "executed_commands": 1,
                    "certificate_hits": 0,
                }

        with (
            patch("auto_agents.release_worker.Orchestrator", FakeOrchestrator),
            patch("auto_agents.release_worker.discover_dependency_links", return_value={}),
            patch("auto_agents.release_worker.runtime_status", return_value={"active": False}),
        ):
            _process_job(root, store, claimed, stream=io.StringIO())

        assert store.get(str(job["job_id"]))["status"] == "passed"
        assert str(stale_path) not in list_worktrees(root)


def test_release_worker_routes_unrecoverable_failure_to_needs_user() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        root.mkdir()
        _repo(root)
        store = ReleaseJobStore(root)
        store.enqueue(source="run:test", affected_proof_ids=[])
        claimed = store.claim_latest()
        assert claimed is not None

        class FakeOrchestrator:
            def __init__(
                self,
                project_root,
                agent_output_stream=None,
                gate_cache_path=None,
                gate_preempt_requested=None,
            ):
                self.project_root = Path(project_root)
                self.config = SimpleNamespace(
                    gates=SimpleNamespace(
                        release_worker=SimpleNamespace(
                            background_parallel_workers=1,
                            max_recovery_attempts=1,
                            max_infrastructure_retries=1,
                        ),
                        parallel_workers="auto",
                        max_auto_workers="auto",
                    )
                )

            def run_verification(self, *, level):
                return {
                    "ok": False,
                    "reason": "assertion failed",
                    "commands": [
                        {"ok": False, "command": "pytest test_failure.py", "stderr": "failed"}
                    ],
                }

        with (
            patch("auto_agents.release_worker.Orchestrator", FakeOrchestrator),
            patch("auto_agents.release_worker.discover_dependency_links", return_value={}),
            patch("auto_agents.release_worker.runtime_status", return_value={"active": False}),
            patch("auto_agents.release_worker._recover_failure", return_value=False),
        ):
            _process_job(root, store, claimed, stream=io.StringIO())

        latest = store.get(str(claimed["job_id"]))
        assert latest["status"] == "needs_user"
        assert latest["recovery_attempts"] == 1


def test_attest_requires_exact_clean_head() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        job = store.enqueue(source="release", affected_proof_ids=[])
        store.complete(str(job["job_id"]), {"ok": True})

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert main(["attest", "--project", str(root)]) == 0

        (root / "value.txt").write_text("dirty\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert main(["attest", "--project", str(root)]) == 1


def test_auto_start_launches_detached_low_priority_worker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        class FakeOrchestrator:
            def __init__(self, project_root):
                self.config = SimpleNamespace(
                    gates=SimpleNamespace(
                        release_worker=SimpleNamespace(enabled=True, auto_start=True)
                    )
                )

        process = Mock()
        with (
            patch("auto_agents.release_worker.Orchestrator", FakeOrchestrator),
            patch("auto_agents.release_worker.shutil.which", return_value="/usr/bin/nice"),
            patch("auto_agents.release_worker.subprocess.Popen", process),
        ):
            assert ensure_release_worker(root)

        command = process.call_args.args[0]
        assert command[:3] == ["nice", "-n", "10"]
        assert "release-worker" in command
        assert process.call_args.kwargs["start_new_session"] is True


def test_foreground_activity_lease_is_observable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lease = ForegroundActivity(root)
        assert not foreground_active(root)
        lease.acquire()
        try:
            assert foreground_active(root)
        finally:
            lease.release()
        assert not foreground_active(root)


def test_release_job_defers_without_spending_retry_budget_for_foreground() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        store.enqueue(source="run:test", affected_proof_ids=[])
        claimed = store.claim_latest()
        assert claimed is not None

        with patch("auto_agents.release_worker._foreground_active", return_value=True):
            _process_job(root, store, claimed, stream=io.StringIO())

        latest = store.get(str(claimed["job_id"]))
        assert latest["status"] == "pending"
        assert latest["infrastructure_attempts"] == 0
        assert latest["recovery_attempts"] == 0


def test_local_gate_executor_preempts_before_background_dispatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        root.mkdir()
        _repo(root)
        executor = LocalGatePlanExecutor(
            root,
            GateConfig(),
            {},
            preempt_requested=lambda: True,
        )
        with executor:
            result = executor.run(
                "python -c 'raise SystemExit(99)'",
                timeout_seconds=10,
                adaptive_timeout_enabled=False,
                idle_timeout_seconds=10,
            )
        assert not result.ok
        assert result.termination_reason == "foreground_preempted"
        assert result.infrastructure_failure_id == "foreground_preempted"
