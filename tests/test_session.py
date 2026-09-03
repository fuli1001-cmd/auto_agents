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
    load_project_config,
    load_session_state,
    load_run_state,
    provider_references_lock_path,
    requirements_trace_path,
    save_project_config,
    save_session_state,
    save_run_state,
    session_state_path,
)
from auto_agents.git_ops import (
    commit_all,
    head_ref,
    working_tree_clean,
    worktree_fingerprint,
)
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
from auto_agents.workflow_chain import IssueBriefBuilder, WorkflowRef, WorkflowStore
from auto_agents.workflow_runtime import WorkflowCoordinator


def _make_project(tmp: str, name: str = "demo") -> Path:
    project_root = Path(tmp) / name
    Orchestrator.init_project(project_root, name, "mock")
    # Mark as completed so session workflows can operate
    from auto_agents.config import load_run_state, save_run_state
    state = load_run_state(project_root)
    state.status = "completed"
    save_run_state(project_root, state)
    return project_root


def _confirm_collab_state(
    state: SessionState,
    mode: str = "simulated",
) -> SessionState:
    state.goal_execution_environment = {
        "schema_version": 1,
        "mode": mode,
        "source": "test_fixture",
        "summary": "Confirmed environment for an existing workflow test.",
        "confirmed": True,
    }
    return state


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


