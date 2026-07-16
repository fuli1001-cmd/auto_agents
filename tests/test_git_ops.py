import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.git_ops import (
    add_worktree,
    cherry_pick_no_commit,
    commit_all,
    commit_all_except,
    commit_changed_paths,
    delete_ref,
    head_ref,
    list_worktrees,
    ref_exists,
    remove_worktree,
    update_ref,
)
from auto_agents.io_utils import write_text
from auto_agents.models import RunState, TaskSpec
from auto_agents.orchestrator import Orchestrator


class GitOpsWorktreeTests(unittest.TestCase):
    @staticmethod
    def _configure_git_identity(project_root: Path) -> None:
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(project_root),
            check=True,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(project_root),
            check=True,
            text=True,
            capture_output=True,
        )

    def test_add_list_and_remove_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            commit_all(project_root, "test: init")

            worktree_path = Path(tmp) / "demo-wt"
            add_worktree(project_root, worktree_path)

            self.assertIn(str(worktree_path), list_worktrees(project_root))

            remove_worktree(project_root, worktree_path)
            self.assertNotIn(str(worktree_path), list_worktrees(project_root))

    def test_retained_ref_survives_worker_worktree_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            commit_all(project_root, "test: init")
            worktree_path = Path(tmp) / "demo-wt"
            add_worktree(project_root, worktree_path)
            try:
                write_text(worktree_path / "artifact.txt", "retained\n")
                worker_sha = commit_all_except(
                    worktree_path,
                    "test: retained worker",
                    exclude_prefixes=(".auto-agents",),
                )
                result_ref = "refs/auto-agents/runs/test/tasks/task-001"
                update_ref(project_root, result_ref, worker_sha)
            finally:
                remove_worktree(project_root, worktree_path)

            self.assertTrue(ref_exists(project_root, result_ref))
            self.assertNotEqual(head_ref(project_root), worker_sha)
            cherry_pick_no_commit(project_root, result_ref)
            self.assertEqual(
                (project_root / "artifact.txt").read_text(encoding="utf-8"),
                "retained\n",
            )
            delete_ref(project_root, result_ref)
            self.assertFalse(ref_exists(project_root, result_ref))

    def test_retained_result_replays_on_latest_head_in_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            write_text(
                project_root / "shared.py",
                "WORKER_VALUE = 0\n" + ("# spacer\n" * 20) + "MAIN_VALUE = 0\n",
            )
            commit_all(project_root, "test: init")

            worker_path = Path(tmp) / "worker"
            add_worktree(project_root, worker_path)
            try:
                write_text(
                    worker_path / "shared.py",
                    "WORKER_VALUE = 1\n" + ("# spacer\n" * 20) + "MAIN_VALUE = 0\n",
                )
                worker_sha = commit_all_except(
                    worker_path,
                    "test: worker",
                    exclude_prefixes=(".auto-agents",),
                )
                result_ref = "refs/auto-agents/runs/test/tasks/task-001"
                update_ref(project_root, result_ref, worker_sha)
            finally:
                remove_worktree(project_root, worker_path)

            write_text(
                project_root / "shared.py",
                "WORKER_VALUE = 0\n" + ("# spacer\n" * 20) + "MAIN_VALUE = 1\n",
            )
            commit_all(project_root, "test: main peer")

            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-001",
                title="Replay",
                description="Replay the retained worker result.",
                acceptance=["both values are one"],
            )
            state = RunState(run_id="test", current_stage="implement", tasks=[task])
            entry = {
                "task": {**task.to_dict(), "status": "done"},
                "commit_sha": worker_sha,
                "result_ref": result_ref,
                "changed_paths": ["shared.py"],
            }

            with patch.object(
                Orchestrator,
                "_run_task_verify",
                return_value={"ok": True, "reason": "passed", "current_failure_ids": []},
            ):
                replay = orchestrator._replay_parallel_pending_result(
                    state, [task], task, entry
                )

            self.assertTrue(replay["ok"], replay)
            rendered = subprocess.run(
                ["git", "show", f"{result_ref}:shared.py"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            self.assertIn("WORKER_VALUE = 1", rendered)
            self.assertIn("MAIN_VALUE = 1", rendered)

    def test_commit_all_except_and_cherry_pick_no_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            commit_all(project_root, "test: init")

            original_plan = (project_root / ".auto-agents" / "state" / "task_plan.json").read_text(
                encoding="utf-8"
            )

            worktree_path = Path(tmp) / "demo-wt"
            add_worktree(project_root, worktree_path)
            try:
                write_text(worktree_path / "artifact.txt", "hello\n")
                write_text(
                    worktree_path / ".auto-agents" / "state" / "task_plan.json",
                    "{\"tasks\": []}\n",
                )
                worker_sha = commit_all_except(
                    worktree_path,
                    "test: worker",
                    exclude_prefixes=(".auto-agents",),
                )
            finally:
                remove_worktree(project_root, worktree_path)

            self.assertEqual(commit_changed_paths(project_root, worker_sha), ["artifact.txt"])

            cherry_pick_no_commit(project_root, worker_sha)

            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(
                (project_root / ".auto-agents" / "state" / "task_plan.json").read_text(encoding="utf-8"),
                original_plan,
            )

    def test_antigravitycli_ignored(self) -> None:
        from auto_agents.git_ops import changed_entries, changed_paths, hard_reset_clean
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            commit_all(project_root, "test: init")

            # Create an untracked file in .antigravitycli and a regular untracked file
            anti_dir = project_root / ".antigravitycli"
            anti_dir.mkdir(parents=True, exist_ok=True)
            (anti_dir / "session.json").write_text('{"history": []}', encoding="utf-8")
            (project_root / "foo.txt").write_text("untracked", encoding="utf-8")

            # Check changed_entries
            entries = changed_entries(project_root)
            paths = [p for _, p in entries]
            self.assertIn("foo.txt", paths)
            self.assertNotIn(".antigravitycli/session.json", paths)

            # Check changed_paths
            all_paths = changed_paths(project_root)
            self.assertIn("foo.txt", all_paths)
            self.assertNotIn(".antigravitycli/session.json", all_paths)

            # Check hard_reset_clean preserves .antigravitycli/ but removes foo.txt
            success = hard_reset_clean(project_root)
            self.assertTrue(success)
            self.assertTrue((anti_dir / "session.json").exists())
            self.assertFalse((project_root / "foo.txt").exists())


if __name__ == "__main__":
    unittest.main()
