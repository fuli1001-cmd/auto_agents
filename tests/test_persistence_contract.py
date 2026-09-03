from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.cli import build_parser, main
from auto_agents.config import (
    load_project_config,
    load_run_state,
    requirements_trace_path,
    run_state_path,
    save_project_config,
    task_plan_path,
)
from auto_agents.io_utils import read_json, write_json
from auto_agents.models import (
    PersistenceConfig,
    PersistenceTargetConfig,
    ProjectConfig,
    RunState,
    SessionState,
    TaskSpec,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.persistence import (
    PersistenceContractError,
    build_persistence_action_manifest,
    detect_persistence_schema_changes,
    execute_persistence_action,
    migration_artifact_immutability_errors,
    persistence_candidate_fingerprint,
)
from auto_agents.persistence_rebind import (
    PersistenceRebindError,
    _replace_json_batch,
    rebind_legacy_persistence_decision,
)
from auto_agents.persistence_upgrade import upgrade_persistence_contract
from auto_agents.requirements import validate_requirements_trace_payload
from auto_agents.session import Session
from auto_agents.validation import (
    validate_active_persistence_target_readiness,
    validate_persistence_config_payload,
    validate_persistence_plan_contract,
    validate_task_plan_payload,
)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _legacy_persistence_project(root: Path) -> None:
    Orchestrator.init_project(root, "demo", "mock")
    config = load_project_config(root)
    config.persistence.targets = [
        PersistenceTargetConfig(
            target_id="local-sqlite-test",
            environment="test",
            kind="local_file",
            locator={"path": ".tmp-tests/app.sqlite3"},
        )
    ]
    save_project_config(root, config)
    legacy_targets = ["REQ-001", "REQ-002"]
    change = {
        "strategy": "initial_schema",
        "decision_id": "PERSIST-001",
        "target_ids": list(legacy_targets),
        "to_version": "1",
        "migration_artifacts": ["migrations/0001.py"],
        "legacy_fixture_refs": [],
    }
    task = {
        "task_id": "task-002",
        "title": "Initial schema",
        "description": "Create the initial schema.",
        "acceptance": ["Schema is explicit."],
        "status": "pending",
        "commit_message": "feat: schema",
        "persistence_change": dict(change),
    }
    write_json(
        requirements_trace_path(root),
        {
            "version": 1,
            "persistence_decisions": [
                {
                    "id": "PERSIST-001",
                    "target_ids": list(legacy_targets),
                    "strategy": "initial_schema",
                    "source": "legacy generated run",
                    "status": "active",
                }
            ],
            "requirements": [],
        },
    )
    write_json(
        task_plan_path(root),
        {
            "persistence_contract_version": 1,
            "tasks": [task],
        },
    )
    write_json(
        run_state_path(root),
        RunState(
            run_id="run-001",
            status="blocked",
            current_stage="implement",
            tasks=[TaskSpec.from_dict(task)],
            last_error=(
                "preflight validation failed: target_ids must reference "
                "persistence targets, not requirement IDs"
            ),
            active_blocker={
                "owner": "target_project",
                "category": "invalid_target_persistence_metadata",
                "status": "blocked",
            },
        ).to_dict(),
    )


class PersistenceContractModelTests(unittest.TestCase):
    def test_active_clean_break_decision_requires_ready_target_commands(self) -> None:
        trace = {
            "persistence_decisions": [
                {
                    "id": "PERSIST-002",
                    "strategy": "clean_break",
                    "target_ids": ["local-sqlite-test"],
                    "status": "active",
                }
            ]
        }
        target = {
            "id": "local-sqlite-test",
            "environment": "test",
            "kind": "local_file",
            "initialize_argv": [],
            "verify_argv": [],
        }

        errors = validate_active_persistence_target_readiness(
            trace,
            configured_targets=[target],
        )

        self.assertEqual(
            errors,
            [
                "persistence decision PERSIST-002 clean_break target local-sqlite-test "
                "requires initialize_argv and verify_argv"
            ],
        )

        target["initialize_argv"] = ["tool", "init"]
        target["verify_argv"] = ["tool", "verify"]
        self.assertEqual(
            validate_active_persistence_target_readiness(
                trace,
                configured_targets=[target],
            ),
            [],
        )

    def test_initial_schema_and_superseded_decisions_do_not_require_commands(self) -> None:
        trace = {
            "persistence_decisions": [
                {
                    "id": "PERSIST-001",
                    "strategy": "initial_schema",
                    "target_ids": ["local-sqlite-test"],
                    "status": "active",
                },
                {
                    "id": "PERSIST-002",
                    "strategy": "clean_break",
                    "target_ids": ["local-sqlite-test"],
                    "status": "superseded",
                },
            ]
        }
        target = {
            "id": "local-sqlite-test",
            "environment": "test",
            "kind": "local_file",
            "initialize_argv": [],
            "verify_argv": [],
        }

        self.assertEqual(
            validate_active_persistence_target_readiness(
                trace,
                configured_targets=[target],
            ),
            [],
        )

    def test_plan_stage_blocks_before_agent_when_active_target_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            config = load_project_config(root)
            config.persistence.targets = [
                PersistenceTargetConfig(
                    target_id="local-sqlite-test",
                    environment="test",
                    kind="local_file",
                    locator={"path": ".tmp-tests/app.sqlite3"},
                )
            ]
            save_project_config(root, config)
            write_json(
                requirements_trace_path(root),
                {
                    "version": 1,
                    "persistence_decisions": [
                        {
                            "id": "PERSIST-002",
                            "strategy": "clean_break",
                            "target_ids": ["local-sqlite-test"],
                            "source": "explicit user choice",
                            "status": "active",
                        }
                    ],
                    "requirements": [],
                },
            )
            spec_file = root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            orchestrator = Orchestrator(root)
            state = load_run_state(root)

            with patch.object(orchestrator, "_run_agent_with_retries") as run_agent:
                returned = orchestrator._run_agent_stage("plan", state, spec_file)

            run_agent.assert_not_called()
            self.assertEqual(returned.status, "blocked")
            self.assertEqual(returned.current_stage, "plan")
            self.assertNotIn("plan", returned.agent_attempts)
            self.assertEqual(
                returned.active_blocker["category"],
                "persistence_configuration_required",
            )
            self.assertEqual(returned.active_blocker["owner"], "target_project")
            self.assertIn("persistence-configure", returned.last_error)
            self.assertIn("initialize_argv and verify_argv", returned.last_error)

    def test_models_round_trip_persistence_contracts(self) -> None:
        target = PersistenceTargetConfig(
            target_id="local-db",
            environment="development",
            kind="local_file",
            locator={"path": ".data/app.db"},
            associated_paths=[".data/media"],
            apply_argv=["tool", "migrate"],
            initialize_argv=["tool", "init"],
            verify_argv=["tool", "verify"],
        )
        config = ProjectConfig(project_name="demo", persistence=PersistenceConfig([target]))
        restored = ProjectConfig.from_dict(config.to_dict())
        self.assertEqual(restored.persistence.targets[0].target_id, "local-db")

        change = {
            "strategy": "clean_break",
            "decision_id": "PERSIST-001",
            "target_ids": ["local-db"],
            "to_version": "v2",
            "migration_artifacts": ["src/db.py"],
            "legacy_fixture_refs": ["tests/test_db.py::test_reset"],
        }
        task = TaskSpec("task-1", "Schema", "Change schema", ["works"], persistence_change=change)
        self.assertEqual(TaskSpec.from_dict(task.to_dict()).persistence_change, change)
        run = RunState(run_id="run", persistence_actions={"task-1": {"status": "verified"}})
        self.assertEqual(RunState.from_dict(run.to_dict()).persistence_actions, run.persistence_actions)
        session = SessionState(
            session_id="session",
            persistence_change=change,
            persistence_actions={"session": {"status": "approved"}},
            auto_approve=True,
        )
        round_trip = SessionState.from_dict(session.to_dict())
        self.assertEqual(round_trip.persistence_change, change)
        self.assertTrue(round_trip.auto_approve)

    def test_versioned_plan_requires_every_active_task_declaration(self) -> None:
        task = {
            "task_id": "task-1",
            "title": "UI",
            "description": "Change UI",
            "acceptance": ["works"],
            "status": "pending",
            "commit_message": "feat: ui",
        }
        errors = validate_task_plan_payload(
            {"persistence_contract_version": 1, "tasks": [task]}
        )
        self.assertTrue(any("persistence_change" in error for error in errors))
        task["persistence_change"] = {"strategy": "none"}
        self.assertEqual(
            validate_task_plan_payload(
                {"persistence_contract_version": 1, "tasks": [task]}
            ),
            [],
        )

    def test_schema_task_must_match_active_decision_and_critical_proof(self) -> None:
        change = {
            "strategy": "startup_compatible",
            "decision_id": "PERSIST-001",
            "target_ids": ["local-db"],
            "to_version": "v2",
            "migration_artifacts": ["src/db.py"],
            "legacy_fixture_refs": ["tests/test_db.py::test_upgrade"],
        }
        plan = {
            "persistence_contract_version": 1,
            "verification_steps": [
                {
                    "kind": "test",
                    "runner": "pytest",
                    "targets": ["tests/test_db.py"],
                    "risk": "critical",
                    "parallel_safe": False,
                    "serial_reason": "shared_mutable_state",
                }
            ],
            "tasks": [
                {
                    "task_id": "task-db",
                    "status": "pending",
                    "persistence_change": change,
                }
            ],
        }
        trace = {
            "persistence_decisions": [
                {
                    "id": "PERSIST-001",
                    "target_ids": ["local-db"],
                    "strategy": "startup_compatible",
                    "source": "user clarification",
                    "status": "active",
                }
            ]
        }
        target = {
            "id": "local-db",
            "environment": "development",
            "kind": "local_file",
            "apply_argv": ["tool", "migrate"],
            "verify_argv": ["tool", "verify"],
        }
        self.assertEqual(
            validate_persistence_plan_contract(
                plan, trace, configured_targets=[target]
            ),
            [],
        )
        plan["tasks"][0]["persistence_change"]["strategy"] = "clean_break"
        errors = validate_persistence_plan_contract(plan, trace, configured_targets=[target])
        self.assertTrue(any("must match" in error for error in errors))

    def test_schema_task_rejects_empty_configured_target_set(self) -> None:
        change = {
            "strategy": "startup_compatible",
            "decision_id": "PERSIST-001",
            "target_ids": ["local-db"],
            "to_version": "v2",
            "migration_artifacts": ["src/db.py"],
            "legacy_fixture_refs": ["tests/test_db.py::test_upgrade"],
        }
        plan = {
            "verification_steps": [
                {
                    "targets": ["tests/test_db.py"],
                    "risk": "critical",
                    "parallel_safe": False,
                    "serial_reason": "shared_mutable_state",
                }
            ],
            "tasks": [{"status": "pending", "persistence_change": change}],
        }
        trace = {
            "persistence_decisions": [
                {
                    "id": "PERSIST-001",
                    "target_ids": ["local-db"],
                    "strategy": "startup_compatible",
                    "source": "user clarification",
                    "status": "active",
                }
            ]
        }

        errors = validate_persistence_plan_contract(
            plan, trace, configured_targets=[]
        )

        self.assertTrue(any("unconfigured targets: local-db" in error for error in errors))

    def test_persistence_contract_rejects_requirement_ids_as_targets(self) -> None:
        change = {
            "strategy": "startup_compatible",
            "decision_id": "PERSIST-001",
            "target_ids": ["REQ-212"],
            "to_version": "v2",
            "migration_artifacts": ["src/db.py"],
            "legacy_fixture_refs": ["tests/test_db.py::test_upgrade"],
        }
        plan = {
            "persistence_contract_version": 1,
            "verification_steps": [
                {
                    "targets": ["tests/test_db.py"],
                    "risk": "critical",
                    "parallel_safe": False,
                    "serial_reason": "ordered_contract",
                }
            ],
            "tasks": [
                {
                    "task_id": "task-db",
                    "title": "Schema",
                    "description": "Upgrade schema",
                    "acceptance": ["works"],
                    "status": "pending",
                    "commit_message": "feat: schema",
                    "persistence_change": change,
                }
            ],
        }
        trace = {
            "persistence_decisions": [
                {
                    "id": "PERSIST-001",
                    "target_ids": ["REQ-212"],
                    "strategy": "startup_compatible",
                    "source": "user clarification",
                    "status": "active",
                }
            ]
        }

        plan_errors = validate_task_plan_payload(plan)
        contract_errors = validate_persistence_plan_contract(plan, trace)

        self.assertTrue(any("not requirement IDs: REQ-212" in error for error in plan_errors))
        self.assertTrue(any("not requirement IDs: REQ-212" in error for error in contract_errors))
        self.assertTrue(
            any(
                "persistence-configure" in error
                and "persistence-rebind" in error
                and "PERSIST-001" in error
                for error in contract_errors
            )
        )

    def test_clean_break_rejects_production_target(self) -> None:
        plan = {
            "verification_steps": [
                {
                    "targets": ["tests/test_db.py"],
                    "risk": "critical",
                    "parallel_safe": False,
                    "serial_reason": "ordered_contract",
                }
            ],
            "tasks": [
                {
                    "status": "pending",
                    "persistence_change": {
                        "strategy": "clean_break",
                        "decision_id": "PERSIST-001",
                        "target_ids": ["prod"],
                        "to_version": "v2",
                        "migration_artifacts": ["src/db.py"],
                        "legacy_fixture_refs": ["tests/test_db.py::test_reset"],
                    },
                }
            ],
        }
        trace = {
            "persistence_decisions": [
                {
                    "id": "PERSIST-001",
                    "target_ids": ["prod"],
                    "strategy": "clean_break",
                    "source": "spec",
                    "status": "active",
                }
            ]
        }
        target = {
            "id": "prod",
            "environment": "production",
            "kind": "local_file",
            "initialize_argv": ["tool", "init"],
            "verify_argv": ["tool", "verify"],
        }
        errors = validate_persistence_plan_contract(plan, trace, configured_targets=[target])
        self.assertTrue(any("cannot target production" in error for error in errors))

    def test_requirements_trace_validates_persistence_decisions(self) -> None:
        trace = {
            "version": 1,
            "persistence_decisions": [
                {
                    "id": "PERSIST-001",
                    "target_ids": ["local"],
                    "strategy": "clean_break",
                    "source": "explicit user choice",
                    "status": "active",
                }
            ],
            "requirements": [],
        }
        self.assertEqual(validate_requirements_trace_payload(trace), [])


class PersistenceDetectionTests(unittest.TestCase):
    def test_detects_inline_ddl_and_migration_paths_but_ignores_tests(self) -> None:
        diff = """diff --git a/app/db.py b/app/db.py
+++ b/app/db.py
@@ -1,0 +2 @@
+connection.execute(\"ALTER TABLE projects ADD COLUMN requested_duration_sec INTEGER\")
diff --git a/migrations/002.sql b/migrations/002.sql
+++ b/migrations/002.sql
@@ -0,0 +1 @@
+SELECT 1;
diff --git a/tests/test_db.py b/tests/test_db.py
+++ b/tests/test_db.py
@@ -0,0 +1 @@
+SQL = \"DROP TABLE projects\"
"""
        findings = detect_persistence_schema_changes(Path.cwd(), diff_text=diff)
        self.assertEqual({finding.path for finding in findings}, {"app/db.py", "migrations/002.sql"})

    def test_detects_inline_sql_column_rename_without_ddl_keyword(self) -> None:
        diff = """diff --git a/app/infrastructure/sqlite.py b/app/infrastructure/sqlite.py
--- a/app/infrastructure/sqlite.py
+++ b/app/infrastructure/sqlite.py
@@ -1 +1 @@
-target_duration_sec INTEGER NOT NULL,
+requested_duration_sec INTEGER NOT NULL,
"""
        findings = detect_persistence_schema_changes(Path.cwd(), diff_text=diff)
        self.assertEqual([finding.path for finding in findings], ["app/infrastructure/sqlite.py", "app/infrastructure/sqlite.py"])

    def test_ignores_python_statements_that_look_like_text_columns(self) -> None:
        diff = """diff --git a/app/domain/models.py b/app/domain/models.py
--- a/app/domain/models.py
+++ b/app/domain/models.py
@@ -1,0 +2,3 @@
+if text is None:
+    return "other"
+return text
"""

        self.assertEqual(
            detect_persistence_schema_changes(Path.cwd(), diff_text=diff),
            [],
        )

    def test_candidate_fingerprint_ignores_orchestrator_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / ".gitignore").write_text(".auto-agents/runs/\n", encoding="utf-8")
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / ".auto-agents" / "state").mkdir(parents=True)
            state_path = root / ".auto-agents" / "state" / "run_state.json"
            state_path.write_text("{}", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "baseline"],
                cwd=root,
                check=True,
            )
            before = persistence_candidate_fingerprint(root)
            state_path.write_text('{"status":"approved"}', encoding="utf-8")
            self.assertEqual(persistence_candidate_fingerprint(root), before)
            (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            self.assertNotEqual(persistence_candidate_fingerprint(root), before)


class PersistenceExecutionTests(unittest.TestCase):
    def test_valid_pytest_selector_is_collected_before_apply_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "apply.py").write_text(
                "from pathlib import Path\nPath('applied').write_text('yes')\n",
                encoding="utf-8",
            )
            (root / "test_db.py").write_text(
                "def test_current_contract():\n    assert True\n",
                encoding="utf-8",
            )
            target = PersistenceTargetConfig(
                target_id="local",
                environment="development",
                kind="local_file",
                locator={"path": ".data/app.db"},
                apply_argv=[sys.executable, "apply.py"],
                verify_argv=[
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "test_db.py::test_current_contract",
                ],
            )

            result = execute_persistence_action(
                root,
                {
                    "strategy": "startup_compatible",
                    "decision_id": "PERSIST-001",
                    "target_ids": ["local"],
                    "to_version": "v2",
                },
                PersistenceConfig([target]),
            )

            self.assertTrue(result["ok"])
            self.assertTrue((root / "applied").exists())

    def test_stale_pytest_selector_is_rejected_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "apply.py").write_text(
                "from pathlib import Path\nPath('applied').write_text('yes')\n",
                encoding="utf-8",
            )
            (root / "test_db.py").write_text(
                "def test_current_contract():\n    assert True\n",
                encoding="utf-8",
            )
            target = PersistenceTargetConfig(
                target_id="local",
                environment="development",
                kind="local_file",
                locator={"path": ".data/app.db"},
                apply_argv=[sys.executable, "apply.py"],
                verify_argv=[
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "test_db.py::test_removed_contract",
                ],
            )

            with self.assertRaises(PersistenceContractError) as raised:
                execute_persistence_action(
                    root,
                    {
                        "strategy": "startup_compatible",
                        "decision_id": "PERSIST-001",
                        "target_ids": ["local"],
                        "to_version": "v2",
                    },
                    PersistenceConfig([target]),
                )

            self.assertIn("configuration is stale", str(raised.exception))
            self.assertIn("run persistence-configure", str(raised.exception))
            self.assertFalse((root / "applied").exists())

    def test_all_targets_are_preflighted_before_the_first_target_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "apply.py").write_text(
                "from pathlib import Path\nPath('applied').write_text('yes')\n",
                encoding="utf-8",
            )
            first = PersistenceTargetConfig(
                target_id="first",
                environment="development",
                kind="local_file",
                locator={"path": ".data/first.db"},
                apply_argv=[sys.executable, "apply.py"],
                verify_argv=["true"],
            )
            invalid_second = PersistenceTargetConfig(
                target_id="second",
                environment="development",
                kind="local_file",
                locator={"path": ".data/second.db"},
                apply_argv=[],
                verify_argv=["true"],
            )

            with self.assertRaisesRegex(
                PersistenceContractError,
                "startup_compatible target second requires apply_argv",
            ):
                execute_persistence_action(
                    root,
                    {
                        "strategy": "startup_compatible",
                        "decision_id": "PERSIST-001",
                        "target_ids": ["first", "second"],
                        "to_version": "v2",
                    },
                    PersistenceConfig([first, invalid_second]),
                )

            self.assertFalse((root / "applied").exists())

    def test_clean_break_deletes_registered_ignored_data_and_reinitializes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / ".gitignore").write_text(".data/\n", encoding="utf-8")
            data = root / ".data"
            (data / "media").mkdir(parents=True)
            (data / "app.db").write_text("legacy", encoding="utf-8")
            (data / "media" / "old.bin").write_text("old", encoding="utf-8")
            (root / "init_db.py").write_text(
                "from pathlib import Path\np=Path('.data/app.db'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('v2')\n",
                encoding="utf-8",
            )
            (root / "verify_db.py").write_text(
                "from pathlib import Path\nraise SystemExit(0 if Path('.data/app.db').read_text() == 'v2' else 1)\n",
                encoding="utf-8",
            )
            target = PersistenceTargetConfig(
                target_id="local",
                environment="development",
                kind="local_file",
                locator={"path": ".data/app.db"},
                associated_paths=[".data/media"],
                initialize_argv=[sys.executable, "init_db.py"],
                verify_argv=[sys.executable, "verify_db.py"],
            )
            change = {
                "strategy": "clean_break",
                "decision_id": "PERSIST-001",
                "target_ids": ["local"],
                "to_version": "v2",
            }
            manifest = build_persistence_action_manifest(
                root, change, PersistenceConfig([target]), candidate_fingerprint="candidate"
            )
            self.assertEqual(manifest["targets"][0]["destructive_paths"], [".data/app.db", ".data/media"])
            result = execute_persistence_action(root, change, PersistenceConfig([target]))
            self.assertTrue(result["ok"])
            self.assertEqual((data / "app.db").read_text(encoding="utf-8"), "v2")
            self.assertFalse((data / "media").exists())

    def test_production_target_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = PersistenceTargetConfig(
                target_id="prod",
                environment="production",
                kind="compose_service",
                locator={"compose_file": "compose.yml", "services": ["db"]},
                apply_argv=["false"],
            )
            result = execute_persistence_action(
                Path(tmp),
                {
                    "strategy": "external_operator",
                    "decision_id": "PERSIST-001",
                    "target_ids": ["prod"],
                    "to_version": "v2",
                },
                PersistenceConfig([target]),
            )
            self.assertEqual(result["targets"][0]["status"], "generate_only")

    def test_clean_break_refuses_paths_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            target = PersistenceTargetConfig(
                target_id="unsafe",
                environment="development",
                kind="local_file",
                locator={"path": "/tmp/not-owned.db"},
                initialize_argv=["true"],
                verify_argv=["true"],
            )
            with self.assertRaises(PersistenceContractError):
                build_persistence_action_manifest(
                    root,
                    {
                        "strategy": "clean_break",
                        "decision_id": "PERSIST-001",
                        "target_ids": ["unsafe"],
                        "to_version": "v2",
                    },
                    PersistenceConfig([target]),
                    candidate_fingerprint="candidate",
                )

    def test_orchestrator_auto_approve_does_not_authorize_clean_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            (root / ".data").mkdir()
            (root / ".data" / "app.db").write_text("legacy", encoding="utf-8")
            (root / "init_db.py").write_text(
                "from pathlib import Path\np=Path('.data/app.db'); p.parent.mkdir(exist_ok=True); p.write_text('v2')\n",
                encoding="utf-8",
            )
            (root / "verify_db.py").write_text(
                "from pathlib import Path\nraise SystemExit(0 if Path('.data/app.db').read_text() == 'v2' else 1)\n",
                encoding="utf-8",
            )
            orchestrator = Orchestrator(root)
            orchestrator.config.persistence.targets = [
                PersistenceTargetConfig(
                    target_id="local",
                    environment="development",
                    kind="local_file",
                    locator={"path": ".data/app.db"},
                    initialize_argv=[sys.executable, "init_db.py"],
                    verify_argv=[sys.executable, "verify_db.py"],
                )
            ]
            change = {
                "strategy": "clean_break",
                "decision_id": "PERSIST-001",
                "target_ids": ["local"],
                "to_version": "v2",
            }
            task = TaskSpec(
                "task-db", "Schema", "Reset schema", ["works"], persistence_change=change
            )
            state = RunState(
                run_id="run",
                tasks=[task],
                resume_context={"auto_approve": True},
            )
            with self.assertRaises(PersistenceContractError):
                orchestrator._run_task_persistence_action(state, task)

            self.assertEqual((root / ".data" / "app.db").read_text(), "legacy")
            self.assertEqual(state.status, "paused")
            self.assertEqual(state.pending_approval, "persistence-reset")
            self.assertEqual(
                state.persistence_actions["_clean_break_approval"]["status"],
                "pending_approval",
            )

    def test_declined_clean_break_persists_resumable_approval_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.persistence.targets = [
                PersistenceTargetConfig(
                    target_id="local",
                    environment="development",
                    kind="local_file",
                    locator={"path": ".data/app.db"},
                    initialize_argv=["true"],
                    verify_argv=["true"],
                )
            ]
            orchestrator._prompt_user = lambda *args, **kwargs: "n"
            task = TaskSpec(
                "task-db",
                "Schema",
                "Reset schema",
                ["works"],
                persistence_change={
                    "strategy": "clean_break",
                    "decision_id": "PERSIST-001",
                    "target_ids": ["local"],
                    "to_version": "v2",
                },
            )
            state = RunState(run_id="run", tasks=[task])
            with self.assertRaises(PersistenceContractError):
                orchestrator._run_task_persistence_action(state, task)
            persisted = load_run_state(root)
            self.assertEqual(persisted.pending_approval, "persistence-reset")
            approved = orchestrator.approve("persistence-reset")
            self.assertEqual(
                approved.persistence_actions["_clean_break_approval"]["status"],
                "approved",
            )

    def test_orchestrator_blocks_undeclared_inline_schema_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            app = root / "app.py"
            app.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "baseline"],
                cwd=root,
                check=True,
            )
            app.write_text(
                "connection.execute('ALTER TABLE projects ADD COLUMN duration INTEGER')\n",
                encoding="utf-8",
            )
            issue = Orchestrator(root)._persistence_contract_issue(
                TaskSpec("task-1", "Change", "Change", ["works"])
            )
            self.assertIn("user-approved persistence strategy", issue)