def _add_second_provider_requirement(project_root: Path) -> str:
    reference = ".auto-agents/docs/provider_references/provider-two.md"
    trace = load_requirements_trace(project_root, normalize=False)
    trace["requirements"].append(
        {
            "id": "REQ-002",
            "text": "Use a second verified provider dependency.",
            "source": "spec",
            "status": "active",
            "priority": "mandatory",
            "acceptance_oracles": ["second provider reference is resolved"],
            "oracle_type": "deterministic_test",
            "oracle_strength": "behavioral",
            "evidence_boundary": "internal_state",
            "forbidden_proxy_oracles": [],
            "forbidden_patterns": [],
            "external_docs_required": True,
            "provider_reference": reference,
            "notes": "",
        }
    )
    write_json(requirements_trace_path(project_root), trace)
    write_text(
        project_root / reference,
        "# Second Provider Reference\n\n## Status\n\nambiguous\n",
    )
    lock = json.loads(
        provider_references_lock_path(project_root).read_text(encoding="utf-8")
    )
    lock["references"]["provider-two"] = {
        "path": reference,
        "status": "ambiguous",
        "retrieved_at": "2026-04-24T00:00:00Z",
        "source_urls": ["https://example.net/official"],
        "notes": "Needs a separate decision.",
    }
    write_json(provider_references_lock_path(project_root), lock)
    return reference


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
            goal_execution_environment={
                "mode": "real",
                "confirmed": True,
                "summary": "Repair the actual button behavior.",
            },
            conversation=[{"role": "user", "content": "hello"}],
            execution_log=[{"attempt": 1, "action": "fix", "result": "ok", "timestamp": "t"}],
            current_attempt=1,
            attempt_epoch=2,
            attempts_since_progress=3,
            max_attempts=4,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:01Z",
        )
        data = state.to_dict()
        restored = SessionState.from_dict(data)
        self.assertEqual(restored.session_id, "abc123")
        self.assertEqual(restored.mode, "fix")
        self.assertEqual(restored.goal, "Button does not work")
        self.assertEqual(restored.goal_execution_environment["mode"], "real")
        self.assertEqual(len(restored.conversation), 1)
        self.assertEqual(len(restored.execution_log), 1)
        self.assertEqual(restored.current_attempt, 1)
        self.assertEqual(restored.attempt_epoch, 2)
        self.assertEqual(restored.attempts_since_progress, 3)
        self.assertEqual(restored.max_attempts, 4)

    def test_defaults(self) -> None:
        state = SessionState(session_id="x")
        self.assertEqual(state.mode, "fix")
        self.assertEqual(state.status, "conversing")
        self.assertEqual(state.conversation, [])
        self.assertEqual(state.max_attempts, 4)
        self.assertEqual(state.attempt_epoch, 0)
        self.assertEqual(state.attempts_since_progress, 0)

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

    def test_project_config_sets_new_session_hard_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            config = load_project_config(project_root)
            config.execution.session_limits.hard_ceiling["collab"] = 7
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="collab")

            state = WorkflowCoordinator(orchestrator)._create_session(session)

            self.assertEqual(state.hard_ceiling, 7)

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

    def test_inconclusive_verification_does_not_start_another_fix_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="fix")
            state = SessionState(
                session_id="inconclusive-fix",
                mode="fix",
                status="executing",
            )

            with (
                patch.object(orchestrator, "_apply_generated_verification_config"),
                patch.object(session, "_ensure_baseline"),
                patch.object(
                    session,
                    "_call_agent",
                    return_value="Applied the targeted fix.",
                ) as call_agent,
                patch.object(
                    orchestrator,
                    "_quick_verify_failure_details",
                    return_value=None,
                ),
                patch.object(
                    session,
                    "_run_verify",
                    return_value={
                        "ok": False,
                        "reason": "failure identity remains unresolved",
                        "retry_fix": False,
                        "failure_kind": "verification_inconclusive",
                    },
                ),
                patch.object(session, "_compute_diff_hash") as diff_hash,
            ):
                result = session._phase_fix_execute(state)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.resolution, "verification_inconclusive")
            self.assertEqual(result.current_attempt, 1)
            self.assertEqual(call_agent.call_count, 1)
            diff_hash.assert_not_called()
            verify_log = next(
                entry
                for entry in result.execution_log
                if entry["action"] == "verify"
            )
            self.assertFalse(verify_log["retry_fix"])
            self.assertEqual(
                verify_log["failure_kind"],
                "verification_inconclusive",
            )

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
            commit_all(project_root, "chore: baseline")
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
    def test_collab_asks_dynamic_environment_question_before_goal_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp, "package-builder")
            state = create_session(project_root, "collab")
            state.goal = "Test publishing the package"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)
            prompts = []
            outputs = []
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda _prompt: "2",
            )

            def mock_run(request):
                prompts.append(request.prompt)
                if len(prompts) == 1:
                    content = (
                        'GOAL_ENVIRONMENT v1: {"decision":"ask_user",'
                        '"question":"这次发布检查希望做到哪一步？",'
                        '"choices":['
                        '{"value":"real","label":"发布到实际仓库",'
                        '"description":"完成一次外部可访问的软件包发布。"},'
                        '{"value":"simulated","label":"只演练发布流程",'
                        '"description":"不连接外部仓库，只检查构建和发布步骤。"}]}'
                    )
                else:
                    content = "GOAL_CLEAR\n"
                outputs.append(content)
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content,
                    stdout=content,
                    returncode=0,
                )

            orchestrator._call_with_failover = mock_run
            result = Session(orchestrator, mode="collab")._phase_converse(state)

            self.assertEqual(result.status, "executing")
            self.assertEqual(
                result.goal_execution_environment["mode"], "simulated"
            )
            self.assertEqual(
                result.goal_execution_environment["source"], "user_selection"
            )
            self.assertEqual(
                result.goal_execution_environment["label"],
                "只演练发布流程",
            )
            self.assertIn("这次发布检查希望做到哪一步？", outputs[0])
            self.assertNotIn("实际成片", prompts[0])
            self.assertIn("Confirmed goal execution environment", prompts[1])

    def test_collab_infers_explicit_real_environment_without_user_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "collab")
            state.goal = "Publish the real package to the configured registry"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"explicit environment unexpectedly prompted user: {prompt}"
                ),
            )
            replies = iter(
                [
                    (
                        'GOAL_ENVIRONMENT v1: {"decision":"real",'
                        '"summary":"Publish an externally accessible package '
                        'to the configured registry."}'
                    ),
                    "GOAL_CLEAR\n",
                ]
            )

            def mock_run(request):
                content = next(replies)
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content,
                    stdout=content,
                    returncode=0,
                )

            orchestrator._call_with_failover = mock_run
            result = Session(orchestrator, mode="collab")._phase_converse(state)

            self.assertEqual(result.status, "executing")
            self.assertEqual(result.goal_execution_environment["mode"], "real")
            self.assertEqual(
                result.goal_execution_environment["source"], "explicit_goal"
            )

    def test_collab_rejects_route_until_environment_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "collab")
            state.goal = "Test export"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"environment should be inferred here: {prompt}"
                ),
            )
            replies = iter(
                [
                    (
                        'ROUTE_WORKFLOW v1: {"target":"fix",'
                        '"reason":"export fails","issue_seed":{}}'
                    ),
                    (
                        'GOAL_ENVIRONMENT v1: {"decision":"simulated",'
                        '"summary":"Exercise export without external services."}'
                    ),
                    (
                        'ROUTE_WORKFLOW v1: {"target":"fix",'
                        '"reason":"export fails",'
                        '"issue_seed":{"summary":"repair export"}}'
                    ),
                ]
            )

            def mock_run(request):
                content = next(replies)
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content,
                    stdout=content,
                    returncode=0,
                )

            orchestrator._call_with_failover = mock_run
            result = Session(orchestrator, mode="collab")._phase_converse(state)

            self.assertEqual(result.status, "waiting_child")
            handoff = WorkflowStore(project_root).load_handoff(
                result.active_handoff_id
            )
            self.assertEqual(handoff.target, "fix")
            self.assertEqual(
                handoff.payload["goal_execution_environment"]["mode"],
                "simulated",
            )
            self.assertTrue(
                any(
                    item.get("action") == "goal_environment_required"
                    for item in result.execution_log
                )
            )

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
                    content = (
                        'GOAL_ENVIRONMENT v1: {"decision":"simulated",'
                        '"summary":"Exercise the frontend video workflow '
                        'with test artifacts."}'
                    )
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 2:
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

    def test_collab_converse_routes_standard_workflow_without_user_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.goal = "Repair the existing video generation regression"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"collab unexpectedly prompted the user: {prompt}"
                ),
            )
            reply = (
                'ROUTE_WORKFLOW v1: {"target":"fix",'
                '"reason":"existing regression","summary":"video fails",'
                '"issue_seed":{"expected":"video completes",'
                '"actual":"generation stops"}}'
            )

            def mock_run(request):
                write_text(request.output_path, reply)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=reply,
                    stdout=reply,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run
            result = Session(orchestrator, mode="collab")._phase_converse(state)

            self.assertEqual(result.status, "waiting_child")
            handoff = WorkflowStore(project_root).load_handoff(
                result.active_handoff_id
            )
            self.assertEqual(handoff.target, "fix")
            self.assertEqual(handoff.payload["issue_seed"]["summary"], "video fails")

    def test_collab_accepts_route_envelope_and_rejects_malformed_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="collab")
            state = create_session(project_root, "collab")
            state.goal = "Add export support"

            routed, error = session._route_collab_workflow_reply(
                state,
                json.dumps(
                    {
                        "ROUTE_WORKFLOW": "v1",
                        "target": "run",
                        "reason": "missing capability",
                        "spec_seed": {"title": "Export", "goal": state.goal},
                    }
                ),
            )

            self.assertEqual(error, "")
            self.assertIsNotNone(routed)
            handoff = WorkflowStore(project_root).load_handoff(
                routed.active_handoff_id
            )
            self.assertEqual(handoff.target, "run")

            malformed_state = create_session(project_root, "collab")
            malformed_state.goal = "Repair export"
            routed, error = session._route_collab_workflow_reply(
                malformed_state,
                'ROUTE_WORKFLOW v1: {"target":"fix","issue_seed":"bad"}',
            )

            self.assertIsNone(routed)
            self.assertIn("issue_seed must be a JSON object", error)

            routed, error = session._route_collab_workflow_reply(
                state,
                (
                    'ROUTE_WORKFLOW v1: {"target":"resume",'
                    '"resume_handoff_id":"missing-handoff"}'
                ),
            )
            self.assertIsNone(routed)
            self.assertIn("Unknown resume_handoff_id", error)

    def test_collab_converse_normalizes_enveloped_fix_disposition_without_user_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.goal = "Generate the requested fox story test video"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"collab unexpectedly prompted the user: {prompt}"
                ),
            )
            reply = json.dumps(
                {
                    "FIX_DISPOSITION": "v1",
                    "decision": "fix",
                    "summary": "Repair storyboard convergence",
                    "reason": "Existing bounded storyboard defect",
                    "reproduction": "Create the fox story video",
                    "expected": "Video generation completes",
                    "actual": "Storyboard repair stops without progress",
                    "evidence_refs": ["tests/test_storyboard.py::test_convergence"],
                    "affected_contracts": ["storyboard-candidate-pipeline-v3"],
                    "verification_command": "pytest -q tests/test_storyboard.py",
                    "persistence_change": {
                        "strategy": "clean_break",
                        "storage_transition": "none",
                        "compatibility_policy": "reject_legacy",
                        "target_ids": ["local-db"],
                        "physical_schema_change": False,
                        "historical_data_action": "none",
                    },
                },
                ensure_ascii=False,
            )

            def mock_run(request):
                write_text(request.output_path, reply)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=reply,
                    stdout=reply,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run
            result = Session(orchestrator, mode="collab")._phase_converse(state)

            self.assertEqual(result.status, "waiting_child")
            handoff = WorkflowStore(project_root).load_handoff(
                result.active_handoff_id
            )
            self.assertEqual(handoff.target, "fix")
            issue_seed = handoff.payload["issue_seed"]
            self.assertEqual(
                issue_seed["reproduction"], ["Create the fox story video"]
            )
            self.assertNotIn("persistence_change", issue_seed)
            normalized = next(
                item
                for item in result.execution_log
                if item.get("action") == "collab_foreign_disposition_normalized"
            )
            self.assertTrue(normalized["discarded_persistence_change"])

    def test_fix_converse_accepts_enveloped_disposition_in_read_only_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            state.goal = "Repair export"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(project_root)
            reply = json.dumps(
                {
                    "FIX_DISPOSITION": "v1",
                    "decision": "fix",
                    "summary": "Repair export",
                    "reason": "Existing bounded defect",
                    "reproduction": ["Export a result"],
                    "expected": "Export succeeds",
                    "actual": "Export fails",
                    "evidence_refs": [],
                    "affected_contracts": ["export"],
                    "verification_command": "",
                    "persistence_change": {
                        "storage_transition": "none",
                        "compatibility_policy": "not_applicable",
                    },
                }
            )
            requests = []

            def mock_run(request):
                requests.append(request)
                write_text(request.output_path, reply)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=reply,
                    stdout=reply,
                    returncode=0,
                )

            orchestrator._call_with_failover = mock_run
            result = Session(orchestrator, mode="fix")._phase_converse(state)

            self.assertEqual(result.status, "executing")
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].sandbox_mode, "read-only")

    def test_routed_fix_prompt_preserves_authoritative_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            state.goal = "Generate a video"
            state.parent_handoff_id = "handoff-engine-fix"
            IssueBriefBuilder(project_root, state.session_id).materialize(
                {
                    "summary": "Repair the engine lifecycle",
                    "reported_goal": state.goal,
                    "constraints": [
                        "The defect is in auto_agents; do not modify product code."
                    ],
                    "source_handoff_id": state.parent_handoff_id,
                }
            )

            prompt = Session(
                Orchestrator(project_root), mode="fix"
            )._build_converse_prompt(state)

            self.assertIn("Authoritative Routed Issue Brief", prompt)
            self.assertIn("Repair the engine lifecycle", prompt)
            self.assertIn(
                "The defect is in auto_agents; do not modify product code.",
                prompt,
            )
            self.assertIn("take precedence over the broader reported goal", prompt)
            self.assertIn("fake, mock, fixture, placeholder", prompt)

    def test_fix_cannot_resume_its_parent_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            store = WorkflowStore(project_root)
            snapshot = store.create_root(WorkflowRef("collab", "parent-collab"))
            handoff = store.prepare_handoff(
                snapshot,
                parent=WorkflowRef("collab", "parent-collab"),
                target="fix",
                goal="Repair engine",
                reason="engine defect",
                payload={},
            )
            store.bind_child(snapshot, handoff, WorkflowRef("fix", "child-fix"))
            state = SessionState(
                session_id="child-fix",
                mode="fix",
                workflow_id=snapshot.workflow_id,
                parent_handoff_id=handoff.handoff_id,
            )

            error = Session(
                Orchestrator(project_root), mode="fix"
            )._resume_handoff_error(state, handoff.handoff_id)

            self.assertIn("cannot resume its parent or a sibling handoff", error)

    def test_resume_rejects_current_session_handoff_without_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            store = WorkflowStore(project_root)
            snapshot = store.create_root(WorkflowRef("fix", "parent-fix"))
            handoff = store.prepare_handoff(
                snapshot,
                parent=WorkflowRef("fix", "parent-fix"),
                target="run",
                goal="Add export",
                reason="new capability",
                payload={},
            )
            state = SessionState(
                session_id="parent-fix",
                mode="fix",
                workflow_id=snapshot.workflow_id,
            )

            error = Session(
                Orchestrator(project_root), mode="fix"
            )._resume_handoff_error(state, handoff.handoff_id)

            self.assertIn("has no child to resume", error)

    def test_collab_loop_normalizes_fix_disposition_marker_to_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"collab unexpectedly prompted the user: {prompt}"
                ),
            )
            reply = (
                'FIX_DISPOSITION v1: {"decision":"fix",'
                '"summary":"Repair storyboard convergence",'
                '"reason":"Existing bounded defect",'
                '"reproduction":["Create video"],'
                '"expected":"generation completes",'
                '"actual":"storyboard stops",'
                '"evidence_refs":[],"affected_contracts":[],'
                '"verification_command":"pytest -q"}'
            )

            def mock_run(request):
                write_text(request.output_path, reply)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=reply,
                    stdout=reply,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.status = "executing"
            state.goal = "Generate video"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)

            result = Session(orchestrator, mode="collab")._phase_collab_loop(
                state
            )

            self.assertEqual(result.status, "waiting_child")
            handoff = WorkflowStore(project_root).load_handoff(
                result.active_handoff_id
            )
            self.assertEqual(handoff.target, "fix")

    def test_collab_converse_normalizes_run_iteration_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.goal = "Add export support"
            state.conversation = [{"role": "user", "content": state.goal}]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"collab unexpectedly prompted the user: {prompt}"
                ),
            )
            reply = json.dumps(
                {
                    "FIX_DISPOSITION": "v1",
                    "decision": "run_iteration",
                    "reason": "Missing public capability",
                    "spec_seed": {
                        "title": "Export support",
                        "goal": "Add export support",
                        "gap": "No export API",
                        "capability": "Export results",
                        "acceptance": ["Export succeeds"],
                        "non_goals": [],
                        "evidence": [],
                        "open_decisions": [],
                    },
                }
            )

            def mock_run(request):
                write_text(request.output_path, reply)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=reply,
                    stdout=reply,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_run
            result = Session(orchestrator, mode="collab")._phase_converse(state)

            self.assertEqual(result.status, "waiting_child")
            handoff = WorkflowStore(project_root).load_handoff(
                result.active_handoff_id
            )
            self.assertEqual(handoff.target, "run")
            self.assertEqual(handoff.payload["spec_seed"]["title"], "Export support")

    def test_collab_resume_consumes_pending_foreign_disposition_without_agent_or_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            reply = json.dumps(
                {
                    "FIX_DISPOSITION": "v1",
                    "decision": "fix",
                    "summary": "Repair storyboard convergence",
                    "reason": "Existing bounded storyboard defect",
                    "reproduction": "Create the fox story video",
                    "expected": "Video generation completes",
                    "actual": "Storyboard repair stops without progress",
                    "evidence_refs": [],
                    "affected_contracts": [],
                    "verification_command": "pytest -q tests/test_storyboard.py",
                }
            )
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.status = "paused"
            state.resolution = "interrupted_by_user"
            state.goal = "Generate the requested fox story test video"
            state.conversation = [
                {"role": "user", "content": state.goal},
                {"role": "agent", "content": reply},
            ]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"collab unexpectedly prompted the user: {prompt}"
                ),
            )
            orchestrator.adapter.run = lambda _request: self.fail(
                "resume should not need another collab agent call before routing"
            )

            with patch(
                "auto_agents.workflow_runtime.WorkflowCoordinator._drive_fix_child",
                return_value={
                    "status": "paused",
                    "resolution": "child paused for test",
                    "summary": "child paused for test",
                    "changed_paths": [],
                },
            ):
                result = Session(orchestrator, mode="collab").resume(
                    state.session_id
                )

            self.assertEqual(result.status, "waiting_child")
            handoff = WorkflowStore(project_root).load_handoff(
                result.active_handoff_id
            )
            self.assertEqual(handoff.target, "fix")
            self.assertEqual(
                handoff.payload["issue_seed"]["summary"],
                "Repair storyboard convergence",
            )

    def test_collab_resume_consumes_pending_standard_route_without_agent_or_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            reply = (
                'ROUTE_WORKFLOW v1: {"target":"fix",'
                '"reason":"bounded regression","summary":"repair export",'
                '"issue_seed":{"expected":"export succeeds",'
                '"actual":"export fails"}}'
            )
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.status = "paused"
            state.resolution = "interrupted_by_user"
            state.resume_phase = "conversing"
            state.goal = "Repair export"
            state.conversation = [
                {"role": "user", "content": state.goal},
                {"role": "agent", "content": reply},
            ]
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"collab unexpectedly prompted the user: {prompt}"
                ),
            )
            orchestrator.adapter.run = lambda _request: self.fail(
                "resume should consume the saved route before another agent call"
            )

            with patch(
                "auto_agents.workflow_runtime.WorkflowCoordinator._drive_fix_child",
                return_value={
                    "status": "paused",
                    "resolution": "child paused for test",
                    "summary": "child paused for test",
                    "changed_paths": [],
                },
            ):
                result = Session(orchestrator, mode="collab").resume(
                    state.session_id
                )

            self.assertEqual(result.status, "waiting_child")
            handoff = WorkflowStore(project_root).load_handoff(
                result.active_handoff_id
            )
            self.assertEqual(handoff.target, "fix")

    def test_collab_reconciles_prepared_handoff_before_replaying_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.status = "executing"
            state.goal = "Repair export"
            state.conversation = [
                {"role": "user", "content": state.goal},
                {
                    "role": "agent",
                    "content": (
                        'ROUTE_WORKFLOW v1: {"target":"fix",'
                        '"reason":"bounded regression",'
                        '"issue_seed":{"summary":"repair export"}}'
                    ),
                },
            ]
            store = WorkflowStore(project_root)
            snapshot = store.create_root(
                WorkflowRef("collab", state.session_id)
            )
            state.workflow_id = snapshot.workflow_id
            prepared = store.prepare_handoff(
                snapshot,
                parent=WorkflowRef("collab", state.session_id),
                target="fix",
                goal=state.goal,
                reason="bounded regression",
                payload={"issue_seed": {"summary": "repair export"}},
            )
            save_session_state(project_root, state)
            session = Session(orchestrator, mode="collab")
            session._call_agent = lambda *_args, **_kwargs: self.fail(
                "the saved handoff must be recovered before replaying the route"
            )

            result = session._drive_local(state)

            self.assertEqual(result.status, "waiting_child")
            self.assertEqual(result.active_handoff_id, prepared.handoff_id)
            handoff_files = list(
                (project_root / ".auto-agents" / "state" / "handoffs").glob(
                    "*.json"
                )
            )
            self.assertEqual(len(handoff_files), 1)
            self.assertTrue(
                any(
                    item.get("action") == "prepared_handoff_reconciled"
                    for item in result.execution_log
                )
            )

    def test_collab_resume_consumes_saved_goal_clear_before_another_agent_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.status = "conversing"
            state.goal = "Verify the browser flow"
            state.conversation = [
                {"role": "user", "content": state.goal},
                {
                    "role": "agent",
                    "content": "The goal is clear and bounded.\nGOAL_CLEAR\n",
                },
            ]
            session = Session(orchestrator, mode="collab")
            session._call_agent = lambda *_args, **_kwargs: self.fail(
                "the saved GOAL_CLEAR must be consumed before another agent call"
            )

            def finish(executing_state):
                self.assertEqual(executing_state.status, "executing")
                executing_state.status = "completed"
                return executing_state

            with patch.object(
                session,
                "_phase_collab_loop",
                side_effect=finish,
            ):
                result = session._drive_local(state)

            self.assertEqual(result.status, "completed")
            self.assertTrue(
                any(
                    item.get("action") == "collab_goal_clear_reconciled"
                    for item in result.execution_log
                )
            )

    def test_collab_resume_finishes_saved_goal_achieved_before_agent_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda _prompt: "y",
            )
            state = create_session(project_root, "collab")
            _confirm_collab_state(state, "real")
            state.status = "executing"
            state.goal = "Verify the browser flow"
            state.conversation = [
                {"role": "user", "content": state.goal},
                {
                    "role": "agent",
                    "content": "GOAL_ACHIEVED: browser flow verified\n",
                },
            ]
            session = Session(orchestrator, mode="collab")
            session._call_agent = lambda *_args, **_kwargs: self.fail(
                "the saved completion must be handled before another agent call"
            )

            with (
                patch.object(
                    session,
                    "_run_verify",
                    return_value={"ok": True, "reason": "passed"},
                ),
                patch.object(session, "_git_commit", return_value=True),
                patch.object(session, "_record_release_attestation"),
                patch.object(session, "_release_baseline"),
            ):
                result = session._drive_local(state)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.resolution, "goal_achieved")

    def test_collab_does_not_report_success_when_receipt_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda _prompt: "y",
            )
            state = create_session(project_root, "collab")
            _confirm_collab_state(state, "real")
            state.status = "executing"
            state.goal = "Verify the browser flow"
            state.conversation = [
                {"role": "user", "content": state.goal},
                {
                    "role": "agent",
                    "content": "GOAL_ACHIEVED: browser flow verified\n",
                },
            ]
            session = Session(orchestrator, mode="collab")

            with (
                patch.object(
                    session,
                    "_run_verify",
                    return_value={"ok": True, "reason": "passed"},
                ),
                patch.object(session, "_git_commit", return_value=False),
            ):
                result = session._drive_local(state)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.resolution, "commit_failed")

    def test_session_commit_tracks_only_durable_session_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            commit_all(project_root, "chore: baseline")
            write_text(
                project_root / ".auto-agents" / ".gitignore",
                "runs/\nstate/sessions/\nstate/run_state.json\n",
            )
            orchestrator = Orchestrator(project_root)
            state = create_session(project_root, "fix")
            state.status = "executing"
            state.goal = "Repair the app"
            store = WorkflowStore(project_root)
            workflow = store.create_root(WorkflowRef("fix", state.session_id))
            state.workflow_id = workflow.workflow_id
            save_session_state(project_root, state)
            root = session_state_path(project_root, state.session_id).parent
            write_json(root / "issue.json", {"summary": "Repair the app"})
            write_text(root / "issue.md", "# Repair the app\n")
            write_text(root / "prompts" / "fix-1.txt", "private prompt\n")
            write_text(root / "outputs" / "fix-1.md", "provider output\n")
            write_json(root / "health" / "summary.json", {"status": "observing"})
            write_text(root / "performance_trace.jsonl", "{}\n")
            write_text(root / "local-debug.txt", "not durable\n")
            session = Session(orchestrator, mode="fix")
            session._coordinator = SimpleNamespace(store=store)

            committed = session._git_commit(
                state,
                "fix",
                reply="COMMIT_MESSAGE: persist durable session state",
            )

            self.assertTrue(committed)
            tracked = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
            prefix = f".auto-agents/state/sessions/{state.session_id}/"
            self.assertIn(prefix + "session_state.json", tracked)
            self.assertIn(prefix + "issue.json", tracked)
            self.assertIn(prefix + "issue.md", tracked)
            self.assertNotIn(prefix + "prompts/fix-1.txt", tracked)
            self.assertNotIn(prefix + "outputs/fix-1.md", tracked)
            self.assertNotIn(prefix + "health/summary.json", tracked)
            self.assertNotIn(prefix + "performance_trace.jsonl", tracked)
            self.assertNotIn(prefix + "local-debug.txt", tracked)
            ignore_entries = (
                project_root / ".auto-agents" / ".gitignore"
            ).read_text(encoding="utf-8").splitlines()
            self.assertNotIn("state/sessions/", ignore_entries)

    def test_collab_allows_health_control_telemetry_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            commit_all(project_root, "chore: baseline")
            control_path = (
                project_root
                / ".auto-agents"
                / "state"
                / "health-watch-control.json"
            )
            write_json(control_path, {"active_operation": {}})
            subprocess.run(
                ["git", "add", "-f", str(control_path.relative_to(project_root))],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "chore: legacy tracked health control"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="collab")
            state = SessionState(
                session_id="readonly-health",
                mode="collab",
                status="executing",
                goal="Diagnose app",
                hard_ceiling=1,
            )
            _confirm_collab_state(state)

            def report_diagnosis(_state, _label, _prompt):
                write_json(
                    control_path,
                    {"active_operation": {"kind": "provider", "label": "collab-1"}},
                )
                return "Diagnosis is complete but has no workflow marker yet."

            session._call_agent = report_diagnosis
            result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "failed")
            self.assertTrue(
                any(item.get("action") == "collab" for item in result.execution_log)
            )
            self.assertFalse(
                any(
                    item.get("action") == "collab_mutation_restored"
                    for item in result.execution_log
                )
            )
            self.assertEqual(
                json.loads(control_path.read_text(encoding="utf-8"))[
                    "active_operation"
                ]["kind"],
                "provider",
            )

    def test_collab_resume_replays_saved_assistance_request_before_agent_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda _prompt: "browser result: passed",
            )
            state = create_session(project_root, "collab")
            _confirm_collab_state(state, "real")
            state.status = "executing"
            state.goal = "Verify the browser flow"
            state.attempt_epoch = 2
            state.attempts_since_progress = 24
            state.conversation = [
                {"role": "user", "content": state.goal},
                {
                    "role": "agent",
                    "content": "NEED_USER_ASSIST: run the browser smoke test",
                },
            ]
            session = Session(orchestrator, mode="collab")
            session._call_agent = lambda *_args, **_kwargs: self.fail(
                "the saved assistance request must be replayed first"
            )

            def finish(executing_state):
                self.assertEqual(executing_state.status, "executing")
                executing_state.status = "completed"
                return executing_state

            with patch.object(
                session,
                "_phase_collab_loop",
                side_effect=finish,
            ):
                result = session._drive_local(state)

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                result.conversation[-1],
                {"role": "user", "content": "browser result: passed"},
            )
            self.assertEqual(result.attempt_epoch, 3)
            self.assertEqual(result.attempts_since_progress, 0)

    def test_collab_resume_rejects_stale_orchestrator_assistance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda _prompt: self.fail(
                    "stale orchestration assistance must not prompt the user"
                ),
            )
            state = create_session(project_root, "collab")
            _confirm_collab_state(state, "real")
            state.status = "executing"
            state.goal = "Generate the video"
            state.current_attempt = 1
            state.attempt_epoch = 2
            state.attempts_since_progress = 0
            state.conversation = [
                {"role": "user", "content": state.goal},
                {
                    "role": "agent",
                    "content": (
                        "NEED_USER_ASSIST: reached the 25 attempt limit; run "
                        "auto-agents resume --no-health-watch"
                    ),
                },
            ]
            session = Session(orchestrator, mode="collab")

            def finish(executing_state):
                self.assertEqual(executing_state.status, "executing")
                self.assertEqual(
                    executing_state.conversation[-1]["role"],
                    "orchestrator",
                )
                executing_state.status = "completed"
                return executing_state

            with patch.object(
                session,
                "_phase_collab_loop",
                side_effect=finish,
            ):
                result = session._drive_local(state)

            self.assertEqual(result.status, "completed")
            self.assertTrue(
                any(
                    item.get("action") == "collab_assistance_rejected"
                    for item in result.execution_log
                )
            )

    def test_repeated_invalid_orchestrator_assistance_stops_on_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda _prompt: self.fail(
                    "invalid orchestration assistance must not prompt the user"
                ),
            )
            state = SessionState(
                session_id="invalid-assistance",
                mode="collab",
                status="executing",
                goal="Generate the video",
                hard_ceiling=25,
            )
            _confirm_collab_state(state)
            session = Session(orchestrator, mode="collab")
            calls = {"count": 0}

            def stale_reply(_state, _label, _prompt):
                calls["count"] += 1
                return (
                    "NEED_USER_ASSIST: 已达到 25 次执行上限，请运行 "
                    "auto-agents resume --no-health-watch"
                )

            session._call_agent = stale_reply
            result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.resolution, "no_progress")
            self.assertLess(calls["count"], state.hard_ceiling)
            self.assertFalse(
                any(
                    item.get("action") == "attempt_epoch_started"
                    and item.get("result") == "user assistance supplied"
                    for item in result.execution_log
                )
            )

    def test_collab_prompt_reports_current_attempt_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            session = Session(Orchestrator(project_root), mode="collab")
            state = SessionState(
                session_id="attempt-context",
                mode="collab",
                goal="Diagnose the app",
                attempt_epoch=3,
                attempts_since_progress=4,
                hard_ceiling=25,
            )

            prompt = session._build_collab_prompt(state, "")

            self.assertIn(
                "Current local attempt budget: epoch=3, provider_calls=4, hard_ceiling=25.",
                prompt,
            )
            self.assertIn(
                "never ask the user to run, resume, stop, or reconfigure auto-agents",
                prompt,
            )

    def test_collab_interrupt_restores_readonly_mutation_before_pausing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            app_path = project_root / "app.py"
            write_text(app_path, "value = 1\n")
            commit_all(project_root, "chore: baseline")
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="collab")
            state = create_session(project_root, "collab")
            _confirm_collab_state(state)
            state.status = "executing"
            state.goal = "Inspect the app"
            snapshot = WorkflowStore(project_root).create_root(
                WorkflowRef("collab", state.session_id)
            )
            state.workflow_id = snapshot.workflow_id
            save_session_state(project_root, state)

            def interrupting_agent(*_args, **_kwargs):
                write_text(app_path, "mutated = True\n")
                raise KeyboardInterrupt

            with patch.object(session, "_call_agent", side_effect=interrupting_agent):
                result = session._drive_local(state)

            self.assertEqual(result.status, "paused")
            self.assertEqual(result.resume_phase, "executing")
            self.assertEqual(app_path.read_text(encoding="utf-8"), "value = 1\n")
            checkpoint_root = (
                project_root
                / ".auto-agents"
                / "state"
                / "workflows"
                / snapshot.workflow_id
                / "checkpoints"
            )
            self.assertEqual(
                list(checkpoint_root.glob(f"collab-{state.session_id}-*")),
                [],
            )

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
                _confirm_collab_state(state)
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

                if marker.startswith("BUG_FOUND"):
                    self.assertEqual(result.status, "waiting_child")
                    self.assertEqual(result.current_attempt, 1)
                    self.assertEqual(call_agent.call_count, 1)
                else:
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.current_attempt, 2)
                    self.assertEqual(result.resolution, "hard_ceiling_reached")
                    self.assertEqual(call_agent.call_count, 2)

    def test_repeated_collab_rollbacks_stop_on_stall_before_hard_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            app_path = project_root / "app.py"
            write_text(app_path, "value = 1\n")
            commit_all(project_root, "chore: baseline")
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="collab")
            state = SessionState(
                session_id="repeated-rollback",
                mode="collab",
                status="executing",
                goal="Diagnose app",
                hard_ceiling=25,
            )
            _confirm_collab_state(state)
            calls = {"count": 0}

            def mutate(_state, _label, _prompt):
                calls["count"] += 1
                write_text(app_path, "value = 2\n")
                return "I changed the file directly."

            session._call_agent = mutate
            result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.resolution, "no_progress")
            self.assertLess(calls["count"], state.hard_ceiling)
            self.assertEqual(app_path.read_text(encoding="utf-8"), "value = 1\n")

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
            _confirm_collab_state(state)
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
            _confirm_collab_state(state)
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

    def test_collab_general_output_is_diagnostic_not_implementation_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "y")
            state = SessionState(session_id="collab-two-tier", mode="collab")
            _confirm_collab_state(state)
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

            self.assertEqual(result.status, "failed")
            verify.assert_not_called()
            self.assertFalse(
                [
                    entry
                    for entry in state.execution_log
                    if entry["action"].endswith("verify")
                ]
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
            _confirm_collab_state(state)
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

    def test_collab_legacy_bug_marker_routes_to_fix_without_verifying_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _prompt: "y")
            state = SessionState(session_id="collab-marker-scopes", mode="collab")
            _confirm_collab_state(state)
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

            self.assertEqual(result.status, "waiting_child")
            self.assertTrue(result.active_handoff_id)
            verify.assert_not_called()

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
                    content = (
                        'GOAL_ENVIRONMENT v1: {"decision":"real",'
                        '"summary":"Verify the actual browser player behavior."}'
                    )
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 2:
                    content = "I'll help test the video player.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 3:
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
                    content = (
                        'GOAL_ENVIRONMENT v1: {"decision":"real",'
                        '"summary":"Verify the actual browser player behavior."}'
                    )
                elif call_count["n"] == 2:
                    content = "I'll help test the video player.\nGOAL_CLEAR\n"
                elif call_count["n"] == 3:
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
            self.assertEqual(captured.getvalue().count("Agent is thinking, please wait..."), 3)

    def test_collab_flow_commits_completed_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            commit_all(project_root, "chore: baseline")

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
                    content = (
                        'GOAL_ENVIRONMENT v1: {"decision":"simulated",'
                        '"summary":"Exercise the frontend video workflow '
                        'with test artifacts."}'
                    )
                elif call_count["n"] == 2:
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

    def test_collab_restores_inline_edits_and_only_coordinates_confirmation(self) -> None:
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
                    content = (
                        'GOAL_ENVIRONMENT v1: {"decision":"real",'
                        '"summary":"Make the actual browser flow work end-to-end."}'
                    )
                elif call_count["n"] == 2:
                    content = "I understand the goal.\nGOAL_CLEAR\n"
                elif call_count["n"] == 3:
                    app_file.write_text("value = 1\n", encoding="utf-8")
                    content = "Applied the first browser fix.\nGOAL_ACHIEVED: The main flow now works\n"
                elif call_count["n"] == 4:
                    content = "Diagnostic check complete.\nGOAL_ACHIEVED: The main flow now works\n"
                else:
                    content = "Final diagnostic check complete.\nGOAL_ACHIEVED: The browser flow is fully working\n"
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
            self.assertEqual(app_file.read_text(encoding="utf-8"), "value = 0\n")

            rev_list = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=str(project_root),
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(rev_list.stdout.strip(), "3")


