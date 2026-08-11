"""Tests for the lightweight Session (fix / collab) workflows."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    clear_sessions,
    create_session,
    delete_session,
    list_sessions,
    load_session_state,
    load_run_state,
    provider_references_lock_path,
    requirements_trace_path,
    save_session_state,
    save_run_state,
    session_state_path,
)
from auto_agents.git_ops import commit_all, working_tree_clean
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import (
    AgentResult,
    CommandResult,
    DEFAULT_SESSION_MAX_ATTEMPTS,
    GateParallelGroup,
    GateResult,
    RunState,
    SessionState,
    TaskSpec,
    VerificationStep,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.requirements import load_requirements_trace, stamp_requirement_contract_hashes
from auto_agents.session import Session


def _make_project(tmp: str, name: str = "demo") -> Path:
    project_root = Path(tmp) / name
    Orchestrator.init_project(project_root, name, "mock")
    # Mark as completed so session workflows can operate
    from auto_agents.config import load_run_state, save_run_state
    state = load_run_state(project_root)
    state.status = "completed"
    save_run_state(project_root, state)
    return project_root


def _make_provider_blocked_project(tmp: str, name: str = "demo") -> tuple[Path, str]:
    project_root = Path(tmp) / name
    Orchestrator.init_project(project_root, name, "mock")
    spec_file = project_root / "spec.md"
    spec_file.write_text("# Spec\n", encoding="utf-8")
    reference = ".auto-agents/docs/provider_references/provider.md"
    write_json(
        requirements_trace_path(project_root),
        {
            "version": 1,
            "requirements": [
                {
                    "id": "REQ-001",
                    "text": "Use verified provider documentation.",
                    "source": "spec",
                    "status": "active",
                    "priority": "mandatory",
                    "acceptance_oracles": ["provider reference is resolved"],
                    "oracle_type": "deterministic_test",
                    "oracle_strength": "behavioral",
                    "evidence_boundary": "internal_state",
                    "forbidden_proxy_oracles": [],
                    "forbidden_patterns": [],
                    "external_docs_required": True,
                    "provider_reference": reference,
                    "notes": "",
                }
            ],
        },
    )
    write_text(
        project_root / reference,
        "# Provider Reference\n\n## Status\n\nambiguous\n",
    )
    write_json(
        provider_references_lock_path(project_root),
        {
            "version": 1,
            "references": {
                "provider": {
                    "path": reference,
                    "status": "ambiguous",
                    "retrieved_at": "2026-04-24T00:00:00Z",
                    "source_urls": ["https://example.com/official"],
                    "notes": "Needs a user decision.",
                }
            },
        },
    )
    state = load_run_state(project_root)
    state.status = "failed"
    state.stage_summaries = {
        "clarify": "done",
        "design": "done",
        "plan": "done",
    }
    state.last_error = (
        "provider research is blocked; provide official docs, defer the requirement, "
        "choose another provider, or explicitly approve assumptions before resuming.\n"
        f"- REQ-001: {reference} is ambiguous"
    )
    state.resume_context = {
        "spec_file": str(spec_file),
        "auto_approve": True,
        "allow_dirty_tree": False,
        "max_tasks": None,
        "skip_validate": False,
        "print_agent_output": False,
        "provider_kind": "",
        "doc_language": "",
    }
    save_run_state(project_root, state)
    return project_root, reference


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


class SessionStateModelTests(unittest.TestCase):
    """Test SessionState serialization round-trip."""

    def test_round_trip(self) -> None:
        state = SessionState(
            session_id="abc123",
            mode="fix",
            status="conversing",
            goal="Button does not work",
            conversation=[{"role": "user", "content": "hello"}],
            execution_log=[{"attempt": 1, "action": "fix", "result": "ok", "timestamp": "t"}],
            current_attempt=1,
            max_attempts=4,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:01Z",
        )
        data = state.to_dict()
        restored = SessionState.from_dict(data)
        self.assertEqual(restored.session_id, "abc123")
        self.assertEqual(restored.mode, "fix")
        self.assertEqual(restored.goal, "Button does not work")
        self.assertEqual(len(restored.conversation), 1)
        self.assertEqual(len(restored.execution_log), 1)
        self.assertEqual(restored.current_attempt, 1)
        self.assertEqual(restored.max_attempts, 4)

    def test_defaults(self) -> None:
        state = SessionState(session_id="x")
        self.assertEqual(state.mode, "fix")
        self.assertEqual(state.status, "conversing")
        self.assertEqual(state.conversation, [])
        self.assertEqual(state.max_attempts, 4)

    def test_json_round_trip(self) -> None:
        state = SessionState(session_id="j1", mode="collab", goal="test")
        blob = json.dumps(state.to_dict())
        restored = SessionState.from_dict(json.loads(blob))
        self.assertEqual(restored.session_id, "j1")
        self.assertEqual(restored.mode, "collab")


class RunStateModelTests(unittest.TestCase):
    """Test RunState serialization for resume context."""

    def test_resume_context_round_trip(self) -> None:
        state = RunState(
            run_id="run-123",
            status="failed",
            current_stage="provider_research",
            implement_verify_baseline_failures=["tests/test_demo.py::test_example"],
            implement_verify_baseline_ref="deadbeef:e3b0c442",
            plan_task_replacements={"task-legacy": ["task-child-a", "task-child-b"]},
            last_recovery_route={
                "task_id": "task-child-a",
                "lineage_id": "task-child-a",
                "outcome": "requeued",
                "epoch": 1,
                "round": 2,
            },
            last_error="provider research is blocked",
            resume_context={
                "spec_file": "/tmp/demo/spec.md",
                "auto_approve": True,
                "allow_dirty_tree": False,
                "max_tasks": 5,
                "skip_validate": False,
                "print_agent_output": True,
                "provider_kind": "copilot-cli",
                "doc_language": "zh-CN",
            },
        )
        restored = RunState.from_dict(state.to_dict())
        self.assertEqual(restored.run_id, "run-123")
        self.assertEqual(restored.current_stage, "provider_research")
        self.assertEqual(
            restored.implement_verify_baseline_failures,
            ["tests/test_demo.py::test_example"],
        )
        self.assertEqual(restored.implement_verify_baseline_ref, "deadbeef:e3b0c442")
        self.assertEqual(
            restored.plan_task_replacements,
            {"task-legacy": ["task-child-a", "task-child-b"]},
        )
        self.assertEqual(restored.last_recovery_route["outcome"], "requeued")
        self.assertEqual(restored.last_recovery_route["epoch"], 1)
        self.assertEqual(restored.resume_context["spec_file"], "/tmp/demo/spec.md")
        self.assertEqual(restored.resume_context["provider_kind"], "copilot-cli")

    def test_resume_context_defaults_to_empty_dict(self) -> None:
        restored = RunState.from_dict({"run_id": "run-456"})
        self.assertEqual(restored.resume_context, {})
        self.assertEqual(restored.last_recovery_route, {})

    def test_task_recovery_lineage_round_trip(self) -> None:
        task = TaskSpec(
            task_id="task-271b",
            title="Split child",
            description="Implement the split slice.",
            acceptance=["The observable proof passes."],
            parent_task_id="task-271",
            split_depth=1,
            task_origin="scope_split",
            recovery_epoch=2,
            recovery_round=1,
            verify_retry_epoch=3,
        )

        restored = TaskSpec.from_dict(task.to_dict())

        self.assertEqual(restored.task_origin, "scope_split")
        self.assertEqual(restored.recovery_epoch, 2)
        self.assertEqual(restored.recovery_round, 1)
        self.assertEqual(restored.verify_retry_epoch, 3)


class SessionConfigTests(unittest.TestCase):
    """Test session config helpers (create, load, save, list)."""

    def test_create_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            self.assertEqual(state.mode, "fix")
            self.assertEqual(state.status, "conversing")
            self.assertEqual(state.max_attempts, DEFAULT_SESSION_MAX_ATTEMPTS["fix"])
            self.assertTrue(len(state.session_id) > 0)

            loaded = load_session_state(project_root, state.session_id)
            self.assertEqual(loaded.session_id, state.session_id)

    def test_save_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "collab")
            state.goal = "Test video generation"
            state.status = "executing"
            save_session_state(project_root, state)

            loaded = load_session_state(project_root, state.session_id)
            self.assertEqual(loaded.goal, "Test video generation")
            self.assertEqual(loaded.status, "executing")

    def test_create_provider_resolve_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "provider_resolve")
            self.assertEqual(state.mode, "provider_resolve")
            self.assertEqual(state.max_attempts, DEFAULT_SESSION_MAX_ATTEMPTS["provider_resolve"])

    def test_list_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            create_session(project_root, "fix")
            create_session(project_root, "collab")
            sessions = list_sessions(project_root)
            self.assertEqual(len(sessions), 2)
            modes = {s.mode for s in sessions}
            self.assertEqual(modes, {"fix", "collab"})

    def test_list_sessions_sorted_by_updated_at_desc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            older = create_session(project_root, "fix")
            older.updated_at = "2026-01-01T00:00:00+00:00"
            save_session_state(project_root, older)
            newer = create_session(project_root, "collab")
            newer.updated_at = "2026-01-02T00:00:00+00:00"
            save_session_state(project_root, newer)

            sessions = list_sessions(project_root)

            self.assertEqual([s.session_id for s in sessions], [newer.session_id, older.session_id])

    def test_delete_session_removes_only_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            keep = create_session(project_root, "fix")
            delete_me = create_session(project_root, "collab")

            delete_session(project_root, delete_me.session_id)

            remaining = list_sessions(project_root)
            self.assertEqual([s.session_id for s in remaining], [keep.session_id])
            self.assertFalse(session_state_path(project_root, delete_me.session_id).exists())

    def test_clear_sessions_removes_all_session_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            create_session(project_root, "fix")
            create_session(project_root, "collab")

            deleted = clear_sessions(project_root)

            self.assertEqual(deleted, 2)
            self.assertEqual(list_sessions(project_root), [])

    def test_load_nonexistent_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            with self.assertRaises(FileNotFoundError):
                load_session_state(project_root, "nonexistent")


class SessionFixFlowTests(unittest.TestCase):
    """Test the fix mode workflow with mock adapter."""

    def _make_session(self, project_root: Path, user_inputs: List[str]) -> Session:
        inputs = iter(user_inputs)
        orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))
        return Session(orchestrator, mode="fix")

    def test_fix_flow_goal_clear_immediate(self) -> None:
        """Agent says GOAL_CLEAR on first round, then fix executes."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            # User inputs: bug description, then nothing needed after GOAL_CLEAR
            user_inputs = [
                "The submit button crashes the app",  # initial bug description
            ]
            inputs = iter(user_inputs)

            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            # Patch the mock adapter to return GOAL_CLEAR on converse, and success on fix
            original_run = orchestrator.adapter.run
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                result = original_run(request)
                # First call is converse - return GOAL_CLEAR
                if call_count["n"] == 1:
                    content = "I understand the bug. The submit button crash is likely in handlers.py.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                # Subsequent calls are fix execution
                content = "Fixed the crash by adding null check.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run

            session = Session(orchestrator, mode="fix")
            state = session.start()

            # With no gate commands configured, verification passes immediately
            self.assertEqual(state.status, "completed")
            self.assertGreater(len(state.conversation), 0)
            self.assertEqual(state.mode, "fix")

    def test_fix_flow_multi_round_converse(self) -> None:
        """Agent asks a question, user answers, then GOAL_CLEAR."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = [
                "The login page is broken",    # initial description
                "It happens on Chrome only",   # answer to agent question
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            original_run = orchestrator.adapter.run
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "What browser does this happen in?\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 2:
                    content = "Got it, Chrome-specific CSS issue.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                content = "Fixed Chrome CSS issue.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")
            # Should have user, agent, user, agent(GOAL_CLEAR) in conversation
            self.assertGreaterEqual(len(state.conversation), 3)

    def test_fix_flow_prints_thinking_after_multiline_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            user_inputs = [
                "The login page is broken",
                "It happens on Chrome only",
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "What browser does this happen in?\n"
                elif call_count["n"] == 2:
                    content = "Got it, Chrome-specific CSS issue.\nGOAL_CLEAR\n"
                else:
                    content = "Fixed Chrome CSS issue.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            captured = io.StringIO()
            with redirect_stderr(captured):
                state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(captured.getvalue().count("Agent is thinking, please wait..."), 2)

    def test_fix_flow_commits_completed_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            user_inputs = [
                "The submit button crashes the app",
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand the bug. The submit button crash is likely in handlers.py.\nGOAL_CLEAR\n"
                else:
                    content = "Fixed the crash by adding null check.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertTrue(working_tree_clean(project_root))

            state_path = session_state_path(project_root, state.session_id)
            show = subprocess.run(
                ["git", "show", f"HEAD:{state_path.relative_to(project_root).as_posix()}"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            committed = json.loads(show.stdout)
            self.assertEqual(committed["status"], "completed")

    def test_fix_flow_commit_message_uses_agent_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            user_inputs = [
                (
                    "When users submit the signup form without selecting a plan, the app crashes "
                    "after several redirects and the saved draft restore flow also breaks."
                ),
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand the bug and can reproduce it.\nGOAL_CLEAR\n"
                else:
                    content = (
                        "## Fix Plan\n"
                        "1. Reproduce the crash.\n"
                        "2. Patch the missing plan guard.\n"
                        "3. Add regression coverage.\n\n"
                        "Added a plan guard so empty-plan signups no longer crash.\n"
                    )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")

            log = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                log.stdout.strip(),
                "fix: Added a plan guard so empty-plan signups no longer crash",
            )

    def test_fix_flow_commit_message_skips_verification_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            user_inputs = [
                "未选择套餐时，注册流程会在恢复草稿后崩溃。",
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "我已经理解问题。\nGOAL_CLEAR\n"
                else:
                    content = (
                        "已修复未选择套餐时的空指针崩溃。\n"
                        "验证已通过：\n"
                        "- 已补充回归测试\n"
                        "- 已手动验证草稿恢复流程\n"
                    )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")

            log = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                log.stdout.strip(),
                "fix: 已修复未选择套餐时的空指针崩溃",
            )

    def test_fix_flow_commit_message_skips_verification_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            user_inputs = [
                "修复测试引导阶段的 conda 环境校验失败。",
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "我已经理解问题。\nGOAL_CLEAR\n"
                else:
                    content = (
                        "修复了测试引导阶段的 conda 环境校验失败。\n"
                        "conda run -p ./.conda python -m unittest\n"
                    )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")

            log = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                log.stdout.strip(),
                "fix: 修复了测试引导阶段的 conda 环境校验失败",
            )

    def test_fix_flow_commit_message_uses_structured_marker(self) -> None:
        """Agent's explicit COMMIT_MESSAGE line is preferred over prose lines."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            user_inputs = ["Fix the null pointer in public voice handler."]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "Understood.\nGOAL_CLEAR\n"
                else:
                    content = (
                        "我做了最小修复：在 [app/application/public_voice.py]"
                        "(/home/fuli/projects/sdgp/app/application/public_voice.py) "
                        "添加了空指针保护。\n"
                        "COMMIT_MESSAGE: 修复公共语音处理器中的空指针异常\n"
                    )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")
            log = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=str(project_root), check=True, text=True, capture_output=True,
            )
            self.assertEqual(
                log.stdout.strip(),
                "fix: 修复公共语音处理器中的空指针异常",
            )

    def test_fix_flow_commit_message_rejects_markdown_link_line(self) -> None:
        """Without a COMMIT_MESSAGE marker, mid-URL truncated markdown-link
        lines must be discarded in favor of a cleaner fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            user_inputs = ["Fix the null pointer in public voice handler."]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "Understood.\nGOAL_CLEAR\n"
                else:
                    content = (
                        "修复公共语音处理器中的空指针异常。\n"
                        "我做了最小修复：在 [app/application/public_voice.py]"
                        "(/home/fuli/projects/sdgp/app/application/public_voice.py) "
                        "添加了空指针保护。\n"
                    )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")
            log = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=str(project_root), check=True, text=True, capture_output=True,
            )
            subject = log.stdout.strip()
            self.assertNotIn("](", subject)
            self.assertNotIn("[", subject)
            self.assertNotIn("/sdgp/", subject)
            self.assertEqual(subject, "fix: 修复公共语音处理器中的空指针异常")


class SessionCollabFlowTests(unittest.TestCase):
    """Test the collab mode workflow with mock adapter."""

    def test_collab_goal_achieved(self) -> None:
        """Agent achieves goal, user confirms."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = [
                "Generate a test video using the frontend",  # initial goal
                "y",  # confirm goal achieved
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            original_run = orchestrator.adapter.run
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand, you want to test video generation.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                content = "Set up test harness and generated video.\nGOAL_ACHIEVED: Test video generated successfully at output/test.mp4\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="collab")
            state = session.start()

            self.assertEqual(state.status, "completed")

    def test_collab_verification_failure_honors_hard_ceiling_for_markers(self) -> None:
        for marker in (
            "GOAL_ACHIEVED: implementation complete",
            "BUG_FOUND: implementation bug fixed",
        ):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                project_root = _make_project(tmp)
                orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")
                state = SessionState(
                    session_id="bounded-collab",
                    mode="collab",
                    status="executing",
                    max_attempts=10,
                    hard_ceiling=2,
                )
                session = Session(orchestrator, mode="collab")

                with (
                    patch.object(session, "_call_agent", return_value=marker) as call_agent,
                    patch.object(
                        session,
                        "_run_verify",
                        return_value={"ok": False, "reason": "unrelated gate failure"},
                    ),
                    patch.object(session, "_compute_diff_hash", return_value="same-diff"),
                ):
                    result = session._phase_collab_loop(state)

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.current_attempt, 2)
                self.assertEqual(call_agent.call_count, 2)

    def test_collab_verification_uses_preexisting_failure_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")
            session = Session(orchestrator, mode="collab")
            session._current_state = SessionState(session_id="collab-baseline", mode="collab")

            with (
                patch.object(
                    session,
                    "_run_baseline_diff_verify",
                    return_value={"ok": True, "reason": "baseline-only failures"},
                ) as baseline_verify,
                patch.object(
                    orchestrator,
                    "_run_task_verify",
                    side_effect=AssertionError("collab must not use absolute full-gate failures"),
                ),
            ):
                result = session._run_verify()

            self.assertTrue(result["ok"])
            baseline_verify.assert_called_once_with(scope="final")

    def test_collab_captures_gate_baseline_before_first_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "y")
            state = SessionState(
                session_id="collab-baseline-capture",
                mode="collab",
                status="executing",
            )
            session = Session(orchestrator, mode="collab")
            orchestrator.config.gates.commands = ["test gate"]

            with (
                patch.object(session, "_ensure_baseline") as ensure_baseline,
                patch.object(
                    session,
                    "_call_agent",
                    return_value="GOAL_ACHIEVED: implementation complete",
                ),
                patch.object(
                    session,
                    "_run_verify",
                    return_value={"ok": True, "reason": "all commands passed"},
                ),
                patch.object(session, "_git_commit", return_value=True),
                patch.object(session, "_release_baseline"),
            ):
                result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "completed")
            ensure_baseline.assert_called_once_with(state)

    def test_collab_applies_generated_config_before_resolving_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "y")
            state = SessionState(session_id="collab-config-sync", mode="collab")
            session = Session(orchestrator, mode="collab")
            events = []
            plan = SimpleNamespace(commands=["test gate"], parallel_groups=[])

            with (
                patch.object(
                    orchestrator,
                    "_apply_generated_verification_config",
                    side_effect=lambda: events.append("apply"),
                ),
                patch.object(
                    session,
                    "_session_gate_plan",
                    side_effect=lambda _scope: events.append("plan") or plan,
                ),
                patch.object(
                    session,
                    "_ensure_baseline",
                    side_effect=lambda _state: events.append("baseline"),
                ),
                patch.object(
                    session,
                    "_call_agent",
                    return_value="GOAL_ACHIEVED: implementation complete",
                ),
                patch.object(
                    session,
                    "_run_verify",
                    return_value={"ok": True, "reason": "passed"},
                ),
                patch.object(session, "_git_commit", return_value=True),
                patch.object(session, "_release_baseline"),
            ):
                result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "completed")
            self.assertEqual(events[:3], ["apply", "plan", "baseline"])

    def test_collab_general_progress_runs_final_only_after_user_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "y")
            state = SessionState(session_id="collab-two-tier", mode="collab")
            session = Session(orchestrator, mode="collab")

            with (
                patch.object(orchestrator, "_apply_generated_verification_config"),
                patch.object(session, "_call_agent", return_value="Implemented the change."),
                patch.object(
                    session,
                    "_run_verify",
                    side_effect=[
                        {"ok": True, "reason": "progress passed", "scope": "progress"},
                        {"ok": True, "reason": "final passed", "scope": "final"},
                    ],
                ) as verify,
                patch.object(session, "_git_commit", return_value=True),
                patch.object(session, "_release_baseline"),
            ):
                result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                [call.kwargs["scope"] for call in verify.call_args_list],
                ["progress", "final"],
            )
            self.assertEqual(
                [entry["verification_scope"] for entry in state.execution_log if entry["action"].endswith("verify")],
                ["progress", "final"],
            )

    def test_collab_final_failure_after_confirmation_does_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "y")
            state = SessionState(
                session_id="collab-final-failure",
                mode="collab",
                hard_ceiling=1,
            )
            session = Session(orchestrator, mode="collab")

            with (
                patch.object(orchestrator, "_apply_generated_verification_config"),
                patch.object(session, "_call_agent", return_value="Implemented the change."),
                patch.object(
                    session,
                    "_run_verify",
                    side_effect=[
                        {"ok": True, "reason": "progress passed", "scope": "progress"},
                        {"ok": False, "reason": "final failed", "scope": "final"},
                    ],
                ),
                patch.object(session, "_git_commit") as git_commit,
            ):
                result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "failed")
            git_commit.assert_not_called()

    def test_collab_marker_scopes_bug_as_progress_and_goal_as_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "y")
            state = SessionState(session_id="collab-marker-scopes", mode="collab")
            session = Session(orchestrator, mode="collab")

            with (
                patch.object(orchestrator, "_apply_generated_verification_config"),
                patch.object(
                    session,
                    "_call_agent",
                    side_effect=[
                        "BUG_FOUND: fixed an intermediate defect",
                        "GOAL_ACHIEVED: all work complete",
                    ],
                ),
                patch.object(
                    session,
                    "_run_verify",
                    side_effect=[
                        {"ok": True, "reason": "progress passed"},
                        {"ok": True, "reason": "final passed"},
                    ],
                ) as verify,
                patch.object(session, "_commit_verified_progress", return_value=False),
                patch.object(session, "_git_commit", return_value=True),
                patch.object(session, "_release_baseline"),
            ):
                result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                [call.kwargs["scope"] for call in verify.call_args_list],
                ["progress", "final"],
            )

    def test_collab_need_user_assist(self) -> None:
        """Agent requests user assistance, then achieves goal."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = [
                "Test the video player in browser",          # initial goal
                "I opened the browser and see the player",   # user assist response
                "y",                                          # confirm goal achieved
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I'll help test the video player.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 2:
                    content = "I've set up the test server.\nNEED_USER_ASSIST: Please open http://localhost:3000 and check the player\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                content = "Based on your feedback, everything works.\nGOAL_ACHIEVED: Video player verified working in browser\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="collab")
            state = session.start()

            self.assertEqual(state.status, "completed")
            # Check that NEED_USER_ASSIST triggered waiting_user
            assist_entries = [e for e in state.execution_log if e.get("action") == "collab"]
            self.assertGreater(len(assist_entries), 0)

    def test_collab_prints_thinking_after_user_assist_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = [
                "Test the video player in browser",
                "I opened the browser and see the player",
                "y",
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I'll help test the video player.\nGOAL_CLEAR\n"
                elif call_count["n"] == 2:
                    content = "I've set up the test server.\nNEED_USER_ASSIST: Please open http://localhost:3000 and check the player\n"
                else:
                    content = "Based on your feedback, everything works.\nGOAL_ACHIEVED: Video player verified working in browser\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="collab")
            captured = io.StringIO()
            with redirect_stderr(captured):
                state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(captured.getvalue().count("Agent is thinking, please wait..."), 2)

    def test_collab_flow_commits_completed_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)

            user_inputs = [
                "Generate a test video using the frontend",
                "y",
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand, you want to test video generation.\nGOAL_CLEAR\n"
                else:
                    content = "Set up test harness and generated video.\nGOAL_ACHIEVED: Test video generated successfully at output/test.mp4\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="collab")
            state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertTrue(working_tree_clean(project_root))

            state_path = session_state_path(project_root, state.session_id)
            show = subprocess.run(
                ["git", "show", f"HEAD:{state_path.relative_to(project_root).as_posix()}"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            committed = json.loads(show.stdout)
            self.assertEqual(committed["status"], "completed")

    def test_collab_commits_verified_progress_before_final_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            commit_all(project_root, "chore: baseline")

            app_file = project_root / "app.py"
            app_file.write_text("value = 0\n", encoding="utf-8")
            commit_all(project_root, "feat: add app stub")

            user_inputs = [
                "Make the browser flow work end-to-end",
                "n",
                "The first fix helped, but one more issue remains",
                "y",
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand the goal.\nGOAL_CLEAR\n"
                elif call_count["n"] == 2:
                    app_file.write_text("value = 1\n", encoding="utf-8")
                    content = "Applied the first browser fix.\nGOAL_ACHIEVED: The main flow now works\n"
                else:
                    app_file.write_text("value = 2\n", encoding="utf-8")
                    content = "Applied the final follow-up fix.\nGOAL_ACHIEVED: The browser flow is fully working\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="collab")
            state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertTrue(working_tree_clean(project_root))

            rev_list = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(rev_list.stdout.strip(), "4")


class SessionResumeTests(unittest.TestCase):
    """Test session resume and persistence."""

    def test_resume_conversing_session(self) -> None:
        """Resume a session that was interrupted during conversation."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            # Create and save a partially-completed session
            state = create_session(project_root, "fix")
            state.goal = "Button crash"
            state.conversation = [
                {"role": "user", "content": "Button crash"},
                {"role": "agent", "content": "Which button?"},
            ]
            save_session_state(project_root, state)

            user_inputs = [
                "The submit button on the form page",  # answer to agent question
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "Got it, submit button on form page.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                content = "Fixed the submit handler.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.resume(state.session_id)

            self.assertEqual(result.status, "completed")
            self.assertGreater(len(result.conversation), 2)
    def test_resume_completed_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            state.status = "completed"
            save_session_state(project_root, state)

            orchestrator = Orchestrator(project_root, user_input_fn=lambda _: "")
            session = Session(orchestrator, mode="fix")
            result = session.resume(state.session_id)
            self.assertEqual(result.status, "completed")

    def test_fix_not_a_bug_user_agrees(self) -> None:
        """Agent says NOT_A_BUG, user agrees → session completed with not_a_bug resolution."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = [
                "The button color changes on hover",  # initial bug description
                "y",  # agree it's not a bug
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            def mock_run(request):
                content = (
                    "After analyzing the code, the hover color change is intentional CSS behavior "
                    "defined in styles.css. This is the expected design.\n"
                    "NOT_A_BUG: Hover color change is intentional CSS behavior per design spec.\n"
                )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.resolution, "not_a_bug")
            # Should have a not_a_bug entry in execution log
            not_bug_entries = [e for e in state.execution_log if e.get("action") == "not_a_bug"]
            self.assertEqual(len(not_bug_entries), 1)
            self.assertIn("intentional", not_bug_entries[0]["result"])

    def test_fix_not_a_bug_user_disagrees(self) -> None:
        """Agent says NOT_A_BUG, user disagrees → conversation continues, then GOAL_CLEAR."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = [
                "The button color changes on hover",  # initial bug description
                "n",  # disagree with NOT_A_BUG
                "No, the color should stay blue, not red. Check the spec.",  # explanation
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = (
                        "The hover effect looks intentional.\n"
                        "NOT_A_BUG: Hover color change is expected CSS behavior.\n"
                    )
                elif call_count["n"] == 2:
                    content = (
                        "I see, you're right — the spec says the button should stay blue. "
                        "The red hover color is a bug in styles.css.\n"
                        "GOAL_CLEAR\n"
                    )
                else:
                    content = "Fixed the hover color in styles.css.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.resolution, "fixed")
            # Conversation should include the user's disagreement
            user_msgs = [m["content"] for m in state.conversation if m["role"] == "user"]
            self.assertTrue(any("blue" in m for m in user_msgs))

    def test_fix_resolution_field_persisted(self) -> None:
        """Verify the resolution field survives serialization round-trip."""
        state = SessionState(
            session_id="test-res-001",
            mode="fix",
            status="completed",
            resolution="not_a_bug",
        )
        data = state.to_dict()
        self.assertEqual(data["resolution"], "not_a_bug")

        restored = SessionState.from_dict(data)
        self.assertEqual(restored.resolution, "not_a_bug")

        # Default resolution is empty string
        state2 = SessionState(session_id="test-res-002")
        self.assertEqual(state2.resolution, "")


class SessionProviderResolveTests(unittest.TestCase):
    def test_provider_resolve_flow_updates_references_and_resumes_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, reference = _make_provider_blocked_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand the provider research blocker and can proceed.\nGOAL_CLEAR\n"
                else:
                    write_text(
                        project_root / reference,
                        "# Provider Reference\n\n## Status\n\nassumption_approved\n",
                    )
                    write_json(
                        provider_references_lock_path(project_root),
                        {
                            "version": 1,
                            "references": {
                                "provider": {
                                    "path": reference,
                                    "status": "assumption_approved",
                                    "retrieved_at": "2026-04-24T00:30:00Z",
                                    "source_urls": ["https://example.com/official"],
                                    "notes": "User approved assumptions for this iteration.",
                                }
                            },
                        },
                    )
                    content = "Updated the provider reference to assumption_approved.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            resume_calls = {"n": 0}

            def mock_resume_saved_run():
                resume_calls["n"] += 1
                resumed = load_run_state(project_root)
                resumed.status = "completed"
                return resumed

            orchestrator.adapter.run = mock_run
            orchestrator.resume_saved_run = mock_resume_saved_run

            session = Session(orchestrator, mode="provider_resolve")
            state = session.start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.resolution, "provider_research_resolved")
            self.assertEqual(resume_calls["n"], 1)
            lock_payload = json.loads(provider_references_lock_path(project_root).read_text(encoding="utf-8"))
            self.assertEqual(
                lock_payload["references"]["provider"]["status"],
                "assumption_approved",
            )

    def test_provider_resolve_rejects_and_restores_contract_field_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, _reference = _make_provider_blocked_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")
            original_trace = load_requirements_trace(project_root, normalize=False)
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                content = "I understand the blocker.\nGOAL_CLEAR\n"
                if call_count["n"] > 1:
                    changed = load_requirements_trace(project_root, normalize=False)
                    changed["requirements"][0]["source"] += "; provider conversation"
                    write_json(requirements_trace_path(project_root), changed)
                    content = "Recorded the provider decision in source.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run
            resume_calls = {"n": 0}
            orchestrator.resume_saved_run = lambda: resume_calls.__setitem__("n", resume_calls["n"] + 1)
            session = Session(orchestrator, mode="provider_resolve")
            session._should_stop = lambda _state, _reason: "stop after policy rejection"

            state = session.start()

            self.assertEqual(state.status, "failed")
            self.assertEqual(resume_calls["n"], 0)
            self.assertEqual(load_requirements_trace(project_root, normalize=False), original_trace)
            self.assertTrue(
                any(entry.get("action") == "provider_trace_rejected" for entry in state.execution_log)
            )

    def test_provider_contract_binding_rejects_a_stale_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, _reference = _make_provider_blocked_project(tmp)
            trace, _ = stamp_requirement_contract_hashes(
                load_requirements_trace(project_root, normalize=False)
            )
            trace["requirements"][0]["source"] += "; stale mutation"
            write_json(requirements_trace_path(project_root), trace)
            orchestrator = Orchestrator(project_root)

            with self.assertRaisesRegex(RuntimeError, "invalid requirements trace"):
                orchestrator.bind_resolved_provider_reference_contracts()

    def test_provider_resolve_preflight_failure_is_not_marked_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, reference = _make_provider_blocked_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                content = "I understand the blocker.\nGOAL_CLEAR\n"
                if call_count["n"] > 1:
                    write_text(
                        project_root / reference,
                        "# Provider Reference\n\n## Status\n\nassumption_approved\n",
                    )
                    write_json(
                        provider_references_lock_path(project_root),
                        {
                            "version": 1,
                            "references": {
                                "provider": {
                                    "path": reference,
                                    "status": "assumption_approved",
                                    "retrieved_at": "2026-04-24T00:30:00Z",
                                    "source_urls": ["https://example.com/official"],
                                    "notes": "User approved assumptions.",
                                }
                            },
                        },
                    )
                    content = "Resolved the provider reference.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run
            orchestrator.validate = lambda: {
                "ok": False,
                "errors": ["requirement #1 contract_sha256 is missing or stale"],
                "warnings": [],
            }
            orchestrator.resume_saved_run = lambda: self.fail("resume must not run")
            session = Session(orchestrator, mode="provider_resolve")

            with self.assertRaisesRegex(RuntimeError, "run preflight is still blocked"):
                session.start()

            self.assertIsNotNone(session._current_state)
            self.assertEqual(session._current_state.status, "failed")
            self.assertEqual(session._current_state.resolution, "preflight_blocked")

    def test_provider_resolve_allows_only_session_approved_defer_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, _reference = _make_provider_blocked_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "yes")
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand the blocker.\nGOAL_CLEAR\n"
                elif call_count["n"] == 2:
                    content = "NEED_USER_DEFER: REQ-001 | Defer this provider-dependent requirement?\n"
                else:
                    trace = load_requirements_trace(project_root, normalize=False)
                    trace["requirements"][0]["status"] = "deferred"
                    trace["requirements"][0]["notes"] = "User explicitly deferred this requirement."
                    write_json(requirements_trace_path(project_root), trace)
                    content = "Applied the approved defer decision.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run
            orchestrator.validate = lambda: {"ok": True, "errors": [], "warnings": []}

            def mock_resume_saved_run():
                resumed = load_run_state(project_root)
                resumed.status = "completed"
                return resumed

            orchestrator.resume_saved_run = mock_resume_saved_run
            state = Session(orchestrator, mode="provider_resolve").start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(
                load_requirements_trace(project_root, normalize=False)["requirements"][0]["status"],
                "deferred",
            )
            self.assertTrue(
                any(entry.get("action") == "provider_defer_approved" for entry in state.execution_log)
            )

    def test_provider_resolve_contract_change_marker_rewinds_to_clarify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, reference = _make_provider_blocked_project(tmp)
            original_reference = (project_root / reference).read_text(encoding="utf-8")
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                content = "I understand the blocker.\nGOAL_CLEAR\n"
                if call_count["n"] > 1:
                    write_text(project_root / reference, "partial provider edit\n")
                    content = "REQUIRES_CLARIFY: The provider duration changes the normative contract.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run

            def mock_resume_saved_run():
                resumed = load_run_state(project_root)
                resumed.status = "completed"
                return resumed

            orchestrator.resume_saved_run = mock_resume_saved_run
            state = Session(orchestrator, mode="provider_resolve").start()
            routed_run = load_run_state(project_root)

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.resolution, "routed_to_clarify")
            self.assertEqual(routed_run.current_stage, "clarify")
            self.assertEqual(routed_run.rejected_stage, "clarify")
            self.assertIn("normative contract", routed_run.rejection_reason)
            self.assertEqual((project_root / reference).read_text(encoding="utf-8"), original_reference)

    def test_provider_resolve_requires_blocked_provider_research_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")
            session = Session(orchestrator, mode="provider_resolve")
            with self.assertRaises(RuntimeError):
                session.start()