class PersistenceRebindTests(unittest.TestCase):
    def test_batch_replace_restores_every_file_after_commit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "one.json", root / "two.json", root / "three.json"]
            for index, path in enumerate(paths, start=1):
                write_json(path, {"value": f"old-{index}"})
            before = {path: path.read_bytes() for path in paths}
            real_replace = os.replace
            failed = False

            def fail_second_staged_replace(source, destination):
                nonlocal failed
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not failed
                    and source_path.name.endswith(".staged")
                    and destination_path.name == "two.json"
                ):
                    failed = True
                    raise OSError("simulated commit failure")
                return real_replace(source, destination)

            with patch(
                "auto_agents.persistence_rebind.os.replace",
                side_effect=fail_second_staged_replace,
            ):
                with self.assertRaisesRegex(
                    PersistenceRebindError,
                    "failed to commit persistence rebind transaction",
                ):
                    _replace_json_batch(
                        {
                            path: {"value": f"new-{index}"}
                            for index, path in enumerate(paths, start=1)
                        }
                    )

            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertEqual(
                [path for path in root.iterdir() if path.name.startswith(".")],
                [],
            )

    def test_rebind_updates_trace_plan_and_run_state_and_clears_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            _legacy_persistence_project(root)

            result = rebind_legacy_persistence_decision(
                root,
                decision_id="PERSIST-001",
                target_ids=["local-sqlite-test"],
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["blocker_cleared"])
            trace = read_json(requirements_trace_path(root))
            plan = read_json(task_plan_path(root))
            state = read_json(run_state_path(root))
            self.assertEqual(
                trace["persistence_decisions"][0]["target_ids"],
                ["local-sqlite-test"],
            )
            self.assertEqual(
                plan["tasks"][0]["persistence_change"]["target_ids"],
                ["local-sqlite-test"],
            )
            self.assertEqual(
                state["tasks"][0]["persistence_change"]["target_ids"],
                ["local-sqlite-test"],
            )
            self.assertEqual(state["status"], "pending")
            self.assertEqual(state["active_blocker"], {})
            self.assertEqual(state["last_error"], "")
            receipt = state["resume_context"]["persistence_rebinds"][
                "PERSIST-001"
            ]
            self.assertEqual(
                receipt["from_target_ids"],
                ["REQ-001", "REQ-002"],
            )

            repeated = rebind_legacy_persistence_decision(
                root,
                decision_id="PERSIST-001",
                target_ids=["local-sqlite-test"],
            )

            self.assertTrue(repeated["no_op"])
            repeated_state = read_json(run_state_path(root))
            self.assertEqual(
                repeated_state["resume_context"]["persistence_rebinds"][
                    "PERSIST-001"
                ],
                receipt,
            )

    def test_rebind_requires_registered_target_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            _legacy_persistence_project(root)
            before = {
                path: path.read_bytes()
                for path in (
                    requirements_trace_path(root),
                    task_plan_path(root),
                    run_state_path(root),
                )
            }

            with self.assertRaisesRegex(
                PersistenceRebindError,
                "run persistence-configure first",
            ):
                rebind_legacy_persistence_decision(
                    root,
                    decision_id="PERSIST-001",
                    target_ids=["missing-target"],
                )

            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_rebind_refuses_to_replace_established_target_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            _legacy_persistence_project(root)
            trace = read_json(requirements_trace_path(root))
            trace["persistence_decisions"][0]["target_ids"] = [
                "established-db"
            ]
            write_json(requirements_trace_path(root), trace)

            with self.assertRaisesRegex(
                PersistenceRebindError,
                "refusing to change an established target binding",
            ):
                rebind_legacy_persistence_decision(
                    root,
                    decision_id="PERSIST-001",
                    target_ids=["local-sqlite-test"],
                )

    def test_validation_failure_leaves_all_canonical_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            _legacy_persistence_project(root)
            plan = read_json(task_plan_path(root))
            plan["tasks"][0]["title"] = ""
            write_json(task_plan_path(root), plan)
            before = {
                path: path.read_bytes()
                for path in (
                    requirements_trace_path(root),
                    task_plan_path(root),
                    run_state_path(root),
                )
            }

            with self.assertRaisesRegex(
                PersistenceRebindError,
                "persistence rebind validation failed",
            ):
                rebind_legacy_persistence_decision(
                    root,
                    decision_id="PERSIST-001",
                    target_ids=["local-sqlite-test"],
                )

            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_cli_rebind_uses_registered_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            _legacy_persistence_project(root)

            exit_code = main(
                [
                    "persistence-rebind",
                    "--project",
                    str(root),
                    "--decision",
                    "PERSIST-001",
                    "--target",
                    "local-sqlite-test",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                read_json(requirements_trace_path(root))[
                    "persistence_decisions"
                ][0]["target_ids"],
                ["local-sqlite-test"],
            )


class PersistenceCLITests(unittest.TestCase):
    def test_fix_and_collab_accept_auto_approve(self) -> None:
        parser = build_parser()
        self.assertTrue(
            parser.parse_args(["fix", "--project", "/tmp/demo", "--auto-approve"]).auto_approve
        )
        self.assertTrue(
            parser.parse_args(["collab", "--project", "/tmp/demo", "--auto-approve"]).auto_approve
        )

    def test_noninteractive_configure_registers_human_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            exit_code = main(
                [
                    "persistence-configure",
                    "--project",
                    str(root),
                    "--id",
                    "local-db",
                    "--environment",
                    "development",
                    "--kind",
                    "local_file",
                    "--path",
                    ".data/app.db",
                    "--initialize-command",
                    "true",
                    "--verify-command",
                    "true",
                    "--auto-approve",
                ]
            )
            self.assertEqual(exit_code, 0)
            target = load_project_config(root).persistence.targets[0]
            self.assertEqual(target.environment, "development")

    def test_config_validation_rejects_shell_control_tokens(self) -> None:
        errors = validate_persistence_config_payload(
            {
                "targets": [
                    {
                        "id": "local-db",
                        "environment": "development",
                        "kind": "local_file",
                        "locator": {"path": ".data/app.db"},
                        "apply_argv": ["tool", "migrate;rm"],
                    }
                ]
            }
        )
        self.assertTrue(any("shell control" in error for error in errors))

    def test_config_validation_rejects_requirement_id_as_target_id(self) -> None:
        errors = validate_persistence_config_payload(
            {
                "targets": [
                    {
                        "id": "req-212",
                        "environment": "development",
                        "kind": "local_file",
                        "locator": {"path": ".data/app.db"},
                        "apply_argv": ["tool", "migrate"],
                        "verify_argv": ["tool", "verify"],
                    }
                ]
            }
        )

        self.assertTrue(any("not a requirement ID" in error for error in errors))

    def test_session_marker_requires_registered_nonproduction_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            session = Session(orchestrator, mode="fix")
            state = SessionState(session_id="session", mode="fix")
            payload = {
                "strategy": "clean_break",
                "decision_id": "session-decision",
                "target_ids": ["missing"],
                "to_version": "v2",
                "migration_artifacts": ["src/db.py"],
                "legacy_fixture_refs": ["tests/test_db.py::test_reset"],
            }
            error = session._apply_session_persistence_marker(
                state, "PERSISTENCE_CHANGE: " + json.dumps(payload)
            )
            self.assertIn("persistence-configure", error)


class PersistenceContractV2Tests(unittest.TestCase):
    def test_human_approved_target_proposal_registers_pending_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            proposal = {
                "persistence_target_proposals": [{
                    "id": "primary-db",
                    "environment": "development",
                    "kind": "local_file",
                    "locator": {"path": ".data/app.db"},
                    "associated_paths": [],
                }]
            }
            with patch.object(orchestrator, "_prompt_user", return_value="y"):
                self.assertTrue(orchestrator._approve_persistence_target_proposals(proposal))
            target = load_project_config(root).persistence.target("primary-db")
            assert target is not None
            self.assertEqual(target.lifecycle, "pending_bootstrap")
            self.assertEqual(target.interface_version, 2)

    def test_existing_migration_artifact_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            migration = root / "migrations/versions/0001.py"
            migration.parent.mkdir(parents=True)
            migration.write_text("VERSION = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
            migration.write_text("VERSION = 99\n", encoding="utf-8")

            errors = migration_artifact_immutability_errors(
                root,
                {
                    "migration_artifacts": [{
                        "id": "0001", "path": "migrations/versions/0001.py", "kind": "baseline"
                    }]
                },
            )
            self.assertTrue(any("immutable migration artifact" in item for item in errors))

    def test_v2_rebuild_executes_json_protocol_and_deletes_sqlite_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init(root)
            (root / ".gitignore").write_text(".data/\n", encoding="utf-8")
            data = root / ".data"
            data.mkdir()
            database = data / "app.db"
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{database}{suffix}").write_text("old", encoding="utf-8")
            script = root / "runner.py"
            script.write_text(
                "import json,sys\n"
                "from pathlib import Path\n"
                "op=sys.argv[1]\n"
                "p=Path('.data/app.db')\n"
                "p.write_text('new') if op=='initialize' else None\n"
                "print(json.dumps({'protocol_version':1,'operation':op,'ok':True,"
                "'state':'ready','current_version':'0001','latest_version':'0001',"
                "'pending_versions':[],'applied_migrations':[],"
                "'schema_fingerprint':'sha256:test'}))\n",
                encoding="utf-8",
            )
            command = [sys.executable, "runner.py"]
            target = PersistenceTargetConfig(
                target_id="local-db",
                environment="test",
                kind="local_file",
                locator={"path": ".data/app.db"},
                interface_version=2,
                lifecycle="ready",
                status_argv=[*command, "status"],
                initialize_argv=[*command, "initialize"],
                migrate_argv=[*command, "migrate"],
                verify_argv=[*command, "verify"],
                migration_roots=["migrations/versions"],
            )
            result = execute_persistence_action(
                root,
                {
                    "storage_transition": "rebuild",
                    "compatibility_policy": "reject_legacy",
                    "decision_id": "PERSIST-001",
                    "target_ids": ["local-db"],
                    "to_version": "0001",
                    "migration_artifacts": [],
                    "contract_artifacts": ["src/contracts.py"],
                    "legacy_fixture_refs": ["tests/test_db.py::test_reset"],
                },
                PersistenceConfig([target]),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(database.read_text(encoding="utf-8"), "new")
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{database}{suffix}").exists())

    def test_v2_status_contract_error_is_structured_as_pre_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "runner.py"
            script.write_text(
                "import json,sys\n"
                "from pathlib import Path\n"
                "op=sys.argv[1]\n"
                "data=Path('.data'); data.mkdir(exist_ok=True)\n"
                "with (data/'calls.log').open('a') as stream: stream.write(op+'\\n')\n"
                "if op=='migrate': (data/'applied').write_text('yes')\n"
                "print(json.dumps({'protocol_version':2 if op=='status' else 1,"
                "'operation':op,'ok':True,'state':'ready','current_version':'2',"
                "'latest_version':'2','pending_versions':[]}))\n",
                encoding="utf-8",
            )
            command = [sys.executable, "runner.py"]
            target = PersistenceTargetConfig(
                target_id="local-db",
                environment="test",
                kind="local_file",
                locator={"path": ".data/app.db"},
                interface_version=2,
                lifecycle="ready",
                status_argv=[*command, "status"],
                migrate_argv=[*command, "migrate"],
                verify_argv=[*command, "verify"],
            )

            with self.assertRaises(PersistenceContractError) as raised:
                execute_persistence_action(
                    root,
                    {
                        "storage_transition": "migrate_in_place",
                        "compatibility_policy": "backward_compatible",
                        "decision_id": "PERSIST-001",
                        "target_ids": ["local-db"],
                        "to_version": "2",
                    },
                    PersistenceConfig([target]),
                )

            error = raised.exception
            self.assertEqual(error.target_id, "local-db")
            self.assertEqual(error.step, "status")
            self.assertEqual(error.code, "unsupported_protocol")
            self.assertIs(error.mutation_started, False)
            self.assertEqual(error.command_outcome["returncode"], 0)
            self.assertEqual(error.command_outcome["protocol_version"], 2)
            self.assertEqual(
                (root / ".data" / "calls.log").read_text(encoding="utf-8"),
                "status\n",
            )
            self.assertFalse((root / ".data" / "applied").exists())

    def test_v2_verify_contract_error_is_structured_as_post_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "runner.py"
            script.write_text(
                "import json,sys\n"
                "from pathlib import Path\n"
                "op=sys.argv[1]\n"
                "data=Path('.data'); data.mkdir(exist_ok=True)\n"
                "with (data/'calls.log').open('a') as stream: stream.write(op+'\\n')\n"
                "if op=='migrate': (data/'applied').write_text('yes')\n"
                "print(json.dumps({'protocol_version':2 if op=='verify' else 1,"
                "'operation':op,'ok':True,'state':'ready','current_version':'2',"
                "'latest_version':'2','pending_versions':[]}))\n",
                encoding="utf-8",
            )
            command = [sys.executable, "runner.py"]
            target = PersistenceTargetConfig(
                target_id="local-db",
                environment="test",
                kind="local_file",
                locator={"path": ".data/app.db"},
                interface_version=2,
                lifecycle="ready",
                status_argv=[*command, "status"],
                migrate_argv=[*command, "migrate"],
                verify_argv=[*command, "verify"],
            )

            with self.assertRaises(PersistenceContractError) as raised:
                execute_persistence_action(
                    root,
                    {
                        "storage_transition": "migrate_in_place",
                        "compatibility_policy": "backward_compatible",
                        "decision_id": "PERSIST-001",
                        "target_ids": ["local-db"],
                        "to_version": "2",
                    },
                    PersistenceConfig([target]),
                )

            error = raised.exception
            self.assertEqual(error.step, "verify")
            self.assertIs(error.mutation_started, True)
            self.assertEqual(
                (root / ".data" / "calls.log").read_text(encoding="utf-8"),
                "status\nmigrate\nverify\n",
            )
            self.assertTrue((root / ".data" / "applied").exists())

    def test_later_status_error_stays_post_mutation_across_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "runner.py"
            script.write_text(
                "import json,sys\n"
                "from pathlib import Path\n"
                "target,op=sys.argv[1:3]\n"
                "data=Path('.data'); data.mkdir(exist_ok=True)\n"
                "with (data/'calls.log').open('a') as stream: "
                "stream.write(target+':'+op+'\\n')\n"
                "if op=='migrate': (data/('applied-'+target)).write_text('yes')\n"
                "protocol=2 if target=='second' and op=='status' else 1\n"
                "print(json.dumps({'protocol_version':protocol,'operation':op,"
                "'ok':True,'state':'ready','current_version':'2',"
                "'latest_version':'2','pending_versions':[]}))\n",
                encoding="utf-8",
            )

            def target(target_id: str) -> PersistenceTargetConfig:
                command = [sys.executable, "runner.py", target_id]
                return PersistenceTargetConfig(
                    target_id=target_id,
                    environment="test",
                    kind="local_file",
                    locator={"path": f".data/{target_id}.db"},
                    interface_version=2,
                    lifecycle="ready",
                    status_argv=[*command, "status"],
                    migrate_argv=[*command, "migrate"],
                    verify_argv=[*command, "verify"],
                )

            with self.assertRaises(PersistenceContractError) as raised:
                execute_persistence_action(
                    root,
                    {
                        "storage_transition": "migrate_in_place",
                        "compatibility_policy": "backward_compatible",
                        "decision_id": "PERSIST-001",
                        "target_ids": ["first", "second"],
                        "to_version": "2",
                    },
                    PersistenceConfig([target("first"), target("second")]),
                )

            error = raised.exception
            self.assertEqual(error.target_id, "second")
            self.assertEqual(error.step, "status")
            self.assertIs(error.mutation_started, True)
            self.assertEqual(
                (root / ".data" / "calls.log").read_text(encoding="utf-8"),
                "first:status\nfirst:migrate\nfirst:verify\nsecond:status\n",
            )
            self.assertTrue((root / ".data" / "applied-first").exists())
            self.assertFalse((root / ".data" / "applied-second").exists())

    def test_pending_v2_target_requires_bootstrap_interface(self) -> None:
        trace = {
            "persistence_decisions": [{
                "id": "PERSIST-001",
                "storage_transition": "initialize",
                "compatibility_policy": "not_applicable",
                "target_ids": ["db"],
                "source": "test",
                "status": "active",
            }]
        }
        plan = {
            "persistence_contract_version": 2,
            "tasks": [{
                "task_id": "task-1",
                "status": "pending",
                "persistence_change": {
                    "storage_transition": "initialize",
                    "compatibility_policy": "not_applicable",
                    "decision_id": "PERSIST-001",
                    "target_ids": ["db"],
                    "to_version": "0001",
                    "migration_artifacts": [{
                        "id": "0001", "path": "migrations/versions/0001.py", "kind": "baseline"
                    }],
                    "contract_artifacts": [],
                    "legacy_fixture_refs": [],
                },
            }],
        }
        errors = validate_persistence_plan_contract(
            plan,
            trace,
            configured_targets=[{
                "id": "db", "environment": "test", "kind": "local_file",
                "locator": {"path": ".data/app.db"}, "interface_version": 2,
                "lifecycle": "pending_bootstrap",
            }],
        )
        self.assertTrue(any("persistence_interface" in error for error in errors))

    def test_upgrade_command_preserves_run_status_and_converts_active_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            _legacy_persistence_project(root)
            before = read_json(run_state_path(root), default={})
            result = upgrade_persistence_contract(
                root,
                decision_policies={"PERSIST-001": ("initialize", "not_applicable")},
            )
            self.assertTrue(result["ok"])
            trace = read_json(requirements_trace_path(root), default={})
            plan = read_json(task_plan_path(root), default={})
            state = read_json(run_state_path(root), default={})
            self.assertEqual(trace["persistence_contract_version"], 2)
            self.assertEqual(plan["persistence_contract_version"], 2)
            self.assertEqual(
                trace["persistence_decisions"][0]["storage_transition"], "initialize"
            )
            self.assertNotIn("strategy", trace["persistence_decisions"][0])
            self.assertEqual(state.get("run_id"), before.get("run_id"))


if __name__ == "__main__":
    unittest.main()