class SessionResumeTests(unittest.TestCase):
    """Test session resume and persistence."""

    def test_resume_rejects_session_from_another_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            state = create_session(project_root, "fix")
            orchestrator = Orchestrator(project_root)

            with self.assertRaisesRegex(ValueError, "is fix, not collab"):
                Session(orchestrator, mode="collab").resume(state.session_id)

    def test_interrupted_conversation_records_exact_resume_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="collab")
            state = create_session(project_root, "collab")
            state.goal = "Clarify the browser test"

            with patch.object(
                session,
                "_phase_converse",
                side_effect=KeyboardInterrupt,
            ):
                interrupted = session._drive_local(state)

            self.assertEqual(interrupted.status, "paused")
            self.assertEqual(interrupted.resume_phase, "conversing")
            restored = load_session_state(project_root, state.session_id)
            self.assertEqual(restored.resume_phase, "conversing")

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

    def test_provider_resolve_blocks_same_unsatisfied_consumer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, reference = _make_provider_blocked_project(tmp)
            blocked_error = load_run_state(project_root).last_error
            run_state = load_run_state(project_root)
            run_state.tasks = [
                TaskSpec(
                    task_id="task-consumer",
                    title="Consume provider contract",
                    description="Exercise the provider boundary.",
                    acceptance=[
                        "The provider evidence is verified; assumptions are insufficient."
                    ],
                    requirement_ids=["REQ-001"],
                )
            ]
            save_run_state(project_root, run_state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda _prompt: "",
            )
            call_count = {"n": 0}

            def mock_run(request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    content = "I understand the provider blocker.\nGOAL_CLEAR\n"
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
                                    "notes": "Assumptions were approved for development.",
                                }
                            },
                        },
                    )
                    content = "Recorded the approved development assumptions.\n"
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
                raise RuntimeError(blocked_error)

            orchestrator.adapter.run = mock_run
            orchestrator.resume_saved_run = mock_resume_saved_run

            state = Session(orchestrator, mode="provider_resolve").start()
            persisted_run = load_run_state(project_root)

            self.assertEqual(state.status, "blocked")
            self.assertEqual(
                state.resolution,
                "provider_recovery_contract_unsatisfied",
            )
            self.assertEqual(call_count["n"], 2)
            self.assertEqual(resume_calls["n"], 1)
            self.assertEqual(persisted_run.status, "blocked")
            self.assertEqual(
                persisted_run.active_blocker["owner"],
                "verification_contract",
            )
            self.assertEqual(
                persisted_run.active_blocker["category"],
                "provider_recovery_contract_unsatisfied",
            )
            self.assertEqual(
                len(
                    [
                        entry
                        for entry in state.execution_log
                        if entry.get("action") == "provider_verify"
                    ]
                ),
                1,
            )

            retried_run = load_run_state(project_root)
            retried_run.status = "failed"
            retried_run.last_error = blocked_error
            retried_run.active_blocker = {}
            save_run_state(project_root, retried_run)

            second_session = Session(
                orchestrator,
                mode="provider_resolve",
            ).start()

            self.assertEqual(second_session.status, "blocked")
            self.assertEqual(call_count["n"], 2)
            self.assertEqual(resume_calls["n"], 1)

    def test_provider_recovery_allows_a_disjoint_consumer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, _reference = _make_provider_blocked_project(tmp)
            _add_second_provider_requirement(project_root)
            run_state = load_run_state(project_root)
            run_state.tasks = [
                TaskSpec(
                    task_id="task-first-consumer",
                    title="Consume first provider contract",
                    description="Exercise the first provider boundary.",
                    acceptance=["The first provider evidence is verified."],
                    requirement_ids=["REQ-001"],
                ),
                TaskSpec(
                    task_id="task-second-consumer",
                    title="Consume second provider contract",
                    description="Exercise the second provider boundary.",
                    acceptance=["The second provider evidence is verified."],
                    requirement_ids=["REQ-002"],
                ),
            ]
            save_run_state(project_root, run_state)
            orchestrator = Orchestrator(project_root)
            original_blocker = run_state.last_error
            expected = orchestrator.provider_recovery_contract_fingerprint(
                state=run_state,
                blocker_message=original_blocker,
            )
            recreated_blocker = (
                "provider research is blocked; resolve the new dependency.\n"
                "- REQ-002: second provider reference is ambiguous"
            )

            blocked = orchestrator.block_repeated_provider_recovery(
                expected_contract_fingerprint=expected,
                blocker_message=recreated_blocker,
                contract_scope_message=original_blocker,
            )

            self.assertIsNone(blocked)
            persisted = load_run_state(project_root)
            self.assertEqual(persisted.status, "failed")
            self.assertNotIn(
                "provider_recovery_contract_receipts",
                persisted.resume_context,
            )

    def test_provider_recovery_receipt_survives_a_narrower_blocker_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, reference = _make_provider_blocked_project(tmp)
            second_reference = _add_second_provider_requirement(project_root)
            run_state = load_run_state(project_root)
            run_state.tasks = [
                TaskSpec(
                    task_id="task-provider-consumer",
                    title="Consume both provider contracts",
                    description="Exercise both provider boundaries.",
                    acceptance=["Both provider references are verified."],
                    requirement_ids=["REQ-001", "REQ-002"],
                )
            ]
            original_blocker = (
                "provider research is blocked; resolve provider dependencies.\n"
                f"- REQ-001: {reference} is ambiguous\n"
                f"- REQ-002: {second_reference} is ambiguous"
            )
            run_state.last_error = original_blocker
            save_run_state(project_root, run_state)
            orchestrator = Orchestrator(project_root)
            expected = orchestrator.provider_recovery_contract_fingerprint(
                state=run_state,
                blocker_message=original_blocker,
            )
            narrower_blocker = (
                "provider research is blocked; one dependency remains.\n"
                f"- REQ-002: {second_reference} is ambiguous"
            )

            blocked = orchestrator.block_repeated_provider_recovery(
                expected_contract_fingerprint=expected,
                blocker_message=narrower_blocker,
                contract_scope_message=original_blocker,
            )

            self.assertIsNotNone(blocked)
            retried_run = load_run_state(project_root)
            retried_run.status = "failed"
            retried_run.last_error = narrower_blocker
            retried_run.active_blocker = {}
            save_run_state(project_root, retried_run)

            restored = orchestrator.restore_exhausted_provider_recovery()

            self.assertIsNotNone(restored)
            self.assertEqual(
                restored.active_blocker["category"],
                "provider_recovery_contract_unsatisfied",
            )

    def test_provider_resolve_does_not_exceed_configured_attempt_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            session = Session(orchestrator, mode="provider_resolve")
            state = SessionState(
                session_id="bounded-provider-recovery",
                mode="provider_resolve",
                status="executing",
                current_attempt=2,
                max_attempts=2,
                hard_ceiling=15,
            )

            with patch.object(session, "_call_agent") as call_agent:
                result = session._phase_provider_resolve_execute(state)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.current_attempt, 2)
            call_agent.assert_not_called()

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


