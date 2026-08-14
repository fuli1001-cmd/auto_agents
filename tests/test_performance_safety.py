import tempfile
import threading
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_baseline_cache import GateBaselineCache
from auto_agents.gates import gate_plan_from_verification_steps, run_gate_plan
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import (
    AgentResult,
    CommandResult,
    GateParallelGroup,
    GateResult,
    RunState,
    TaskSpec,
    VerificationStep,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.provider_limits import ParallelTuningStore
from auto_agents.requirements import (
    requirements_audit_context_sha256,
    run_requirements_audit,
)


def _requirement(pattern: str) -> dict:
    return {
        "id": "REQ-001",
        "text": "Remove the legacy behavior.",
        "source": "spec.md",
        "status": "active",
        "priority": "mandatory",
        "acceptance_oracles": ["The legacy behavior is absent."],
        "oracle_type": "deterministic_test",
        "oracle_strength": "behavioral",
        "evidence_boundary": "internal_state",
        "forbidden_proxy_oracles": [],
        "forbidden_patterns": [pattern],
        "external_docs_required": False,
        "provider_reference": "",
        "notes": "",
    }


class RequirementsAuditPerformanceTests(unittest.TestCase):
    def _project(self, root: Path, pattern: str) -> TaskSpec:
        Orchestrator.init_project(root, "demo", "mock")
        write_json(
            root / ".auto-agents" / "state" / "requirements_trace.json",
            {"version": 1, "requirements": [_requirement(pattern)]},
        )
        return TaskSpec(
            task_id="task-001",
            title="Done",
            description="Done",
            acceptance=["done"],
            requirement_ids=["REQ-001"],
            status="done",
        )

    def test_dangerous_pattern_fails_closed_without_running_matcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            task = self._project(
                root,
                r"(?s)for\s+.*check.*(?:retry|attempt).*for\s+.*(?:all_checks|checks)",
            )
            write_text(root / "large.ts", "for check " + ("x" * 500_000))

            started = time.monotonic()
            result = run_requirements_audit(root, [task])

            self.assertLess(time.monotonic() - started, 2.0)
            self.assertFalse(result["ok"])
            blockers = result["issues"][0]["blockers"]
            self.assertTrue(any(item["kind"] == "forbidden_pattern_safety" for item in blockers))
            self.assertEqual(result["metrics"]["matcher_calls"], 0)

    def test_incremental_cache_rescans_only_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            task = self._project(root, r"legacy_gateway")
            write_text(root / "one.py", "print('ok')\n")
            write_text(root / "two.py", "print('ok')\n")

            first = run_requirements_audit(root, [task])
            second = run_requirements_audit(root, [task])
            write_text(root / "one.py", "print('changed')\n")
            third = run_requirements_audit(root, [task])

            self.assertGreater(first["metrics"]["matcher_calls"], 0)
            self.assertEqual(second["metrics"]["matcher_calls"], 0)
            self.assertEqual(third["metrics"]["matcher_calls"], 1)
            self.assertGreater(third["metrics"]["cache_hits"], 0)

    def test_audit_context_ignores_runtime_commit_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            task = self._project(root, r"legacy_gateway")
            before = requirements_audit_context_sha256(root, [task])
            task.commit_sha = "a" * 40
            after = requirements_audit_context_sha256(root, [task])
            self.assertEqual(before, after)


class ParallelTuningTests(unittest.TestCase):
    def test_parallel_worker_failure_preserves_scope_rewind_metadata(self) -> None:
        task = TaskSpec(
            task_id="task-split",
            title="Split",
            description="Split this oversized task.",
            acceptance=["done"],
        )
        gate_result = {
            "ok": False,
            "reason": "scope_overflow: split required",
            "review": "two independent slices",
            "rewind_to_plan": True,
            "split_task_id": task.task_id,
            "split_trigger": "two real review failures",
            "split_fingerprint": "fingerprint",
            "arbiter": {
                "decision": "SPLIT",
                "rationale": "separate audio and cover work",
                "split_axis": ["audio", "cover"],
            },
        }

        result = Orchestrator._parallel_task_failure_result(task, gate_result)

        self.assertTrue(result["rewind_to_plan"])
        self.assertEqual(result["split_trigger"], "two real review failures")
        self.assertEqual(result["arbiter"]["decision"], "SPLIT")

    def test_parallel_scope_overflow_routes_to_plan_and_preserves_peer_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.execution.parallel_tasks.enabled = True
            orchestrator.config.execution.parallel_tasks.workers = 2
            split_task = TaskSpec(
                task_id="task-split",
                title="Split",
                description="Split this oversized task.",
                acceptance=["done"],
            )
            peer_task = TaskSpec(
                task_id="task-peer",
                title="Peer",
                description="Independent successful work.",
                acceptance=["done"],
            )
            tasks = [split_task, peer_task]
            state = RunState(run_id="test", current_stage="implement", tasks=tasks)
            split_payload = {**split_task.to_dict(), "status": "blocked"}
            peer_payload = {**peer_task.to_dict(), "status": "done"}
            results = {
                split_task.task_id: {
                    "ok": False,
                    "task": split_payload,
                    "reason": "scope_overflow: split required",
                    "review": "two independent slices",
                    "rewind_to_plan": True,
                    "split_task_id": split_task.task_id,
                    "split_trigger": "two real review failures",
                    "split_fingerprint": "fingerprint",
                    "arbiter": {"decision": "SPLIT"},
                },
                peer_task.task_id: {
                    "ok": True,
                    "task": peer_payload,
                    "reason": "",
                    "review": "passed",
                    "commit_sha": "a" * 40,
                    "result_ref": "",
                    "changed_paths": ["peer.py"],
                    "verify_current_failure_ids": [],
                },
            }
            orchestrator._parallel_execution_fallback_reason = lambda _tasks: ""
            orchestrator._parallel_worker_count = lambda: 2
            orchestrator._log_parallel_worker_resolution = Mock()
            orchestrator._ensure_evidence_preflight = lambda _state, _task: None
            orchestrator._require_clean_tree_excluding_agent_instructions = Mock()
            orchestrator._deferred_parallel_task_reasons = lambda _tasks: []
            orchestrator._run_parallel_task_batch = Mock(return_value=results)
            orchestrator._integrate_parallel_task_result = Mock(return_value="b" * 40)
            orchestrator._warm_clean_head_verify_baseline = Mock()
            orchestrator._delete_parallel_result_ref = Mock()
            orchestrator._record_parallel_success = Mock(return_value=2)
            rewind = Mock(return_value=state)
            orchestrator._handle_scope_overflow_rewind = rewind

            result = orchestrator._run_parallel_implementation_loop(
                state, tasks, max_tasks=None
            )

            self.assertIs(result, state)
            self.assertEqual(peer_task.status, "done")
            rewind.assert_called_once()
            self.assertTrue(rewind.call_args.kwargs["preserve_current_head"])
            orchestrator._record_parallel_success.assert_not_called()

    def test_conflict_aware_batch_packing_skips_predicted_path_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            state = RunState(run_id="test")
            first = TaskSpec(
                task_id="task-a",
                title="A",
                description="Update app/shared.py.",
                acceptance=["done"],
            )
            overlapping = TaskSpec(
                task_id="task-b",
                title="B",
                description="Also update app/shared.py.",
                acceptance=["done"],
            )
            independent = TaskSpec(
                task_id="task-c",
                title="C",
                description="Update app/independent.py.",
                acceptance=["done"],
            )

            batch = orchestrator._select_parallel_batch(
                state, [first, overlapping, independent], 2
            )

            self.assertEqual([task.task_id for task in batch], ["task-a", "task-c"])

    def test_overlapping_worker_result_is_persisted_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="task-b",
                title="B",
                description="Update shared code.",
                acceptance=["done"],
            )
            tasks = [task]
            state = RunState(run_id="test", current_stage="implement", tasks=tasks)
            result = {
                "task": {**task.to_dict(), "status": "done"},
                "commit_sha": "a" * 40,
                "result_ref": "refs/auto-agents/runs/test/tasks/task-b",
                "base_ref": "b" * 40,
                "changed_paths": ["app/shared.py"],
                "verify_current_failure_ids": [],
            }

            orchestrator._defer_parallel_task_result(
                state,
                tasks,
                task,
                result,
                {"app/shared.py"},
                ["pytest -q tests/test_peer.py"],
            )

            pending = state.resume_context["parallel_integration_pending"]["task-b"]
            self.assertEqual(pending["commit_sha"], "a" * 40)
            self.assertEqual(pending["overlapping_paths"], ["app/shared.py"])
            self.assertEqual(
                pending["peer_verification_commands"],
                ["pytest -q tests/test_peer.py"],
            )
            self.assertEqual(
                state.resume_context["parallel_integration_metrics"]["deferred"], 1
            )

    def test_clean_replay_integrates_retained_result_without_agent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="task-b",
                title="B",
                description="Update shared code.",
                acceptance=["done"],
            )
            tasks = [task]
            state = RunState(run_id="test", current_stage="implement", tasks=tasks)
            state.resume_context["parallel_integration_pending"] = {
                task.task_id: {
                    "task": {**task.to_dict(), "status": "done"},
                    "commit_sha": "a" * 40,
                    "result_ref": "refs/auto-agents/runs/test/tasks/task-b",
                    "changed_paths": ["app/shared.py"],
                }
            }
            orchestrator._replay_parallel_pending_result = Mock(
                return_value={
                    "ok": True,
                    "commit_sha": "c" * 40,
                    "verify_current_failure_ids": [],
                }
            )
            orchestrator._integrate_parallel_task_result = Mock(return_value="d" * 40)
            orchestrator._delete_parallel_result_ref = Mock()
            orchestrator._warm_clean_head_verify_baseline = Mock()

            outcome = orchestrator._process_next_parallel_pending_integration(
                state, tasks
            )

            self.assertEqual(outcome, "integrated")
            self.assertEqual(task.status, "done")
            self.assertEqual(task.commit_sha, "d" * 40)
            self.assertNotIn("parallel_integration_pending", state.resume_context)
            metrics = state.resume_context["parallel_integration_metrics"]
            self.assertEqual(metrics["integrated"], 1)
            self.assertEqual(metrics["replayed"], 1)

    def test_replay_conflict_schedules_persistent_sequential_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="task-b",
                title="B",
                description="Update shared code.",
                acceptance=["done"],
                recovery_round=2,
                verify_retry_epoch=4,
                verify_history=[
                    {
                        "attempt": 2,
                        "decision": "fail",
                        "summary": "worker verification failed",
                        "failure_ids": ["tests/test_shared.py::test_contract"],
                        "comparable_failures": True,
                        "recovery_epoch": 0,
                        "recovery_round": 2,
                        "verify_retry_epoch": 4,
                    }
                ],
            )
            tasks = [task]
            state = RunState(run_id="test", current_stage="implement", tasks=tasks)
            state.resume_context["parallel_integration_pending"] = {
                task.task_id: {
                    "task": {**task.to_dict(), "status": "done"},
                    "commit_sha": "a" * 40,
                    "result_ref": "refs/auto-agents/runs/test/tasks/task-b",
                    "changed_paths": ["app/shared.py"],
                }
            }
            orchestrator._replay_parallel_pending_result = Mock(
                return_value={"ok": False, "kind": "conflict", "reason": "conflict"}
            )
            orchestrator._delete_parallel_result_ref = Mock()

            outcome = orchestrator._process_next_parallel_pending_integration(
                state, tasks
            )

            self.assertEqual(outcome, "retry")
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.recovery_round, 2)
            self.assertEqual(task.verify_retry_epoch, 5)
            self.assertEqual(len(task.verify_history), 1)
            analysis = orchestrator._analyze_verify_failure(
                task,
                ["tests/test_shared.py::test_contract"],
            )
            self.assertFalse(analysis["stop_retry"])
            self.assertEqual(
                state.resume_context["parallel_sequential_retry_tasks"], ["task-b"]
            )
            self.assertEqual(
                state.resume_context["parallel_integration_metrics"]["replay_conflicts"],
                1,
            )

    def test_resume_capture_preserves_parallel_integration_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            state = RunState(run_id="test")
            state.resume_context = {
                "parallel_integration_pending": {
                    "task-b": {"result_ref": "refs/auto-agents/test"}
                },
                "parallel_sequential_retry_tasks": ["task-c"],
                "parallel_integration_metrics": {"deferred": 1},
                "parallel_task_path_history": {
                    "task-b": {"fingerprint": "fp", "paths": ["app/shared.py"]}
                },
                "implementation_ready_tasks": {"task-d": True},
                Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT: True,
            }

            orchestrator._capture_resume_context(
                state,
                spec_file=root / "spec.md",
                auto_approve=True,
                allow_dirty_tree=False,
                max_tasks=None,
                skip_validate=False,
                print_agent_output=False,
                provider_kind="mock",
                doc_language="en",
            )

            self.assertIn("parallel_integration_pending", state.resume_context)
            self.assertEqual(
                state.resume_context["parallel_sequential_retry_tasks"], ["task-c"]
            )
            self.assertEqual(
                state.resume_context["implementation_ready_tasks"], {"task-d": True}
            )
            self.assertTrue(
                state.resume_context[
                    Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
                ]
            )

    def test_integration_conflict_batch_does_not_scale_workers_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.execution.parallel_tasks.workers = "auto"
            orchestrator.config.execution.parallel_tasks.adaptive = True

            workers = orchestrator._record_parallel_inefficiency(
                4, launched=4, integrated=2
            )

            self.assertEqual(workers, 3)
            entry = orchestrator._parallel_tuning.get_entry(
                orchestrator._parallel_tuning_key()
            )
            self.assertEqual(entry["event"], "integration_conflict")

    def test_parallel_scheduler_rechecks_after_resume_task_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.execution.parallel_tasks.enabled = True
            orchestrator.config.execution.parallel_tasks.workers = 2
            recovery = TaskSpec(
                task_id="task-recovery",
                title="Resume",
                description="Resume interrupted work.",
                acceptance=["done"],
                status="in_progress",
            )
            pending = TaskSpec(
                task_id="task-pending",
                title="Next",
                description="Run after recovery.",
                acceptance=["done"],
                status="pending",
            )
            tasks = [recovery, pending]
            state = RunState(run_id="test", current_stage="implement", tasks=tasks)
            executed = []

            def fallback_reason(current_tasks):
                if any(task.status not in {"pending", "done"} for task in current_tasks):
                    return (
                        "parallel task execution only supports fresh pending/done task sets; "
                        "resume and blocked retries stay sequential"
                    )
                return ""

            def execute(_state, _tasks, task):
                executed.append(task.task_id)
                task.status = "done"
                return None

            resolution_log = Mock()
            orchestrator._parallel_execution_fallback_reason = fallback_reason
            orchestrator._parallel_worker_count = lambda: 2
            orchestrator._log_parallel_worker_resolution = resolution_log
            orchestrator._ready_parallel_tasks = lambda current: [
                task for task in current if task.status == "pending"
            ]
            orchestrator._ensure_evidence_preflight = lambda _state, _task: ""
            orchestrator._execute_task_in_main_worktree = execute

            result = orchestrator._run_parallel_implementation_loop(
                state, tasks, max_tasks=None
            )

            self.assertEqual(executed, ["task-recovery", "task-pending"])
            resolution_log.assert_called_once_with(2)
            self.assertEqual([task.status for task in result.tasks], ["done", "done"])

    def test_interrupted_implementation_requires_success_marker_before_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="task-001",
                title="Interrupted",
                description="Resume safely.",
                acceptance=["done"],
                status="in_progress",
            )
            state = RunState(run_id="test", current_stage="implement", tasks=[task])
            state.agent_attempts["implement-task-001"] = 1
            write_text(root / "partial.py", "PARTIAL = True\n")

            orchestrator._set_implementation_ready_marker(state, task, False)
            self.assertFalse(
                orchestrator._in_progress_implementation_is_ready(state, task)
            )
            orchestrator._set_implementation_ready_marker(state, task, True)
            self.assertTrue(
                orchestrator._in_progress_implementation_is_ready(state, task)
            )

    def test_missing_planned_pytest_target_is_retryable_implementation_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)

            result = orchestrator._quick_verify_failure_details(
                ["python -m pytest -q tests/test_planned_proof.py::test_contract"]
            )

            self.assertIsNotNone(result)
            reason, retryable = result
            self.assertIn("references missing pytest target", reason)
            self.assertTrue(retryable)

    def test_worker_one_recovers_with_two_worker_canary_after_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = [1_000]
            store = ParallelTuningStore(Path(tmp), time_fn=lambda: now[0])
            store.put_workers("new", 1, event="hard_pressure")

            now[0] = 4_599
            active = store.resolve_workers(
                "new", initial_workers=3, cooldown_seconds=3_600
            )
            now[0] = 4_600
            canary = store.resolve_workers(
                "new", initial_workers=3, cooldown_seconds=3_600
            )

            self.assertEqual(active["workers"], 1)
            self.assertTrue(active["cooldown_active"])
            self.assertEqual(canary["workers"], 2)
            self.assertEqual(canary["event"], "canary")

    def test_legacy_tuning_key_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ParallelTuningStore(Path(tmp), time_fn=lambda: 10)
            store.put_workers("legacy", 3, event="success")
            result = store.resolve_workers(
                "new",
                initial_workers=2,
                cooldown_seconds=3_600,
                legacy_keys=["legacy"],
            )
            self.assertEqual(result["workers"], 3)
            self.assertEqual(result["source_key"], "legacy")

    def test_soft_pressure_requires_two_observations_and_hard_pressure_is_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.execution.parallel_tasks.workers = "auto"
            orchestrator.config.execution.parallel_tasks.adaptive = True
            orchestrator.config.execution.parallel_tasks.max_auto_workers = 4

            first = orchestrator._record_parallel_pressure(4, "soft")
            second = orchestrator._record_parallel_pressure(4, "soft")
            hard = orchestrator._record_parallel_pressure(4, "hard")

            self.assertEqual(first, 4)
            self.assertEqual(second, 2)
            self.assertEqual(hard, 2)
            self.assertEqual(
                orchestrator._parallel_pressure_kind({"reason": "HTTP 429 rate limit"}),
                "hard",
            )
            self.assertEqual(
                orchestrator._parallel_pressure_kind({"reason": "provider timeout"}),
                "soft",
            )