class SessionCLITests(unittest.TestCase):
    """Test that CLI properly parses fix/collab commands."""

    def test_fix_parser(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["fix", "--project", "/tmp/test"])
        self.assertEqual(args.command, "fix")
        self.assertEqual(args.project, "/tmp/test")

    def test_collab_parser(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["collab", "--project", "/tmp/test", "--session", "abc123"])
        self.assertEqual(args.command, "collab")
        self.assertEqual(args.session, "abc123")

    def test_fix_parser_with_provider(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["fix", "--project", "/tmp/test", "--provider", "codex"])
        self.assertEqual(args.provider, "codex")

    def test_collab_parser_print_output(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["collab", "--project", "/tmp/test", "--print-agent-output"])
        self.assertTrue(args.print_agent_output)

    def test_collab_parser_full_verify(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["collab", "--project", "/tmp/test", "--full-verify"])
        self.assertTrue(args.full_verify)

    def test_provider_resolve_parser(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["provider-resolve", "--project", "/tmp/test", "--session", "abc123"])
        self.assertEqual(args.command, "provider-resolve")
        self.assertEqual(args.project, "/tmp/test")
        self.assertEqual(args.session, "abc123")

    def test_sessions_parser(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["sessions", "--project", "/tmp/test"])
        self.assertEqual(args.command, "sessions")
        self.assertEqual(args.project, "/tmp/test")
        self.assertFalse(getattr(args, "all", False))

    def test_sessions_parser_with_mode_filter(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["sessions", "--project", "/tmp/test", "--mode", "fix", "--all"])
        self.assertEqual(args.mode, "fix")
        self.assertTrue(args.all)

    def test_sessions_parser_with_provider_resolve_mode_filter(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["sessions", "--project", "/tmp/test", "--mode", "provider-resolve"])
        self.assertEqual(args.mode, "provider-resolve")

    def test_sessions_delete_parser(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["sessions-delete", "--project", "/tmp/test", "--session", "abc123"])
        self.assertEqual(args.command, "sessions-delete")
        self.assertEqual(args.session, "abc123")

    def test_sessions_clear_parser(self) -> None:
        from auto_agents.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["sessions-clear", "--project", "/tmp/test"])
        self.assertEqual(args.command, "sessions-clear")


