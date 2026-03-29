import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.git_ops import commit_all, ensure_repo, is_repo, working_tree_clean


class GitOpsTests(unittest.TestCase):
    def test_commit_all_creates_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            ensure_repo(project_root, auto_init=True)
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

            (project_root / "hello.txt").write_text("hello\n", encoding="utf-8")
            sha = commit_all(project_root, "test: first commit")

            self.assertTrue(is_repo(project_root))
            self.assertTrue(len(sha) >= 7)
            self.assertTrue(working_tree_clean(project_root))


if __name__ == "__main__":
    unittest.main()

