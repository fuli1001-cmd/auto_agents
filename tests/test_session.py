"""Tests for the lightweight Session (fix / collab) workflows."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import List
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    create_session,
    list_sessions,
    load_session_state,
    save_session_state,
    session_state_path,
)
from auto_agents.git_ops import commit_all, working_tree_clean
from auto_agents.io_utils import write_text
from auto_agents.models import AgentResult, SessionState, DEFAULT_SESSION_MAX_ATTEMPTS
from auto_agents.orchestrator import Orchestrator
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

    def test_list_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            create_session(project_root, "fix")
            create_session(project_root, "collab")
            sessions = list_sessions(project_root)
            self.assertEqual(len(sessions), 2)
            modes = {s.mode for s in sessions}
            self.assertEqual(modes, {"fix", "collab"})

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


if __name__ == "__main__":
    unittest.main()