class SessionListCommandTests(unittest.TestCase):
    """Test the sessions list command end-to-end."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        state_dir = self.tmpdir / ".auto-agents" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "run_state.json").write_text(json.dumps({"status": "completed"}))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_sessions_list_empty(self) -> None:
        from auto_agents.cli import main
        import io
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir)])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(data, [])

    def test_sessions_list_with_active_session(self) -> None:
        from auto_agents.config import create_session
        state = create_session(self.tmpdir, "fix")
        sid = state.session_id

        from auto_agents.cli import main
        import io
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir)])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["session_id"], sid)

    def test_sessions_list_mode_filter(self) -> None:
        from auto_agents.config import create_session, save_session_state
        fix_state = create_session(self.tmpdir, "fix")
        collab_state = create_session(self.tmpdir, "collab")

        from auto_agents.cli import main
        import io
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir), "--mode", "collab"])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["mode"], "collab")

    def test_sessions_list_excludes_completed_by_default(self) -> None:
        from auto_agents.config import create_session, save_session_state
        state = create_session(self.tmpdir, "fix")
        state.status = "completed"
        save_session_state(self.tmpdir, state)

        from auto_agents.cli import main
        import io
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir)])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(data, [])

    def test_sessions_list_all_includes_completed(self) -> None:
        from auto_agents.config import create_session, save_session_state
        state = create_session(self.tmpdir, "fix")
        state.status = "completed"
        save_session_state(self.tmpdir, state)

        from auto_agents.cli import main
        import io
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir), "--all"])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "completed")

    def test_sessions_list_sorted_newest_first(self) -> None:
        from auto_agents.config import create_session, save_session_state
        older = create_session(self.tmpdir, "fix")
        older.updated_at = "2026-01-01T00:00:00+00:00"
        save_session_state(self.tmpdir, older)
        newer = create_session(self.tmpdir, "fix")
        newer.updated_at = "2026-01-02T00:00:00+00:00"
        save_session_state(self.tmpdir, newer)

        from auto_agents.cli import main
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir), "--all"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual([row["session_id"] for row in data], [newer.session_id, older.session_id])

    def test_sessions_delete_command(self) -> None:
        from auto_agents.cli import main
        state = create_session(self.tmpdir, "fix")

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with patch("auto_agents.cli._confirm_prompt", return_value="y"):
                rc = main(["sessions-delete", "--project", str(self.tmpdir), "--session", state.session_id])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_session"], state.session_id)
        self.assertFalse(session_state_path(self.tmpdir, state.session_id).exists())

    def test_sessions_delete_cancelled(self) -> None:
        from auto_agents.cli import main
        state = create_session(self.tmpdir, "fix")

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with patch("auto_agents.cli._confirm_prompt", return_value="n"):
                rc = main(["sessions-delete", "--project", str(self.tmpdir), "--session", state.session_id])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 1)
        data = json.loads(captured.getvalue())
        self.assertFalse(data["ok"])
        self.assertTrue(session_state_path(self.tmpdir, state.session_id).exists())

    def test_sessions_clear_command(self) -> None:
        from auto_agents.cli import main
        create_session(self.tmpdir, "fix")
        create_session(self.tmpdir, "collab")

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with patch("auto_agents.cli._confirm_prompt", return_value="y"):
                rc = main(["sessions-clear", "--project", str(self.tmpdir)])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_sessions"], 2)
        self.assertEqual(list_sessions(self.tmpdir), [])


class ErrorFeedbackTests(unittest.TestCase):
    """Test _build_error_feedback for stall/timeout/transient classification."""

    def _make_session(self, tmp: str) -> Session:
        project_root = _make_project(tmp)
        orchestrator = Orchestrator(project_root)
        return Session(orchestrator, mode="collab")

    def test_stall_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_session(tmp)
            err = "stderr=stalled (no output) after 300s\n--- last output ---\nPolling render status..."
            fb = sess._build_error_feedback(err)
            self.assertIn("STALLED", fb)
            self.assertIn("bounded retries", fb)
            self.assertIn("Polling render status", fb)

    def test_timeout_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_session(tmp)
            err = "stderr=timed out after 1800s\n--- last output ---\nWaiting for server..."
            fb = sess._build_error_feedback(err)
            self.assertIn("TIMED OUT", fb)
            self.assertIn("Waiting for server", fb)

    def test_transient_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_session(tmp)
            err = "stderr=Connection refused"
            fb = sess._build_error_feedback(err)
            self.assertIn("transient error", fb)


class CollabAlwaysStreamTests(unittest.TestCase):
    """Test that collab/fix modes always provide a stream_output callback."""

    def test_collab_always_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = ["Test goal", "y"]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            stream_was_set = {"value": False}
            original_failover = orchestrator._call_with_failover

            def capture_failover(request):
                stream_was_set["value"] = request.stream_output is not None
                # Return a successful result with GOAL_CLEAR then GOAL_ACHIEVED
                content = "Understood.\nGOAL_CLEAR\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator._call_with_failover = capture_failover
            session = Session(orchestrator, mode="collab", print_agent_output=False)
            # Just run converse phase to check stream_output is set
            state = session.start()
            self.assertTrue(stream_was_set["value"], "stream_output should be set in collab mode even without print_agent_output flag")


class CollabStallRetryTests(unittest.TestCase):
    """Test that a stalled agent triggers retry with diagnostic feedback."""

    def test_stall_triggers_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = [
                "Generate a test video",  # initial goal
                "y",  # confirm goal achieved
            ]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_failover(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "Understood.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 2:
                    # Simulate a stall — agent returns failure with stall indicator
                    return AgentResult(
                        ok=False, command=["mock"], output_path=request.output_path,
                        summary="", stdout="Starting server...\nPolling render...\n",
                        stderr="stalled (no output) after 300s\n--- last output ---\nPolling render...",
                        returncode=-1,
                    )
                # Third call succeeds after retry
                content = "Fixed the issue by using bounded retries.\nGOAL_ACHIEVED: Video generated successfully\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator._call_with_failover = mock_failover
            session = Session(orchestrator, mode="collab")
            state = session.start()

            self.assertEqual(state.status, "completed")
            # Verify that the stall was logged
            stall_entries = [e for e in state.execution_log if e.get("action") == "agent_error"]
            self.assertEqual(len(stall_entries), 1)
            self.assertIn("stalled", stall_entries[0]["result"])


class TailLinesTests(unittest.TestCase):
    """Test the _tail_lines helper from base.py."""

    def test_tail_lines_basic(self) -> None:
        from auto_agents.adapters.base import _tail_lines
        chunks = ["line1\n", "line2\n", "line3\n", "line4\n", "line5\n"]
        result = _tail_lines(chunks, 3)
        self.assertEqual(result, "line3\nline4\nline5")

    def test_tail_lines_fewer_than_n(self) -> None:
        from auto_agents.adapters.base import _tail_lines
        chunks = ["line1\n", "line2\n"]
        result = _tail_lines(chunks, 5)
        self.assertEqual(result, "line1\nline2")

    def test_tail_lines_empty(self) -> None:
        from auto_agents.adapters.base import _tail_lines
        result = _tail_lines([], 5)
        self.assertEqual(result, "")


class ProcessGroupKillTests(unittest.TestCase):
    """Test _kill_process_group handles various process states."""

    def test_kill_already_exited(self) -> None:
        from auto_agents.adapters.base import _kill_process_group
        # Start a process that exits immediately
        process = subprocess.Popen(
            ["true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        process.wait()
        # Should not raise even if process already exited
        _kill_process_group(process)

    def test_kill_running_process(self) -> None:
        from auto_agents.adapters.base import _kill_process_group
        process = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _kill_process_group(process)
        self.assertIsNotNone(process.returncode)


class CodexJsonStreamFilterTests(unittest.TestCase):
    """Test that CodexAdapter._make_json_stream_filter parses JSON lines correctly."""

    def test_agent_message_forwarded(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Hello world"}})
        cb("stdout", line + "\n")
        self.assertEqual(received, [("stdout", "Hello world\n")])

    def test_non_message_events_suppressed(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb("stdout", json.dumps({"type": "turn.started"}) + "\n")
        cb("stdout", json.dumps({"type": "turn.completed", "usage": {}}) + "\n")
        self.assertEqual(received, [])

    def test_error_events_forwarded_as_stderr(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb("stdout", json.dumps({"type": "error", "message": "quota exceeded"}) + "\n")
        self.assertEqual(received, [("stderr", "quota exceeded\n")])

    def test_stderr_passthrough(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb("stderr", "Reading prompt from stdin...\n")
        self.assertEqual(received, [("stderr", "Reading prompt from stdin...\n")])

    def test_non_json_forwarded_as_is(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb("stdout", "plain text output\n")
        self.assertEqual(received, [("stdout", "plain text output\n")])


# ── New convergence / resume / slim tests ────────────────────


class SessionStateNewFieldsTests(unittest.TestCase):
    """Test new convergence-related fields on SessionState."""

    def test_new_fields_defaults(self) -> None:
        state = SessionState(session_id="x")
        self.assertEqual(state.stall_count, 0)
        self.assertEqual(state.last_diff_hash, "")
        self.assertEqual(state.last_verify_sig, "")
        self.assertEqual(state.consecutive_agent_errors, 0)
        self.assertEqual(state.hard_ceiling, 15)
        self.assertEqual(state.fix_verify_command, "")
        self.assertEqual(state.baseline_failures, [])
        self.assertEqual(state.baseline_git_ref, "")

    def test_new_fields_round_trip(self) -> None:
        state = SessionState(
            session_id="rt1",
            stall_count=2,
            last_diff_hash="abc",
            last_verify_sig="def",
            consecutive_agent_errors=3,
            hard_ceiling=20,
            fix_verify_command="pytest -k test_bug",
            baseline_failures=["tests/test_a.py::test_x", "cmd:npm test"],
            baseline_git_ref="abc123",
        )
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.stall_count, 2)
        self.assertEqual(restored.last_diff_hash, "abc")
        self.assertEqual(restored.last_verify_sig, "def")
        self.assertEqual(restored.consecutive_agent_errors, 3)
        self.assertEqual(restored.hard_ceiling, 20)
        self.assertEqual(restored.fix_verify_command, "pytest -k test_bug")
        self.assertEqual(restored.baseline_failures, ["tests/test_a.py::test_x", "cmd:npm test"])
        self.assertEqual(restored.baseline_git_ref, "abc123")

    def test_backward_compat_missing_new_fields(self) -> None:
        """Old session_state.json without new fields should deserialize with defaults."""
        old_data = {
            "session_id": "old1",
            "mode": "fix",
            "status": "executing",
            "goal": "some bug",
            "conversation": [],
            "execution_log": [],
            "current_attempt": 2,
            "max_attempts": 4,
            "resolution": "",
            "created_at": "t",
            "updated_at": "t",
        }
        restored = SessionState.from_dict(old_data)
        self.assertEqual(restored.stall_count, 0)
        self.assertEqual(restored.last_diff_hash, "")
        self.assertEqual(restored.consecutive_agent_errors, 0)
        self.assertEqual(restored.hard_ceiling, 15)  # default for fix mode
        self.assertEqual(restored.fix_verify_command, "")
        self.assertEqual(restored.baseline_failures, [])
        self.assertEqual(restored.baseline_git_ref, "")


class ResumeFailedSessionTests(unittest.TestCase):
    """Test that a failed session can be resumed via --session."""

    def test_resume_failed_resets_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            state.status = "failed"
            state.goal = "Button crash"
            state.current_attempt = 4
            state.conversation = [
                {"role": "user", "content": "Button crash"},
                {"role": "agent", "content": "I see the crash.\nGOAL_CLEAR\n"},
            ]
            save_session_state(project_root, state)

            user_inputs: list = []
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                content = "Fixed the crash.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.resume(state.session_id)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.resolution, "fixed")
            # current_attempt should have been reset and incremented
            self.assertEqual(result.current_attempt, 1)

    def test_resume_completed_still_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            state.status = "completed"
            save_session_state(project_root, state)

            orchestrator = Orchestrator(project_root, user_input_fn=lambda _: "")
            session = Session(orchestrator, mode="fix")
            result = session.resume(state.session_id)
            self.assertEqual(result.status, "completed")

    def test_offer_resume_includes_failed(self) -> None:
        """offer_resume_or_new should offer to resume a failed session."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            state.status = "failed"
            state.goal = "some bug"
            state.conversation = [
                {"role": "user", "content": "some bug"},
                {"role": "agent", "content": "OK\nGOAL_CLEAR\n"},
            ]
            save_session_state(project_root, state)

            # User accepts the default recommended session, then agent fixes
            user_inputs = [""]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            def mock_run(request):
                content = "Fixed.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.offer_resume_or_new()

            self.assertEqual(result.session_id, state.session_id)
            self.assertEqual(result.status, "completed")

    def test_offer_resume_prefers_newest_unfinished_session(self) -> None:
        """Blank selection should resume the newest unfinished session."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            older = create_session(project_root, "fix")
            older.goal = "older bug"
            older.updated_at = "2026-01-01T00:00:00+00:00"
            save_session_state(project_root, older)

            newer = create_session(project_root, "fix")
            newer.status = "failed"
            newer.goal = "newer bug"
            newer.updated_at = "2026-01-02T00:00:00+00:00"
            newer.conversation = [
                {"role": "user", "content": "newer bug"},
                {"role": "agent", "content": "OK\nGOAL_CLEAR\n"},
            ]
            save_session_state(project_root, newer)

            user_inputs = [""]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            def mock_run(request):
                content = "Fixed newer session.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.offer_resume_or_new()

            self.assertEqual(result.session_id, newer.session_id)
            self.assertEqual(result.status, "completed")

    def test_collab_offer_resume_uses_same_newest_first_logic(self) -> None:
        """Collab mode should share the same unfinished-session chooser."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            older = create_session(project_root, "collab")
            older.goal = "older collab goal"
            older.updated_at = "2026-01-01T00:00:00+00:00"
            save_session_state(project_root, older)

            newer = create_session(project_root, "collab")
            newer.status = "failed"
            newer.goal = "newer collab goal"
            newer.updated_at = "2026-01-02T00:00:00+00:00"
            newer.conversation = [
                {"role": "user", "content": "newer collab goal"},
                {"role": "agent", "content": "I understand.\nGOAL_CLEAR\n"},
            ]
            save_session_state(project_root, newer)

            user_inputs = [""]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            def mock_run(request):
                content = "Applied collab fix.\nGOAL_ACHIEVED: done\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="collab")
            result = session.offer_resume_or_new()

            self.assertEqual(result.session_id, newer.session_id)
            self.assertEqual(result.status, "completed")

    def test_resume_session_display_uses_multiline_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "")
            session = Session(orchestrator, mode="fix")
            state = SessionState(
                session_id="abc123",
                mode="fix",
                status="failed",
                goal="first line\nsecond line",
                updated_at="2026-04-21T05:30:09.905369+00:00",
                execution_log=[{"action": "verify", "result": "should not render"}],
            )

            rendered = session._describe_session_for_resume(state)

            self.assertEqual(
                rendered,
                "\n".join([
                    "session_id=abc123",
                    "status=failed",
                    "updated=2026-04-21 05:30:09",
                    "goal=first line",
                    "second line",
                ]),
            )


