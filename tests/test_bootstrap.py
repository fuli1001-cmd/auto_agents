import json
import subprocess
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
    ensure_auto_gitignore,
    load_project_config,
    migrate_project_config,
    project_rules_path,
    provider_references_dir,
    provider_references_lock_path,
    requirements_trace_path,
    run_state_path,
    task_plan_path,
)
from auto_agents.git_ops import is_repo
from auto_agents.orchestrator import Orchestrator
from auto_agents.run_lock import ProjectRunLock


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
            self.assertTrue((project_root / "CLAUDE.md").exists())
            self.assertTrue((project_root / ".github" / "copilot-instructions.md").exists())
            self.assertTrue((project_root / ".github" / "instructions" / "product-contract.instructions.md").exists())
            self.assertTrue(is_repo(project_root))
            auto_gitignore = (auto_dir(project_root) / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(
                auto_gitignore,
                "operator/\nruntime/\nfailed-verification-logs/\nruns/\n"
                "state/gate_baseline_cache.json\nstate/gate_baseline_cache.sqlite3\n"
                "state/gate_baseline_cache.sqlite3-*\nstate/requirements_audit_cache.sqlite3\n"
                "state/requirements_audit_cache.sqlite3-*\nstate/repomap_cache.json\n"
                "state/parallel_tuning.json\nstate/release_jobs.sqlite3\n"
                "state/release_jobs.sqlite3-shm\nstate/release_jobs.sqlite3-wal\n"
                "state/release-worker.log\nstate/release-worker.lock\n"
                "state/health-watch-control.json\nstate/health-watch-control.lock\n"
                "state/checkpoint_blobs/\nstate/root_cause_certificates/\n"
                "state/sessions/*/prompts/\nstate/sessions/*/outputs/\n"
                "state/sessions/*/health/\n"
                "state/sessions/*/logs/\n"
                "state/sessions/*/performance_trace.jsonl\n"
                "state/workflows/*/checkpoints/\n"
                "state/workflows/*/event_index.sqlite3\n"
                "state/workflows/*/event_index.sqlite3-*\n",
            )
            task_archive_ignore = subprocess.run(
                ["git", "check-ignore", "-q", ".auto-agents/history/task_plans/run-001.json"],
                cwd=str(project_root),
            )
            run_state_archive_ignore = subprocess.run(
                ["git", "check-ignore", "-q", ".auto-agents/runs/run-001/run_state.final.json"],
                cwd=str(project_root),
            )
            run_log_ignore = subprocess.run(
                ["git", "check-ignore", "-q", ".auto-agents/runs/run-001/run.log"],
                cwd=str(project_root),
            )
            failed_log_ignore = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "-q",
                    ".auto-agents/failed-verification-logs/verify-stage.log",
                ],
                cwd=str(project_root),
            )
            session_ignore = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "-q",
                    ".auto-agents/state/sessions/session-001/session_state.json",
                ],
                cwd=str(project_root),
            )
            session_prompt_ignore = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "-q",
                    ".auto-agents/state/sessions/session-001/prompts/fix-1.txt",
                ],
                cwd=str(project_root),
            )
            health_control_ignore = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "-q",
                    ".auto-agents/state/health-watch-control.json",
                ],
                cwd=str(project_root),
            )
            self.assertEqual(task_archive_ignore.returncode, 1)
            self.assertEqual(run_state_archive_ignore.returncode, 0)
            self.assertEqual(run_log_ignore.returncode, 0)
            self.assertEqual(failed_log_ignore.returncode, 0)
            self.assertEqual(session_ignore.returncode, 1)
            self.assertEqual(session_prompt_ignore.returncode, 0)
            self.assertEqual(health_control_ignore.returncode, 0)
            gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".env", gitignore)
            self.assertIn(".conda/", gitignore)
            self.assertIn(".venv/", gitignore)
            self.assertIn("node_modules/", gitignore)
            self.assertIn(".data/", gitignore)
            self.assertIn(".tmp/", gitignore)
            self.assertIn(".tmp-tests/", gitignore)
            self.assertIn(".antigravitycli/", gitignore)
            config = load_project_config(project_root)
            self.assertEqual(config.docs.language, "en")
            self.assertEqual(config.active_provider, "mock")
            self.assertIn("codex", config.providers)
            self.assertIn("claude-code", config.providers)
            self.assertIn("copilot-cli", config.providers)
            self.assertIn("antigravity-claude", config.providers)
            self.assertIn("antigravity-gemini", config.providers)
            self.assertFalse(config.providers["antigravity-claude"].prompt_via_stdin)
            self.assertFalse(config.providers["antigravity-gemini"].prompt_via_stdin)
            self.assertIn("mock", config.providers)

    def test_auto_gitignore_migrates_legacy_session_directory_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            ignore_path = auto_dir(project_root) / ".gitignore"
            ignore_path.write_text(
                "runs/\nstate/sessions/\nstate/run_state.json\n",
                encoding="utf-8",
            )

            ensure_auto_gitignore(project_root)

            entries = ignore_path.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("state/sessions/", entries)
            self.assertNotIn("state/run_state.json", entries)
            self.assertIn("state/sessions/*/prompts/", entries)
            self.assertIn("state/sessions/*/outputs/", entries)
            self.assertIn("state/sessions/*/health/", entries)
            session_state_ignore = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "-q",
                    ".auto-agents/state/sessions/session-001/session_state.json",
                ],
                cwd=str(project_root),
            )
            prompt_ignore = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "-q",
                    ".auto-agents/state/sessions/session-001/prompts/fix-1.txt",
                ],
                cwd=str(project_root),
            )
            self.assertEqual(session_state_ignore.returncode, 1)
            self.assertEqual(prompt_ignore.returncode, 0)

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
            self.assertEqual(copilot.vision, "auto")
            claude = config.providers["claude-code"]
            self.assertEqual(claude.binary, "claude")
            self.assertEqual(claude.profile_map, {"balanced": "sonnet", "deep": "opus", "max": "opus"})
            self.assertEqual(claude.timeout_seconds, 3600)
            self.assertEqual(config.providers["antigravity-claude"].vision, "disabled")
            self.assertEqual(config.providers["antigravity-gemini"].vision, "disabled")
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

    def test_explicit_project_config_migration_upgrades_v2_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo")
            config_file = config_path(project_root)
            raw = json.loads(config_file.read_text(encoding="utf-8"))
            raw["gates"]["verification_policy_version"] = 2
            raw["gates"].pop("incremental", None)
            raw["gates"]["steps"] = [
                {
                    "kind": "test",
                    "runner": "pytest",
                    "targets": ["tests/test_example.py"],
                    "result_cache_scope": "candidate",
                }
            ]
            config_file.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

            before = config_file.read_bytes()
            config = load_project_config(project_root)
            self.assertEqual(config_file.read_bytes(), before)
            with ProjectRunLock(project_root, environ={}):
                self.assertTrue(migrate_project_config(project_root))
            persisted = json.loads(config_file.read_text(encoding="utf-8"))

            self.assertEqual(config.gates.verification_policy_version, 3)
            self.assertEqual(config.gates.incremental_mode, "auto")
            self.assertEqual(config.gates.steps[0].result_cache_scope, "auto")
            self.assertEqual(persisted["gates"]["verification_policy_version"], 3)
            self.assertEqual(
                persisted["gates"]["incremental"]["warm_target_seconds"],
                900,
            )


if __name__ == "__main__":
    unittest.main()
