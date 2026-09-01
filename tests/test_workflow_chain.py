from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_agents.cli import build_parser, main
from auto_agents.config import load_run_state, save_run_state
from auto_agents.git_ops import commit_only_paths
from auto_agents.io_utils import write_text
from auto_agents.models import AgentResult, SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_chain import (
    IterationSpecBuilder,
    WorkflowRef,
    WorkflowStore,
)
from auto_agents.workflow_runtime import WorkflowCoordinator

from test_session import _configure_git_identity, _make_project


def _commit_baseline(root: Path) -> None:
    _configure_git_identity(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: baseline"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )


class WorkflowStoreTests(unittest.TestCase):
    def test_atomic_text_write_preserves_existing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "document.md"
            path.write_text("before\n", encoding="utf-8")
            path.chmod(0o640)

            write_text(path, "after\n")

            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")

    def test_event_sequence_reloads_durable_head_for_stale_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            store = WorkflowStore(root)
            created = store.create_root(WorkflowRef("collab", "session-a"))
            first = store.load(created.workflow_id)
            stale = store.load(created.workflow_id)

            store.append_event(first, "first")
            store.append_event(stale, "second")

            events = sorted(
                (store.workflow_root(created.workflow_id) / "events").glob("*.json")
            )
            sequences = [json.loads(path.read_text())["sequence"] for path in events]
            self.assertEqual(sequences, [1, 2, 3])
            self.assertEqual(store.load(created.workflow_id).event_sequence, 3)

    def test_iteration_spec_is_immutable_idempotent_and_has_commit_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            store = WorkflowStore(root)
            snapshot = store.create_root(WorkflowRef("collab", "session-a"))
            handoff = store.prepare_handoff(
                snapshot,
                parent=WorkflowRef("collab", "session-a"),
                target="run",
                goal="Add export support",
                reason="The capability does not exist",
                payload={},
            )
            builder = IterationSpecBuilder(root)
            seed = {
                "title": "Export support",
                "goal": "Allow users to export results",
                "acceptance": ["An export can be downloaded"],
            }

            first = builder.materialize(handoff, seed)
            second = builder.materialize(handoff, seed)

            self.assertEqual(first["path"], second["path"])
            self.assertEqual(first["sha256"], second["sha256"])
            body = subprocess.run(
                ["git", "log", "-1", "--pretty=%B"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            self.assertIn(f"Auto-Agents-Operation: spec-{handoff.handoff_id}", body)
            self.assertIn(f"Auto-Agents-Workflow: {snapshot.workflow_id}", body)

    def test_corrupt_snapshot_is_rebuilt_from_hash_chained_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            store = WorkflowStore(root)
            snapshot = store.create_root(WorkflowRef("collab", "session-a"))
            store.append_event(snapshot, "diagnostic_progress", details={"step": 1})
            store.snapshot_path(snapshot.workflow_id).write_text("{broken", encoding="utf-8")

            rebuilt = store.load(snapshot.workflow_id)

            self.assertEqual(rebuilt.root, WorkflowRef("collab", "session-a"))
            self.assertEqual(rebuilt.event_sequence, 2)
            self.assertEqual(len(store.events(snapshot.workflow_id)), 2)

    def test_open_commit_operation_is_reconciled_from_git_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            orchestrator = Orchestrator(root)
            coordinator = WorkflowCoordinator(orchestrator)
            snapshot = coordinator.store.create_root(WorkflowRef("fix", "session-a"))
            coordinator.store.append_event(
                snapshot,
                "operation_intent",
                operation_id="interrupted-commit",
                details={"kind": "session_commit"},
            )
            write_text(root / "fixed.py", "ok = True\n")
            commit_only_paths(
                root,
                "fix: interrupted receipt",
                ["fixed.py"],
                trailers=["Auto-Agents-Operation: interrupted-commit"],
            )

            coordinator._reconcile_open_operations(snapshot)

            completed = [
                event
                for event in coordinator.store.events(snapshot.workflow_id)
                if event.get("kind") == "operation_completed"
                and event.get("operation_id") == "interrupted-commit"
            ]
            self.assertEqual(len(completed), 1)
            self.assertTrue(completed[0]["details"]["reconciled_after_interruption"])

    def test_resume_cli_accepts_explicit_workflow_and_returns_root_status(self) -> None:
        args = build_parser().parse_args(
            ["resume", "--project", "/tmp/demo", "--workflow", "wf-123"]
        )
        self.assertEqual(args.command, "resume")
        self.assertEqual(args.workflow, "wf-123")

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            store = WorkflowStore(root)
            snapshot = store.create_root(WorkflowRef("collab", "session-a"))
            completed = SessionState(
                session_id="session-a",
                mode="collab",
                status="completed",
                workflow_id=snapshot.workflow_id,
            )
            with patch.object(
                WorkflowCoordinator,
                "resume_workflow",
                return_value=completed,
            ) as resume:
                exit_code = main(
                    [
                        "resume",
                        "--project",
                        str(root),
                        "--workflow",
                        snapshot.workflow_id,
                        "--no-health-watch",
                    ]
                )
            self.assertEqual(exit_code, 0)
            resume.assert_called_once_with(snapshot.workflow_id)


class RoutedWorkflowTests(unittest.TestCase):
    def test_collab_routes_bug_through_fix_and_returns_for_goal_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            inputs = iter(["Stop the button crash", "y"])
            orchestrator = Orchestrator(
                root, user_input_fn=lambda _prompt: next(inputs, "")
            )

            def mock_run(request):
                prompt = request.prompt
                if "read-only diagnostic and routing" in prompt:
                    if "Child workflow fix returned" in prompt:
                        content = "GOAL_ACHIEVED: the button no longer crashes\n"
                    else:
                        content = (
                            "ROUTE_WORKFLOW v1: "
                            '{"target":"fix","reason":"existing regression",'
                            '"summary":"button crash","issue_seed":'
                            '{"summary":"button crash","expected":"no crash",'
                            '"actual":"crash"}}\n'
                        )
                elif "FIX_DISPOSITION v1" in prompt and "Before modifying files" in prompt:
                    content = (
                        "FIX_DISPOSITION v1: "
                        '{"decision":"fix","summary":"button crash",'
                        '"reason":"existing regression","reproduction":["click button"],'
                        '"expected":"no crash","actual":"crash","evidence_refs":[],'
                        '"affected_contracts":[],"verification_command":"",'
                        '"persistence_change":{"storage_transition":"none",'
                        '"compatibility_policy":"not_applicable"}}\n'
                    )
                elif "Fix this bug:" in prompt:
                    write_text(root / "app.py", "fixed = True\n")
                    content = "Fixed the crash.\nCOMMIT_MESSAGE: fix button crash\n"
                else:
                    content = "Goal understood.\nGOAL_CLEAR\n"
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
            state = Session(orchestrator, mode="collab").start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.lineage_changed_paths, ["app.py"])
            self.assertEqual((root / "app.py").read_text(), "fixed = True\n")
            self.assertTrue(any(item.get("action") == "child_returned" for item in state.execution_log))

    def test_standalone_fix_can_upgrade_to_run_and_verify_original_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            inputs = iter(["Add the missing export capability"])
            orchestrator = Orchestrator(
                root, user_input_fn=lambda _prompt: next(inputs, "")
            )

            def mock_agent(request):
                content = (
                    "FIX_DISPOSITION v1: "
                    '{"decision":"run_iteration","summary":"missing export",'
                    '"reason":"requires a new public capability","reproduction":[],'
                    '"expected":"export exists","actual":"export is absent",'
                    '"evidence_refs":[],"affected_contracts":[],"verification_command":"",'
                    '"spec_seed":{"title":"Export capability",'
                    '"goal":"Add export support","gap":"Export is absent",'
                    '"capability":"Users can export results",'
                    '"acceptance":["Export succeeds"],"non_goals":[],"evidence":[],'
                    '"open_decisions":[]}}\n'
                )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_agent

            def fake_run(**_kwargs):
                write_text(root / "export.py", "enabled = True\n")
                commit_only_paths(root, "feat: add export support", ["export.py"])
                run_state = load_run_state(root)
                run_state.status = "completed"
                run_state.current_stage = "readme"
                save_run_state(root, run_state)
                return run_state

            orchestrator.run = fake_run
            state = Session(orchestrator, mode="fix").start()

            self.assertEqual(state.status, "completed")
            self.assertEqual(state.resolution, "resolved_by_iteration")
            self.assertIn("export.py", state.lineage_changed_paths)
            self.assertEqual(len(list((root / "specs" / "iterations").glob("*.md"))), 1)

    def test_late_fix_upgrade_rolls_back_attempt_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            write_text(root / "app.py", "value = 1\n")
            commit_only_paths(root, "feat: add app", ["app.py"])
            inputs = iter(["Repair export behavior"])
            orchestrator = Orchestrator(
                root, user_input_fn=lambda _prompt: next(inputs, "")
            )
            calls = {"count": 0}

            def mock_agent(request):
                calls["count"] += 1
                if calls["count"] == 1:
                    content = (
                        "FIX_DISPOSITION v1: "
                        '{"decision":"fix","summary":"export defect",'
                        '"reason":"initially appears bounded","reproduction":[],'
                        '"expected":"export","actual":"missing","evidence_refs":[],'
                        '"affected_contracts":[],"verification_command":"",'
                        '"persistence_change":{"storage_transition":"none",'
                        '"compatibility_policy":"not_applicable"}}\n'
                    )
                else:
                    write_text(root / "app.py", "unsafe partial edit\n")
                    content = (
                        "FIX_DISPOSITION v1: "
                        '{"decision":"run_iteration","reason":"needs public API",'
                        '"summary":"export capability","spec_seed":'
                        '{"title":"Export API","goal":"Add export API",'
                        '"gap":"No public API","capability":"Public export",'
                        '"acceptance":["API exports"],"non_goals":[],"evidence":[],'
                        '"open_decisions":[]}}\n'
                    )
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            orchestrator.adapter.run = mock_agent

            def fake_run(**_kwargs):
                self.assertEqual((root / "app.py").read_text(), "value = 1\n")
                write_text(root / "export.py", "enabled = True\n")
                commit_only_paths(root, "feat: add export API", ["export.py"])
                run_state = load_run_state(root)
                run_state.status = "completed"
                run_state.current_stage = "readme"
                save_run_state(root, run_state)
                return run_state

            orchestrator.run = fake_run
            state = Session(orchestrator, mode="fix").start()

            self.assertEqual(state.status, "completed")
            self.assertEqual((root / "app.py").read_text(), "value = 1\n")

    def test_unrelated_active_run_blocks_new_iteration_without_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            orchestrator = Orchestrator(root)
            current = load_run_state(root)
            current.status = "pending"
            current.current_stage = "implement"
            save_run_state(root, current)
            coordinator = WorkflowCoordinator(orchestrator)
            snapshot = coordinator.store.create_root(WorkflowRef("collab", "parent"))
            handoff = coordinator.store.prepare_handoff(
                snapshot,
                parent=WorkflowRef("collab", "parent"),
                target="run",
                goal="Add capability",
                reason="feature gap",
                payload={"spec_seed": {"title": "Capability"}},
            )

            result = coordinator._drive_run_child(handoff, snapshot)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["resolution"], "active_run_conflict")
            self.assertFalse((root / "specs" / "iterations").exists())

    def test_collab_restores_product_mutation_instead_of_committing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            write_text(root / "app.py", "value = 1\n")
            commit_only_paths(root, "feat: add app", ["app.py"])
            orchestrator = Orchestrator(root, user_input_fn=lambda _prompt: "")
            session = Session(orchestrator, mode="collab")
            state = SessionState(
                session_id="readonly-collab",
                mode="collab",
                status="executing",
                goal="Diagnose app",
                hard_ceiling=1,
            )

            def mutate(_state, _label, _prompt):
                write_text(root / "app.py", "value = 2\n")
                return "I changed the file directly."

            session._call_agent = mutate
            result = session._phase_collab_loop(state)

            self.assertEqual(result.status, "failed")
            self.assertEqual((root / "app.py").read_text(), "value = 1\n")
            self.assertTrue(
                any(item.get("action") == "collab_mutation_restored" for item in result.execution_log)
            )

    def test_resume_reconciles_durable_collab_readonly_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            write_text(root / "app.py", "value = 1\n")
            commit_only_paths(root, "feat: add app", ["app.py"])
            orchestrator = Orchestrator(root)
            session = Session(orchestrator, mode="collab")
            store = WorkflowStore(root)
            snapshot = store.create_root(WorkflowRef("collab", "session-a"))
            state = SessionState(
                session_id="session-a",
                mode="collab",
                status="executing",
                workflow_id=snapshot.workflow_id,
            )
            before = orchestrator._worktree_change_snapshot()
            checkpoint = (
                store.workflow_root(snapshot.workflow_id)
                / "checkpoints"
                / "collab-session-a-1"
            )
            session._capture_collab_restore_point(checkpoint, before)
            write_text(root / "app.py", "interrupted mutation\n")

            session._reconcile_interrupted_collab_checkpoints(state)

            self.assertEqual((root / "app.py").read_text(), "value = 1\n")
            self.assertFalse(checkpoint.exists())
            self.assertTrue(
                any(
                    item.get("action") == "collab_interruption_reconciled"
                    for item in state.execution_log
                )
            )

    def test_failed_child_rollback_preserves_preexisting_staged_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            write_text(root / "app.py", "value = 1\n")
            write_text(root / "user.txt", "base\n")
            commit_only_paths(root, "feat: add files", ["app.py", "user.txt"])
            write_text(root / "user.txt", "user staged\n")
            subprocess.run(["git", "add", "user.txt"], cwd=root, check=True)

            orchestrator = Orchestrator(root)
            coordinator = WorkflowCoordinator(orchestrator)
            snapshot = coordinator.store.create_root(WorkflowRef("collab", "parent"))
            handoff = coordinator.store.prepare_handoff(
                snapshot,
                parent=WorkflowRef("collab", "parent"),
                target="fix",
                goal="Repair app",
                reason="test rollback",
                payload={},
            )
            coordinator._ensure_handoff_checkpoint(snapshot, handoff)
            write_text(root / "app.py", "child edit\n")
            write_text(root / "user.txt", "child overwrote user\n")
            write_text(root / "new.py", "partial = True\n")

            rolled_back = coordinator._rollback_handoff_uncommitted(snapshot, handoff)

            self.assertEqual((root / "app.py").read_text(), "value = 1\n")
            self.assertEqual((root / "user.txt").read_text(), "user staged\n")
            self.assertFalse((root / "new.py").exists())
            self.assertEqual(set(rolled_back), {"app.py", "new.py", "user.txt"})
            cached = subprocess.run(
                ["git", "diff", "--cached", "--", "user.txt"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            self.assertIn("user staged", cached)

    def test_resume_consumes_child_commit_recorded_before_parent_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            baseline = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            orchestrator = Orchestrator(root, user_input_fn=lambda _prompt: "y")
            coordinator = WorkflowCoordinator(orchestrator)
            parent = SessionState(
                session_id="parent-collab",
                mode="collab",
                status="waiting_child",
                goal="Verify repaired app",
                baseline_git_ref=baseline,
                baseline_head_ref=baseline,
                lineage_head_ref=baseline,
            )
            snapshot = coordinator.store.create_root(
                WorkflowRef("collab", parent.session_id)
            )
            parent.workflow_id = snapshot.workflow_id
            handoff = coordinator.store.prepare_handoff(
                snapshot,
                parent=WorkflowRef("collab", parent.session_id),
                target="fix",
                goal=parent.goal,
                reason="existing regression",
                payload={"head_before": baseline},
            )
            child = SessionState(
                session_id="child-fix",
                mode="fix",
                status="completed",
                resolution="fixed",
                workflow_id=snapshot.workflow_id,
                parent_handoff_id=handoff.handoff_id,
                goal=parent.goal,
            )
            handoff.payload["child_session_id"] = child.session_id
            coordinator.store.save_handoff(handoff)
            coordinator.store.bind_child(
                snapshot, handoff, WorkflowRef("fix", child.session_id)
            )
            parent.active_handoff_id = handoff.handoff_id
            from auto_agents.config import save_session_state

            save_session_state(root, parent)
            save_session_state(root, child)
            write_text(root / "app.py", "fixed = True\n")
            commit_only_paths(root, "fix: repair app", ["app.py"])

            def goal_agent(request):
                content = "GOAL_ACHIEVED: repaired app verified\n"
                write_text(request.output_path, content)
                return AgentResult(
                    ok=True,
                    command=["mock"],
                    output_path=request.output_path,
                    summary=content.strip(),
                    stdout=content,
                    returncode=0,
                )

            orchestrator.adapter.run = goal_agent
            result = coordinator.resume_workflow(snapshot.workflow_id)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.lineage_changed_paths, ["app.py"])
            returned = coordinator.store.load_handoff(handoff.handoff_id)
            self.assertTrue(returned.returned_at)

    def test_repeated_identical_owner_loss_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp)
            _commit_baseline(root)
            orchestrator = Orchestrator(root)
            coordinator = WorkflowCoordinator(orchestrator)
            coordinator.store.create_root(WorkflowRef("collab", "session-a"))
            payload = {
                "detected_at": "now",
                "owner": {"pid": 123},
                "control": {"updated_at": "then"},
            }
            limit = orchestrator.config.execution.recovery.max_rounds
            for _ in range(limit):
                coordinator.reconcile_interruption(payload)
            with self.assertRaisesRegex(RuntimeError, "interrupted repeatedly"):
                coordinator.reconcile_interruption(payload)


if __name__ == "__main__":
    unittest.main()
