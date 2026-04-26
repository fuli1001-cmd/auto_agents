import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.orchestrator import Orchestrator


def _seed_python_project(project_root: Path) -> None:
    """Add Python signals + source files so detector returns eligible."""
    (project_root / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.0.1'\n", encoding="utf-8"
    )
    pkg = project_root / "src" / "demo"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "module_a.py").write_text(
        "class WidgetEngine:\n"
        "    def assemble(self, parts): return parts\n",
        encoding="utf-8",
    )
    (pkg / "module_b.py").write_text(
        "def lonely_function(x): return x\n",
        encoding="utf-8",
    )


class OrchestratorRepoMapInjectionTests(unittest.TestCase):
    def test_disabled_yields_byte_identical_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            _seed_python_project(project_root)

            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            orchestrator.config.repo_map.enabled = False
            disabled_prompt = orchestrator._build_task_prompt(task, "implement")

            # Repeating the same call must produce a byte-identical prompt
            again = orchestrator._build_task_prompt(task, "implement")
            self.assertEqual(disabled_prompt, again)
            self.assertNotIn("## Repo Map", disabled_prompt)

    def test_enabled_injects_repo_map_into_implement_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            _seed_python_project(project_root)

            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]
            orchestrator.config.repo_map.enabled = True

            prompt = orchestrator._build_task_prompt(task, "implement")

            self.assertIn("## Repo Map", prompt)
            self.assertIn("WidgetEngine", prompt)
            # Tokens stayed within budget
            res = orchestrator._last_repo_map_result
            self.assertIsNotNone(res)
            self.assertLessEqual(res.tokens_actual, res.tokens_budget)

    def test_enabled_injects_repo_map_into_review_prompt_with_smaller_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            _seed_python_project(project_root)

            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            prompt = orchestrator._build_task_prompt(task, "review")
            self.assertIn("## Repo Map", prompt)
            res = orchestrator._last_repo_map_result
            self.assertIsNotNone(res)
            self.assertEqual(res.tokens_budget, orchestrator.config.repo_map.review_budget_tokens)

    def test_non_python_project_skips_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            # Do NOT seed any python files

            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]
            orchestrator.config.repo_map.enabled = True

            prompt = orchestrator._build_task_prompt(task, "implement")
            self.assertNotIn("## Repo Map", prompt)
            res = orchestrator._last_repo_map_result
            self.assertIsNotNone(res)
            self.assertIsNotNone(res.skipped_reason)
            self.assertNotEqual(res.skipped_reason, "disabled")

    def test_config_persists_through_save_and_reload(self) -> None:
        from auto_agents.config import load_project_config, save_project_config

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            cfg = load_project_config(project_root)
            self.assertTrue(cfg.repo_map.enabled)
            self.assertEqual(cfg.repo_map.budget_tokens, 1500)
            cfg.repo_map.enabled = False
            cfg.repo_map.budget_tokens = 999
            save_project_config(project_root, cfg)

            reloaded = load_project_config(project_root)
            self.assertFalse(reloaded.repo_map.enabled)
            self.assertEqual(reloaded.repo_map.budget_tokens, 999)

    def test_new_project_writes_repo_map_default_globs(self) -> None:
        from auto_agents.config import load_project_config

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")

            cfg = load_project_config(project_root)
            self.assertEqual(cfg.repo_map.include_globs, ["**/*.py"])
            self.assertIn(".conda-pkgs/**", cfg.repo_map.exclude_globs)


if __name__ == "__main__":
    unittest.main()
