import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.repomap import RepoMapBuilder, RepoMapConfig
from auto_agents.repomap.builder import estimate_tokens
from auto_agents.repomap.detector import is_python_project


class _FakeTask:
    title = ""
    description = ""
    acceptance = []
    scope_boundaries = ""
    commit_message = ""


def _setup_python_project(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "foo.py").write_text(
        "class Foo:\n    def hello(self): return 1\n", encoding="utf-8"
    )
    (root / "pkg" / "orchestrator.py").write_text(
        "class Orchestrator:\n    def run(self): pass\n", encoding="utf-8"
    )
    (root / "pkg" / "util.py").write_text(
        "def helper(): return 'ok'\n", encoding="utf-8"
    )


class RepoMapBuilderTests(unittest.TestCase):
    def test_disabled_returns_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _setup_python_project(root)
            cfg = RepoMapConfig(enabled=False)
            res = RepoMapBuilder(root, cfg).build(_FakeTask())
            self.assertEqual(res.text, "")
            self.assertEqual(res.skipped_reason, "disabled")
            self.assertFalse(res.enabled)

    def test_non_python_project_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.go").write_text("package main\n", encoding="utf-8")
            cfg = RepoMapConfig()
            res = RepoMapBuilder(root, cfg).build(_FakeTask())
            self.assertEqual(res.text, "")
            self.assertIsNotNone(res.skipped_reason)
            self.assertNotEqual(res.skipped_reason, "disabled")

    def test_includes_relevant_file_first_and_respects_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _setup_python_project(root)
            task = _FakeTask()
            task.title = "Update Orchestrator behavior"
            task.description = "Modify the orchestrator class to support repo map."
            cfg = RepoMapConfig(budget_tokens=300)
            res = RepoMapBuilder(root, cfg).build(task)
            self.assertTrue(res.text)
            self.assertIn("orchestrator.py", res.text)
            # The orchestrator file should appear before util.py in the rendered text.
            self.assertLess(
                res.text.index("orchestrator.py"),
                res.text.find("util.py") if "util.py" in res.text else len(res.text),
            )
            self.assertLessEqual(res.tokens_actual, res.tokens_budget)

    def test_anchor_path_is_included_even_under_tight_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _setup_python_project(root)
            task = _FakeTask()
            task.description = "Touch pkg/foo.py only."
            # Tight budget: only ~50 tokens; anchor should still appear.
            cfg = RepoMapConfig(budget_tokens=50)
            res = RepoMapBuilder(root, cfg).build(task)
            self.assertIn("pkg/foo.py", res.text)

    def test_discovery_prefers_git_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (root / "tracked.py").write_text("def tracked(): pass\n", encoding="utf-8")
            (root / "untracked.py").write_text("def untracked(): pass\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True, text=True)
            subprocess.run(["git", "add", "pyproject.toml", "tracked.py"], cwd=str(root), check=True)

            res = RepoMapBuilder(root, RepoMapConfig()).build(_FakeTask())

            self.assertIn("tracked.py", res.text)
            self.assertNotIn("untracked.py", res.text)

    def test_estimate_tokens_chars_div_4(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcdefgh"), 2)
        self.assertEqual(estimate_tokens("a"), 1)


class DetectorTests(unittest.TestCase):
    def test_pyproject_marker_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            ok, reason = is_python_project(root)
            self.assertTrue(ok)
            self.assertTrue(reason.startswith("marker:"))

    def test_no_python_signals_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.go").write_text("package main\n", encoding="utf-8")
            (root / "lib.rs").write_text("fn main(){}\n", encoding="utf-8")
            ok, _reason = is_python_project(root)
            self.assertFalse(ok)

    def test_conda_pkgs_does_not_make_project_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / ".conda-pkgs" / "lib"
            pkg.mkdir(parents=True)
            (pkg / "stdlib.py").write_text("def helper(): pass\n", encoding="utf-8")
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            ok, reason = is_python_project(root)
            self.assertFalse(ok)
            self.assertEqual(reason, "no_python_files")


if __name__ == "__main__":
    unittest.main()
