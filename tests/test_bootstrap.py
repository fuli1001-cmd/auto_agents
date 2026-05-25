import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    auto_dir,
    agent_instructions_lock_path,
    config_path,
    docs_dir,
    load_project_config,
    project_rules_path,
    provider_references_dir,
    provider_references_lock_path,
    requirements_trace_path,
    run_state_path,
    task_plan_path,
)
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
            self.assertTrue(provider_references_dir(project_root).exists())
            self.assertTrue(requirements_trace_path(project_root).exists())
            self.assertTrue(provider_references_lock_path(project_root).exists())
            self.assertTrue(task_plan_path(project_root).exists())
            self.assertTrue(run_state_path(project_root).exists())
            self.assertTrue(project_rules_path(project_root).exists())
            self.assertTrue(agent_instructions_lock_path(project_root).exists())
            self.assertTrue((project_root / "AGENTS.md").exists())
            self.assertTrue((project_root / ".github" / "copilot-instructions.md").exists())
            self.assertTrue((project_root / ".github" / "instructions" / "product-contract.instructions.md").exists())
            self.assertTrue(is_repo(project_root))
            auto_gitignore = (auto_dir(project_root) / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(
                auto_gitignore,
                "runs/\nstate/gate_baseline_cache.json\nstate/repomap_cache.json\n",
            )
            gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".conda/", gitignore)
            self.assertIn(".venv/", gitignore)
            self.assertIn("node_modules/", gitignore)
            self.assertIn(".data/", gitignore)
            self.assertIn(".tmp/", gitignore)
            self.assertIn(".tmp-tests/", gitignore)
            config = load_project_config(project_root)
            self.assertEqual(config.docs.language, "en")
            self.assertEqual(config.active_provider, "mock")
            self.assertIn("codex", config.providers)
            self.assertIn("copilot-cli", config.providers)
            self.assertIn("mock", config.providers)

    def test_init_project_bootstraps_copilot_cli_profile_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo")

            self.assertTrue(auto_dir(project_root).exists())
            self.assertTrue(config_path(project_root).exists())
            config = load_project_config(project_root)
            copilot = config.providers["copilot-cli"]
            self.assertEqual(copilot.kind, "copilot-cli")
            self.assertEqual(copilot.binary, "copilot")
            self.assertEqual(copilot.profile_map["balanced"], "balanced")
            self.assertEqual(copilot.profile_map["deep"], "deep")
            self.assertEqual(copilot.profile_map["max"], "max")
            self.assertEqual(copilot.timeout_seconds, 3600)
            self.assertEqual(copilot.idle_timeout_seconds, 3600)
            self.assertEqual(config.providers["codex"].idle_timeout_seconds, 3600)

    def test_init_project_writes_idle_timeout_3600_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo")

            raw = json.loads(config_path(project_root).read_text(encoding="utf-8"))
            self.assertEqual(raw["providers"]["codex"]["idle_timeout_seconds"], 3600)
            self.assertEqual(raw["providers"]["copilot-cli"]["idle_timeout_seconds"], 3600)

    def test_load_project_config_upgrades_legacy_copilot_timeout_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo")

            config_file = config_path(project_root)
            raw = json.loads(config_file.read_text(encoding="utf-8"))
            raw["providers"]["copilot-cli"]["timeout_seconds"] = 1800
            raw["providers"]["copilot-cli"]["idle_timeout_seconds"] = 300
            config_file.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

            config = load_project_config(project_root)
            self.assertEqual(config.providers["copilot-cli"].timeout_seconds, 3600)

    def test_load_project_config_defaults_missing_copilot_idle_timeout_to_3600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo")

            config_file = config_path(project_root)
            raw = json.loads(config_file.read_text(encoding="utf-8"))
            del raw["providers"]["copilot-cli"]["idle_timeout_seconds"]
            config_file.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

            config = load_project_config(project_root)
            self.assertEqual(config.providers["copilot-cli"].idle_timeout_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
