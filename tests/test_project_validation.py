import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.cli import main
from auto_agents.config import config_path, load_project_config, load_run_state, save_run_state, task_plan_path
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult
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

    def test_validation_report_warns_when_no_verification_commands_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            report = validation_report(project_root)
            self.assertTrue(any("no verification commands" in item for item in report["warnings"]))

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
            idea_file = project_root / "idea.md"
            write_text(idea_file, "# Idea\n")

            orchestrator = Orchestrator(project_root)

            class FailingIfCalledAdapter:
                def run(self, request):
                    raise AssertionError(f"adapter should not be called during failed preflight: {request.stage}")

            orchestrator.adapter = FailingIfCalledAdapter()

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator.run(idea_file=idea_file)

            self.assertIn("preflight validation failed", str(ctx.exception))
            state = load_run_state(project_root)
            self.assertEqual(state.status, "failed")
            self.assertIn("preflight validation failed", state.last_error)

    def test_cli_run_returns_nonzero_json_when_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_text(config_path(project_root), "{broken\n")
            idea_file = project_root / "idea.md"
            write_text(idea_file, "# Idea\n")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["run", "--project", str(project_root), "--idea-file", str(idea_file)])

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

    def test_cli_run_defaults_idea_file_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(["run", "--project", str(project_root)])

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn(str(project_root / "idea.md"), payload["error"])

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
            idea_file = project_root / "idea.md"
            idea_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(idea_file, "# Idea\n")
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
                        ["run", "--project", str(project_root), "--idea-file", str(idea_file), "--print-agent-output"]
                    )

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertTrue(calls["print_agent_output"])
            self.assertTrue(calls["has_stream"])

    def test_cli_run_passes_document_language_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            idea_file = project_root / "idea.md"
            idea_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(idea_file, "# Idea\n")
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
                        ["run", "--project", str(project_root), "--idea-file", str(idea_file), "--doc-language", "zh"]
                    )

            payload = json.loads(buffer.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(calls["doc_language"], "zh")

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

    def test_run_can_persist_document_language_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            idea_file = project_root / "idea.md"
            write_text(idea_file, "# Idea\n")
            state = load_run_state(project_root)
            state.status = "completed"
            save_run_state(project_root, state)

            orchestrator = Orchestrator(project_root)
            orchestrator.run(idea_file=idea_file, doc_language="zh")

            config = load_project_config(project_root)
            self.assertEqual(config.docs.language, "zh")

    def test_clarify_prompt_uses_selected_document_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock", doc_language="zh")
            orchestrator = Orchestrator(project_root)
            idea_file = project_root / "idea.md"
            write_text(idea_file, "# Idea\n")

            prompt = orchestrator._build_prompt("clarify", idea_file)

            self.assertIn("Simplified Chinese", prompt)

    def test_readme_prompt_uses_selected_document_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock", doc_language="zh")
            orchestrator = Orchestrator(project_root)
            idea_file = project_root / "idea.md"
            write_text(idea_file, "# Idea\n")

            prompt = orchestrator._build_prompt("readme", idea_file)

            self.assertIn("Simplified Chinese", prompt)
            self.assertIn(str(project_root / "README.md"), prompt)

    def test_mock_readme_stage_updates_project_readme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            idea_file = project_root / "idea.md"
            write_text(idea_file, "# Idea\n")

            state = load_run_state(project_root)
            state = orchestrator._run_readme(state, idea_file)

            readme = (project_root / "README.md").read_text(encoding="utf-8")
            self.assertEqual(state.current_stage, "readme")
            self.assertIn("## Overview", readme)
            self.assertIn("## Usage", readme)


if __name__ == "__main__":
    unittest.main()
