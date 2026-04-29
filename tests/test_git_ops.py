import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.git_ops import (
    add_worktree,
    cherry_pick_no_commit,
    commit_all,
    commit_all_except,
    list_worktrees,
    remove_worktree,
)
from auto_agents.io_utils import write_text
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

            cherry_pick_no_commit(project_root, worker_sha)

            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(
                (project_root / ".auto-agents" / "state" / "task_plan.json").read_text(encoding="utf-8"),
                original_plan,
            )


if __name__ == "__main__":
    unittest.main()
