import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import conversation_history_path, load_run_state, save_run_state
from auto_agents.io_utils import write_text
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator


class ClarifyResumeTests(unittest.TestCase):
    """Test that crash-resume during clarify preserves conversation history."""

    def _setup_project(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        project_root = Path(td.name) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        spec_file = project_root / "spec.md"
        spec_file.write_text("# Idea\nBuild something.", encoding="utf-8")
        return project_root, spec_file

    def test_crash_resume_with_ready_to_generate_confirms_generation(self):
        """When history ends with READY_TO_GENERATE, the next run should
        resume to confirmation (not start fresh) and generate the brief."""
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)

        # Simulate a previous run that reached READY_TO_GENERATE but crashed
        history = [
            {"role": "user", "content": "I want feature X"},
            {"role": "agent", "content": "Got it. Any constraints?"},
            {"role": "user", "content": "No constraints."},
            {"role": "agent", "content": "Summary of requirements.\nREADY_TO_GENERATE"},
        ]
        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(history_path, json.dumps(history, ensure_ascii=False))

        # User confirms generation on resume
        orchestrator._user_input_fn = lambda prompt: "y"

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.return_value = AgentResult(
                ok=True, command=[], output_path=Path("."),
                summary="Generated brief.", stdout="",
            )
            state = orchestrator._run_interactive_clarify(state, spec_file)

            # Should have called the generate step, not a conversation round
            self.assertEqual(mock_run.call_count, 1)
            call_kwargs = mock_run.call_args.kwargs
            self.assertEqual(call_kwargs["stage_key"], "clarify-generate")

        # History should still have all 4 original messages
        saved_history = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved_history), 4)

    def test_crash_resume_with_ready_to_generate_user_rejects(self):
        """When user rejects at resumed confirmation, the conversation
        loop should start with the user's feedback appended."""
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)

        history = [
            {"role": "user", "content": "Build a CLI tool."},
            {"role": "agent", "content": "Understood.\nREADY_TO_GENERATE"},
        ]
        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(history_path, json.dumps(history, ensure_ascii=False))

        # First call: reject. Second call: provide feedback.
        call_count = [0]
        def mock_input(prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                return "n"
            if call_count[0] == 2:
                return "Add database support."
            return ""
        orchestrator._user_input_fn = mock_input

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.return_value = AgentResult(
                ok=True, command=[], output_path=Path("."),
                summary="READY_TO_GENERATE", stdout="",
            )
            captured = io.StringIO()
            with redirect_stderr(captured):
                state = orchestrator._run_interactive_clarify(state, spec_file)

        # History should have the original 2 messages + user feedback + new agent + generate
        saved_history = json.loads(history_path.read_text(encoding="utf-8"))
        user_msgs = [m for m in saved_history if m.get("role") == "user"]
        self.assertTrue(
            any("Add database support." in m.get("content", "") for m in user_msgs),
            "User's rejection feedback should be in the history"
        )
        self.assertIn("Agent is thinking, please wait...", captured.getvalue())

    def test_ready_to_generate_rejection_prints_thinking_indicator(self):
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)

        user_inputs = iter([
            "n",
            "Also support database-backed state.",
            "y",
        ])
        orchestrator._user_input_fn = lambda _prompt: next(user_inputs)

        clarify_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="Summary updated.\nREADY_TO_GENERATE",
            stdout="",
        )
        generate_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="Generated brief.",
            stdout="",
        )

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.side_effect = [clarify_result, clarify_result, generate_result]
            captured = io.StringIO()
            with redirect_stderr(captured):
                orchestrator._run_interactive_clarify(state, spec_file)

        output = captured.getvalue()
        self.assertIn("Agent is thinking, please wait...", output)
        self.assertIn("Also support database-backed state.", conversation_history_path(project_root, state.run_id).read_text(encoding="utf-8"))

    def test_ready_to_generate_rejection_without_feedback_keeps_clarifying(self):
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)
        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(
            history_path,
            json.dumps(
                [
                    {"role": "user", "content": "Build a CLI tool."},
                    {"role": "agent", "content": "Summary.\nREADY_TO_GENERATE"},
                ],
                ensure_ascii=False,
            ),
        )

        user_inputs = iter([
            "n",
            "",
            "Add authentication.",
            "y",
        ])
        orchestrator._user_input_fn = lambda _prompt: next(user_inputs)

        clarify_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="What authentication flow do you need?",
            stdout="",
        )
        ready_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="Updated summary.\nREADY_TO_GENERATE",
            stdout="",
        )
        generate_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="Generated brief.",
            stdout="",
        )

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.side_effect = [clarify_result, ready_result, generate_result]
            orchestrator._run_interactive_clarify(state, spec_file)

        call_stage_keys = [call.kwargs["stage_key"] for call in mock_run.call_args_list]
        self.assertEqual(
            call_stage_keys,
            ["clarify-conv-3", "clarify-conv-5", "clarify-generate"],
        )

        saved_history = json.loads(history_path.read_text(encoding="utf-8"))
        user_msgs = [m.get("content", "") for m in saved_history if m.get("role") == "user"]
        self.assertIn(
            "I am not ready to generate the project brief yet. Please continue clarifying the requirements with me.",
            user_msgs,
        )
        self.assertIn("Add authentication.", user_msgs)

    def test_rejection_reentry_preserves_history_and_appends_feedback(self):
        """Requirements rejection should preserve prior clarify context and
        append the rejection reason as new user feedback."""
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)
        state.rejected_stage = "clarify"
        state.rejection_reason = "Please add auth."

        history = [
            {"role": "user", "content": "Original discussion."},
            {"role": "agent", "content": "Plan.\nREADY_TO_GENERATE"},
        ]
        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(history_path, json.dumps(history, ensure_ascii=False))

        orchestrator._user_input_fn = lambda prompt: ""

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.return_value = AgentResult(
                ok=True, command=[], output_path=Path("."),
                summary="READY_TO_GENERATE", stdout="",
            )
            state = orchestrator._run_interactive_clarify(state, spec_file)

        saved_history = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertTrue(
            any(m.get("content") == "Original discussion." for m in saved_history),
            "Old clarify history should be preserved on rejection re-entry",
        )
        self.assertTrue(
            any("Please add auth." in m.get("content", "") for m in saved_history if m.get("role") == "user"),
            "Rejection reason should be appended as user feedback",
        )

        clarify_calls = [
            call for call in mock_run.call_args_list
            if call.kwargs.get("stage_key", "").startswith("clarify-conv")
        ]
        self.assertTrue(clarify_calls, "Rejecting requirements should run a new clarify conversation turn")
        prompt = clarify_calls[0].kwargs.get("prompt", "")
        self.assertIn("Original discussion.", prompt)
        self.assertIn("Please add auth.", prompt)
        self.assertIn("revision pass after a requirements rejection", prompt)

    def test_rejection_reentry_ready_proceeds_to_generate_without_extra_reply(self):
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)
        state.rejected_stage = "clarify"
        state.rejection_reason = "Fix the requirements audit findings."
        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(
            history_path,
            json.dumps(
                [
                    {"role": "user", "content": "Original discussion."},
                    {"role": "agent", "content": "Plan.\nREADY_TO_GENERATE"},
                ],
                ensure_ascii=False,
            ),
        )

        def fail_if_prompted(prompt: str) -> str:
            raise AssertionError(f"post-rejection ready clarify should not prompt for extra input: {prompt}")

        orchestrator._user_input_fn = fail_if_prompted
        clarify_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="Audit feedback addressed.\nREADY_TO_GENERATE",
            stdout="",
        )
        generate_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="Generated brief.",
            stdout="",
        )

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.side_effect = [clarify_result, generate_result]
            state = orchestrator._run_interactive_clarify(state, spec_file)

        call_stage_keys = [call.kwargs["stage_key"] for call in mock_run.call_args_list]
        self.assertEqual(call_stage_keys, ["clarify-conv-3", "clarify-generate"])
        self.assertEqual(state.stage_summaries["clarify"], "Generated brief.")

    def test_requirements_audit_rejection_reentry_generates_without_conversation_turn(self):
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)
        state.rejected_stage = "clarify"
        state.rejection_reason = (
            "The requirements audit failed. Use "
            f"{project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'} "
            "as the source of truth.\n"
            "Recovery route: rerun from clarify.\n"
            "Address every failing mandatory requirement before continuing.\n"
            "Fix only the requirements source of truth."
        )

        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(
            history_path,
            json.dumps([{"role": "user", "content": "Original discussion."}], ensure_ascii=False),
        )

        generate_result = AgentResult(
            ok=True,
            command=[],
            output_path=Path("."),
            summary="Generated brief.",
            stdout="",
        )

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.return_value = generate_result
            state = orchestrator._run_interactive_clarify(state, spec_file)

        call_stage_keys = [call.kwargs["stage_key"] for call in mock_run.call_args_list]
        self.assertEqual(call_stage_keys, ["clarify-generate"])
        self.assertEqual(state.stage_summaries["clarify"], "Generated brief.")
        saved_history = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertTrue(
            any(
                "The requirements audit failed" in item.get("content", "")
                for item in saved_history
                if item.get("role") == "user"
            )
        )

    def test_reject_requirements_keeps_clarify_history_file(self):
        project_root, _spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)
        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(history_path, json.dumps([{"role": "user", "content": "Keep me."}], ensure_ascii=False))

        orchestrator.reject("requirements", "Revise the brief.")

        self.assertTrue(history_path.exists())
        saved_history = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_history[0]["content"], "Keep me.")

    def test_non_rtg_trailing_agent_resumes_normally(self):
        """When last agent message does NOT have READY_TO_GENERATE,
        the normal trailing-agent resume logic should apply."""
        project_root, spec_file = self._setup_project()
        orchestrator = Orchestrator(project_root)

        state = load_run_state(project_root)

        history = [
            {"role": "user", "content": "Build a web app."},
            {"role": "agent", "content": "What framework?"},
        ]
        history_path = conversation_history_path(project_root, state.run_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(history_path, json.dumps(history, ensure_ascii=False))

        call_count = [0]
        def mock_input(prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                return "Use Flask."
            return ""
        orchestrator._user_input_fn = mock_input

        with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
            mock_run.return_value = AgentResult(
                ok=True, command=[], output_path=Path("."),
                summary="READY_TO_GENERATE", stdout="",
            )
            state = orchestrator._run_interactive_clarify(state, spec_file)

        saved_history = json.loads(history_path.read_text(encoding="utf-8"))
        user_msgs = [m for m in saved_history if m.get("role") == "user"]
        self.assertTrue(
            any("Use Flask." in m.get("content", "") for m in user_msgs),
            "User's reply to resumed non-RTG agent message should be in history"
        )


if __name__ == "__main__":
    unittest.main()