class GateOptimizationTests(unittest.TestCase):
    def test_only_explicitly_safe_steps_are_grouped(self) -> None:
        steps = [
            VerificationStep(runner="pytest", targets=["tests/a.py"]),
            VerificationStep(runner="pytest", targets=["tests/b.py"], parallel_safe=True),
            VerificationStep(runner="pytest", targets=["tests/c.py"], parallel_safe=True),
        ]
        commands, groups = gate_plan_from_verification_steps(steps, Path("/tmp/demo"))
        self.assertEqual(len(commands), 1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].commands), 2)

    def test_parallel_gate_respects_worker_cap_and_preserves_order(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        def fake_run(command: str, cwd: Path, **_kwargs) -> CommandResult:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return CommandResult(command=command, ok=True, returncode=0, stdout=command)

        with patch("auto_agents.gates._run_command", side_effect=fake_run):
            result = run_gate_plan(
                [],
                [GateParallelGroup(name="safe", commands=["a", "b", "c", "d"])],
                Path("/tmp"),
                collect_all=True,
                parallel_workers=2,
            )
        self.assertEqual(peak, 2)
        self.assertEqual([item.stdout for item in result.commands], ["a", "b", "c", "d"])

    def test_command_cache_runs_only_new_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = GateBaselineCache(Path(tmp), Path(tmp) / "cache.sqlite3")
            first = CommandResult(command="check-a", ok=True, returncode=0)
            cache.put(
                "head",
                ["check-a"],
                collect_all=True,
                failure_ids=[],
                command_results=[first],
            )
            self.assertEqual(
                cache.missing_commands(
                    "head", ["check-a", "check-b"], collect_all=True
                ),
                ["check-b"],
            )
            second = CommandResult(
                command="check-b",
                ok=False,
                returncode=1,
                stdout="FAILED tests/test_b.py::test_b",
            )
            cache.put(
                "head",
                ["check-a", "check-b"],
                collect_all=True,
                failure_ids=["tests/test_b.py::test_b"],
                command_results=[second],
            )
            self.assertEqual(
                cache.get("head", ["check-a", "check-b"], collect_all=True),
                ["tests/test_b.py::test_b"],
            )

    def test_command_cache_promotes_resume_baseline_and_leaves_new_command_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = GateBaselineCache(Path(tmp), Path(tmp) / "cache.sqlite3")
            cache.put(
                "old",
                ["check-a"],
                collect_all=True,
                failure_ids=[],
                command_results=[
                    CommandResult(command="check-a", ok=True, returncode=0)
                ],
            )

            promoted = cache.promote(
                "old", "new", ["check-a", "check-b"], collect_all=True
            )

            self.assertEqual(promoted, 1)
            self.assertEqual(
                cache.missing_commands(
                    "new", ["check-a", "check-b"], collect_all=True
                ),
                ["check-b"],
            )

    def test_recovery_state_promotes_run_baseline_without_rerunning_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            command = "python3 -c \"print('ok')\""
            orchestrator.config.gates.commands = [command]
            task = TaskSpec(
                task_id="task-001",
                title="Resume",
                description="Resume interrupted work.",
                acceptance=["done"],
                status="blocked",
            )
            state = RunState(
                run_id="test",
                current_stage="implement",
                tasks=[task],
                implement_verify_baseline_ref="old",
            )
            orchestrator._gate_baseline_cache.put(
                "old",
                [command],
                collect_all=True,
                failure_ids=[],
                command_results=[
                    CommandResult(command=command, ok=True, returncode=0)
                ],
            )
            orchestrator._run_requirements_audit = lambda *_args, **_kwargs: {
                "input_context_sha256": "sha256:new"
            }
            orchestrator._run_missing_baseline_commands = Mock(
                side_effect=AssertionError("warm resume cache should avoid gate execution")
            )

            changed = orchestrator._ensure_implement_verify_baseline(state, [task])

            self.assertTrue(changed)
            self.assertEqual(state.implement_verify_baseline_failures, [])
            orchestrator._run_missing_baseline_commands.assert_not_called()

    def test_source_cache_survives_run_context_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            orchestrator.config.gates.verification_policy_version = 3
            orchestrator.config.gates.incremental_mode = "off"
            orchestrator.config.gates.steps = [
                VerificationStep(
                    runner="pytest",
                    targets=["tests/source.py"],
                    cache_scope="source",
                ),
                VerificationStep(
                    runner="pytest",
                    targets=["tests/context.py"],
                    cache_scope="run_context",
                ),
            ]
            task = TaskSpec(
                task_id="task-001",
                title="Cache scopes",
                description="Verify split cache behavior.",
                acceptance=["done"],
            )
            state = RunState(run_id="test", tasks=[task])
            audit_hashes = iter(("sha256:first", "sha256:second"))
            orchestrator._run_requirements_audit = lambda *_args, **_kwargs: {
                "input_context_sha256": next(audit_hashes)
            }
            executed = []

            def successful_gate(_ref, commands, parallel_groups, *, context):
                pending = list(commands) + [
                    command for group in parallel_groups for command in group.commands
                ]
                executed.extend(pending)
                return (
                    GateResult(
                        ok=True,
                        commands=[
                            CommandResult(command=command, ok=True, returncode=0)
                            for command in pending
                        ],
                        summary="ok",
                    ),
                    "",
                )

            orchestrator._run_missing_baseline_commands = successful_gate

            orchestrator._ensure_implement_verify_baseline(state, [task])
            orchestrator._ensure_implement_verify_baseline(state, [task])

            source_runs = [command for command in executed if "source.py" in command]
            context_runs = [command for command in executed if "context.py" in command]
            self.assertEqual(len(source_runs), 1)
            self.assertEqual(len(context_runs), 2)

    def test_proof_evidence_reuses_identical_successful_task_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            task = TaskSpec(
                task_id="task-001",
                title="Proof reuse",
                description="Reuse exact verification evidence.",
                acceptance=["done"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 0,
                        "status": "verified",
                        "evidence_refs": ["tests/test_demo.py::test_contract"],
                    }
                ],
            )
            command = orchestrator._build_task_proof_evidence_command_for_ref(
                "tests/test_demo.py::test_contract"
            )
            self.assertTrue(command)
            orchestrator._proof_execution_fingerprint = Mock(return_value="same")
            orchestrator._task_verify_proof_reuse[task.task_id] = (
                "same",
                {
                    command: CommandResult(
                        command=command,
                        ok=True,
                        returncode=0,
                        stdout="passed",
                    )
                },
                None,
            )

            with patch(
                "auto_agents.orchestrator.run_gate_plan",
                side_effect=AssertionError("identical proof command should be reused"),
            ):
                result = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(result["ok"])


