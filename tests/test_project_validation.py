import contextlib
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
from auto_agents.adapters.copilot_cli import CopilotCliAdapter
from auto_agents.adapters.codex import CodexAdapter
from auto_agents.config import config_path, load_project_config, load_run_state, save_run_state, task_plan_path
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentRequest, AgentResult, AgentUsage, ProviderConfig, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.validation import (
    validate_required_document,
    validate_project_config_payload,
    validation_report,
)


class ProjectValidationTests(unittest.TestCase):
    def test_validate_project_config_payload_rejects_bad_effort_and_template(self) -> None:
        payload = {
            "project_name": "demo",
            "provider": {
                "kind": "codex",
                "binary": "codex",
                "profile_map": {},
                "extra_args": [],
                "cwd_flag": "-C",
                "prompt_via_stdin": True,
                "output_flag": "-o",
            },
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
            "provider": {
                "kind": "codex",
                "binary": "codex",
                "profile_map": {"balanced": "m", "deep": "h"},
                "extra_args": [],
                "cwd_flag": "-C",
                "prompt_via_stdin": True,
                "output_flag": "-o",
            },
            "docs": {
                "language": "en",
            },
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
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
            "provider": {
                "kind": "codex",
                "binary": "codex",
                "profile_map": {"balanced": "m", "deep": "h"},
                "extra_args": [],
                "cwd_flag": "-C",
                "prompt_via_stdin": True,
                "output_flag": "-o",
            },
            "docs": {
                "language": "zh",
            },
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
            },
            "gates": {
                "commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
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
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_accepts_legacy_config_without_docs(self) -> None:
        payload = {
            "project_name": "demo",
            "provider": {
                "kind": "codex",
                "binary": "codex",
                "profile_map": {"balanced": "m", "deep": "h"},
                "extra_args": [],
                "cwd_flag": "-C",
                "prompt_via_stdin": True,
                "output_flag": "-o",
            },
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
            },
            "gates": {
                "commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
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
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_accepts_structured_provider_profiles(self) -> None:
        payload = {
            "project_name": "demo",
            "provider": {
                "kind": "copilot-cli",
                "binary": "copilot",
                "profile_map": {"balanced": "fast", "deep": "quality", "max": "quality"},
                "profiles": {
                    "fast": {
                        "copilot-cli": {
                            "model": "claude-sonnet-4.5",
                            "effort_level": "medium",
                        }
                    },
                    "quality": {
                        "copilot-cli": {
                            "model": "claude-opus-4.5",
                            "effort_level": "high",
                            "allow_all_tools": True,
                        }
                    },
                },
                "extra_args": [],
                "cwd_flag": "-C",
                "prompt_via_stdin": True,
                "output_flag": "-o",
            },
            "docs": {
                "language": "en",
            },
            "efforts": {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "implement": "balanced",
                "review": "deep",
                "verify": "balanced",
            },
            "gates": {
                "commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
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
                    "implement": 2,
                    "review": 2,
                },
            },
        }

        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validation_report_warns_when_no_verification_commands_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            report = validation_report(project_root)
            self.assertTrue(any("no verification commands" in item for item in report["warnings"]))

    def test_validation_report_warns_for_legacy_flat_provider_profile_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")

            payload = load_project_config(project_root).to_dict()
            payload["provider"]["kind"] = "copilot-cli"
            payload["provider"]["binary"] = "copilot"
            payload["provider"]["profile_map"] = {
                "balanced": "claude-sonnet-4.5",
                "deep": "claude-opus-4.5",
                "max": "claude-opus-4.6",
            }
            if "profiles" in payload["provider"]:
                del payload["provider"]["profiles"]
            write_json(config_path(project_root), payload)

            report = validation_report(project_root)

            self.assertTrue(report["ok"])
            self.assertTrue(any("legacy flat mapping" in item for item in report["warnings"]))

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
                    return {"status": "completed"}

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

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertTrue(calls["print_agent_output"])
            self.assertTrue(calls["has_stream"])

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

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(calls["doc_language"], "zh")

    def test_cli_run_prints_utf8_json_for_chinese_stage_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            spec_file = project_root / "spec.md"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(spec_file, "# Spec\n")

            class FakeState:
                def to_dict(self):
                    return {
                        "status": "completed",
                        "stage_summaries": {
                            "clarify": "聚焦核心目标",
                        },
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
            self.assertIn("聚焦核心目标", rendered)
            self.assertNotIn("\\u805a\\u7126", rendered)
            payload = json.loads(rendered)
            self.assertEqual(payload["stage_summaries"]["clarify"], "聚焦核心目标")

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
                    write_text(request.output_path, "clarified scope\n")
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary="clarified scope",
                        returncode=0,
                    )

            orchestrator.adapter = ClarifyOnlyAdapter()
            state = orchestrator.run(spec_file=spec_file)

            self.assertEqual(state.status, "paused")
            rendered = stream.getvalue()
            self.assertIn("[stage:clarify] start model=mock", rendered)

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
                            "test_strategy": "unittest",
                            "verification_commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
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

            with patch("auto_agents.adapters.codex.subprocess.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    args=["codex"],
                    returncode=0,
                    stdout=(
                        '{"type":"thread.started","thread_id":"t"}\n'
                        '{"type":"item.completed","item":{"type":"agent_message","text":"final summary"}}\n'
                        '{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":50,"output_tokens":25}}\n'
                    ),
                    stderr="",
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

    def test_codex_adapter_supports_structured_profile_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            output_path = project_root / "agent.md"
            provider = ProviderConfig(
                kind="codex",
                binary="codex",
                profile_map={"deep": "quality"},
                profiles={
                    "quality": {
                        "codex": {
                            "codex_profile": "xh",
                            "model": "gpt-5.3-codex",
                            "args": ["--sandbox", "workspace-write"],
                        }
                    }
                },
                extra_args=[],
            )
            adapter = CodexAdapter(provider)
            request = AgentRequest(
                stage="clarify",
                effort="deep",
                prompt="prompt",
                cwd=project_root,
                output_path=output_path,
            )

            with patch("auto_agents.adapters.codex.subprocess.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    args=["codex"],
                    returncode=0,
                    stdout='{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
                    stderr="",
                )
                result = adapter.run(request)

            called_command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
            self.assertIn("--profile", called_command)
            self.assertIn("xh", called_command)
            self.assertIn("--model", called_command)
            self.assertIn("gpt-5.3-codex", called_command)
            self.assertIn("--sandbox", called_command)
            self.assertEqual(result.model, "gpt-5.3-codex")

    def test_copilot_cli_adapter_uses_structured_profile_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            output_path = project_root / "agent.md"
            provider = ProviderConfig(
                kind="copilot-cli",
                binary="copilot",
                profile_map={"deep": "quality"},
                profiles={
                    "quality": {
                        "copilot-cli": {
                            "model": "claude-opus-4.5",
                            "effort_level": "high",
                            "allow_tools": ["shell(git:*)"],
                            "deny_tools": ["shell(git push)"],
                            "args": ["--stream=off"],
                        }
                    }
                },
                extra_args=[],
            )
            adapter = CopilotCliAdapter(provider)
            request = AgentRequest(
                stage="plan",
                effort="deep",
                prompt="generate plan",
                cwd=project_root,
                output_path=output_path,
            )

            with patch("auto_agents.adapters.copilot_cli.subprocess.run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    args=["copilot"],
                    returncode=0,
                    stdout="copilot output\n",
                    stderr="",
                )
                result = adapter.run(request)

            called_command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
            self.assertIn("--model", called_command)
            self.assertIn("claude-opus-4.5", called_command)
            self.assertTrue(any(item.startswith("--allow-tool=shell(git:*)") for item in called_command))
            self.assertTrue(any(item.startswith("--deny-tool=shell(git push)") for item in called_command))
            self.assertIn("--stream=off", called_command)
            self.assertTrue(any(item.startswith("--config-dir=") for item in called_command))
            self.assertEqual(result.summary, "copilot output")

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
            self.assertIn("must run inside that env via 'conda run -p ./.conda ...'", prompt)
            self.assertIn("Do not include bare 'python', 'python3', 'pytest', 'coverage', or 'pip'", prompt)

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