class FixConvergenceTests(unittest.TestCase):
    """Test convergence-based stopping for fix mode."""

    def test_stall_stops_after_threshold(self) -> None:
        """Fix loop should stop when diff and verify error are unchanged."""
        from auto_agents.models import SESSION_STALL_THRESHOLD

        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = ["The test fails"]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # Emit FIX_VERIFY so the targeted check always fails
                    content = "I see the test issue.\nFIX_VERIFY: exit 1\nGOAL_CLEAR\n"
                else:
                    # Always produce the same fix — no progress
                    content = "Applied same fix attempt.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.start()

            self.assertEqual(result.status, "failed")
            self.assertGreaterEqual(result.stall_count, SESSION_STALL_THRESHOLD)

    def test_agent_errors_independent_counter(self) -> None:
        """Consecutive agent errors should use independent counter."""
        from auto_agents.models import SESSION_AGENT_ERROR_THRESHOLD

        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = ["The test fails"]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I see.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                # All subsequent calls fail
                write_text(request.output_path, "")
                return AgentResult(
                    ok=False, command=["mock"], output_path=request.output_path,
                    summary="", stdout="", returncode=1,
                    stderr="network error",
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.start()

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.consecutive_agent_errors, SESSION_AGENT_ERROR_THRESHOLD)
            # stall_count should NOT have been bumped by agent errors
            self.assertEqual(result.stall_count, 0)


