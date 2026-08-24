import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.cli import (
    SELF_REPAIR_STRICT_ENV,
    _preflight_automatic_self_repair,
    build_parser,
    main,
)
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
    run_path,
    save_project_config,
    save_run_state,
    save_task_plan,
    task_plan_path,
)
from auto_agents.git_ops import working_tree_clean
from auto_agents.gates import GateCommandTimeoutError
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import (
    AgentRequest,
    AgentResult,
    AgentUsage,
    CommandResult,
    GateResult,
    ProjectConfig,
    ProviderConfig,
    RunState,
    SessionState,
    TaskSpec,
    VerificationStep,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.run_lock import (
    RUN_LOCK_FD_ENV,
    RUN_LOCK_KEY_ENV,
    RUN_LOCK_TOKEN_ENV,
    ProjectRunLock,
    RunAlreadyActiveError,
    runtime_status,
    stop_project_run,
)
from auto_agents.self_repair import (
    SELF_REPAIR_DISABLED_ENV,
    SELF_REPAIR_LAST_FINGERPRINT_ENV,
    SELF_REPAIR_PROVIDER_CONFIDENCE_THRESHOLD,
    SELF_REPAIR_REPEAT_COUNT_ENV,
    AutoAgentsSelfRepairRunner,
    SelfRepairDecision,
    SelfRepairJudgment,
    SelfRepairResult,
    SelfRepairTriageResult,
    adjudicate_auto_agents_error,
    classify_auto_agents_error,
    parse_self_repair_judgment,
    self_repair_runtime_evidence,
)
from auto_agents.validation import (
    validate_required_document,
    validate_project_config_payload,
    validate_task_dependencies,
    validate_task_plan_payload,
    validation_report,
)


class ProjectRunLockTests(unittest.TestCase):
    def test_rejects_a_second_external_run_for_same_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            project_root.mkdir()
            with ProjectRunLock(project_root):
                with self.assertRaises(RunAlreadyActiveError) as ctx:
                    ProjectRunLock(project_root, environ={}).acquire()

            self.assertIn("another auto_agents run is already active", str(ctx.exception))
            self.assertIn(str(project_root.resolve()), str(ctx.exception))

    def test_accepts_explicit_inherited_self_repair_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            project_root.mkdir()
            with ProjectRunLock(project_root) as parent_lock:
                inherited_fd = os.dup(parent_lock.fileno)
                inherited = ProjectRunLock(
                    project_root,
                    environ={
                        RUN_LOCK_FD_ENV: str(inherited_fd),
                        RUN_LOCK_KEY_ENV: parent_lock.key,
                        RUN_LOCK_TOKEN_ENV: parent_lock.run_token,
                    },
                )
                try:
                    inherited.acquire()
                    self.assertEqual(inherited.fileno, inherited_fd)
                finally:
                    inherited.release()

    def test_acquire_reports_stale_control_as_interrupted_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            project_root.mkdir()
            first = ProjectRunLock(project_root)
            first.acquire()
            owner_payload = first.owner_payload()
            control_payload = json.loads(
                first.control_path.read_text(encoding="utf-8")
            )
            control_path = first.control_path
            first.release()
            write_json(control_path, control_payload)

            second = ProjectRunLock(project_root, environ={})
            try:
                second.acquire()
                snapshot = second.interrupted_snapshot
            finally:
                second.release()

            self.assertEqual(snapshot["owner"]["pid"], owner_payload["pid"])
            self.assertEqual(
                snapshot["control"]["project"], str(project_root.resolve())
            )

    def test_cli_rejects_duplicate_before_constructing_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            project_root.mkdir()
            stdout = io.StringIO()
            with (
                ProjectRunLock(project_root),
                patch(
                    "auto_agents.cli.Orchestrator",
                    side_effect=AssertionError("duplicate run must stop before orchestration"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(["run", "--project", str(project_root)])

            self.assertEqual(exit_code, 2)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("already active", payload["error"])

    def test_runtime_status_and_stop_terminate_external_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            project_root.mkdir()
            script = Path(tmp) / "owner.py"
            script.write_text(
                "import os, subprocess, sys, time\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
                "from auto_agents.process_supervision import ACTIVE_PROCESSES\n"
                "from auto_agents.run_lock import ProjectRunLock\n"
                "root = __import__('pathlib').Path(sys.argv[1])\n"
                "with ProjectRunLock(root):\n"
                "    child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
                "    ACTIVE_PROCESSES.register(child, kind='test-child')\n"
                "    print('ready', flush=True)\n"
                "    time.sleep(60)\n",
                encoding="utf-8",
            )
            owner = subprocess.Popen(
                [sys.executable, str(script), str(project_root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(owner.stdout.readline().strip(), "ready")
                status = runtime_status(project_root)
                self.assertTrue(status["active"])
                self.assertEqual(status["owner_pid"], owner.pid)
                self.assertEqual(status["active_process_groups"], 1)

                payload, exit_code = stop_project_run(project_root, grace_seconds=1)
                owner.wait(timeout=5)

                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["status"], "stopped")
                self.assertFalse(runtime_status(project_root)["active"])
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)

    def test_stop_escalates_to_sigkill_for_term_ignoring_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            project_root.mkdir()
            script = Path(tmp) / "stubborn_owner.py"
            script.write_text(
                "import signal, subprocess, sys, time\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
                "from auto_agents.process_supervision import ACTIVE_PROCESSES\n"
                "from auto_agents.run_lock import ProjectRunLock\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "root = __import__('pathlib').Path(sys.argv[1])\n"
                "with ProjectRunLock(root):\n"
                "    child = subprocess.Popen([sys.executable, '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'], "
                "start_new_session=True)\n"
                "    ACTIVE_PROCESSES.register(child, kind='stubborn-child')\n"
                "    print('ready', flush=True)\n"
                "    time.sleep(60)\n",
                encoding="utf-8",
            )
            owner = subprocess.Popen(
                [sys.executable, str(script), str(project_root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(owner.stdout.readline().strip(), "ready")

                payload, exit_code = stop_project_run(
                    project_root,
                    grace_seconds=0.1,
                    kill_grace_seconds=2,
                )
                owner.wait(timeout=5)

                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["status"], "stopped")
                self.assertTrue(payload["forced"])
                self.assertFalse(runtime_status(project_root)["active"])
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)


class ProjectValidationTests(unittest.TestCase):
    def test_cli_recover_command_is_removed(self) -> None:
        with (
            self.assertRaises(SystemExit) as raised,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            main(["recover", "--project", "/tmp/demo"])

        self.assertEqual(raised.exception.code, 2)

    def test_cli_run_uses_saved_context_for_blocked_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            saved_spec = project_root / "specs" / "iteration.md"
            saved_spec.parent.mkdir()
            write_text(saved_spec, "# Spec\n")
            state = load_run_state(project_root)
            state.status = "blocked"
            state.active_blocker = {
                "owner": "external_provider",
                "category": "provider_unavailable",
                "status": "blocked",
            }
            state.resume_context = {
                "spec_file": str(saved_spec),
                "auto_approve": True,
                "print_agent_output": True,
                "provider_kind": "mock",
            }
            save_run_state(project_root, state)
            calls = {}

            class FakeState:
                def to_dict(self):
                    return {"status": "completed", "current_stage": "readme"}

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    pass

                def run(self, **kwargs):
                    calls.update(kwargs)
                    return FakeState()

            with (
                patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = main(["run", "--project", str(project_root)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(calls["spec_file"], saved_spec)
            self.assertTrue(calls["auto_approve"])
            self.assertTrue(calls["print_agent_output"])
            self.assertEqual(calls["provider_kind"], "mock")

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
                "commit_message_template": "feat: missing placeholders",
            },
            "approvals": {
                "enabled": ["requirements", "bad"],
            },
            "retries": {
                "default_max_attempts": 0,
                "per_stage": {
                    "plan": 2,
                    "sync-agent-instructions": 2,
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

    def test_validate_project_config_rejects_invalid_infrastructure_and_failover_settings(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["gates"]["reported_infrastructure_markers"] = [
            {"id": "Bad ID", "contains": ""},
            {"id": "browser_failed", "contains": "one"},
            {"id": "browser_failed", "contains": "two"},
        ]
        payload["gates"]["distributed"][
            "reported_infrastructure_max_workers"
        ] = 0
        payload["execution"]["provider_failover"] = {
            "probe_enabled": "yes",
            "probe_timeout_seconds": 0,
            "connection_cooldown_seconds": 60,
            "pressure_cooldown_seconds": 300,
            "timeout_cooldown_seconds": 1800,
            "quota_cooldown_seconds": 3600,
            "max_cooldown_seconds": 300,
        }
        payload["execution"]["self_repair_diagnosis"] = {
            "mode": "sometimes",
            "investigator_timeout_seconds": 0,
            "reviewer_timeout_seconds": 600,
            "arbiter_timeout_seconds": 600,
            "command_timeout_seconds": 300,
            "max_dynamic_commands": 0,
            "confidence_threshold": 1.5,
            "arbiter_confidence_threshold": 0.9,
            "max_repair_cycles": 0,
            "network_enabled": "yes",
        }

        errors = validate_project_config_payload(payload)

        self.assertTrue(any("reported_infrastructure_markers[0].id" in item for item in errors))
        self.assertTrue(any("reported_infrastructure_markers[0].contains" in item for item in errors))
        self.assertTrue(any("reported_infrastructure_markers[2].id must be unique" in item for item in errors))
        self.assertTrue(any("reported_infrastructure_max_workers" in item for item in errors))
        self.assertTrue(any("provider_failover.probe_enabled" in item for item in errors))
        self.assertTrue(any("provider_failover.probe_timeout_seconds" in item for item in errors))
        self.assertTrue(any("max_cooldown_seconds must be" in item for item in errors))
        self.assertTrue(any("self_repair_diagnosis.mode" in item for item in errors))
        self.assertTrue(
            any(
                "self_repair_diagnosis.investigator_timeout_seconds" in item
                for item in errors
            )
        )
        self.assertTrue(
            any(
                "self_repair_diagnosis.confidence_threshold" in item
                for item in errors
            )
        )
        self.assertTrue(
            any(
                "self_repair_diagnosis.network_enabled" in item
                for item in errors
            )
        )

    def test_task_plan_validation_rejects_invalid_recovery_lineage(self) -> None:
        task = {
            "task_id": "task-271b",
            "title": "Split child",
            "description": "Implement the split slice.",
            "acceptance": ["Observable proof passes."],
            "status": "pending",
            "commit_message": "",
            "task_origin": "generated-by-id-guess",
            "recovery_epoch": -1,
            "recovery_round": True,
            "verify_retry_epoch": -1,
        }

        errors = validate_task_plan_payload({"tasks": [task]})

        self.assertTrue(any("task_origin must be one of" in item for item in errors))
        self.assertTrue(any("recovery_epoch must be an integer >= 0" in item for item in errors))
        self.assertTrue(any("recovery_round must be an integer >= 0" in item for item in errors))
        self.assertTrue(any("verify_retry_epoch must be an integer >= 0" in item for item in errors))

    def test_validate_project_config_payload_rejects_non_isolated_python_commands(self) -> None:
        payload = {
            "project_name": "demo",
            "providers": {
                "codex": {
                    "kind": "codex",
                    "binary": "codex",
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
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
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
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
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
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
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
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
                "workers": "auto",
                "max_auto_workers": 3,
                "adaptive": True,
                "strict": False,
                "worktree_root": "",
            }
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_rejects_unquoted_pytest_marker_expression(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["gates"]["commands"] = [
            "conda run -p ./.conda python -m pytest -q "
            "-m not storage_real_smoke and not real_provider_smoke tests"
        ]

        errors = validate_project_config_payload(payload)

        self.assertTrue(
            any(
                "pytest -m expression must be one shell argument" in error
                for error in errors
            )
        )

    def test_validate_project_config_payload_rejects_invalid_parallel_workers(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["execution"] = {
            "parallel_tasks": {
                "enabled": True,
                "workers": "many",
                "max_auto_workers": 2,
                "adaptive": True,
                "strict": True,
                "worktree_root": "",
            }
        }

        errors = validate_project_config_payload(payload)
        self.assertTrue(any("execution.parallel_tasks.workers" in item for item in errors))

    def test_gate_command_timeout_defaults_to_7200_seconds(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload["gates"].pop("command_timeout_seconds", None)
        payload["gates"].pop("worker_slot_wait_timeout_seconds", None)

        config = ProjectConfig.from_dict(payload)

        self.assertEqual(config.gates.command_timeout_seconds, 7200)
        self.assertEqual(config.gates.worker_slot_wait_timeout_seconds, 7200)
        self.assertEqual(config.gates.command_idle_timeout_seconds, 900)
        self.assertTrue(config.gates.adaptive_timeout_enabled)

    def test_validate_project_config_payload_rejects_invalid_gate_timeout(self) -> None:
        for field in (
            "command_timeout_seconds",
            "worker_slot_wait_timeout_seconds",
        ):
            for invalid in (0, -1, True, "1800"):
                with self.subTest(field=field, value=invalid):
                    payload = copy.deepcopy(DEFAULT_CONFIG)
                    payload["gates"][field] = invalid
                    errors = validate_project_config_payload(payload)
                    self.assertTrue(
                        any(f"gates.{field}" in item for item in errors)
                    )

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
                    "profile_map": {"balanced": "balanced", "deep": "deep", "max": "max"},
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
        del payload["efforts"]["self_repair"]

        self.assertEqual(validate_project_config_payload(payload), [])

        config = ProjectConfig.from_dict(payload)
        self.assertEqual(config.efforts["provider_research"], "deep")
        self.assertEqual(config.efforts["sync-agent-instructions"], "deep")
        self.assertEqual(config.efforts["self_repair"], "max")

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

    def test_validation_report_accepts_pytest_nodeid_in_verification_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(
                project_root / "tests" / "test_contract.py",
                (
                    "import unittest\n\n"
                    "class ContractTests(unittest.TestCase):\n"
                    "    def test_public_contract(self):\n"
                    "        pass\n"
                ),
            )
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [
                        {
                            "kind": "test",
                            "runner": "pytest",
                            "targets": [
                                "tests/test_contract.py::ContractTests::test_public_contract"
                            ],
                        }
                    ],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Preserve the public contract",
                            "description": "Keep the existing contract covered.",
                            "acceptance": ["The focused contract check passes."],
                            "status": "pending",
                            "commit_message": "test: preserve public contract",
                        }
                    ],
                },
            )

            report = validation_report(project_root)

            self.assertTrue(report["ok"], msg=str(report["errors"]))
            self.assertFalse(
                any("missing pytest target" in item for item in report["errors"])
            )

    def test_validation_report_rejects_missing_file_behind_pytest_nodeid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            target = "tests/test_missing.py::ContractTests::test_public_contract"
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [
                        {
                            "kind": "test",
                            "runner": "pytest",
                            "targets": [target],
                        }
                    ],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Preserve the public contract",
                            "description": "Keep the existing contract covered.",
                            "acceptance": ["The focused contract check passes."],
                            "status": "pending",
                            "commit_message": "test: preserve public contract",
                        }
                    ],
                },
            )

            report = validation_report(project_root)

            self.assertFalse(report["ok"])
            self.assertTrue(
                any(
                    "missing pytest target" in item and target in item
                    for item in report["errors"]
                )
            )

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
            self.assertTrue(any("contains 30 active tasks" in item for item in report["warnings"]))

    def test_validation_report_allows_empty_plan_before_archived_iteration_plan_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(task_plan_path(project_root), {"tasks": []})
            state = load_run_state(project_root)
            state.status = "paused"
            state.current_stage = "clarify"
            state.resume_context = {
                "previous_run_id": "oldrun123",
                "previous_task_plan_archive": str(project_root / ".auto-agents" / "history" / "task_plans" / "oldrun123.json"),
            }
            save_run_state(project_root, state)

            report = validation_report(project_root)

            self.assertTrue(report["ok"])
            self.assertFalse(any("at least one task" in item for item in report["errors"]))

    def test_validation_report_allows_empty_plan_during_restarted_blocked_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(task_plan_path(project_root), {"tasks": []})
            state = load_run_state(project_root)
            state.status = "paused"
            state.current_stage = "prototype"
            state.resume_context = {
                "restarted_blocked_run_id": "blockedrun123",
            }
            save_run_state(project_root, state)

            report = validation_report(project_root)

            self.assertTrue(report["ok"])
            self.assertFalse(any("at least one task" in item for item in report["errors"]))

    def test_run_resumes_restarted_blocked_prototype_with_empty_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(task_plan_path(project_root), {"tasks": []})
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")
            state = load_run_state(project_root)
            state.status = "blocked"
            state.current_stage = "prototype"
            state.pending_approval = "prototype"
            state.stage_summaries["prototype"] = "Generated candidate."
            state.resume_context = {
                "restarted_blocked_run_id": "blockedrun123",
            }
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "orchestrator_transition",
                "status": "blocked",
            }
            save_run_state(project_root, state)

            orchestrator = Orchestrator(project_root)
            orchestrator.mark_self_repair_applied("repairabc123")

            class FailingIfCalledAdapter:
                def run(self, request):
                    raise AssertionError(
                        f"adapter should not run while prototype approval is pending: {request.stage}"
                    )

            orchestrator.adapter = FailingIfCalledAdapter()

            resumed = orchestrator.run(spec_file=spec_file)

            self.assertEqual(resumed.status, "paused")
            self.assertEqual(resumed.pending_approval, "prototype")
            self.assertEqual(resumed.last_error, "")
            self.assertEqual(
                resumed.active_blocker["prepared_self_repair_commit"],
                "repairabc123",
            )

    def test_validation_report_rejects_empty_plan_after_plan_stage(self) -> None:
        for lineage_key in ("previous_run_id", "restarted_blocked_run_id"):
            with self.subTest(lineage_key=lineage_key), tempfile.TemporaryDirectory() as tmp:
                project_root = Path(tmp) / "demo"
                Orchestrator.init_project(project_root, "demo", "mock")
                write_json(task_plan_path(project_root), {"tasks": []})
                state = load_run_state(project_root)
                state.status = "pending"
                state.current_stage = "implement"
                state.stage_summaries["plan"] = "planned"
                state.resume_context = {lineage_key: "oldrun123"}
                save_run_state(project_root, state)

                report = validation_report(project_root)

                self.assertFalse(report["ok"])
                self.assertTrue(
                    any("at least one task" in item for item in report["errors"])
                )

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
            self.assertEqual(exit_code, 3)
            self.assertFalse(payload["ok"])
            self.assertIn("Expecting property name enclosed in double quotes", payload["error"])

    def test_dirty_auto_agents_checkout_warns_before_run_but_allows_normal_work(self) -> None:
        args = build_parser().parse_args(["run", "--project", "/tmp/demo"])
        stderr = io.StringIO()

        with (
            patch("auto_agents.cli.changed_paths", return_value=["README.md", "src/change.py"]),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = _preflight_automatic_self_repair(args, env={})

        self.assertIsNone(exit_code)
        self.assertIn("Normal run can continue", stderr.getvalue())
        self.assertIn("README.md", stderr.getvalue())
        self.assertIn("automatic auto_agents self-repair is unavailable", stderr.getvalue())

    def test_strict_self_repair_rejects_dirty_checkout_before_run(self) -> None:
        stdout = io.StringIO()

        with (
            patch.dict(os.environ, {SELF_REPAIR_DISABLED_ENV: ""}, clear=False),
            patch("auto_agents.cli.changed_paths", return_value=["src/change.py"]),
            patch(
                "auto_agents.cli.Orchestrator",
                side_effect=AssertionError("strict preflight must run before orchestration"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["run", "--project", "/tmp/demo", "--strict-self-repair"]
            )

        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("self-repair preflight failed", payload["error"])
        self.assertIn("src/change.py", payload["error"])

    def test_self_repair_preflight_respects_disable_env_and_session_scope(self) -> None:
        parser = build_parser()
        run_args = parser.parse_args(["run", "--project", "/tmp/demo"])
        with patch(
            "auto_agents.cli.changed_paths",
            side_effect=AssertionError("disabled self-repair must not inspect its checkout"),
        ):
            self.assertIsNone(
                _preflight_automatic_self_repair(
                    run_args,
                    env={SELF_REPAIR_DISABLED_ENV: "true"},
                )
            )

        for command in ("fix", "collab"):
            with self.subTest(command=command):
                args = parser.parse_args([command, "--project", "/tmp/demo"])
                with patch(
                    "auto_agents.cli.changed_paths",
                    side_effect=AssertionError(
                        "commands without self-repair must not inspect its checkout"
                    ),
                ):
                    self.assertIsNone(
                        _preflight_automatic_self_repair(
                            args,
                            env={SELF_REPAIR_STRICT_ENV: "true"},
                        )
                    )

    def test_self_repair_classifier_accepts_gate_scope_mismatch_only(self) -> None:
        scope_error = (
            "Task task-224 failed gates: verification scope mismatch: new failures are outside "
            "this task's owned test/proof surface. Owned scope: cmd:npm test. "
            "New failure paths: tests/test_requirements_audit_state.py. Treat this as a "
            "product-contract or gate-scope issue instead of retrying implementation."
        )
        decision = classify_auto_agents_error(scope_error, env={})
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.category, "verification_scope_mismatch")

        audit_no_progress = classify_auto_agents_error(
            "Task task-239 failed gates: unchanged verify failure set repeated from "
            "attempt-1 (repeat=2); stopping retries early\n"
            "requirements audit still fails for this task's bound requirement(s) REQ-134",
            env={},
        )
        self.assertTrue(audit_no_progress.eligible)
        self.assertEqual(
            audit_no_progress.category,
            "requirements_audit_no_progress_route",
        )

        audit_error = (
            "requirements audit failed: /tmp/demo/.auto-agents/docs/requirements_audit.md\n"
            "Automatic recovery is unsafe for at least one blocker:\n"
            "- REQ-116: forbidden pattern 'old contract' found in specs/2026-06-29-iter-01.md; "
            "automatic recovery is unsafe because specs/2026-06-29-iter-01.md is an immutable "
            "input specification"
        )
        audit_decision = classify_auto_agents_error(audit_error, env={})
        self.assertTrue(audit_decision.eligible)
        self.assertEqual(audit_decision.category, "requirements_audit_immutable_input_scope")

        diagnostic_audit_decision = classify_auto_agents_error(
            "requirements audit failed: /tmp/demo/.auto-agents/docs/requirements_audit.md\n"
            "Automatic recovery is unsafe for at least one blocker:\n"
            "- REQ-001: forbidden pattern 'old wording' found in "
            ".auto-agents/docs/requirements_audit.md; automatic recovery is unsafe because "
            ".auto-agents/docs/requirements_audit.md is an orchestrator diagnostic report, "
            "not implementation-owned source-of-truth",
            env={},
        )
        self.assertTrue(diagnostic_audit_decision.eligible)
        self.assertEqual(
            diagnostic_audit_decision.category,
            "requirements_audit_diagnostic_scope",
        )

        ownership_decision = classify_auto_agents_error(
            "stage clarify modified files outside its ownership during clarify-conv-4. "
            "Changed paths: .auto-agents/docs/project_brief.md, "
            ".auto-agents/state/requirements_trace.json. "
            "Allowed scope: .auto-agents/runs/e4c4bc2fa90d/**; "
            ".auto-agents/state/run_state.json; .auto-agents/.gitignore.",
            env={},
        )
        self.assertTrue(ownership_decision.eligible)
        self.assertEqual(ownership_decision.category, "clarify_conversation_mutation_scope")

        readme_proposal_decision = classify_auto_agents_error(
            "stage readme modified files outside its ownership during readme-propose. "
            "Changed paths: README.md. Allowed scope: .auto-agents/runs/demo/**; "
            ".auto-agents/state/run_state.json; .auto-agents/.gitignore.",
            env={},
        )
        self.assertTrue(readme_proposal_decision.eligible)
        self.assertEqual(readme_proposal_decision.category, "readme_proposal_mutation_scope")

        verification_steps_decision = classify_auto_agents_error(
            "generated verification steps are invalid:\n"
            "- unsupported verification step runner: custom-shell",
            env={},
        )
        self.assertTrue(verification_steps_decision.eligible)
        self.assertEqual(verification_steps_decision.category, "generated_verification_contract")

        verification_commands_decision = classify_auto_agents_error(
            "generated verification commands are invalid:\n"
            "- task plan verification_commands references a path outside the project",
            env={},
        )
        self.assertTrue(verification_commands_decision.eligible)
        self.assertEqual(verification_commands_decision.category, "generated_verification_contract")

        provider_reference_validation_decision = classify_auto_agents_error(
            "provider_research exhausted retries: Provider research output is incomplete. "
            "Update the local provider references and lock file.\n"
            "- REQ-131: no lock entry for .auto-agents/docs/provider_references/a.md; "
            ".auto-agents/docs/provider_references/b.md\n"
            "- REQ-131: missing provider reference file .auto-agents/docs/provider_references/a.md; "
            ".auto-agents/docs/provider_references/b.md",
            env={},
        )
        self.assertTrue(provider_reference_validation_decision.eligible)
        self.assertEqual(
            provider_reference_validation_decision.category,
            "provider_research_reference_validation",
        )

        recovery_loop_decision = classify_auto_agents_error(
            "recovery loop orchestration no-op: provider_research was rejected, "
            "but all provider references are still considered verified and no refresh "
            "target could be identified",
            env={},
        )
        self.assertTrue(recovery_loop_decision.eligible)
        self.assertEqual(
            recovery_loop_decision.category,
            "recovery_loop_orchestration_noop",
        )

        target_no_progress = classify_auto_agents_error(
            "recovery no progress: same failure; target_stage=clarify; engine_invariant=none",
            env={},
        )
        self.assertFalse(target_no_progress.eligible)
        self.assertEqual(target_no_progress.category, "recovery_no_progress")

        routed_no_progress = classify_auto_agents_error(
            "recovery no progress: same failure; target_stage=clarify; "
            "engine_invariant=route_owner_mismatch",
            env={},
        )
        self.assertTrue(routed_no_progress.eligible)
        self.assertEqual(routed_no_progress.category, "recovery_route_invariant")

        implement_config_decision = classify_auto_agents_error(
            "stage implement modified files outside its ownership during implement-task-225. "
            "Changed paths: .auto-agents/config.json. "
            "Allowed scope: .auto-agents/runs/52d7c48bd1f2/**; "
            ".auto-agents/state/run_state.json; .auto-agents/.gitignore; "
            "any non-.auto-agents project path except input specs.",
            env={},
        )
        self.assertFalse(implement_config_decision.eligible)

        restore_invariant = classify_auto_agents_error(
            "auto_agents implementation ownership restore invariant failed. "
            "The protected path state did not return to its pre-attempt "
            "worktree and Git index snapshot: spec.md",
            env={},
        )
        self.assertTrue(restore_invariant.eligible)
        self.assertEqual(
            restore_invariant.category,
            "implementation_ownership_restore_invariant",
        )

        review_decision = classify_auto_agents_error(
            "Task task-001 failed gates: review rejected the task",
            env={},
        )
        self.assertFalse(review_decision.eligible)

    def test_self_repair_requires_structured_recovery_route_invariant(self) -> None:
        state = RunState(
            run_id="run-123",
            status="failed",
            last_recovery_route={
                "task_id": "task-271b",
                "lineage_id": "task-271b",
                "outcome": "invariant_violation",
                "engine_invariant": "review_recovery_route_missing",
            },
        )

        invariant = classify_auto_agents_error(
            "Task task-271b failed gates: review rejected the task",
            state=state,
            env={},
        )
        self.assertTrue(invariant.eligible)
        self.assertEqual(invariant.category, "recovery_route_invariant")

        state.last_recovery_route = {
            "task_id": "task-271b",
            "lineage_id": "task-271b",
            "outcome": "judge_stopped",
            "engine_invariant": "",
        }
        product_failure = classify_auto_agents_error(
            "Task task-271b failed gates: review rejected the task",
            state=state,
            env={},
        )
        self.assertFalse(product_failure.eligible)

    def test_persisted_auto_agents_blocker_is_eligible_when_provider_triage_falls_back(self) -> None:
        state = RunState(
            run_id="run-123",
            status="blocked",
            last_error="generic engine failure",
            active_blocker={
                "owner": "auto_agents",
                "category": "generic_engine_failure",
                "status": "blocked",
            },
        )

        decision = classify_auto_agents_error(
            state.last_error,
            state=state,
            env={},
        )

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.category, "generic_engine_failure")

    def test_provider_self_repair_judgment_requires_high_confidence_composite_gate(self) -> None:
        payload = {
            "decision": "SELF_REPAIR",
            "owner": "auto_agents",
            "generic": True,
            "safe_to_self_repair": True,
            "confidence": SELF_REPAIR_PROVIDER_CONFIDENCE_THRESHOLD,
            "category": "generated_artifact_lifecycle",
            "reason": "verification was invalidated by an orchestrator-owned rewrite",
            "evidence": ["verification passed before the generated artifact was rewritten"],
        }
        judgment = parse_self_repair_judgment(json.dumps(payload))
        self.assertTrue(judgment.approved)

        infrastructure = dict(payload)
        infrastructure.update(
            {
                "decision": "DO_NOT_REPAIR",
                "owner": "verification_infrastructure",
                "generic": False,
                "safe_to_self_repair": False,
                "category": "worker_capacity_unavailable",
            }
        )
        self.assertFalse(
            parse_self_repair_judgment(json.dumps(infrastructure)).approved
        )

        for field, value in (
            ("decision", "DO_NOT_REPAIR"),
            ("owner", "target_project"),
            ("generic", False),
            ("safe_to_self_repair", False),
            ("confidence", SELF_REPAIR_PROVIDER_CONFIDENCE_THRESHOLD - 0.01),
            ("evidence", []),
        ):
            with self.subTest(field=field):
                candidate = dict(payload)
                candidate[field] = value
                self.assertFalse(
                    parse_self_repair_judgment(json.dumps(candidate)).approved
                )

    def test_provider_self_repair_judgment_rejects_invalid_structured_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_self_repair_judgment("DECISION: SELF_REPAIR")
        with self.assertRaisesRegex(ValueError, "confidence"):
            parse_self_repair_judgment(
                json.dumps(
                    {
                        "decision": "SELF_REPAIR",
                        "owner": "auto_agents",
                        "generic": True,
                        "safe_to_self_repair": True,
                        "confidence": "high",
                        "category": "generic_orchestrator_bug",
                        "reason": "reason",
                        "evidence": ["evidence"],
                    }
                )
            )
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            parse_self_repair_judgment(
                json.dumps(
                    {
                        "decision": "DO_NOT_REPAIR",
                        "owner": "target_project",
                        "generic": False,
                        "safe_to_self_repair": False,
                        "confidence": 0.99,
                        "category": "target_contract_failure",
                        "reason": "reason",
                        "evidence": ["evidence"],
                        "extra": "not allowed",
                    }
                )
            )

    def test_provider_judgment_can_approve_review_failure_rejected_by_heuristic(self) -> None:
        review_error = "Task repair-task failed gates: review rejected the task"
        payload = {
            "decision": "SELF_REPAIR",
            "owner": "auto_agents",
            "generic": True,
            "safe_to_self_repair": True,
            "confidence": 0.96,
            "category": "generated_artifact_lifecycle",
            "reason": "orchestrator rewrote verified evidence before review",
            "evidence": ["four verify passes were followed by the same review rejection"],
        }

        class FakeConfig:
            efforts = {"self_repair": "max"}

        class FakeOrchestrator:
            config = FakeConfig()

            def _call_with_failover(self, request):
                self.request = request
                role = request.stage.removeprefix("self_repair_")
                structured = {
                    "schema_version": 1,
                    "role": role,
                    "verdict": (
                        "ROOT_CAUSE"
                        if role == "investigator"
                        else "AGREE"
                    ),
                    "owner": "auto_agents",
                    "confidence": 0.96,
                    "category": payload["category"],
                    "generic": True,
                    "safe_to_repair": True,
                    "causal_chain": [
                        "orchestrator rewrote verified evidence",
                        "review observed stale generated evidence",
                    ],
                    "evidence": [
                        {
                            "kind": "source",
                            "ref": "src/auto_agents/orchestrator.py:1",
                            "claim": (
                                "verified evidence is rewritten before review"
                            ),
                        },
                        {
                            "kind": "test",
                            "ref": "tests/test_retry_flow.py",
                            "claim": "four verification passes precede rejection",
                        },
                    ],
                    "rejected_hypotheses": [],
                    "reproduction_commands": [
                        "pytest -q tests/test_retry_flow.py"
                    ],
                    "reproduction_outcome": "lifecycle defect reproduced",
                    "proposed_fix_scope": [
                        "src/auto_agents/orchestrator.py"
                    ],
                    "verification_commands": [
                        "python -m pytest -q tests/test_retry_flow.py"
                    ],
                    "resume_strategy": "repair_and_resume",
                }
                return AgentResult(
                    ok=True,
                    command=[],
                    output_path=request.output_path,
                    summary=json.dumps(structured),
                )

        orchestrator = FakeOrchestrator()
        task = TaskSpec(
            task_id="repair-task",
            title="Repair generated evidence",
            description="",
            acceptance=["verification ref passes"],
            review_history=[
                {"attempt": 2, "summary": "REQ-102 audit paragraph is still stale"},
                {"attempt": 3, "summary": "REQ-102 audit paragraph is still stale"},
            ],
            verify_history=[
                {"attempt": 2, "decision": "pass", "summary": "all commands passed"},
                {"attempt": 3, "decision": "pass", "summary": "all commands passed"},
            ],
        )
        state = RunState(run_id="triage-run", status="failed", tasks=[task])
        result = adjudicate_auto_agents_error(
            orchestrator,
            target_project_root=Path("/tmp/demo"),
            error=review_error,
            state=state,
            traceback_text="Traceback evidence",
            env={},
        )

        self.assertTrue(result.decision.eligible)
        self.assertEqual(result.source, "root_cause_consensus")
        self.assertEqual(result.decision.category, "generated_artifact_lifecycle")
        self.assertEqual(orchestrator.request.stage, "self_repair_reviewer")
        self.assertIn("review rejected the task", orchestrator.request.prompt)
        self.assertIn("REQ-102 audit paragraph is still stale", orchestrator.request.prompt)
        self.assertIn("all commands passed", orchestrator.request.prompt)

        payload["category"] = "orchestrator_state_lifecycle"
        repeated = adjudicate_auto_agents_error(
            orchestrator,
            target_project_root=Path("/tmp/demo"),
            error=review_error,
            state=state,
            env={
                SELF_REPAIR_LAST_FINGERPRINT_ENV: result.decision.fingerprint,
                SELF_REPAIR_REPEAT_COUNT_ENV: "1",
            },
        )
        self.assertTrue(repeated.decision.eligible)
        self.assertEqual(repeated.decision.repeat_count, 2)
        self.assertEqual(repeated.decision.fingerprint, result.decision.fingerprint)

    def test_provider_triage_includes_referenced_requirements_audit_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            audit_path = project_root / ".auto-agents" / "docs" / "requirements_audit.md"
            write_text(
                audit_path,
                "# Requirements Audit\n\n"
                "## REQ-102: fail\n\n"
                "The required public API wording is present.\n\n"
                "Findings:\n"
                "- REQ-102 acceptance oracle #3 has no valid verified proof\n\n"
                "## REQ-103: fail\n\n"
                "Findings:\n"
                "- unrelated implementation blocker\n",
            )
            payload = {
                "decision": "DO_NOT_REPAIR",
                "owner": "target_project",
                "generic": False,
                "safe_to_self_repair": False,
                "confidence": 0.98,
                "category": "target_contract_failure",
                "reason": "target proof metadata is incomplete",
                "evidence": ["REQ-102 audit finding"],
            }

            class FakeOrchestrator:
                config = type("Config", (), {"efforts": {"self_repair": "max"}})()

                def _call_with_failover(self, request):
                    self.request = request
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=json.dumps(payload),
                    )

            orchestrator = FakeOrchestrator()
            task = TaskSpec(
                task_id="task-236",
                title="Bind requirement proofs",
                description="",
                acceptance=[],
                verify_history=[
                    {
                        "attempt": 2,
                        "decision": "fail",
                        "failure_ids": ["REQ-102"],
                    }
                ],
            )
            state = RunState(run_id="triage-run", status="failed", tasks=[task])

            adjudicate_auto_agents_error(
                orchestrator,
                target_project_root=project_root,
                error="Task task-236 failed gates: requirements audit failed for REQ-102",
                state=state,
                env={},
            )

            prompt = orchestrator.request.prompt
            self.assertIn('"requirements_audit_findings"', prompt)
            self.assertIn("The required public API wording is present.", prompt)
            self.assertIn("acceptance oracle #3 has no valid verified proof", prompt)
            self.assertNotIn("unrelated implementation blocker", prompt)

    def test_provider_triage_includes_full_active_execution_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            incident_id = "incident-worker-capability"
            incident_path = (
                run_path(project_root, "triage-run")
                / "recovery_incidents"
                / f"{incident_id}.json"
            )
            write_json(
                incident_path,
                {
                    "incident_id": incident_id,
                    "kind": "gate_infrastructure_error",
                    "command": "pytest test_real_minio",
                    "diagnosis": {"owner": "verification_infrastructure"},
                    "process_snapshot": {
                        "required_slots": 2,
                        "required_capabilities": ["docker", "ffmpeg"],
                        "infrastructure_attempts": [
                            {
                                "worker_id": "local",
                                "capabilities": ["ffmpeg"],
                            }
                        ],
                    },
                },
            )
            payload = {
                "decision": "DO_NOT_REPAIR",
                "owner": "verification_infrastructure",
                "generic": False,
                "safe_to_self_repair": False,
                "confidence": 0.9,
                "category": "worker_capacity_unavailable",
                "reason": "no source defect was proven",
                "evidence": ["worker attempt evidence"],
            }

            class FakeOrchestrator:
                config = type("Config", (), {"efforts": {"self_repair": "max"}})()

                def _call_with_failover(self, request):
                    self.request = request
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=json.dumps(payload),
                    )

            state = RunState(
                run_id="triage-run",
                status="blocked",
                active_execution_incident_id=incident_id,
            )
            orchestrator = FakeOrchestrator()

            with patch(
                "auto_agents.self_repair.self_repair_runtime_evidence",
                return_value={"healthy_not_advertised": ["docker"]},
            ):
                adjudicate_auto_agents_error(
                    orchestrator,
                    target_project_root=project_root,
                    error="no eligible worker",
                    state=state,
                    env={},
                )

            prompt = orchestrator.request.prompt
            self.assertIn('"active_execution_incident"', prompt)
            self.assertIn('"required_slots": 2', prompt)
            self.assertIn('"required_capabilities"', prompt)
            self.assertIn('"healthy_not_advertised"', prompt)

    def test_valid_provider_rejection_overrides_eligible_heuristic(self) -> None:
        scope_error = (
            "Task task-224 failed gates: verification scope mismatch: new failures are outside "
            "this task's owned test/proof surface"
        )
        payload = {
            "decision": "DO_NOT_REPAIR",
            "owner": "target_project",
            "generic": False,
            "safe_to_self_repair": False,
            "confidence": 0.98,
            "category": "target_contract_failure",
            "reason": "the target test exposes a product contract failure",
            "evidence": ["the failure is confined to target project behavior"],
        }

        class FakeOrchestrator:
            config = type("Config", (), {"efforts": {"self_repair": "max"}})()

            def _call_with_failover(self, request):
                return AgentResult(
                    ok=True,
                    command=[],
                    output_path=request.output_path,
                    summary=json.dumps(payload),
                )

        result = adjudicate_auto_agents_error(
            FakeOrchestrator(),
            target_project_root=Path("/tmp/demo"),
            error=scope_error,
            env={},
        )

        self.assertFalse(result.decision.eligible)
        self.assertEqual(result.source, "root_cause_consensus")
        self.assertEqual(result.judgment.owner, "target_project")

    def test_root_cause_diagnosis_failure_blocks_without_consensus(self) -> None:
        scope_error = (
            "Task task-224 failed gates: verification scope mismatch: new failures are outside "
            "this task's owned test/proof surface"
        )

        class InvalidProviderOrchestrator:
            config = type("Config", (), {"efforts": {"self_repair": "max"}})()

            def _call_with_failover(self, request):
                return AgentResult(
                    ok=True,
                    command=[],
                    output_path=request.output_path,
                    summary="not json",
                )

        result = adjudicate_auto_agents_error(
            InvalidProviderOrchestrator(),
            target_project_root=Path("/tmp/demo"),
            error=scope_error,
            env={},
        )

        self.assertFalse(result.decision.eligible)
        self.assertEqual(result.source, "root_cause_failed")
        self.assertIn("JSON object", result.provider_error)

        rejected = adjudicate_auto_agents_error(
            InvalidProviderOrchestrator(),
            target_project_root=Path("/tmp/demo"),
            error="Task task-001 failed gates: review rejected the task",
            env={},
        )
        self.assertFalse(rejected.decision.eligible)
        self.assertEqual(rejected.source, "root_cause_failed")

    def test_self_repair_disabled_skips_provider_triage(self) -> None:
        class ProviderMustNotRun:
            def _call_with_failover(self, request):
                raise AssertionError("provider must not run when self-repair is disabled")

        result = adjudicate_auto_agents_error(
            ProviderMustNotRun(),
            target_project_root=Path("/tmp/demo"),
            error="Task task-001 failed gates: review rejected the task",
            env={SELF_REPAIR_DISABLED_ENV: "true"},
        )

        self.assertFalse(result.decision.eligible)
        self.assertEqual(result.source, "disabled")

    def test_self_repair_classifier_stops_after_same_error_repeats_three_times(self) -> None:
        scope_error = (
            "Task task-224 failed gates: verification scope mismatch: new failures are outside "
            "this task's owned test/proof surface."
        )
        first = classify_auto_agents_error(scope_error, env={})
        self.assertTrue(first.eligible)
        self.assertEqual(first.repeat_count, 1)
        self.assertTrue(first.fingerprint)

        second = classify_auto_agents_error(
            scope_error,
            env={
                SELF_REPAIR_LAST_FINGERPRINT_ENV: first.fingerprint,
                SELF_REPAIR_REPEAT_COUNT_ENV: "1",
            },
        )
        self.assertTrue(second.eligible)
        self.assertEqual(second.repeat_count, 2)
        self.assertEqual(second.fingerprint, first.fingerprint)

        third = classify_auto_agents_error(
            scope_error,
            env={
                SELF_REPAIR_LAST_FINGERPRINT_ENV: first.fingerprint,
                SELF_REPAIR_REPEAT_COUNT_ENV: "2",
            },
        )
        self.assertFalse(third.eligible)
        self.assertEqual(third.repeat_count, 3)
        self.assertIn("3 consecutive", third.reason)

    def test_self_repair_classifier_resets_count_for_different_error(self) -> None:
        scope_error = (
            "Task task-224 failed gates: verification scope mismatch: new failures are outside "
            "this task's owned test/proof surface."
        )
        first = classify_auto_agents_error(scope_error, env={})
        self.assertTrue(first.eligible)

        audit_error = (
            "requirements audit failed: /tmp/demo/.auto-agents/docs/requirements_audit.md\n"
            "Automatic recovery is unsafe for at least one blocker:\n"
            "- REQ-125: forbidden pattern 'detail entry' found in specs/iter.md; automatic "
            "recovery is unsafe because specs/iter.md is an immutable input specification"
        )
        changed = classify_auto_agents_error(
            audit_error,
            env={
                SELF_REPAIR_LAST_FINGERPRINT_ENV: first.fingerprint,
                SELF_REPAIR_REPEAT_COUNT_ENV: "2",
            },
        )
        self.assertTrue(changed.eligible)
        self.assertEqual(changed.repeat_count, 1)
        self.assertNotEqual(changed.fingerprint, first.fingerprint)

    def test_self_repair_runner_uses_dedicated_effort(self) -> None:
        class FakeConfig:
            efforts = {"self_repair": "balanced", "implement": "max"}

        class FakeOrchestrator:
            config = FakeConfig()

        runner = AutoAgentsSelfRepairRunner(
            FakeOrchestrator(),
            target_project_root=Path("/tmp/demo"),
            error="boom",
            decision=SelfRepairDecision(True, category="auto_agents_traceback", reason="traceback"),
        )

        self.assertEqual(runner._effort(), "balanced")

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
                patch("auto_agents.cli.notify_run_finished") as notify,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(["run", "--project", str(project_root), "--spec-file", str(spec_file)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(session_calls["start"], 1)
            self.assertEqual(session_calls["offer"], 0)
            self.assertIn("Run completed successfully.", stdout.getvalue())
            self.assertIn("Starting automatic provider recovery", stderr.getvalue())
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[1]["status"], "completed")

    def test_cli_run_auto_repairs_auto_agents_and_resumes_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            review_error = "Task repair-task failed gates: review rejected the task"

            def mock_run(_self, *args, **kwargs):
                state = load_run_state(project_root)
                state.status = "failed"
                state.current_stage = "implement"
                state.last_error = review_error
                save_run_state(project_root, state)
                raise RuntimeError(review_error)

            runner_calls = {"count": 0}

            class FakeSelfRepairRunner:
                repo_root = Path("/tmp/auto_agents_repo")

                def __init__(self, orchestrator, **kwargs):
                    runner_calls["count"] += 1
                    self.kwargs = kwargs

                def run(self):
                    return SelfRepairResult(
                        ok=True,
                        status="completed",
                        reason="repaired",
                        category="generated_artifact_lifecycle",
                        commit_sha="abc123456789",
                        summary="generic fix\nCOMMIT_MESSAGE: repair gate scope handling",
                        verification="tests passed",
                    )

            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(
                    True,
                    category="generated_artifact_lifecycle",
                    reason="orchestrator rewrote verified evidence before review",
                    fingerprint="triage-fingerprint",
                    repeat_count=1,
                ),
                source="provider",
                reason="provider approved self-repair",
                judgment=SelfRepairJudgment(
                    decision="SELF_REPAIR",
                    owner="auto_agents",
                    generic=True,
                    safe_to_self_repair=True,
                    confidence=0.96,
                    category="generated_artifact_lifecycle",
                    reason="orchestrator rewrote verified evidence before review",
                    evidence=["verification passed before review observed stale evidence"],
                ),
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(Orchestrator, "run", mock_run),
                patch("auto_agents.cli.adjudicate_auto_agents_error", return_value=triage),
                patch("auto_agents.cli.AutoAgentsSelfRepairRunner", FakeSelfRepairRunner),
                patch("auto_agents.cli._run_self_repair_resume_process", return_value=0) as resume_run,
                patch("auto_agents.cli.notify_self_repair_finished") as notify_self_repair,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main([
                    "run",
                    "--project",
                    str(project_root),
                    "--spec-file",
                    str(spec_file),
                    "--auto-approve",
                ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(runner_calls["count"], 1)
            self.assertIn("Starting automatic auto_agents self-repair", stderr.getvalue())
            self.assertIn("Resuming run with repaired code", stderr.getvalue())
            notify_self_repair.assert_called_once()
            resume_run.assert_called_once()
            resumed_command = resume_run.call_args.args[0]
            self.assertIn("run", resumed_command)
            self.assertIn("--auto-approve", resumed_command)
            resume_env = resume_run.call_args.kwargs["env"]
            self.assertIn(SELF_REPAIR_LAST_FINGERPRINT_ENV, resume_env)
            self.assertEqual(resume_env[SELF_REPAIR_REPEAT_COUNT_ENV], "1")
            self.assertIn(RUN_LOCK_FD_ENV, resume_env)
            self.assertIn(RUN_LOCK_KEY_ENV, resume_env)
            self.assertEqual(
                resume_run.call_args.kwargs["pass_fd"],
                int(resume_env[RUN_LOCK_FD_ENV]),
            )

    def test_cli_meta_triages_non_auto_agents_blocker_and_resumes_after_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            def mock_run(_self, *args, **kwargs):
                state = load_run_state(project_root)
                state.status = "blocked"
                state.current_stage = "implement"
                state.last_error = "no eligible worker has required docker capability"
                state.active_blocker = {
                    "owner": "verification_infrastructure",
                    "category": "gate_infrastructure_error",
                    "reason": state.last_error,
                    "status": "blocked",
                }
                save_run_state(project_root, state)
                return state

            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(
                    True,
                    category="worker_capability_false_negative",
                    reason="worker probe omitted a healthy host capability",
                    fingerprint="blocked-triage-fingerprint",
                    repeat_count=1,
                ),
                source="provider",
                reason="provider approved self-repair",
                judgment=SelfRepairJudgment(
                    decision="SELF_REPAIR",
                    owner="auto_agents",
                    generic=True,
                    safe_to_self_repair=True,
                    confidence=0.97,
                    category="worker_capability_false_negative",
                    reason="worker probe omitted a healthy host capability",
                    evidence=["docker is healthy but absent from worker capabilities"],
                ),
            )

            class FakeSelfRepairRunner:
                repo_root = Path("/tmp/auto_agents_repo")

                def __init__(self, orchestrator, **kwargs):
                    self.kwargs = kwargs

                def run(self):
                    return SelfRepairResult(
                        ok=True,
                        status="completed",
                        reason="repaired",
                        category="worker_capability_false_negative",
                        commit_sha="def123456789",
                        summary="fixed worker probe",
                        verification="tests passed",
                    )

            with (
                patch.object(Orchestrator, "run", mock_run),
                patch(
                    "auto_agents.cli._triage_terminal_run_error",
                    return_value=triage,
                ) as triage_run,
                patch("auto_agents.cli.AutoAgentsSelfRepairRunner", FakeSelfRepairRunner),
                patch("auto_agents.cli._run_self_repair_resume_process", return_value=0) as resume_run,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    ["run", "--project", str(project_root), "--spec-file", str(spec_file)]
                )

            self.assertEqual(exit_code, 0)
            triage_run.assert_called_once()
            self.assertIn("no eligible worker", str(triage_run.call_args.args[2]))
            resume_run.assert_called_once()
            repaired_state = load_run_state(project_root)
            self.assertEqual(repaired_state.status, "pending")
            self.assertEqual(repaired_state.active_blocker["owner"], "auto_agents")
            self.assertEqual(
                repaired_state.active_blocker["self_repair_commit"],
                "def123456789",
            )

    def test_cli_preserves_blocker_and_records_rejected_meta_triage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            def mock_run(_self, *args, **kwargs):
                state = load_run_state(project_root)
                state.status = "blocked"
                state.last_error = "production credentials are missing"
                state.active_blocker = {
                    "owner": "user_input",
                    "category": "credentials_required",
                    "reason": state.last_error,
                    "status": "blocked",
                }
                save_run_state(project_root, state)
                return state

            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(
                    False,
                    category="credentials_required",
                    reason="credentials require user input",
                ),
                source="provider",
                reason="provider rejected self-repair",
                judgment=SelfRepairJudgment(
                    decision="DO_NOT_REPAIR",
                    owner="user_input",
                    generic=False,
                    safe_to_self_repair=False,
                    confidence=0.99,
                    category="credentials_required",
                    reason="credentials require user input",
                    evidence=["no production credential was supplied"],
                ),
            )

            with (
                patch.object(Orchestrator, "run", mock_run),
                patch(
                    "auto_agents.cli._triage_terminal_run_error",
                    return_value=triage,
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    ["run", "--project", str(project_root), "--spec-file", str(spec_file)]
                )

            self.assertEqual(exit_code, 3)
            blocked_state = load_run_state(project_root)
            self.assertEqual(blocked_state.active_blocker["owner"], "user_input")
            self.assertEqual(
                blocked_state.active_blocker["self_repair_triage"]["judgment"]["decision"],
                "DO_NOT_REPAIR",
            )

    def test_self_repair_runtime_evidence_detects_worker_capability_mismatch(self) -> None:
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="27.5.1\n",
                stderr="",
            )

        with (
            patch(
                "auto_agents.self_repair.shutil.which",
                side_effect=lambda program: "/usr/bin/docker" if program == "docker" else None,
            ),
            patch("auto_agents.self_repair.subprocess.run", side_effect=fake_run),
            patch(
                "auto_agents.workers.worker_probe",
                return_value={"capabilities": ["ffmpeg", "python"]},
            ),
        ):
            evidence = self_repair_runtime_evidence()

        self.assertEqual(evidence["healthy_not_advertised"], ["docker"])
        self.assertEqual(
            evidence["worker_advertised_capabilities"],
            ["ffmpeg", "python"],
        )

    def test_cli_gate_timeout_enters_unified_triage_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")
            error = GateCommandTimeoutError(
                "baseline gate command timeout: pytest -q (timeout=1800s)"
            )
            stdout = io.StringIO()
            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(
                    False,
                    category="gate_timeout",
                    reason="target-project gate timed out",
                ),
                source="provider",
                reason="target project owns the timeout",
            )

            with (
                patch.object(Orchestrator, "run", side_effect=error),
                patch(
                    "auto_agents.cli._triage_terminal_run_error",
                    return_value=triage,
                ) as triage_run,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    ["run", "--project", str(project_root), "--spec-file", str(spec_file)]
                )

            self.assertEqual(exit_code, 3)
            triage_run.assert_called_once()
            self.assertIn("baseline gate command timeout", stdout.getvalue())
            self.assertEqual(load_run_state(project_root).status, "blocked")

    def test_cli_failed_result_enters_terminal_root_cause_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            def mock_run(_self, *args, **kwargs):
                del args, kwargs
                state = load_run_state(project_root)
                state.status = "failed"
                state.current_stage = "implement"
                state.last_error = "terminal failed result"
                save_run_state(project_root, state)
                return state

            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(
                    False,
                    category="target_failure",
                    reason="target project owns the failure",
                ),
                source="root_cause_consensus",
                reason="no auto_agents repair",
            )
            with (
                patch.object(Orchestrator, "run", mock_run),
                patch(
                    "auto_agents.cli._triage_terminal_run_error",
                    return_value=triage,
                ) as diagnose,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "run",
                        "--project",
                        str(project_root),
                        "--spec-file",
                        str(spec_file),
                    ]
                )

            self.assertEqual(exit_code, 3)
            diagnose.assert_called_once()
            self.assertIn(
                "terminal failed result",
                str(diagnose.call_args.args[2]),
            )

    def test_cli_run_sends_unexpected_exception_to_provider_triage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            def mock_run(_self, *args, **kwargs):
                raise TypeError("unexpected state shape")

            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(False, reason="provider rejected self-repair"),
                source="provider",
                reason="provider judgment did not satisfy the composite gate",
            )
            with (
                patch.object(Orchestrator, "run", mock_run),
                patch(
                    "auto_agents.cli.adjudicate_auto_agents_error",
                    return_value=triage,
                ) as adjudicate,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    ["run", "--project", str(project_root), "--spec-file", str(spec_file)]
                )

            self.assertEqual(exit_code, 3)
            adjudicate.assert_called_once()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["error"], "unexpected state shape")
            state = load_run_state(project_root)
            triage_artifact = (
                run_path(project_root, state.run_id)
                / "outputs"
                / "self-repair-triage.json"
            )
            self.assertTrue(triage_artifact.exists())
            self.assertEqual(
                json.loads(triage_artifact.read_text(encoding="utf-8"))["source"],
                "provider",
            )

    def test_cli_session_commands_notify_completed_state(self) -> None:
        for command, mode in (
            ("fix", "fix"),
            ("collab", "collab"),
            ("provider-resolve", "provider_resolve"),
        ):
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as tmp:
                    project_root = Path(tmp) / "demo"

                    class FakeOrchestrator:
                        def __init__(self, project_root, agent_output_stream=None):
                            self.project_root = project_root
                            self._print_agent_output = False

                        def _ensure_agent_instructions_synced(self):
                            return None

                    class FakeSession:
                        def __init__(self, orchestrator, mode, print_agent_output=False):
                            self.mode = mode

                        def offer_resume_or_new(self):
                            return SessionState(
                                session_id=f"{self.mode}-123",
                                mode=self.mode,
                                status="completed",
                                resolution="done",
                            )

                    with (
                        patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                        patch("auto_agents.session.Session", FakeSession),
                        patch("auto_agents.cli.notify_session_finished") as notify,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        exit_code = main([command, "--project", str(project_root)])

                    self.assertEqual(exit_code, 0)
                    notify.assert_called_once()
                    self.assertEqual(notify.call_args.args[0], project_root)
                    self.assertEqual(notify.call_args.args[1]["status"], "completed")
                    self.assertEqual(notify.call_args.kwargs["command"], command)

    def test_cli_collab_full_verify_reaches_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            received = {}

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    self.project_root = project_root
                    self._print_agent_output = False

                def _ensure_agent_instructions_synced(self):
                    return None

            class FakeSession:
                def __init__(
                    self,
                    orchestrator,
                    mode,
                    print_agent_output=False,
                    full_verify=False,
                ):
                    received["mode"] = mode
                    received["full_verify"] = full_verify

                def offer_resume_or_new(self):
                    return SessionState(
                        session_id="collab-full-verify",
                        mode="collab",
                        status="completed",
                    )

            with (
                patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                patch("auto_agents.session.Session", FakeSession),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = main(
                    ["collab", "--project", str(project_root), "--full-verify"]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(received, {"mode": "collab", "full_verify": True})

    def test_cli_session_command_notifies_failure_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    self.project_root = project_root
                    self._print_agent_output = False

                def _ensure_agent_instructions_synced(self):
                    return None

            class FakeSession:
                def __init__(self, orchestrator, mode, print_agent_output=False):
                    pass

                def offer_resume_or_new(self):
                    raise RuntimeError("session boom")

            with (
                patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                patch("auto_agents.session.Session", FakeSession),
                patch("auto_agents.cli.notify_session_finished") as notify,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = main(["fix", "--project", str(project_root)])

            self.assertEqual(exit_code, 1)
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[0], project_root)
            self.assertEqual(notify.call_args.kwargs["status"], "failed")
            self.assertEqual(notify.call_args.kwargs["error"], "session boom")

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
            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(False, reason="target project input failure"),
                source="provider",
                reason="provider rejected self-repair",
            )
            with (
                patch("auto_agents.cli.adjudicate_auto_agents_error", return_value=triage),
                contextlib.redirect_stdout(buffer),
            ):
                exit_code = main(["run", "--project", str(project_root)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 3)
            self.assertFalse(payload["ok"])
            self.assertIn(str(project_root / "spec.md"), payload["error"])

    def test_cli_run_provider_override_persists_as_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            buffer = io.StringIO()
            triage = SelfRepairTriageResult(
                decision=SelfRepairDecision(False, reason="provider availability failure"),
                source="heuristic_fallback",
                reason="provider triage unavailable",
            )
            with (
                patch.object(
                    Orchestrator,
                    "_call_with_failover",
                    side_effect=RuntimeError("provider unavailable"),
                ),
                patch("auto_agents.cli.adjudicate_auto_agents_error", return_value=triage),
                contextlib.redirect_stdout(buffer),
            ):
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

            self.assertEqual(exit_code, 3)
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

    def test_cli_run_notifies_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")

            class FakeState:
                def to_dict(self):
                    return {
                        "status": "completed",
                        "run_id": "run-notify",
                        "current_stage": "readme",
                    }

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    pass

                def run(self, **kwargs):
                    return FakeState()

            with (
                patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                patch("auto_agents.cli.notify_run_finished") as notify,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = main(["run", "--project", str(project_root), "--spec-file", str(spec_file)])

            self.assertEqual(exit_code, 0)
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[0], project_root)
            self.assertEqual(notify.call_args.args[1]["status"], "completed")

    def test_cli_run_loads_cwd_dotenv_before_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            workspace_root.mkdir()
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")
            write_text(workspace_root / ".env", "WECHAT_WEBHOOK_URL=https://example.test/wechat\n")
            captured = {}

            class FakeState:
                def to_dict(self):
                    return {
                        "status": "completed",
                        "run_id": "run-dotenv",
                        "current_stage": "readme",
                    }

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    pass

                def run(self, **kwargs):
                    return FakeState()

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"errcode": 0}'

            def fake_urlopen(request, timeout):
                captured["url"] = request.full_url
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                return FakeResponse()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                patch("urllib.request.urlopen", fake_urlopen),
                patch("pathlib.Path.cwd", return_value=workspace_root),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = main(["run", "--project", str(project_root), "--spec-file", str(spec_file)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["url"], "https://example.test/wechat")
            self.assertEqual(captured["payload"]["msgtype"], "markdown")
            self.assertIn("auto-agents run completed", captured["payload"]["markdown"]["content"])

    def test_cli_run_does_not_load_project_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            workspace_root.mkdir()
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")
            write_text(project_root / ".env", "WECHAT_WEBHOOK_URL=https://example.test/project\n")

            class FakeState:
                def to_dict(self):
                    return {
                        "status": "completed",
                        "run_id": "run-no-project-dotenv",
                        "current_stage": "readme",
                    }

            class FakeOrchestrator:
                def __init__(self, project_root, agent_output_stream=None):
                    pass

                def run(self, **kwargs):
                    return FakeState()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                patch("urllib.request.urlopen") as urlopen,
                patch("pathlib.Path.cwd", return_value=workspace_root),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = main(["run", "--project", str(project_root), "--spec-file", str(spec_file)])

            self.assertEqual(exit_code, 0)
            urlopen.assert_not_called()

    def test_cli_run_notifies_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")

            class FakeOrchestrator:
                @staticmethod
                def is_provider_research_blocked_error(message):
                    return False

                def __init__(self, project_root, agent_output_stream=None):
                    pass

                def run(self, **kwargs):
                    raise RuntimeError("boom")

            with (
                patch("auto_agents.cli.Orchestrator", FakeOrchestrator),
                patch("auto_agents.cli.load_run_state", side_effect=FileNotFoundError("missing state")),
                patch("auto_agents.cli.notify_run_finished") as notify,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = main(["run", "--project", str(project_root), "--spec-file", str(spec_file)])

            self.assertEqual(exit_code, 3)
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[0], project_root)
            self.assertEqual(notify.call_args.kwargs["status"], "blocked")
            self.assertEqual(notify.call_args.kwargs["error"], "boom")

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

    def test_plan_validation_feedback_repairs_negative_oracle_token_preservation(self) -> None:
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
                            "text": "Default moderation remains fake fixture based.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": [
                                "默认审核测试可用 fake/fixture 触发 `pass/review/block`，无需新增外部审核 API 文档。"
                            ],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
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
                    "oracle_proof_schema_version": 1,
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Moderation boundary",
                            "description": "Keep fake moderation available.",
                            "acceptance": [
                                "默认 `fake` / `fixture` 审核 `decision=pass` 自动流转。"
                            ],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "proof_type": "integration_test",
                                    "oracle_strength": "behavioral",
                                    "evidence_boundary": "system_boundary",
                                    "evidence_refs": [
                                        "tests/test_project_api.py::ProjectApiTests::test_default_moderation_backends_are_fake_pass_and_auto_continue"
                                    ],
                                    "forbidden_proxy_oracles": [],
                                    "status": "planned",
                                }
                            ],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            result = AgentResult(
                ok=True,
                command=[],
                output_path=Path("."),
                summary="valid plan",
                stdout="",
            )

            self.assertIsNone(Orchestrator(project_root)._plan_validation_feedback(result))
            repaired = json.loads(task_plan_path(project_root).read_text(encoding="utf-8"))
            self.assertIn("fake/fixture", repaired["tasks"][0]["acceptance"][0])

    def test_plan_validation_feedback_normalizes_copied_done_task_with_planned_proofs(self) -> None:
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
                            "text": "Provider output stays normalized.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["The public API returns normalized provider output."],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
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
                    "oracle_proof_schema_version": 1,
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Copied done task",
                            "description": "Planner copied a runtime status into a new plan.",
                            "acceptance": ["The public API returns normalized provider output."],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "proof_type": "integration_test",
                                    "oracle_strength": "behavioral",
                                    "evidence_boundary": "system_boundary",
                                    "evidence_refs": ["tests/test_public_api.py::test_normalized_provider_output"],
                                    "forbidden_proxy_oracles": [],
                                    "status": "planned",
                                }
                            ],
                            "status": "done",
                            "commit_message": "",
                        }
                    ],
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            result = AgentResult(
                ok=True,
                command=[],
                output_path=Path("."),
                summary="valid plan",
                stdout="",
            )

            self.assertIsNone(Orchestrator(project_root)._plan_validation_feedback(result))
            repaired = json.loads(task_plan_path(project_root).read_text(encoding="utf-8"))
            self.assertEqual(repaired["tasks"][0]["status"], "pending")

    def test_plan_validation_rejects_oversized_active_tasks(self) -> None:
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
                            "text": "New active capability is covered.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["new active capability works"],
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
                            "title": "Oversized active task",
                            "description": "Does too much.",
                            "acceptance": [f"criterion {index}" for index in range(8)],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "proof_type": "deterministic_test",
                                    "oracle_strength": "behavioral",
                                    "evidence_boundary": "internal_state",
                                    "evidence_refs": ["tests/test_demo.py"],
                                    "status": "planned",
                                }
                            ],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            write_text(tests_dir / "test_demo.py", "def test_demo():\n    assert True\n")

            result = AgentResult(
                ok=True,
                command=[],
                output_path=Path("."),
                summary="COVERAGE ANALYSIS: REQ-001 is uncovered and assigned to task-001.",
                stdout="",
            )

            feedback = Orchestrator(project_root)._plan_validation_feedback(result)

            self.assertIsNotNone(feedback)
            self.assertIn("more than 7 criteria must be split", feedback)

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
                attachments=[project_root / "prototype.png", project_root / "actual.png"],
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
            command = run_mock.call_args.args[0]
            self.assertIn("--sandbox", command)
            self.assertIn("workspace-write", command)
            self.assertNotIn("--full-auto", command)
            image_paths = [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--image"
            ]
            self.assertEqual(
                image_paths,
                [str(project_root / "prototype.png"), str(project_root / "actual.png")],
            )
            self.assertEqual(result.model, "profile:deep")
            self.assertIsNotNone(result.usage)
            usage = result.usage
            self.assertEqual(usage.input_tokens if usage else None, 200)
            self.assertEqual(usage.cached_input_tokens if usage else None, 50)
            self.assertEqual(usage.output_tokens if usage else None, 25)
            self.assertEqual(usage.total_tokens if usage else None, 225)

    def test_codex_adapter_honors_diagnostic_sandbox_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            output_path = project_root / "diagnosis.json"
            write_text(output_path, "{}\n")
            adapter = CodexAdapter(
                ProviderConfig(timeout_seconds=1800)
            )
            request = AgentRequest(
                stage="self_repair_investigator",
                effort="max",
                prompt="inspect only",
                cwd=project_root,
                output_path=output_path,
                sandbox_mode="read-only",
                timeout_seconds=123,
            )

            with patch(
                "auto_agents.adapters.codex.run_subprocess_with_optional_streaming"
            ) as run_mock:
                run_mock.return_value = ("", "", 0, False, False)
                adapter.run(request)

            command = run_mock.call_args.args[0]
            sandbox_index = command.index("--sandbox")
            self.assertEqual(command[sandbox_index + 1], "read-only")
            self.assertEqual(run_mock.call_args.kwargs["timeout"], 123)

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
            self.assertEqual(config.retries.per_stage["sync-agent-instructions"], 2)

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

    def test_provider_image_attachment_capabilities_fail_closed(self) -> None:
        from auto_agents.adapters.antigravity import AntigravityAdapter
        from auto_agents.adapters.mock import MockAdapter
        from auto_agents.adapters.shell import ShellAdapter

        self.assertTrue(CodexAdapter(ProviderConfig(kind="codex")).supports_image_attachments())
        self.assertFalse(
            AntigravityAdapter(
                ProviderConfig(kind="antigravity", binary="agy")
            ).supports_image_attachments()
        )
        self.assertFalse(
            ShellAdapter(
                ProviderConfig(kind="shell", binary="provider-wrapper")
            ).supports_image_attachments()
        )
        self.assertFalse(MockAdapter().supports_image_attachments())

    def test_copilot_cli_adapter_detects_native_attachment_support(self) -> None:
        from auto_agents.adapters.copilot_cli import (
            CopilotCliAdapter,
            _copilot_cli_supports_image_attachments,
        )

        config = ProviderConfig(kind="copilot-cli", binary="copilot-test")
        adapter = CopilotCliAdapter(config)
        _copilot_cli_supports_image_attachments.cache_clear()
        try:
            with (
                patch(
                    "auto_agents.adapters.copilot_cli.shutil.which",
                    return_value="/tmp/copilot-test",
                ),
                patch(
                    "auto_agents.adapters.copilot_cli.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["/tmp/copilot-test", "--help"],
                        0,
                        stdout="  --attachment <path>  Attach an image\n",
                        stderr="",
                    ),
                ),
            ):
                self.assertTrue(adapter.supports_image_attachments())
        finally:
            _copilot_cli_supports_image_attachments.cache_clear()

    def test_copilot_cli_adapter_rejects_cli_without_attachment_flag(self) -> None:
        from auto_agents.adapters.copilot_cli import (
            CopilotCliAdapter,
            _copilot_cli_supports_image_attachments,
        )

        config = ProviderConfig(kind="copilot-cli", binary="copilot-old")
        adapter = CopilotCliAdapter(config)
        _copilot_cli_supports_image_attachments.cache_clear()
        try:
            with (
                patch(
                    "auto_agents.adapters.copilot_cli.shutil.which",
                    return_value="/tmp/copilot-old",
                ),
                patch(
                    "auto_agents.adapters.copilot_cli.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["/tmp/copilot-old", "--help"],
                        0,
                        stdout="Usage: copilot-old [options]\n",
                        stderr="",
                    ),
                ),
            ):
                self.assertFalse(adapter.supports_image_attachments())
        finally:
            _copilot_cli_supports_image_attachments.cache_clear()

    def test_copilot_cli_adapter_attaches_images_with_noninteractive_prompt(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            attachments = [project_root / "prototype.png", project_root / "actual.png"]
            adapter = CopilotCliAdapter(
                ProviderConfig(
                    kind="copilot-cli",
                    binary="copilot",
                    profile_map={},
                    prompt_via_stdin=True,
                )
            )
            request = AgentRequest(
                stage="visual_judge",
                effort="balanced",
                prompt="compare the screenshots",
                cwd=project_root,
                output_path=project_root / "judge.md",
                attachments=attachments,
            )

            with patch(
                "auto_agents.adapters.copilot_cli.run_subprocess_with_optional_streaming",
                return_value=("", "", 0, False, False),
            ) as run_mock:
                result = adapter.run(request)

            self.assertTrue(result.ok)
            command = run_mock.call_args.args[0]
            attachment_paths = [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--attachment"
            ]
            self.assertEqual(attachment_paths, [str(path) for path in attachments])
            self.assertEqual(command[-2:], ["-p", "compare the screenshots"])
            self.assertEqual(run_mock.call_args.kwargs["stdin_input"], "")

    def test_copilot_cli_adapter_keeps_stdin_for_text_only_request(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            adapter = CopilotCliAdapter(
                ProviderConfig(
                    kind="copilot-cli",
                    binary="copilot",
                    profile_map={},
                    prompt_via_stdin=True,
                )
            )
            request = AgentRequest(
                stage="review",
                effort="balanced",
                prompt="review this",
                cwd=project_root,
                output_path=project_root / "review.md",
            )

            with patch(
                "auto_agents.adapters.copilot_cli.run_subprocess_with_optional_streaming",
                return_value=("", "", 0, False, False),
            ) as run_mock:
                adapter.run(request)

            command = run_mock.call_args.args[0]
            self.assertNotIn("--attachment", command)
            self.assertNotIn("-p", command)
            self.assertEqual(run_mock.call_args.kwargs["stdin_input"], "review this")

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

    def test_generated_verification_config_persists_valid_shard_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            schema_tests = project_root / "tests" / "schema"
            schema_tests.mkdir(parents=True)
            for index in range(8):
                (schema_tests / f"test_{index}.py").write_text("", encoding="utf-8")
            (project_root / "tests" / "test_api.py").write_text(
                "def test_contract(): pass\n",
                encoding="utf-8",
            )

            config = load_project_config(project_root)
            config.gates.verification_policy_version = 4
            config.gates.fallback_proof_ids = ["legacy.proof"]
            save_project_config(project_root, config)
            source_steps = [
                VerificationStep(
                    proof_id="affected.schema",
                    runner="pytest",
                    targets=["tests/schema"],
                    levels=["affected"],
                    impact_paths=["src/schema/**"],
                    parallel_safe=True,
                    cache_scope="source",
                    result_cache_scope="auto",
                ),
                VerificationStep(
                    proof_id="affected.api",
                    runner="pytest",
                    targets=["tests/test_api.py::test_contract"],
                    levels=["affected"],
                    impact_paths=["src/api/**"],
                    depends_on_proofs=["affected.schema"],
                    parallel_safe=True,
                    cache_scope="source",
                    result_cache_scope="auto",
                ),
            ]
            save_task_plan(
                project_root,
                {
                    "tasks": [],
                    "verification_policy_version": 4,
                    "verification_steps": [step.to_dict() for step in source_steps],
                },
            )

            Orchestrator(project_root)._apply_generated_verification_config()

            persisted = load_project_config(project_root)
            schema_proofs = [
                step.proof_id
                for step in persisted.gates.steps
                if step.proof_id.startswith("affected.schema.shard-")
            ]
            api = next(
                step
                for step in persisted.gates.steps
                if step.proof_id == "affected.api"
            )
            self.assertGreater(len(schema_proofs), 1)
            self.assertEqual(api.depends_on_proofs, schema_proofs)
            self.assertEqual(
                persisted.gates.fallback_proof_ids,
                [
                    step.proof_id
                    for step in persisted.gates.steps
                    if "affected" in step.levels
                ],
            )
            errors = validate_project_config_payload(
                json.loads(config_path(project_root).read_text(encoding="utf-8"))
            )
            self.assertFalse(
                any(
                    "unknown proof_id" in error or "fallback_proof_ids" in error
                    for error in errors
                ),
                errors,
            )

    def test_generated_artifact_step_keeps_one_proof_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            tests = project_root / "tests" / "schema"
            tests.mkdir(parents=True)
            for index in range(4):
                (tests / f"test_{index}.py").write_text(
                    "def test_contract(): pass\n", encoding="utf-8"
                )

            config = load_project_config(project_root)
            config.gates.verification_policy_version = 4
            save_project_config(project_root, config)
            source = VerificationStep(
                proof_id="affected.schema.evidence",
                runner="pytest",
                targets=["tests/schema"],
                levels=["affected"],
                impact_paths=["src/schema/**"],
                artifact_globs=[".tmp-tests/evidence/schema-*.json"],
                result_cache_scope="auto",
            )
            save_task_plan(
                project_root,
                {
                    "tasks": [],
                    "verification_policy_version": 4,
                    "verification_steps": [source.to_dict()],
                },
            )

            Orchestrator(project_root)._apply_generated_verification_config()

            persisted = load_project_config(project_root)
            self.assertEqual(len(persisted.gates.steps), 1)
            self.assertEqual(
                persisted.gates.steps[0].proof_id,
                "affected.schema.evidence",
            )
            self.assertEqual(
                persisted.gates.steps[0].artifact_globs,
                [".tmp-tests/evidence/schema-*.json"],
            )
            errors = validate_project_config_payload(
                json.loads(config_path(project_root).read_text(encoding="utf-8"))
            )
            self.assertFalse(
                any("duplicates artifact ownership" in error for error in errors),
                errors,
            )

    def test_preflight_reconciles_generated_verification_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)

            with (
                patch.object(
                    orchestrator,
                    "_apply_generated_verification_config",
                ) as reconcile,
                patch(
                    "auto_agents.orchestrator.validation_report",
                    return_value={"ok": True, "errors": [], "warnings": []},
                ),
            ):
                orchestrator._ensure_preconditions(state, spec_file, False)

            reconcile.assert_called_once_with()

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
                "runs/\nfailed-verification-logs/\n"
                "state/gate_baseline_cache.json\nstate/gate_baseline_cache.sqlite3\n"
                "state/gate_baseline_cache.sqlite3-*\nstate/requirements_audit_cache.sqlite3\n"
                "state/requirements_audit_cache.sqlite3-*\nstate/repomap_cache.json\n"
                "state/parallel_tuning.json\nstate/release_jobs.sqlite3\n"
                "state/release_jobs.sqlite3-shm\nstate/release_jobs.sqlite3-wal\n"
                "state/release-worker.log\nstate/release-worker.lock\n",
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
            self.assertIn("Read the requirements trace", prompt)
            self.assertIn("must not contradict any active mandatory requirement", prompt)

    def test_clarify_prompt_requires_forbidden_patterns_for_removed_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\nRemove the legacy process review path.\n")

            prompt = orchestrator._build_prompt("clarify", spec_file)

            self.assertIn("add precise forbidden_patterns regexes", prompt)
            self.assertIn("stale terms or old semantic claims", prompt)

    def test_design_validation_rejects_architecture_forbidden_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Architecture must remove legacy review semantics.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["architecture.md no longer describes the legacy process review path"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_process_review"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_text(
                project_root / ".auto-agents" / "docs" / "architecture.md",
                "# Architecture\n\n"
                "## System Boundary\nlegacy_process_review remains in the workflow.\n\n"
                "## Core Modules\n- API\n\n"
                "## Data Flow\nRequest to task.\n\n"
                "## Risks\n- Drift.\n",
            )

            feedback = orchestrator._design_validation_feedback(
                AgentResult(ok=True, command=[], output_path=project_root / "out.txt")
            )

            self.assertIsNotNone(feedback)
            self.assertIn(".auto-agents/docs/architecture.md violates REQ-001", feedback or "")

    def test_unsafe_pattern_definition_routes_before_design_without_agent_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            unsafe = r"(?s)for\s+.*check.*(?:retry|attempt)"
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-150",
                            "text": "Checks use a bounded retry policy.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["The policy is enforced."],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [unsafe],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")
            state = load_run_state(project_root)
            state.status = "failed"
            state.current_stage = "design"
            state.stage_summaries = {"clarify": "done"}
            state.last_error = "design exhausted retries"

            class FailingIfCalledAdapter:
                def run(self, request):
                    raise AssertionError(f"design adapter must not run: {request.stage}")

            orchestrator.adapter = FailingIfCalledAdapter()
            returned = orchestrator._run_agent_stage("design", state, spec_file)
            feedback = orchestrator._design_validation_feedback(
                AgentResult(ok=True, command=[], output_path=project_root / "out.txt")
            )

            self.assertIs(returned, state)
            self.assertEqual(state.current_stage, "clarify")
            self.assertEqual(state.rejected_stage, "clarify")
            self.assertEqual(state.status, "pending")
            self.assertNotIn("design", state.agent_attempts)
            self.assertIn("Recovery route: rerun from clarify", state.rejection_reason)
            self.assertNotIn(unsafe, state.rejection_reason)
            self.assertIn("rerun from clarify", feedback or "")
            self.assertNotIn("architecture document failed validation", (feedback or "").lower())

    def test_pattern_recovery_validation_relaxes_only_definition_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-150",
                            "text": "Checks use a bounded retry policy.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["The policy is enforced."],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [r"(?s)for\s+.*check.*retry"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(task_plan_path(project_root), {"tasks": []})

            strict = validation_report(project_root)
            recovery = validation_report(
                project_root,
                allow_unsafe_forbidden_pattern_definitions=True,
            )

            self.assertTrue(
                any("definition is unsafe" in error for error in strict["errors"])
            )
            self.assertFalse(
                any("definition is unsafe" in error for error in recovery["errors"])
            )
            self.assertTrue(
                any("at least one task" in error for error in recovery["errors"])
            )

    def test_legacy_unsafe_pattern_design_exhaustion_is_self_repairable(self) -> None:
        unsafe = r"(?s)for\s+.*check.*(?:retry|attempt).*for\s+.*checks"
        error = (
            "design exhausted retries: The architecture document failed validation. "
            "Rewrite the file in place.\n"
            f"- .auto-agents/docs/architecture.md violates REQ-150 forbidden_patterns: {unsafe}"
        )

        decision = classify_auto_agents_error(error, env={})

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.category, "forbidden_pattern_validation_routing")

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

    def test_iteration_plan_prompt_uses_archived_task_plan_as_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            state = load_run_state(project_root)
            archive_path = project_root / ".auto-agents" / "history" / "task_plans" / "oldrun123.json"
            state.resume_context = {
                "previous_run_id": "oldrun123",
                "previous_task_plan_archive": str(archive_path),
            }
            save_run_state(project_root, state)
            orchestrator = Orchestrator(project_root)
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Spec\n")

            prompt = orchestrator._build_prompt("plan", spec_file, is_iteration=True)

            self.assertIn(str(archive_path), prompt)
            self.assertIn(f"Also review the current active task plan at: {task_plan_path(project_root)}", prompt)
            self.assertIn("Do NOT copy archived done tasks back into the active task_plan.json", prompt)
            self.assertIn("preserve those done tasks", prompt)
            self.assertIn("archived done tasks with verified requirement_proofs already count as historical coverage", prompt)
            self.assertIn("Do NOT create regression-lock or baseline-preservation tasks", prompt)
            self.assertNotIn("APPEND new tasks to the end of the JSON array", prompt)

    def test_plan_merge_preserves_current_run_done_tasks_and_prunes_duplicates(self) -> None:
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
                            "text": "Already delivered behavior remains covered.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["The current run already verified behavior A."],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        },
                        {
                            "id": "REQ-002",
                            "text": "New recovery behavior still needs work.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["The recovery task verifies behavior B."],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        },
                    ],
                },
            )
            done_task = {
                "task_id": "task-001",
                "title": "Delivered behavior A",
                "description": "Already done in this run.",
                "acceptance": ["The current run already verified behavior A."],
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "proof_type": "integration_test",
                        "oracle_strength": "behavioral",
                        "evidence_boundary": "system_boundary",
                        "evidence_refs": ["tests/test_a.py::test_behavior_a"],
                        "forbidden_proxy_oracles": [],
                        "status": "verified",
                    }
                ],
                "status": "done",
                "commit_message": "",
            }
            duplicate_task = {
                "task_id": "task-002",
                "title": "Duplicate behavior A",
                "description": "Planner regenerated a task already covered by task-001.",
                "acceptance": ["The current run already verified behavior A."],
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "proof_type": "integration_test",
                        "oracle_strength": "behavioral",
                        "evidence_boundary": "system_boundary",
                        "evidence_refs": ["tests/test_a.py::test_behavior_a"],
                        "forbidden_proxy_oracles": [],
                        "status": "planned",
                    }
                ],
                "status": "pending",
                "commit_message": "",
            }
            recovery_task = {
                "task_id": "task-003",
                "title": "Recovery behavior B",
                "description": "Still needs implementation.",
                "acceptance": ["The recovery task verifies behavior B."],
                "requirement_ids": ["REQ-002"],
                "requirement_proofs": [
                    {
                        "requirement_id": "REQ-002",
                        "oracle_index": 1,
                        "proof_type": "integration_test",
                        "oracle_strength": "behavioral",
                        "evidence_boundary": "system_boundary",
                        "evidence_refs": ["tests/test_b.py::test_behavior_b"],
                        "forbidden_proxy_oracles": [],
                        "status": "planned",
                    }
                ],
                "status": "pending",
                "commit_message": "",
                "depends_on": ["task-002", "task-001"],
            }
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [duplicate_task, recovery_task],
                },
            )

            Orchestrator(project_root)._merge_prior_done_tasks_into_generated_plan(
                [TaskSpec.from_dict(done_task)]
            )

            payload = json.loads(task_plan_path(project_root).read_text(encoding="utf-8"))
            self.assertEqual(
                [task["task_id"] for task in payload["tasks"]],
                ["task-001", "task-003"],
            )
            self.assertEqual(payload["tasks"][0]["status"], "done")
            self.assertEqual(payload["tasks"][1]["depends_on"], ["task-001"])
            self.assertEqual(validate_task_dependencies(payload["tasks"]), [])

    def test_plan_validation_uses_current_run_done_tasks_as_coverage(self) -> None:
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
                            "text": "Already verified in the current run.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["Behavior A is verified."],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        },
                        {
                            "id": "REQ-002",
                            "text": "Still uncovered recovery item.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["Behavior B is verified."],
                            "oracle_type": "integration_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        },
                    ],
                },
            )
            done_task = {
                "task_id": "task-001",
                "title": "Done A",
                "description": "Already verified.",
                "acceptance": ["Behavior A is verified."],
                "requirement_ids": ["REQ-001"],
                "requirement_proofs": [
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "proof_type": "integration_test",
                        "oracle_strength": "behavioral",
                        "evidence_boundary": "system_boundary",
                        "evidence_refs": ["tests/test_a.py::test_a"],
                        "forbidden_proxy_oracles": [],
                        "status": "verified",
                    }
                ],
                "status": "done",
                "commit_message": "",
            }
            write_json(
                task_plan_path(project_root),
                {
                    "oracle_proof_schema_version": 1,
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-002",
                            "title": "Recovery B",
                            "description": "Still needs work.",
                            "acceptance": ["Behavior B is verified."],
                            "requirement_ids": ["REQ-002"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-002",
                                    "oracle_index": 1,
                                    "proof_type": "integration_test",
                                    "oracle_strength": "behavioral",
                                    "evidence_boundary": "system_boundary",
                                    "evidence_refs": ["tests/test_b.py::test_b"],
                                    "forbidden_proxy_oracles": [],
                                    "status": "planned",
                                }
                            ],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir()
            orchestrator = Orchestrator(project_root)
            orchestrator._plan_prior_done_task_payloads = [done_task]
            result = AgentResult(
                ok=True,
                command=[],
                output_path=Path("."),
                summary="valid recovery plan",
                stdout="",
            )

            self.assertIsNone(orchestrator._plan_validation_feedback(result))

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
