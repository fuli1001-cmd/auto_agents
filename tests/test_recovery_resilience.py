from __future__ import annotations

import tempfile
import unittest
import sys
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gates import GateCommandInfrastructureError
from auto_agents.git_ops import (
    changed_files,
    changed_paths,
    commit_all,
    hard_reset_clean,
    head_ref,
    ref_exists,
)
from auto_agents.io_utils import write_text
from auto_agents.models import CommandResult, GateResult, RunState, TaskSpec
from auto_agents.orchestrator import (
    VERIFY_BASELINE_SCHEMA_VERSION,
    Orchestrator,
)


class RecoveryResilienceTests(unittest.TestCase):
    def _project(self, root: Path) -> Orchestrator:
        Orchestrator.init_project(root, "demo", "mock")
        return Orchestrator(root)

    def test_non_comparable_test_baseline_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="baseline",
                description="",
                acceptance=[],
            )
            command = "python -m pytest -q tests/test_demo.py"
            failed = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stderr="pytest could not start",
                    )
                ],
                summary="command failed",
            )
            orchestrator._build_task_verify_commands = Mock(
                return_value=[command]
            )
            orchestrator._run_missing_baseline_commands = Mock(
                return_value=(failed, "")
            )
            orchestrator._run_verify_failure_identity_diagnostic = Mock(
                return_value=failed
            )
            orchestrator._gate_baseline_cache.put = Mock()

            with self.assertRaises(GateCommandInfrastructureError):
                orchestrator._ensure_task_verify_baseline(task)

            self.assertEqual(task.verify_baseline_ref, "")
            self.assertEqual(task.verify_baseline_failures, [])
            self.assertEqual(task.verify_baseline_schema_version, 0)
            orchestrator._gate_baseline_cache.put.assert_not_called()

    def test_stable_test_baseline_is_versioned_after_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="baseline",
                description="",
                acceptance=[],
            )
            command = "python -m pytest -q tests/test_demo.py"
            failed = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout="FAILED tests/test_demo.py::test_contract - assert 1 == 2",
                    )
                ],
                summary="one failed",
            )
            orchestrator._build_task_verify_commands = Mock(
                return_value=[command]
            )
            orchestrator._run_missing_baseline_commands = Mock(
                return_value=(failed, "")
            )
            orchestrator._gate_baseline_cache.get = Mock(
                side_effect=[None, ["tests/test_demo.py::test_contract"]]
            )
            orchestrator._gate_baseline_cache.put = Mock()

            changed = orchestrator._ensure_task_verify_baseline(task)

            self.assertTrue(changed)
            self.assertTrue(task.verify_baseline_ref)
            self.assertEqual(
                task.verify_baseline_failures,
                ["tests/test_demo.py::test_contract"],
            )
            self.assertEqual(
                task.verify_baseline_schema_version,
                VERIFY_BASELINE_SCHEMA_VERSION,
            )

    def test_legacy_command_test_baseline_requeues_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="baseline",
                description="",
                acceptance=[],
                status="blocked",
                verify_baseline_ref="deadbeef:dirty",
                verify_baseline_failures=[
                    "cmd:python -m pytest -q tests/test_demo.py"
                ],
            )
            state = RunState(
                run_id="run-001",
                status="blocked",
                current_stage="implement",
                tasks=[task],
                last_recovery_route={
                    "task_id": task.task_id,
                    "outcome": "exhausted",
                },
            )

            self.assertTrue(
                orchestrator._normalize_legacy_verify_baselines(state)
            )
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.verify_baseline_ref, "")
            self.assertEqual(task.verify_retry_epoch, 1)
            self.assertEqual(task.recovery_epoch, 1)
            self.assertEqual(
                task.verify_baseline_schema_version,
                VERIFY_BASELINE_SCHEMA_VERSION,
            )
            self.assertFalse(
                orchestrator._normalize_legacy_verify_baselines(state)
            )

    def test_legacy_provider_baseline_rewinds_to_owner_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="provider baseline",
                description="",
                acceptance=[],
                status="blocked",
                review_history=[
                    {
                        "attempt": 1,
                        "summary": (
                            "REQ-102 canonical provider reference lacks "
                            "the required size contract"
                        ),
                    }
                ],
                verify_baseline_ref="deadbeef:dirty",
                verify_baseline_failures=[
                    "cmd:python -m pytest -q tests/test_provider.py"
                ],
            )
            state = RunState(
                run_id="run-001",
                status="blocked",
                current_stage="implement",
                tasks=[task],
            )
            reference = (
                ".auto-agents/docs/provider_references/provider.md"
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value={reference}
            )
            orchestrator._mark_provider_references_needs_refresh = Mock(
                return_value=[reference]
            )

            self.assertTrue(
                orchestrator._normalize_legacy_verify_baselines(state)
            )

            self.assertEqual(state.current_stage, "provider_research")
            self.assertEqual(state.rejected_stage, "provider_research")
            self.assertIn("legacy invalid", state.rejection_reason)
            orchestrator._mark_provider_references_needs_refresh.assert_called_once()

    def test_same_failures_with_changed_candidate_do_not_stop_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="retry",
                description="",
                acceptance=[],
                verify_baseline_schema_version=VERIFY_BASELINE_SCHEMA_VERSION,
            )
            failure_id = "tests/test_demo.py::test_contract"
            orchestrator._record_verify_result(
                task,
                1,
                "fail",
                "failed",
                [failure_id],
            )
            write_text(root / "candidate.py", "changed = True\n")

            analysis = orchestrator._analyze_verify_failure(
                task,
                [failure_id],
            )

            self.assertFalse(analysis["stop_retry"])

    def test_provider_reference_verify_failure_routes_to_owner_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            task = TaskSpec(
                task_id="task-001",
                title="provider contract",
                description="",
                acceptance=[],
                review_history=[
                    {
                        "attempt": 1,
                        "summary": "REQ-102 canonical document is incomplete",
                    }
                ],
            )
            orchestrator._provider_reference_paths_from_review = Mock(
                return_value={
                    ".auto-agents/docs/provider_references/provider.md"
                }
            )

            stage, feedback = orchestrator._verification_failure_owner_route(
                task,
                {
                    "reason": (
                        "tests/test_contract.py::"
                        "test_canonical_reference_records_sizes failed"
                    ),
                    "failure_ids": [
                        "tests/test_contract.py::"
                        "test_canonical_reference_records_sizes"
                    ],
                },
            )

            self.assertEqual(stage, "provider_research")
            self.assertIn("provider.md", feedback)

    def test_parallel_worktree_installs_dependency_links_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            (root / ".conda" / "conda-meta").mkdir(parents=True)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            task = TaskSpec(
                task_id="task-001",
                title="parallel",
                description="",
                acceptance=[],
            )
            state = RunState(run_id="run-001", tasks=[task])

            with patch(
                "auto_agents.orchestrator.install_dependency_links",
                side_effect=RuntimeError("dependency links installed first"),
            ) as install:
                result = orchestrator._run_task_in_worktree(
                    state,
                    [task],
                    task.task_id,
                )

            self.assertFalse(result["ok"])
            self.assertIn("dependency links installed first", result["reason"])
            install.assert_called_once()
            installed_root, links = install.call_args.args
            self.assertEqual(
                installed_root.name,
                task.task_id,
            )
            self.assertIn(".conda", links)

    def test_parallel_result_commit_excludes_installed_dependency_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            (root / ".conda" / "conda-meta").mkdir(parents=True)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            task = TaskSpec(
                task_id="task-001",
                title="parallel",
                description="",
                acceptance=[],
            )
            state = RunState(run_id="run-001", tasks=[task])

            def complete(
                worker: Orchestrator,
                worker_state: RunState,
                worker_task: TaskSpec,
                resume_existing: bool = False,
                gate_recheck_first: bool = False,
            ) -> dict:
                del worker_state, worker_task, resume_existing, gate_recheck_first
                write_text(worker.project_root / "candidate.py", "ready = True\n")
                return {
                    "ok": True,
                    "reason": "",
                    "review": "accepted",
                    "verify_current_failure_ids": [],
                }

            with patch.object(
                Orchestrator,
                "_ensure_task_verify_baseline",
                return_value=False,
            ), patch.object(
                Orchestrator,
                "_execute_task_with_retries",
                new=complete,
            ):
                result = orchestrator._run_task_in_worktree(
                    state,
                    [task],
                    task.task_id,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["changed_paths"], ["candidate.py"])
            dependency_entry = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "--name-only",
                    str(result["commit_sha"]),
                    "--",
                    ".conda",
                ],
                cwd=str(root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(dependency_entry.stdout.strip(), "")

    def test_failed_checkpoint_excludes_installed_dependency_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            (root / ".conda" / "conda-meta").mkdir(parents=True)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            task = TaskSpec(
                task_id="task-001",
                title="parallel failure",
                description="",
                acceptance=[],
            )
            state = RunState(run_id="run-001", tasks=[task])

            def fail(
                worker: Orchestrator,
                worker_state: RunState,
                worker_task: TaskSpec,
                resume_existing: bool = False,
                gate_recheck_first: bool = False,
            ) -> dict:
                del worker_state, worker_task, resume_existing, gate_recheck_first
                write_text(worker.project_root / "candidate.py", "ready = False\n")
                return {
                    "ok": False,
                    "reason": "verification failed",
                    "review": "candidate is incomplete",
                    "failure_ids": ["tests/test_demo.py::test_contract"],
                }

            with patch.object(
                Orchestrator,
                "_ensure_task_verify_baseline",
                return_value=False,
            ), patch.object(
                Orchestrator,
                "_execute_task_with_retries",
                new=fail,
            ):
                result = orchestrator._run_task_in_worktree(
                    state,
                    [task],
                    task.task_id,
                )

            self.assertFalse(result["ok"])
            checkpoint = result["failure_checkpoint"]
            self.assertEqual(checkpoint["changed_paths"], ["candidate.py"])
            dependency_entry = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "--name-only",
                    str(checkpoint["commit_sha"]),
                    "--",
                    ".conda",
                ],
                cwd=str(root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(dependency_entry.stdout.strip(), "")

    def test_failed_candidate_checkpoint_and_log_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            orchestrator = self._project(root)
            if changed_files(root):
                commit_all(root, "chore: initialize test project")
            initial_ref = head_ref(root)
            write_text(root / "candidate.py", "candidate = True\n")
            log_dir = (
                root / ".auto-agents" / "failed-verification-logs"
            )
            log_dir.mkdir(parents=True)
            write_text(log_dir / "task-verify.log", "FAILED contract\n")
            task = TaskSpec(
                task_id="task-001",
                title="checkpoint",
                description="",
                acceptance=[],
                verify_retry_epoch=2,
            )
            state = RunState(run_id="run-001", tasks=[task])

            checkpoint = orchestrator._preserve_failed_task_checkpoint(
                state,
                task,
                root,
                {
                    "reason": "verification failed",
                    "review": "",
                    "failure_ids": [
                        "tests/test_demo.py::test_contract"
                    ],
                },
            )

            self.assertTrue(checkpoint["has_candidate_changes"])
            self.assertTrue(ref_exists(root, str(checkpoint["ref"])))
            self.assertEqual(
                checkpoint["changed_paths"],
                ["candidate.py"],
            )
            archived_logs = [
                root / str(path)
                for path in checkpoint["diagnostic_paths"]
                if str(path).endswith(".log")
            ]
            self.assertEqual(len(archived_logs), 1)
            self.assertTrue(archived_logs[0].is_file())
            self.assertEqual(changed_paths(root), [])

            self.assertTrue(hard_reset_clean(root, initial_ref))
            state.task_failure_checkpoints[task.task_id] = checkpoint
            restored = orchestrator._restore_task_failure_checkpoint(
                state,
                task,
                root,
            )
            self.assertEqual(restored, checkpoint["ref"])
            self.assertEqual(changed_paths(root), ["candidate.py"])


if __name__ == "__main__":
    unittest.main()