class ConvergenceHelperTests(unittest.TestCase):
    """Test convergence detection helper methods."""

    def test_normalize_verify_reason(self) -> None:
        raw = "Error at 2026-04-15T10:30:00Z pid=12345 addr 0xDEADBEEF"
        cleaned = Session._normalize_verify_reason(raw)
        self.assertNotIn("2026", cleaned)
        self.assertNotIn("12345", cleaned)
        self.assertNotIn("DEADBEEF", cleaned)
        self.assertIn("<TS>", cleaned)
        self.assertIn("pid=<PID>", cleaned)
        self.assertIn("<HEX>", cleaned)

    def test_update_stall_state_no_change(self) -> None:
        state = SessionState(session_id="x", last_diff_hash="aaa", last_verify_sig="bbb")
        session = self._make_stub_session()
        session._update_stall_state(state, "aaa", "bbb")
        self.assertEqual(state.stall_count, 1)
        session._update_stall_state(state, "aaa", "bbb")
        self.assertEqual(state.stall_count, 2)

    def test_update_stall_state_resets_on_change(self) -> None:
        state = SessionState(session_id="x", last_diff_hash="aaa", last_verify_sig="bbb", stall_count=2)
        session = self._make_stub_session()
        session._update_stall_state(state, "ccc", "bbb")  # diff changed
        self.assertEqual(state.stall_count, 0)

    def test_should_stop_on_stall(self) -> None:
        from auto_agents.models import SESSION_STALL_THRESHOLD
        state = SessionState(session_id="x", stall_count=SESSION_STALL_THRESHOLD)
        session = self._make_stub_session()
        result = session._should_stop(state, "some error")
        self.assertIsNotNone(result)
        self.assertIn("No progress", result)

    def test_should_stop_on_agent_errors(self) -> None:
        from auto_agents.models import SESSION_AGENT_ERROR_THRESHOLD
        state = SessionState(session_id="x", consecutive_agent_errors=SESSION_AGENT_ERROR_THRESHOLD)
        session = self._make_stub_session()
        result = session._should_stop(state, "agent_error")
        self.assertIsNotNone(result)
        self.assertIn("transient errors", result)

    def test_should_stop_on_hard_ceiling(self) -> None:
        state = SessionState(session_id="x", current_attempt=15, hard_ceiling=15)
        session = self._make_stub_session()
        result = session._should_stop(state, "still failing")
        self.assertIsNotNone(result)
        self.assertIn("Hard attempt ceiling", result)

    def test_should_not_stop_when_progressing(self) -> None:
        state = SessionState(session_id="x", current_attempt=5, stall_count=1)
        session = self._make_stub_session()
        result = session._should_stop(state, "some error")
        self.assertIsNone(result)

    @staticmethod
    def _make_stub_session():
        """Build a minimal Session with a stub orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _: "")
            return Session(orchestrator, mode="fix")


class SessionsListSlimTests(unittest.TestCase):
    """Test that the sessions command omits verbose fields."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        state_dir = self.tmpdir / ".auto-agents" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "run_state.json").write_text(json.dumps({"status": "completed"}))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_omits_verbose_fields(self) -> None:
        from auto_agents.cli import main
        state = create_session(self.tmpdir, "fix")
        state.conversation = [{"role": "user", "content": "hello"}]
        state.execution_log = [{"attempt": 1, "action": "fix", "result": "ok"}]
        save_session_state(self.tmpdir, state)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir)])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertIn("session_id", row)
        self.assertIn("mode", row)
        self.assertIn("status", row)
        self.assertIn("goal", row)
        self.assertNotIn("conversation", row)
        self.assertNotIn("execution_log", row)
        self.assertNotIn("current_attempt", row)
        self.assertNotIn("max_attempts", row)
        self.assertNotIn("updated_at", row)

    def test_goal_not_truncated(self) -> None:
        from auto_agents.cli import main
        state = create_session(self.tmpdir, "fix")
        state.goal = "A" * 200
        save_session_state(self.tmpdir, state)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main(["sessions", "--project", str(self.tmpdir)])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        data = json.loads(captured.getvalue())
        goal = data[0]["goal"]
        self.assertEqual(len(goal), 200)