class EvidencePreflightTests(unittest.TestCase):
    def test_parse_ready_and_reject_invalid_payload(self) -> None:
        parsed = Orchestrator._parse_evidence_preflight(
            'EVIDENCE_PREFLIGHT: {"decision":"READY","reason":"feasible","checklist":["boundary test"]}'
        )
        self.assertEqual(parsed["decision"], "READY")
        self.assertIsNone(Orchestrator._parse_evidence_preflight("READY"))

    def test_routing_happens_before_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            state = orchestrator._load_tasks_from_plan()
            task = state[0]
            run_state = orchestrator._run_requirements_audit([], current_spec=None)
            self.assertIn("metrics", run_state)
            from auto_agents.config import load_run_state

            actual_state = load_run_state(root)
            actual_state.tasks = [task]
            routed = orchestrator._route_evidence_preflight(
                actual_state,
                [task],
                task,
                {"decision": "SPLIT", "reason": "two independent proof surfaces"},
            )
            self.assertEqual(routed.current_stage, "plan")
            self.assertEqual(task.status, "pending")

    def test_cached_ready_result_skips_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            from auto_agents.config import load_run_state

            state = load_run_state(root)
            task = orchestrator._load_tasks_from_plan()[0]
            state.tasks = [task]
            orchestrator._commit_planning_baseline_if_needed([task])
            with patch.object(orchestrator, "_task_needs_evidence_preflight", return_value=True):
                fingerprint = orchestrator._evidence_preflight_fingerprint(task)
                task.evidence_preflight = {
                    "fingerprint": fingerprint,
                    "decision": "READY",
                    "reason": "cached",
                    "checklist": ["boundary test"],
                }
                with patch.object(orchestrator, "_call_with_failover") as provider:
                    result = orchestrator._ensure_evidence_preflight(state, task)
            self.assertIsNone(result)
            provider.assert_not_called()

    def test_preflight_provider_setup_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            from auto_agents.config import load_run_state

            state = load_run_state(root)
            task = orchestrator._load_tasks_from_plan()[0]
            state.tasks = [task]
            with patch.object(orchestrator, "_task_needs_evidence_preflight", return_value=True), patch(
                "auto_agents.orchestrator.add_worktree", side_effect=RuntimeError("unavailable")
            ):
                result = orchestrator._ensure_evidence_preflight(state, task)
            self.assertIsNone(result)
            self.assertEqual(task.evidence_preflight, {})

    def test_ready_preflight_runs_in_isolated_worktree_and_caches_checklist(self) -> None:
        class ReadyAdapter:
            def run(self, request):
                self.cwd = request.cwd
                summary = (
                    'EVIDENCE_PREFLIGHT: {"decision":"READY","reason":"feasible",'
                    '"checklist":["exercise the public boundary"]}'
                )
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            orchestrator = Orchestrator(root)
            adapter = ReadyAdapter()
            orchestrator.adapter = adapter
            from auto_agents.config import load_run_state

            state = load_run_state(root)
            task = orchestrator._load_tasks_from_plan()[0]
            state.tasks = [task]
            orchestrator._commit_planning_baseline_if_needed([task])
            with patch.object(orchestrator, "_task_needs_evidence_preflight", return_value=True):
                result = orchestrator._ensure_evidence_preflight(state, task)

            self.assertIsNone(result)
            self.assertEqual(task.evidence_preflight["decision"], "READY")
            self.assertIn("public boundary", task.evidence_preflight["checklist"][0])
            self.assertNotEqual(adapter.cwd, root)
            self.assertFalse(Path(adapter.cwd).exists())


if __name__ == "__main__":
    unittest.main()
