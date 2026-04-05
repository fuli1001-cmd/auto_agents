import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import auto_dir, config_path, docs_dir, load_project_config, run_state_path, task_plan_path
from auto_agents.git_ops import is_repo
from auto_agents.orchestrator import Orchestrator


class BootstrapTests(unittest.TestCase):
    def test_init_project_bootstraps_structure_and_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")

            self.assertTrue(auto_dir(project_root).exists())
            self.assertTrue(config_path(project_root).exists())
            self.assertTrue((project_root / ".gitignore").exists())
            self.assertTrue((docs_dir(project_root) / "project_brief.md").exists())
            self.assertTrue((docs_dir(project_root) / "architecture.md").exists())
            self.assertTrue(task_plan_path(project_root).exists())
            self.assertTrue(run_state_path(project_root).exists())
            self.assertTrue(is_repo(project_root))
            gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".conda/", gitignore)
            self.assertIn(".venv/", gitignore)
            self.assertIn("node_modules/", gitignore)
            config = load_project_config(project_root)
            self.assertEqual(config.docs.language, "en")

    def test_init_project_with_copilot_cli_bootstraps_profile_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "copilot-cli")

            self.assertTrue(auto_dir(project_root).exists())
            self.assertTrue(config_path(project_root).exists())
            config = load_project_config(project_root)
            self.assertEqual(config.provider.kind, "copilot-cli")
            self.assertEqual(config.provider.binary, "copilot-cli")
            self.assertEqual(config.provider.profile_map["balanced"], "balanced")
            self.assertEqual(config.provider.profile_map["deep"], "deep")
            self.assertEqual(config.provider.profile_map["max"], "max")


if __name__ == "__main__":
    unittest.main()