class BaselineDiffVerifyTests(unittest.TestCase):
    """Test baseline-diff verification for fix mode."""

    def test_policy_v3_structured_baseline_captures_source_without_running_gates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.verification_policy_version = 3
            orchestrator.config.gates.steps = [
                VerificationStep(
                    kind="test",
                    runner="pytest",
                    targets=["tests/test_contract.py"],
                    parallel_safe=False,
                    serial_reason="ordered_contract",
                    cache_scope="source",
                    result_cache_scope="auto",
                )
            ]
            state = SessionState(session_id="lazybaseline", mode="fix")
            session = Session(orchestrator, mode="fix")

            with patch(
                "auto_agents.session.run_gate_plan",
                side_effect=AssertionError("baseline gates must be lazy"),
            ):
                session._snapshot_baseline(state)

            self.assertEqual(state.baseline_failures, [])
            self.assertTrue(
                state.baseline_git_ref.startswith(
                    "refs/auto-agents/gate-snapshots/"
                )
            )
            self.assertEqual(len(state.baseline_commands), 1)
            session._release_baseline(state)

    def test_collab_progress_plan_excludes_final_only_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.steps = [
                VerificationStep(
                    kind="test",
                    runner="pytest",
                    targets=["tests/test_progress.py"],
                    cadence="implement_and_final",
                    parallel_safe=False,
                    serial_reason="ordered",
                ),
                VerificationStep(
                    kind="test",
                    runner="pytest",
                    targets=["tests/test_final.py"],
                    cadence="final_only",
                    parallel_safe=False,
                    serial_reason="ordered",
                ),
            ]
            session = Session(orchestrator, mode="collab")

            progress = session._logical_gate_commands(
                session._session_gate_plan("progress")
            )
            final = session._logical_gate_commands(session._session_gate_plan("final"))

            self.assertEqual(len(progress), 1)
            self.assertIn("tests/test_progress.py", progress[0])
            self.assertEqual(len(final), 2)
            self.assertTrue(any("tests/test_final.py" in command for command in final))

    def test_baseline_command_set_includes_parallel_group_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.commands = ["echo serial"]
            orchestrator.config.gates.parallel_groups = [
                GateParallelGroup(name="parallel", commands=["echo parallel"])
            ]
            state = SessionState(session_id="parallel-baseline", mode="collab")
            session = Session(orchestrator, mode="collab")

            with patch(
                "auto_agents.session.run_gate_plan",
                return_value=GateResult(ok=True, commands=[], summary="passed"),
            ):
                session._snapshot_baseline(state)

            self.assertEqual(
                state.baseline_commands,
                ["echo serial", "echo parallel"],
            )

    def test_collab_full_verify_bypasses_cache_only_for_final_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.commands = ["echo gate"]
            state = SessionState(session_id="collab-full-verify", mode="collab")
            session = Session(orchestrator, mode="collab", full_verify=True)
            session._current_state = state
            cache_settings = []

            def executor_context(_metadata, **kwargs):
                cache_settings.append(kwargs.get("use_result_cache", True))
                return nullcontext(None)

            def successful_gate(commands, parallel_groups, *_args, **_kwargs):
                logical = list(commands) + [
                    command
                    for group in parallel_groups
                    for command in group.commands
                ]
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(command=command, ok=True, returncode=0)
                        for command in logical
                    ],
                    summary="passed",
                )

            with (
                patch.object(orchestrator, "_gate_executor_context", side_effect=executor_context),
                patch("auto_agents.session.changed_paths", return_value=[]),
                patch("auto_agents.session.run_gate_plan", side_effect=successful_gate),
            ):
                progress = session._run_verify(scope="progress")
                final = session._run_verify(scope="final")

            self.assertTrue(progress["ok"])
            self.assertTrue(final["ok"])
            self.assertEqual(cache_settings, [True, False])
            self.assertFalse(orchestrator._force_full_verify)

    def test_verification_metrics_report_certificate_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.commands = ["echo gate"]
            state = SessionState(session_id="collab-metrics", mode="collab")
            session = Session(orchestrator, mode="collab")
            session._current_state = state
            cached_gate = GateResult(
                ok=True,
                commands=[
                    CommandResult(
                        command="echo gate",
                        ok=True,
                        returncode=0,
                        cached=True,
                    )
                ],
                summary="certificate hit",
            )

            with (
                patch("auto_agents.session.changed_paths", return_value=[]),
                patch("auto_agents.session.run_gate_plan", return_value=cached_gate),
            ):
                result = session._run_verify(scope="progress")

            self.assertEqual(result["scope"], "progress")
            self.assertEqual(result["logical_commands"], 1)
            self.assertEqual(result["executed_commands"], 0)
            self.assertEqual(result["certificate_hits"], 1)
            self.assertGreaterEqual(result["duration_seconds"], 0.0)

    def test_preexisting_failure_tolerated(self) -> None:
        """A gate failure that exists in the baseline should not block completion."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = ["The submit button crashes"]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            # Gate command that always fails — will be captured in baseline
            orchestrator.config.gates.commands = ["echo 'FAILED tests/test_old.py::test_legacy'; exit 1"]

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I see the crash.\nGOAL_CLEAR\n"
                else:
                    content = "Fixed the crash.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.start()

            # Should complete because the failure was in the baseline
            self.assertEqual(result.status, "completed")
            self.assertIn("tests/test_old.py::test_legacy", result.baseline_failures)

    def test_new_failure_blocks_completion(self) -> None:
        """A gate failure NOT in the baseline should block completion."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = ["The submit button crashes"]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            # Baseline captured with this command (will pass)
            # Then we swap to a failing command after baseline capture
            attempt_count = {"n": 0}

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I see the crash.\nFIX_VERIFY: exit 0\nGOAL_CLEAR\n"
                else:
                    # After first fix attempt, swap gate to fail with a new failure
                    orchestrator.config.gates.commands = [
                        "echo 'FAILED tests/test_new.py::test_regression'; exit 1"
                    ]
                    content = "Applied fix.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.start()

            # Should fail because "tests/test_new.py::test_regression" is a new failure
            self.assertEqual(result.status, "failed")

    def test_fix_verify_command_parsed_and_used(self) -> None:
        """FIX_VERIFY command emitted during clarify should be stored and used."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = ["The submit button crashes"]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = (
                        "I see the crash in handlers.py.\n"
                        "FIX_VERIFY: python -c \"print('ok')\"\n"
                        "GOAL_CLEAR\n"
                    )
                else:
                    content = "Fixed by adding null check.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.start()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.fix_verify_command, "python -c \"print('ok')\"")

    def test_fix_verify_command_failure_blocks_completion(self) -> None:
        """If fix_verify_command fails, verification should fail."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)

            user_inputs = ["The submit button crashes"]
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I see the bug.\nFIX_VERIFY: exit 1\nGOAL_CLEAR\n"
                else:
                    content = "Applied same fix.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.start()

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.fix_verify_command, "exit 1")

    def test_fix_verify_python_command_uses_project_conda(self) -> None:
        """Python FIX_VERIFY commands should run inside the project-local conda env."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            (project_root / ".conda" / "conda-meta").mkdir(parents=True)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.commands = []
            session = Session(orchestrator, mode="fix")
            state = create_session(project_root, "fix")
            state.fix_verify_command = 'pytest -q tests/test_issue_regressions_api.py -k "bug_case"'
            session._current_state = state

            with patch("auto_agents.session.run_gate_plan") as run_mock:
                run_mock.return_value = GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=(
                                'conda run -p ./.conda pytest -q '
                                'tests/test_issue_regressions_api.py -k "bug_case"'
                            ),
                            ok=True,
                            returncode=0,
                        )
                    ],
                    summary="all commands passed",
                )
                result = session._run_baseline_diff_verify()

            self.assertTrue(result["ok"])
            self.assertEqual(
                run_mock.call_args.args[0],
                [
                    'conda run -p ./.conda pytest -q '
                    'tests/test_issue_regressions_api.py -k "bug_case"'
                ],
            )

    def test_fix_converse_prompt_includes_conda_fix_verify_guidance(self) -> None:
        """Fix clarify prompt should surface conda requirements and current gate commands."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.commands = [
                "test -d ./.conda/conda-meta",
                "conda run -p ./.conda python -m unittest discover -s tests",
            ]
            session = Session(orchestrator, mode="fix")
            state = SessionState(session_id="prompt1", mode="fix", goal="Planning output uses wrong language")

            prompt = session._build_converse_prompt(state)

            self.assertIn("every Python-oriented FIX_VERIFY command must run inside it", prompt)
            self.assertIn("conda run -p ./.conda python -m unittest discover -s tests", prompt)

    def test_fix_converse_prompt_derives_gate_commands_from_structured_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.commands = []
            orchestrator.config.gates.steps = [
                VerificationStep(kind="test", runner="pytest", targets=["tests"])
            ]
            session = Session(orchestrator, mode="fix")
            state = SessionState(session_id="prompt1", mode="fix", goal="Planning output uses wrong language")

            prompt = session._build_converse_prompt(state)

            self.assertIn("conda run -p ./.conda python -m pytest -q tests", prompt)

    def test_baseline_snapshot_on_resume_stale(self) -> None:
        """If git HEAD changes between sessions, baseline should be re-captured."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)

            state = create_session(project_root, "fix")
            state.status = "failed"
            state.goal = "Button crash"
            state.baseline_git_ref = "stale_ref_000"
            state.baseline_failures = ["tests/test_old.py::test_legacy"]
            state.conversation = [
                {"role": "user", "content": "Button crash"},
                {"role": "agent", "content": "I see.\nGOAL_CLEAR\n"},
            ]
            save_session_state(project_root, state)

            user_inputs: list = []
            inputs = iter(user_inputs)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: next(inputs, ""))

            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                content = "Fixed.\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True, command=["mock"], output_path=request.output_path,
                    summary=content.strip(), stdout=content, returncode=0,
                )

            orchestrator.adapter.run = mock_run
            session = Session(orchestrator, mode="fix")
            result = session.resume(state.session_id)

            self.assertEqual(result.status, "completed")
            # baseline_git_ref should have been updated (no longer "stale_ref_000")
            self.assertNotEqual(result.baseline_git_ref, "stale_ref_000")


class GatesCollectAllTests(unittest.TestCase):
    """Test run_commands_collect_all and extract_failure_ids."""

    def test_collect_all_does_not_short_circuit(self) -> None:
        from auto_agents.gates import run_commands_collect_all
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands_collect_all(
                ["exit 1", "echo ok", "exit 2"],
                Path(tmp),
            )
            self.assertFalse(result.ok)
            # All 3 commands should have been executed
            self.assertEqual(len(result.commands), 3)
            self.assertFalse(result.commands[0].ok)
            self.assertTrue(result.commands[1].ok)
            self.assertFalse(result.commands[2].ok)

    def test_extract_pytest_failure_ids(self) -> None:
        from auto_agents.gates import extract_failure_ids
        from auto_agents.models import CommandResult, GateResult
        cmd = CommandResult(
            command="pytest",
            ok=False,
            returncode=1,
            stdout="FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n",
            stderr="",
        )
        gate = GateResult(ok=False, commands=[cmd])
        ids = extract_failure_ids(gate)
        self.assertEqual(ids, ["tests/test_a.py::test_x", "tests/test_b.py::test_y"])

    def test_extract_non_pytest_failure_ids(self) -> None:
        from auto_agents.gates import extract_failure_ids
        from auto_agents.models import CommandResult, GateResult
        cmd = CommandResult(
            command="npm test",
            ok=False,
            returncode=1,
            stdout="Error: build failed",
            stderr="",
        )
        gate = GateResult(ok=False, commands=[cmd])
        ids = extract_failure_ids(gate)
        self.assertEqual(ids, ["cmd:npm test"])


if __name__ == "__main__":
    unittest.main()