class SessionContinuationTests(unittest.TestCase):
    def test_provider_session_resumes_only_for_identical_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            orchestrator = Orchestrator(root)
            session = Session(orchestrator, mode="collab")
            state = SessionState(
                session_id="continuation-session",
                mode="collab",
                status="executing",
            )
            state.provider_continuations["collab"] = {
                "provider_session_id": "legacy-provider-session",
                "provider": "mock",
                "head": head_ref(root),
                "workspace_fingerprint": worktree_fingerprint(root),
                "policy_version": 1,
            }
            requests = []

            def run(request):
                requests.append(request)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary="diagnosed",
                    provider_session_id="provider-session-1",
                )

            orchestrator._call_with_failover = run

            session._call_agent(state, "collab-1", "first")
            session._call_agent(state, "collab-2", "second")
            write_text(root / "product.py", "changed = True\n")
            session._call_agent(state, "collab-3", "third")

            self.assertEqual(requests[0].resume_session_id, "")
            self.assertEqual(requests[1].resume_session_id, "provider-session-1")
            self.assertEqual(requests[2].resume_session_id, "")
            self.assertEqual(
                state.provider_continuations["collab"]["policy_version"],
                2,
            )
            self.assertTrue(
                all(item.sandbox_mode == "read-only" for item in requests)
            )

    def test_checkpoint_content_is_deduplicated_by_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            orchestrator = Orchestrator(root)
            session = Session(orchestrator, mode="collab")
            source = root / "source.txt"
            source.write_text("same checkpoint bytes\n", encoding="utf-8")
            first = root / ".auto-agents" / "state" / "restore-a" / "source.txt"
            second = root / ".auto-agents" / "state" / "restore-b" / "source.txt"

            session._copy_checkpoint_file(source, first)
            session._copy_checkpoint_file(source, second)

            blobs = list(
                (root / ".auto-agents" / "state" / "checkpoint_blobs").glob("*/*")
            )
            self.assertEqual(len(blobs), 1)
            self.assertEqual(first.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertEqual(second.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertEqual(first.stat().st_ino, second.stat().st_ino)


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
                    content = (
                        'GOAL_ENVIRONMENT v1: {"decision":"simulated",'
                        '"summary":"Exercise video generation with test artifacts."}'
                    )
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 2:
                    content = "Understood.\nGOAL_CLEAR\n"
                    write_text(request.output_path, content)
                    return AgentResult(
                        ok=True, command=["mock"], output_path=request.output_path,
                        summary=content.strip(), stdout=content, returncode=0,
                    )
                if call_count["n"] == 3:
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
        self.assertEqual(state.attempt_epoch, 0)
        self.assertEqual(state.attempts_since_progress, 0)
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
            attempt_epoch=4,
            attempts_since_progress=5,
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
        self.assertEqual(restored.attempt_epoch, 4)
        self.assertEqual(restored.attempts_since_progress, 5)
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
        self.assertEqual(restored.attempt_epoch, 0)
        self.assertEqual(restored.attempts_since_progress, 2)
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
            state.provider_continuations = {
                "fix": {
                    "provider_session_id": "stale-provider-session",
                    "provider": "mock",
                    "policy_version": 2,
                }
            }
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
            self.assertEqual(result.provider_continuations, {})
            self.assertTrue(
                any(
                    item.get("action") == "provider_continuation_invalidated"
                    for item in result.execution_log
                )
            )

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

    def test_resume_completed_session_finishes_missing_commit_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            _configure_git_identity(project_root)
            commit_all(project_root, "chore: baseline")
            state = create_session(project_root, "collab")
            state.status = "completed"
            state.resolution = "goal_achieved"
            store = WorkflowStore(project_root)
            snapshot = store.create_root(
                WorkflowRef("collab", state.session_id)
            )
            state.workflow_id = snapshot.workflow_id
            save_session_state(project_root, state)
            orchestrator = Orchestrator(
                project_root,
                user_input_fn=lambda prompt: self.fail(
                    f"completion reconciliation prompted unexpectedly: {prompt}"
                ),
            )

            result = Session(orchestrator, mode="collab").resume(
                state.session_id
            )

            self.assertEqual(result.status, "completed")
            committed = json.loads(
                subprocess.run(
                    [
                        "git",
                        "show",
                        (
                            "HEAD:.auto-agents/state/sessions/"
                            f"{state.session_id}/session_state.json"
                        ),
                    ],
                    cwd=project_root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            self.assertEqual(committed["status"], "completed")
            self.assertEqual(store.load(snapshot.workflow_id).status, "completed")
            self.assertIsNone(store.active())

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
            _confirm_collab_state(newer)
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

    def test_hard_ceiling_uses_calls_since_last_progress_boundary(self) -> None:
        state = SessionState(
            session_id="x",
            current_attempt=40,
            attempt_epoch=3,
            attempts_since_progress=2,
            hard_ceiling=25,
        )
        session = self._make_stub_session()

        self.assertIsNone(session._should_stop(state, "still progressing"))
        state.attempts_since_progress = 25
        result = session._should_stop(state, "stalled in current epoch")

        self.assertIsNotNone(result)
        self.assertIn("since the last durable progress boundary", result)

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
        self.assertNotIn("attempt_epoch", row)
        self.assertNotIn("attempts_since_progress", row)
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

    def test_collab_interactive_attestations_exclude_release_only_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.verification_policy_version = 3
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
            self.assertEqual(final, progress)

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

    def test_non_comparable_failure_is_rerun_once_for_stable_identity(
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
                    targets=["tests/test_demo.py"],
                    parallel_safe=False,
                    serial_reason="ordered",
                )
            ]
            command = "python -m pytest -q tests/test_demo.py"
            diagnostic_command = (
                "python -m pytest -vv -rA --tb=short "
                "-o console_output_style=classic tests/test_demo.py"
            )
            failure_line = (
                "FAILED tests/test_demo.py::test_example - AssertionError\n"
            )
            candidate_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout="pytest exited before printing a summary",
                    )
                ],
                summary="command failed",
            )
            diagnostic_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=diagnostic_command,
                        ok=False,
                        returncode=1,
                        stdout=failure_line,
                    )
                ],
                summary="diagnostic failure",
            )
            baseline_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout=failure_line,
                    )
                ],
                summary="baseline failure",
            )
            state = SessionState(
                session_id="diagnostic-baseline",
                mode="fix",
                baseline_git_ref="baseline-ref",
                baseline_commands=[command],
            )
            session = Session(orchestrator, mode="fix")
            session._current_state = state
            plan = SimpleNamespace(
                commands=[command],
                parallel_groups=[],
                metadata={command: {}},
            )

            with (
                patch.object(session, "_session_gate_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_gate_executor_context",
                    return_value=nullcontext(None),
                ),
                patch("auto_agents.session.changed_paths", return_value=[]),
                patch(
                    "auto_agents.session.run_gate_plan",
                    side_effect=[candidate_gate, diagnostic_gate, baseline_gate],
                ) as run_plan,
            ):
                result = session._run_baseline_diff_verify()

            self.assertTrue(result["ok"])
            self.assertEqual(run_plan.call_count, 3)
            self.assertEqual(
                state.baseline_failures,
                ["tests/test_demo.py::test_example"],
            )

    def test_lazy_baseline_runs_candidate_only_failed_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            orchestrator.config.gates.verification_policy_version = 3
            orchestrator.config.gates.incremental_mode = "auto"
            orchestrator.config.gates.steps = [
                VerificationStep(
                    kind="test",
                    runner="pytest",
                    targets=["tests/test_newly_selected.py"],
                    parallel_safe=False,
                    serial_reason="ordered",
                )
            ]
            command = "python -m pytest -q tests/test_newly_selected.py"
            failure_line = (
                "FAILED tests/test_newly_selected.py::test_existing_failure "
                "- AssertionError\n"
            )
            failed_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout=failure_line,
                        comparable_failures=True,
                    )
                ],
                summary="failure",
            )
            state = SessionState(
                session_id="candidate-only-baseline",
                mode="fix",
                baseline_git_ref="baseline-ref",
                baseline_commands=[],
            )
            session = Session(orchestrator, mode="fix")
            session._current_state = state
            plan = SimpleNamespace(
                commands=[command],
                parallel_groups=[],
                metadata={command: {}},
            )

            with (
                patch.object(session, "_session_gate_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_gate_executor_context",
                    return_value=nullcontext(None),
                ),
                patch("auto_agents.session.changed_paths", return_value=[]),
                patch(
                    "auto_agents.session.run_gate_plan",
                    side_effect=[failed_gate, failed_gate],
                ) as run_plan,
            ):
                result = session._run_baseline_diff_verify()

            self.assertTrue(result["ok"])
            self.assertEqual(run_plan.call_count, 2)
            self.assertEqual(
                state.baseline_failures,
                ["tests/test_newly_selected.py::test_existing_failure"],
            )

    def test_non_comparable_failure_that_passes_identity_rerun_is_transient(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            command = "python -m pytest -q tests/test_demo.py"
            candidate_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout="pytest exited before printing a summary",
                    )
                ],
                summary="command failed",
            )
            rerun_gate = GateResult(
                ok=True,
                commands=[
                    CommandResult(
                        command=(
                            "python -m pytest -vv -rA --tb=short "
                            "-o console_output_style=classic tests/test_demo.py"
                        ),
                        ok=True,
                        returncode=0,
                    )
                ],
                summary="passed",
            )
            state = SessionState(session_id="transient-gate", mode="fix")
            session = Session(orchestrator, mode="fix")
            session._current_state = state
            plan = SimpleNamespace(
                commands=[command],
                parallel_groups=[],
                metadata={command: {}},
            )

            with (
                patch.object(session, "_session_gate_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_gate_executor_context",
                    return_value=nullcontext(None),
                ),
                patch("auto_agents.session.changed_paths", return_value=[]),
                patch(
                    "auto_agents.session.run_gate_plan",
                    side_effect=[candidate_gate, rerun_gate],
                ),
            ):
                result = session._run_baseline_diff_verify()

            self.assertTrue(result["ok"])
            self.assertEqual(result["failure_kind"], "transient_verification")

    def test_unresolved_identity_stops_fix_retry_and_persists_raw_log(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            command = "python -m pytest -q tests/test_demo.py"
            candidate_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stderr="worker exited without a pytest summary",
                    )
                ],
                summary="command failed",
            )
            diagnostic_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=(
                            "python -m pytest -vv -rA --tb=short "
                            "-o console_output_style=classic tests/test_demo.py"
                        ),
                        ok=False,
                        returncode=1,
                        stderr="worker exited again without a pytest summary",
                    )
                ],
                summary="diagnostic command failed",
            )
            state = SessionState(session_id="unresolved-gate", mode="fix")
            session = Session(orchestrator, mode="fix")
            session._current_state = state
            plan = SimpleNamespace(
                commands=[command],
                parallel_groups=[],
                metadata={command: {}},
            )

            with (
                patch.object(session, "_session_gate_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_gate_executor_context",
                    return_value=nullcontext(None),
                ),
                patch("auto_agents.session.changed_paths", return_value=[]),
                patch(
                    "auto_agents.session.run_gate_plan",
                    side_effect=[candidate_gate, diagnostic_gate],
                ),
            ):
                result = session._run_baseline_diff_verify()

            self.assertFalse(result["ok"])
            self.assertFalse(result["retry_fix"])
            self.assertEqual(
                result["failure_kind"],
                "verification_inconclusive",
            )
            self.assertIn(command, result["reason"])
            raw_log = project_root / str(result["raw_log_path"])
            self.assertTrue(raw_log.is_file())
            self.assertIn(
                "worker exited again without a pytest summary",
                raw_log.read_text(encoding="utf-8"),
            )

    def test_unrelated_command_level_baseline_does_not_hide_new_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root)
            command = "python -m pytest tests/test_new.py"
            gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=1,
                        stdout=(
                            "FAILED tests/test_new.py::test_regression - "
                            "AssertionError\n"
                        ),
                    )
                ],
                summary="command failed",
            )
            state = SessionState(
                session_id="unrelated-command-baseline",
                mode="fix",
                baseline_failures=["cmd:npm test -- unrelated-suite"],
            )
            session = Session(orchestrator, mode="fix")
            session._current_state = state
            plan = SimpleNamespace(
                commands=[command],
                parallel_groups=[],
                metadata={command: {}},
            )

            with (
                patch.object(session, "_session_gate_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_gate_executor_context",
                    return_value=nullcontext(None),
                ),
                patch("auto_agents.session.changed_paths", return_value=[]),
                patch("auto_agents.session.run_gate_plan", return_value=gate),
            ):
                result = session._run_baseline_diff_verify()

            self.assertFalse(result["ok"])
            self.assertNotEqual(
                result.get("failure_kind"),
                "verification_inconclusive",
            )
            self.assertIn(
                "tests/test_new.py::test_regression",
                result["reason"],
            )

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

            self.assertIn(
                "every Python-oriented verification_command must run inside it",
                prompt,
            )
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
