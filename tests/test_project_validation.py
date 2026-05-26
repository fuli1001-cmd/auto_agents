import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.cli import main
from auto_agents.adapters.codex import CodexAdapter
from auto_agents.config import (
    DEFAULT_CONFIG,
    auto_dir,
    config_path,
    create_session,
    load_project_config,
    load_run_state,
    save_session_state,
    requirements_trace_path,
    save_project_config,
    save_run_state,
    task_plan_path,
)
from auto_agents.git_ops import working_tree_clean
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentRequest, AgentResult, AgentUsage, ProjectConfig, ProviderConfig, SessionState, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.validation import (
    validate_required_document,
    validate_project_config_payload,
    validation_report,
)


class ProjectValidationTests(unittest.TestCase):
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

    def test_validate_project_config_payload_rejects_bad_effort_and_template(self) -> None:
        payload = {
            "project_name": "demo",
            "providers": {
                "codex": {
                    "kind": "codex",
                    "binary": "codex",
                    "profile_map": {},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
                "copilot-cli": {
                    "kind": "copilot-cli",
                    "binary": "copilot",
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
            },
            "active_provider": "codex",
            "docs": {
                "language": "jp",
            },
            "efforts": {
                "clarify": "deep",
                "design": "wrong",
                "plan": "balanced",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
            },
            "gates": {
                "commands": [],
                "require_clean_git_before_task": True,
                "allow_agent_updates": True,
            },
            "git": {
                "auto_init_repo": True,
                "commit_each_task": True,
                "commit_message_template": "feat: missing placeholders",
            },
            "approvals": {
                "enabled": ["requirements", "bad"],
            },
            "retries": {
                "default_max_attempts": 0,
                "per_stage": {
                    "plan": 2,
                    "normalize_project_rules": 2,
                    "unknown": 1,
                },
            },
        }
        errors = validate_project_config_payload(payload)
        self.assertTrue(any("efforts.design" in item for item in errors))
        self.assertTrue(any("commit_message_template must contain '{task_id}'" in item for item in errors))
        self.assertTrue(any("invalid values" in item for item in errors))
        self.assertTrue(any("default_max_attempts" in item for item in errors))
        self.assertTrue(any("unknown stage" in item for item in errors))
        self.assertTrue(any("docs.language" in item for item in errors))

    def test_validate_project_config_payload_rejects_non_isolated_python_commands(self) -> None:
        payload = {
            "project_name": "demo",
            "providers": {
                "codex": {
                    "kind": "codex",
                    "binary": "codex",
                    "profile_map": {"balanced": "m", "deep": "h", "max": "xh"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
                "copilot-cli": {
                    "kind": "copilot-cli",
                    "binary": "copilot",
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
            },
            "active_provider": "codex",
            "docs": {
                "language": "en",
            },
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "provider_research": "deep",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
                "readme": "balanced",
            },
            "gates": {
                "commands": ["python3 -m unittest discover -s tests", "python3 -m pip install requests"],
                "require_clean_git_before_task": True,
                "allow_agent_updates": True,
            },
            "git": {
                "auto_init_repo": True,
                "commit_each_task": True,
                "commit_message_template": "feat({task_id}): {title}",
            },
            "approvals": {
                "enabled": ["requirements", "architecture", "release"],
            },
            "retries": {
                "default_max_attempts": 2,
                "per_stage": {
                    "clarify": 2,
                    "design": 2,
                    "plan": 3,
                    "provider_research": 2,
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        errors = validate_project_config_payload(payload)
        self.assertTrue(any("project-local conda env" in item for item in errors))
        self.assertTrue(any("must not modify shared system environments" in item for item in errors))

    def test_validate_project_config_payload_accepts_isolated_python_commands(self) -> None:
        payload = {
            "project_name": "demo",
            "providers": {
                "codex": {
                    "kind": "codex",
                    "binary": "codex",
                    "profile_map": {"balanced": "m", "deep": "h", "max": "xh"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
                "copilot-cli": {
                    "kind": "copilot-cli",
                    "binary": "copilot",
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
            },
            "active_provider": "codex",
            "docs": {
                "language": "zh",
            },
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "provider_research": "deep",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
                "readme": "balanced",
            },
            "gates": {
                "commands": ["conda run -p ./.conda python -m pytest -q tests"],
                "require_clean_git_before_task": True,
                "allow_agent_updates": True,
            },
            "git": {
                "auto_init_repo": True,
                "commit_each_task": True,
                "commit_message_template": "feat({task_id}): {title}",
            },
            "approvals": {
                "enabled": ["requirements", "architecture", "release"],
            },
            "retries": {
                "default_max_attempts": 2,
                "per_stage": {
                    "clarify": 2,
                    "design": 2,
                    "plan": 3,
                    "provider_research": 2,
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_accepts_parallel_gate_groups(self) -> None:
        payload = {
            "project_name": "demo",
            "providers": {
                "codex": {
                    "kind": "codex",
                    "binary": "codex",
                    "profile_map": {"balanced": "m", "deep": "h", "max": "xh"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
                "copilot-cli": {
                    "kind": "copilot-cli",
                    "binary": "copilot",
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
            },
            "active_provider": "codex",
            "docs": {"language": "en"},
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "provider_research": "deep",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
                "readme": "balanced",
            },
            "gates": {
                "commands": ["conda run -p ./.conda python -m pytest -q tests"],
                "parallel_groups": [
                    {
                        "name": "quality",
                        "commands": [
                            "conda run -p ./.conda python -m pytest -q tests",
                            "conda run -p ./.conda python -m pytest -q tests/test_ok.py",
                        ],
                    }
                ],
                "require_clean_git_before_task": True,
                "allow_agent_updates": True,
            },
            "git": {
                "auto_init_repo": True,
                "commit_each_task": True,
                "commit_message_template": "feat({task_id}): {title}",
            },
            "approvals": {"enabled": ["requirements", "architecture", "release"]},
            "retries": {
                "default_max_attempts": 2,
                "per_stage": {
                    "clarify": 2,
                    "design": 2,
                    "plan": 3,
                    "provider_research": 2,
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_rejects_invalid_parallel_gate_groups(self) -> None:
        payload = {
            "project_name": "demo",
            "providers": {
                "codex": {
                    "kind": "codex",
                    "binary": "codex",
                    "profile_map": {"balanced": "m", "deep": "h", "max": "xh"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
                "copilot-cli": {
                    "kind": "copilot-cli",
                    "binary": "copilot",
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
            },
            "active_provider": "codex",
            "docs": {"language": "en"},
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "provider_research": "deep",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
                "readme": "balanced",
            },
            "gates": {
                "commands": [],
                "parallel_groups": [{"name": "", "commands": [""]}],
                "require_clean_git_before_task": True,
                "allow_agent_updates": True,
            },
            "git": {
                "auto_init_repo": True,
                "commit_each_task": True,
                "commit_message_template": "feat({task_id}): {title}",
            },
            "approvals": {"enabled": ["requirements", "architecture", "release"]},
            "retries": {
                "default_max_attempts": 2,
                "per_stage": {
                    "clarify": 2,
                    "design": 2,
                    "plan": 3,
                    "provider_research": 2,
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        errors = validate_project_config_payload(payload)
        self.assertTrue(any("gates.parallel_groups[1].name" in item for item in errors))
        self.assertTrue(any("gates.parallel_groups[1].commands" in item for item in errors))

    def test_validate_project_config_payload_accepts_parallel_task_execution(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["execution"] = {
            "parallel_tasks": {
                "enabled": True,
                "max_workers": 3,
                "strict": False,
                "worktree_root": "",
            }
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_rejects_parallel_tasks_without_commits(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["git"]["commit_each_task"] = False
        payload["execution"] = {
            "parallel_tasks": {
                "enabled": True,
                "max_workers": 2,
                "strict": True,
                "worktree_root": "",
            }
        }

        errors = validate_project_config_payload(payload)
        self.assertTrue(any("requires git.commit_each_task=true" in item for item in errors))

    def test_validate_project_config_payload_rejects_invalid_sync_agent_instructions_effort(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["efforts"]["sync-agent-instructions"] = "wrong"

        errors = validate_project_config_payload(payload)

        self.assertTrue(any("efforts.sync-agent-instructions" in item for item in errors))

    def test_validate_project_config_payload_accepts_config_without_docs(self) -> None:
        payload = {
            "project_name": "demo",
            "providers": {
                "codex": {
                    "kind": "codex",
                    "binary": "codex",
                    "profile_map": {"balanced": "m", "deep": "h", "max": "xh"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
                "copilot-cli": {
                    "kind": "copilot-cli",
                    "binary": "copilot",
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
                    "extra_args": [],
                    "cwd_flag": "-C",
                    "prompt_via_stdin": True,
                    "output_flag": "-o",
                },
            },
            "active_provider": "codex",
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "provider_research": "deep",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
                "readme": "balanced",
            },
            "gates": {
                "commands": ["conda run -p ./.conda python -m pytest -q tests"],
                "require_clean_git_before_task": True,
                "allow_agent_updates": True,
            },
            "git": {
                "auto_init_repo": True,
                "commit_each_task": True,
                "commit_message_template": "feat({task_id}): {title}",
            },
            "approvals": {
                "enabled": ["requirements", "architecture", "release"],
            },
            "retries": {
                "default_max_attempts": 2,
                "per_stage": {
                    "clarify": 2,
                    "design": 2,
                    "plan": 3,
                    "provider_research": 2,
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_legacy_efforts_missing_defaulted_stages_are_accepted(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        del payload["efforts"]["provider_research"]
        del payload["efforts"]["sync-agent-instructions"]

        self.assertEqual(validate_project_config_payload(payload), [])

        config = ProjectConfig.from_dict(payload)
        self.assertEqual(config.efforts["provider_research"], "deep")
        self.assertEqual(config.efforts["sync-agent-instructions"], "deep")

    def test_project_config_rejects_legacy_agent_instructions_node(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["agent_instructions"] = {
            "normalize_with_llm": False,
            "normalization_effort_stage": "design",
        }

        errors = validate_project_config_payload(payload)
        self.assertTrue(any("agent_instructions is no longer supported" in item for item in errors))

        with self.assertRaisesRegex(ValueError, "agent_instructions"):
            ProjectConfig.from_dict(payload)

    def test_validate_project_config_payload_still_requires_other_effort_stages(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        del payload["efforts"]["readme"]

        errors = validate_project_config_payload(payload)

        self.assertTrue(any("efforts missing stages: readme" in item for item in errors))

    def test_validation_report_warns_when_no_verification_commands_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            report = validation_report(project_root)
            self.assertTrue(any("no verification steps" in item for item in report["warnings"]))

    def test_validation_report_rejects_missing_pytest_targets_in_verification_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")

            config = load_project_config(project_root)
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests/test_missing.py"]
            save_project_config(project_root, config)
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_commands": ["conda run -p ./.conda python -m pytest -q tests/test_missing.py"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Add CLI entrypoint",
                            "description": "Add a runnable command line entrypoint.",
                            "acceptance": ["`python -m demo --help` exits successfully."],
                            "status": "pending",
                            "commit_message": "feat(task-001): add CLI entrypoint",
                        }
                    ],
                },
            )

            report = validation_report(project_root)

            self.assertFalse(report["ok"])
            self.assertTrue(any("missing pytest target" in item for item in report["errors"]))

    def test_validation_report_warns_when_task_plan_looks_oversliced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": f"task-{index:03d}",
                            "title": f"Step {index}",
                            "description": "Tiny change.",
                            "acceptance": ["one check"],
                            "status": "pending",
                            "commit_message": "",
                        }
                        for index in range(1, 31)
                    ]
                },
            )

            report = validation_report(project_root)

            self.assertTrue(report["ok"])
            self.assertTrue(any("contains 30 tasks" in item for item in report["warnings"]))

    def test_validation_report_passes_for_bootstrapped_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            report = validation_report(project_root)

            self.assertTrue(report["ok"])
            self.assertEqual(report["errors"], [])
            self.assertIn("project_config", report["schemas"])
            self.assertTrue(Path(report["schemas"]["project_config"]).exists())

    def test_validation_report_catches_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(config_path(project_root), "{broken\n")
            report = validation_report(project_root)

            self.assertFalse(report["ok"])
            self.assertTrue(any("not valid JSON" in item for item in report["errors"]))

    def test_validate_required_document_reports_missing_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "architecture.md"
            write_text(path, "# Architecture\n\nOnly one heading\n")
            errors = validate_required_document(path, "architecture.md")
            self.assertTrue(any("## System Boundary" in item for item in errors))

    def test_cli_validate_returns_nonzero_for_invalid_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(task_plan_path(project_root), {"tasks": []})

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["validate", "--project", str(project_root)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertTrue(any("at least one task" in item for item in payload["errors"]))

    def test_run_fails_preflight_before_any_agent_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(task_plan_path(project_root), {"tasks": []})
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            orchestrator = Orchestrator(project_root)

            class FailingIfCalledAdapter:
                def run(self, request):
                    raise AssertionError(f"adapter should not be called during failed preflight: {request.stage}")

            orchestrator.adapter = FailingIfCalledAdapter()

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator.run(spec_file=spec_file)

            self.assertIn("preflight validation failed", str(ctx.exception))
            state = load_run_state(project_root)
            self.assertEqual(state.status, "failed")
            self.assertIn("preflight validation failed", state.last_error)

    def test_cli_run_returns_nonzero_json_when_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(config_path(project_root), "{broken\n")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["run", "--project", str(project_root), "--spec-file", str(spec_file)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("Expecting property name enclosed in double quotes", payload["error"])

    def test_cli_run_auto_starts_fresh_provider_resolve_for_current_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            blocked_error = (
                "provider research is blocked; provide official docs, defer the requirement, "
                "choose another provider, or explicitly approve assumptions before resuming.\n"
                "- REQ-001: .auto-agents/docs/provider_references/provider.md is ambiguous"
            )
            run_state = load_run_state(project_root)
            run_state.status = "failed"
            run_state.current_stage = "provider_research"
            run_state.last_error = blocked_error
            save_run_state(project_root, run_state)

            old_session = create_session(project_root, "provider_resolve")
            old_session.status = "failed"
            old_session.goal = "old blocker"
            save_session_state(project_root, old_session)

            session_calls = {"start": 0, "offer": 0}

            def mock_run(_self, *args, **kwargs):
                raise RuntimeError(blocked_error)

            def mock_start(self):
                session_calls["start"] += 1
                resumed = load_run_state(project_root)
                resumed.status = "completed"
                resumed.current_stage = "readme"
                resumed.last_error = ""
                save_run_state(project_root, resumed)
                return SessionState(
                    session_id="provider-auto-001",
                    mode="provider_resolve",
                    status="completed",
                    goal="Recover current blocker",
                    resolution="provider_research_resolved",
                )

            def fail_offer(_self):
                session_calls["offer"] += 1
                raise AssertionError("automatic recovery must not use offer_resume_or_new")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(Orchestrator, "run", mock_run),
                patch("auto_agents.session.Session.start", mock_start),
                patch("auto_agents.session.Session.offer_resume_or_new", fail_offer),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(["run", "--project", str(project_root), "--spec-file", str(spec_file)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(session_calls["start"], 1)
            self.assertEqual(session_calls["offer"], 0)
            self.assertIn("Run completed successfully.", stdout.getvalue())
            self.assertIn("Starting automatic provider recovery", stderr.getvalue())

    def test_cli_init_defaults_name_from_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "aa-demo"

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["init", "--project", str(project_root)])

            self.assertEqual(exit_code, 0)
            config = load_project_config(project_root)
            self.assertEqual(config.project_name, "aa-demo")
            self.assertEqual(config.provider.kind, "codex")
            self.assertEqual(config.docs.language, "en")

    def test_cli_init_can_set_document_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "aa-demo"

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["init", "--project", str(project_root), "--doc-language", "zh"])

            self.assertEqual(exit_code, 0)
            config = load_project_config(project_root)
            self.assertEqual(config.docs.language, "zh")

    def test_cli_sync_agent_instructions_generates_project_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(
                project_root / ".auto-agents" / "project-rules.md",
                "- Default output review pass must proceed to export.\n",
            )

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["sync-agent-instructions", "--project", str(project_root)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["synced"])
            self.assertTrue(payload["project_rules_meaningful"])
            agents = (project_root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Default output review pass must proceed to export", agents)

    def test_cli_run_defaults_spec_file_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["run", "--project", str(project_root)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn(str(project_root / "spec.md"), payload["error"])

    def test_cli_run_provider_override_persists_as_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        str(project_root),
                        "--spec-file",
                        str(spec_file),
                        "--provider",
                        "copilot-cli",
                    ]
                )

            self.assertEqual(exit_code, 1)
            config = load_project_config(project_root)
            self.assertEqual(config.active_provider, "copilot-cli")

    def test_cli_approve_defaults_to_pending_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            state = load_run_state(project_root)
            state.status = "paused"
            state.current_stage = "clarify"
            state.pending_approval = "requirements"
            save_run_state(project_root, state)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["approve", "--project", str(project_root)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "pending")
            self.assertEqual(payload["pending_approval"], "")
            self.assertIn("requirements", payload["approved_gates"])

    def test_cli_approve_can_infer_gate_from_paused_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            state = load_run_state(project_root)
            state.status = "paused"
            state.current_stage = "design"
            state.pending_approval = ""
            save_run_state(project_root, state)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["approve", "--project", str(project_root)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "pending")
            self.assertIn("architecture", payload["approved_gates"])

    def test_cli_run_passes_print_agent_output_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")
            calls = {}

            class FakeState:
                def to_dict(self):
                    return {"status": "completed", "run_id": "run-123"}

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    calls["project_root"] = str(project_root)
                    calls["has_stream"] = agent_output_stream is not None

                def run(self, **kwargs):
                    calls.update(kwargs)
                    return FakeState()

            buffer = io.StringIO()
            with patch("auto_agents.cli.Orchestrator", FakeOrchestrator):
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        ["run", "--project", str(project_root), "--spec-file", str(spec_file), "--print-agent-output"]
                    )

            rendered = buffer.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("Run completed successfully.", rendered)
            self.assertIn(".auto-agents/state/task_plan.json", rendered)
            self.assertIn(".auto-agents/state/run_state.json", rendered)
            self.assertIn(".auto-agents/runs/run-123/outputs", rendered)
            self.assertTrue(calls["print_agent_output"])
            self.assertTrue(calls["has_stream"])

    def test_cli_run_passes_allow_dirty_tree_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")
            calls = {}

            class FakeState:
                def to_dict(self):
                    return {"status": "completed"}

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    calls["project_root"] = str(project_root)

                def run(self, **kwargs):
                    calls.update(kwargs)
                    return FakeState()

            buffer = io.StringIO()
            with patch("auto_agents.cli.Orchestrator", FakeOrchestrator):
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        ["run", "--project", str(project_root), "--spec-file", str(spec_file), "--allow-dirty-tree"]
                    )

            rendered = buffer.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("Run completed successfully.", rendered)
            self.assertIn("python3 -m auto_agents status --project", rendered)
            self.assertTrue(calls["allow_dirty_tree"])

    def test_cli_run_passes_document_language_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")
            calls = {}

            class FakeState:
                def to_dict(self):
                    return {"status": "completed"}

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    calls["project_root"] = str(project_root)

                def run(self, **kwargs):
                    calls.update(kwargs)
                    return FakeState()

            buffer = io.StringIO()
            with patch("auto_agents.cli.Orchestrator", FakeOrchestrator):
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        ["run", "--project", str(project_root), "--spec-file", str(spec_file), "--doc-language", "zh"]
                    )

            rendered = buffer.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("Run completed successfully.", rendered)
            self.assertIn("README.md", rendered)
            self.assertEqual(calls["doc_language"], "zh")

    def test_cli_run_prints_pending_approval_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")

            class FakeState:
                def to_dict(self):
                    return {
                        "status": "paused",
                        "pending_approval": "requirements",
                        "run_id": "run-456",
                    }

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    pass

                def run(self, **kwargs):
                    return FakeState()

            buffer = io.StringIO()
            with patch("auto_agents.cli.Orchestrator", FakeOrchestrator):
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        ["run", "--project", str(project_root), "--spec-file", str(spec_file), "--doc-language", "zh"]
                    )

            rendered = buffer.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("Run paused: approval required for requirements.", rendered)
            self.assertIn(".auto-agents/docs/project_brief.md", rendered)
            self.assertIn(".auto-agents/state/requirements_trace.json", rendered)
            self.assertIn(".auto-agents/state/run_state.json", rendered)
            self.assertIn(".auto-agents/runs/run-456/outputs", rendered)
            self.assertIn("python3 -m auto_agents approve --project", rendered)
            self.assertIn("--gate requirements", rendered)
            self.assertIn("python3 -m auto_agents reject --project", rendered)

    def test_orchestrator_emits_agent_output_to_stderr_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            class EchoAdapter:
                def run(self, request):
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary="stage output",
                        stderr="stage warning",
                        returncode=0,
                    )

            orchestrator.adapter = EchoAdapter()
            orchestrator._print_agent_output = True
            state = load_run_state(project_root)
            orchestrator._run_agent_with_retries(
                state=state,
                stage="clarify",
                stage_key="clarify",
                prompt="prompt",
            )

            rendered = stream.getvalue()
            self.assertIn("[agent:clarify]", rendered)
            self.assertIn("stage output", rendered)
            self.assertIn("stage warning", rendered)

    def test_orchestrator_emits_agent_metrics_without_print_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            class UsageAdapter:
                def run(self, request):
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary="stage output",
                        model="profile:h",
                        usage=AgentUsage(input_tokens=120, cached_input_tokens=30, output_tokens=10),
                        returncode=0,
                    )

            orchestrator.adapter = UsageAdapter()
            state = load_run_state(project_root)
            orchestrator._run_agent_with_retries(
                state=state,
                stage="clarify",
                stage_key="clarify",
                prompt="prompt",
            )

            rendered = stream.getvalue()
            self.assertIn("[agent:clarify] completed", rendered)
            self.assertIn("model=profile:h", rendered)
            self.assertIn("tokens=input=120 cached_input=30 output=10 total=130", rendered)
            self.assertNotIn("stage output", rendered)

    def test_orchestrator_streams_agent_output_chunks_when_adapter_supports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            class StreamingAdapter:
                def run(self, request):
                    if request.stream_output is None:
                        raise AssertionError("expected a stream callback")
                    request.stream_output("stdout", "line one\n")
                    request.stream_output("stderr", "warn one\n")
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary="line one",
                        stdout="line one\n",
                        stderr="warn one",
                        returncode=0,
                        streamed_stdout=True,
                        streamed_stderr=True,
                    )

            orchestrator.adapter = StreamingAdapter()
            orchestrator._print_agent_output = True
            state = load_run_state(project_root)
            orchestrator._run_agent_with_retries(
                state=state,
                stage="clarify",
                stage_key="clarify",
                prompt="prompt",
            )

            rendered = stream.getvalue()
            self.assertIn("[agent:clarify:stdout] line one", rendered)
            self.assertIn("[agent:clarify:stderr] warn one", rendered)
            self.assertIn("[agent:clarify] returncode=0 ok=true", rendered)

    def test_orchestrator_keeps_stage_summary_when_streamed_stdout_is_only_runtime_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            class StreamingLogsAdapter:
                def run(self, request):
                    if request.stream_output is None:
                        raise AssertionError("expected a stream callback")
                    request.stream_output("stdout", "progress line\n")
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary="# Clarify\n\nfinal requirements\n",
                        stdout="progress line\n",
                        returncode=0,
                        streamed_stdout=True,
                    )

            orchestrator.adapter = StreamingLogsAdapter()
            orchestrator._print_agent_output = True
            state = load_run_state(project_root)
            orchestrator._run_agent_with_retries(
                state=state,
                stage="clarify",
                stage_key="clarify",
                prompt="prompt",
            )

            rendered = stream.getvalue()
            self.assertIn("[agent:clarify:stdout] progress line", rendered)
            self.assertIn("# Clarify", rendered)
            self.assertIn("final requirements", rendered)

    def test_run_emits_top_level_stage_start_log_before_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            class ClarifyOnlyAdapter:
                def run(self, request):
                    write_text(request.output_path, "clarified scope\nREADY_TO_GENERATE\n")
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary="clarified scope\nREADY_TO_GENERATE",
                        returncode=0,
                    )

            orchestrator.adapter = ClarifyOnlyAdapter()
            state = orchestrator.run(spec_file=spec_file)

            self.assertEqual(state.status, "paused")
            rendered = stream.getvalue()
            self.assertIn("[stage:clarify] start provider=mock model=mock", rendered)

    def test_plan_stage_emits_task_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            class PlanAdapter:
                def run(self, request):
                    write_json(
                        task_plan_path(project_root),
                        {
                            "test_strategy": "python-pytest",
                            "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                            "tasks": [
                                {
                                    "task_id": "task-001",
                                    "title": "Task one",
                                    "description": "desc",
                                    "acceptance": ["ok"],
                                    "status": "pending",
                                    "commit_message": "",
                                }
                            ],
                        },
                    )
                    write_text(request.output_path, "valid plan\n")
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary="valid plan",
                        returncode=0,
                    )

            orchestrator.adapter = PlanAdapter()
            state = load_run_state(project_root)
            orchestrator._run_agent_stage("plan", state, spec_file)

            rendered = stream.getvalue()
            self.assertIn("[stage:plan] tasks=1", rendered)

    def test_plan_validation_accepts_no_new_iteration_tasks_with_coverage_justification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Existing capability remains covered.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["done task still covers capability"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already done.",
                            "acceptance": ["done task still covers capability"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ],
                },
            )

            result = AgentResult(
                ok=True,
                command=[],
                output_path=Path("."),
                summary="COVERAGE ANALYSIS: REQ-001 is covered by task-001. UNCOVERED: none.",
                stdout="",
            )

            self.assertIsNone(Orchestrator(project_root)._plan_validation_feedback(result))

    def test_execute_task_emits_implement_and_review_task_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            state = load_run_state(project_root)
            task = TaskSpec(
                task_id="task-001",
                title="Build health endpoint",
                description="desc",
                acceptance=["ok"],
            )

            class PassingAdapter:
                def run(self, request):
                    if request.stage == "review":
                        summary = "DECISION: pass\nlooks good\n"
                    else:
                        summary = "implemented\n"
                    write_text(request.output_path, summary)
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary=summary.strip(),
                        returncode=0,
                    )

            orchestrator.adapter = PassingAdapter()
            orchestrator._execute_task_with_retries(state, task)

            rendered = stream.getvalue()
            self.assertIn("[task:task-001] implement attempt=1 title=Build health endpoint", rendered)
            self.assertIn("[task:task-001] review attempt=1 title=Build health endpoint", rendered)
            self.assertIn("[task:task-001] review decision=pass", rendered)
            self.assertIn("looks good", rendered)

    def test_codex_adapter_parses_usage_from_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            output_path = project_root / "agent.md"
            write_text(output_path, "final summary\n")
            adapter = CodexAdapter(ProviderConfig())
            request = AgentRequest(
                stage="clarify",
                effort="deep",
                prompt="prompt",
                cwd=project_root,
                output_path=output_path,
            )

            with patch("auto_agents.adapters.codex.run_subprocess_with_optional_streaming") as run_mock:
                run_mock.return_value = (
                    (
                        '{"type":"thread.started","thread_id":"t"}\n'
                        '{"type":"item.completed","item":{"type":"agent_message","text":"final summary"}}\n'
                        '{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":50,"output_tokens":25}}\n'
                    ),
                    "",
                    0,
                    False,
                    False,
                )
                result = adapter.run(request)

            self.assertTrue(result.ok)
            self.assertEqual(result.model, "profile:h")
            self.assertIsNotNone(result.usage)
            usage = result.usage
            self.assertEqual(usage.input_tokens if usage else None, 200)
            self.assertEqual(usage.cached_input_tokens if usage else None, 50)
            self.assertEqual(usage.output_tokens if usage else None, 25)
            self.assertEqual(usage.total_tokens if usage else None, 225)

    def test_run_can_persist_document_language_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")
            state = load_run_state(project_root)
            state.status = "completed"
            save_run_state(project_root, state)

            orchestrator = Orchestrator(project_root)
            orchestrator.run(spec_file=spec_file, doc_language="zh")

            config = load_project_config(project_root)
            self.assertEqual(config.docs.language, "zh")

    def test_init_project_defaults_to_four_implement_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")

            config = load_project_config(project_root)
            self.assertEqual(config.retries.per_stage["implement"], 4)
            self.assertEqual(config.retries.per_stage["normalize_project_rules"], 2)

    def test_copilot_cli_adapter_builds_command_with_profile_config_dir(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter, DEFAULT_PROFILES_ROOT

        config = ProviderConfig(
            kind="copilot-cli",
            binary="copilot",
            profile_map={"balanced": "balanced", "deep": "deep", "max": "max"},
        )
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(
            stage="implement",
            effort="deep",
            prompt="do something",
            cwd=Path("/tmp/test"),
            output_path=Path("/tmp/test/out.md"),
        )
        cmd = adapter._build_command(request)
        self.assertEqual(cmd[0], "copilot")
        self.assertIn("--config-dir", cmd)
        config_dir_index = cmd.index("--config-dir")
        resolved = cmd[config_dir_index + 1]
        self.assertEqual(resolved, str(DEFAULT_PROFILES_ROOT / "deep"))
        self.assertIn("--allow-all", cmd)
        self.assertIn("--no-ask-user", cmd)
        self.assertIn("--no-color", cmd)
        self.assertIn("-s", cmd)

    def test_copilot_cli_adapter_forwards_model_from_profile_config(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter

        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "deep-profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            write_text(profile_dir / "config.json", '{"model": "gpt-4.1"}\n')

            config = ProviderConfig(
                kind="copilot-cli",
                binary="copilot",
                profile_map={"deep": str(profile_dir)},
            )
            adapter = CopilotCliAdapter(config)
            request = AgentRequest(
                stage="implement",
                effort="deep",
                prompt="do something",
                cwd=Path("/tmp/test"),
                output_path=Path("/tmp/test/out.md"),
            )

            cmd = adapter._build_command(request)
            self.assertIn("--model", cmd)
            model_index = cmd.index("--model")
            self.assertEqual(cmd[model_index + 1], "gpt-4.1")

    def test_copilot_cli_adapter_skips_allow_all_when_explicit(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter

        config = ProviderConfig(
            kind="copilot-cli",
            binary="copilot",
            profile_map={"balanced": "balanced"},
            extra_args=["--deny-tool", "dangerous-tool"],
        )
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(
            stage="implement",
            effort="balanced",
            prompt="do something",
            cwd=Path("/tmp/test"),
            output_path=Path("/tmp/test/out.md"),
        )
        cmd = adapter._build_command(request)
        self.assertNotIn("--allow-all", cmd)
        self.assertIn("--deny-tool", cmd)

    def test_copilot_cli_adapter_model_label_uses_profile(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter

        config = ProviderConfig(
            kind="copilot-cli",
            binary="copilot",
            profile_map={"deep": "unit-test-profile-without-config"},
        )
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(
            stage="implement",
            effort="deep",
            prompt="do something",
            cwd=Path("/tmp/test"),
            output_path=Path("/tmp/test/out.md"),
        )
        self.assertEqual(adapter._model_label(request), "profile:unit-test-profile-without-config")

    def test_copilot_cli_adapter_model_label_explicit_model(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter

        config = ProviderConfig(
            kind="copilot-cli",
            binary="copilot",
            profile_map={"deep": "deep"},
            extra_args=["--model", "gpt-4o"],
        )
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(
            stage="implement",
            effort="deep",
            prompt="do something",
            cwd=Path("/tmp/test"),
            output_path=Path("/tmp/test/out.md"),
        )
        self.assertEqual(adapter._model_label(request), "gpt-4o")

    def test_copilot_cli_adapter_no_config_dir_for_unmapped_effort(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter

        config = ProviderConfig(
            kind="copilot-cli",
            binary="copilot",
            profile_map={},
        )
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(
            stage="implement",
            effort="balanced",
            prompt="do something",
            cwd=Path("/tmp/test"),
            output_path=Path("/tmp/test/out.md"),
        )
        cmd = adapter._build_command(request)
        self.assertNotIn("--config-dir", cmd)

    def test_orchestrator_routes_copilot_cli_to_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "copilot-cli")

            from auto_agents.adapters.copilot_cli import CopilotCliAdapter

            orchestrator = Orchestrator(project_root)
            self.assertIsInstance(orchestrator.adapter, CopilotCliAdapter)

    def test_orchestrator_model_label_for_copilot_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "copilot-cli")

            orchestrator = Orchestrator(project_root)
            label = orchestrator._model_label_for_agent_stage("implement", "deep")
            self.assertEqual(label, "profile:deep")

    def test_save_run_state_persists_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock", doc_language="zh")
            state = load_run_state(project_root)
            state.stage_summaries["clarify"] = "聚焦核心目标"
            state.stage_summaries["design"] = "架构边界清晰"
            save_run_state(project_root, state)

            payload = (project_root / ".auto-agents" / "state" / "run_state.json").read_text(encoding="utf-8")
            self.assertIn("聚焦核心目标", payload)
            self.assertIn("架构边界清晰", payload)
            self.assertNotIn("\\u805a\\u7126", payload)

    def test_run_commits_completed_run_state_for_legacy_auto_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            state = load_run_state(project_root)
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
                "implement": "done",
                "verify": "done",
            }
            state.tasks = [
                TaskSpec(
                    task_id="task-001",
                    title="Done task",
                    description="desc",
                    acceptance=["ok"],
                    status="done",
                )
            ]
            save_run_state(project_root, state)
            write_text(auto_dir(project_root) / ".gitignore", "runs/\nstate/run_state.json\n")

            orchestrator = Orchestrator(project_root)
            result = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(result.status, "completed")
            self.assertTrue(working_tree_clean(project_root))

            run_state_show = subprocess.run(
                ["git", "show", "HEAD:.auto-agents/state/run_state.json"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            committed_state = json.loads(run_state_show.stdout)
            self.assertEqual(committed_state["status"], "completed")

            gitignore_show = subprocess.run(
                ["git", "show", "HEAD:.auto-agents/.gitignore"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                gitignore_show.stdout,
                "runs/\nstate/gate_baseline_cache.json\nstate/repomap_cache.json\n",
            )

    def test_clarify_prompt_uses_selected_document_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock", doc_language="zh")
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            prompt = orchestrator._build_prompt("clarify", spec_file)

            self.assertIn("Simplified Chinese", prompt)

    def test_readme_prompt_uses_selected_document_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock", doc_language="zh")
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            prompt = orchestrator._build_prompt("readme", spec_file)

            self.assertIn("Simplified Chinese", prompt)
            self.assertIn(str(project_root / "README.md"), prompt)

    def test_spec_analysis_classifies_idea_like_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "spec.md"
            write_text(
                spec_file,
                "# Product Idea\n\n## Problem\nSmall teams need a lightweight release checklist.\n\n"
                "## MVP Scope\n- Create tasks\n- Mark tasks done\n\n## Non-Goals\n- No integrations yet.\n",
            )

            analysis = orchestrator._analyze_spec(spec_file)
            prompt = orchestrator._build_prompt("clarify", spec_file)

            self.assertEqual(analysis["kind"], "idea")
            self.assertIn("Detected spec profile: idea", prompt)
            self.assertIn("Treat the spec as early product intent", prompt)

    def test_spec_analysis_classifies_design_like_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "design.md"
            write_text(
                spec_file,
                "# Architecture\n\n## System Boundary\nBrowser client and API service.\n\n"
                "## Core Modules\n- API\n- Storage\n\n## Data Flow\nRequests enter the API and persist to SQLite.\n\n"
                "## Interfaces\nREST API endpoints for tasks.\n",
            )

            analysis = orchestrator._analyze_spec(spec_file)
            prompt = orchestrator._build_prompt("design", spec_file)

            self.assertEqual(analysis["kind"], "design")
            self.assertIn("Detected spec profile: design", prompt)
            self.assertIn("Treat the input spec as the primary architecture source", prompt)

    def test_spec_analysis_classifies_mixed_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "spec.md"
            write_text(
                spec_file,
                "# Task App\n\n## Problem\nTrack work without spreadsheets.\n\n## MVP Scope\n- Create tasks\n\n"
                "## Core Modules\n- Web UI\n- API\n\n## Data Flow\nThe UI sends task updates to the API.\n",
            )

            analysis = orchestrator._analyze_spec(spec_file)
            prompt = orchestrator._build_prompt("plan", spec_file)

            self.assertEqual(analysis["kind"], "mixed")
            self.assertIn("Detected spec profile: mixed", prompt)
            self.assertIn("Prefer the explicit design decisions in the input spec", prompt)
            self.assertIn("Choose the number of tasks based on project complexity", prompt)
            self.assertIn("do not split into trivial housekeeping-only tasks", prompt)
            self.assertIn("Avoid oversized tasks", prompt)
            self.assertIn("verification_steps entries with kind='test' and runner='pytest'", prompt)
            self.assertIn("Do not generate free-form shell verification commands", prompt)

    def test_mock_readme_stage_updates_project_readme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            state = load_run_state(project_root)
            state = orchestrator._run_readme(state, spec_file)

            readme = (project_root / "README.md").read_text(encoding="utf-8")
            self.assertEqual(state.current_stage, "readme")
            self.assertIn("## Overview", readme)
            self.assertIn("## Usage", readme)


if __name__ == "__main__":
    unittest.main()
