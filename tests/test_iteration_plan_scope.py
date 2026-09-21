"""Offline synthetic regressions for independent-iteration planning.

These fixtures model the retained incident; they are not provider-response
replays or evidence of delivery of the target product's historical backlog.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    load_run_state,
    load_task_plan,
    provider_references_lock_path,
    requirements_trace_path,
    save_project_config,
    save_run_state,
    task_plan_path,
)
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, PersistenceTargetConfig, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.requirements import (
    requirement_contract_sha256,
    stamp_provider_reference_consumer_hashes,
    run_requirements_audit,
    validate_task_requirement_coverage,
    validate_task_requirement_proofs,
)
from auto_agents.validation import validate_task_plan_with_requirements
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
from auto_agents.workflow_runtime import WorkflowCoordinator


SPEC = Path("specs/current-iteration.md")


def requirement(req_id, source):
    return {
        "id": req_id,
        "text": "Preserve the public API contract.",
        "source": source,
        "status": "active",
        "priority": "mandatory",
        "acceptance_oracles": [
            "The public API returns normalized provider output.",
            "The public API records durable provider evidence.",
        ],
        "oracle_type": "integration_test",
        "oracle_strength": "behavioral",
        "evidence_boundary": "system_boundary",
        "forbidden_proxy_oracles": ["config-only checks"],
        "forbidden_patterns": [],
        "external_docs_required": False,
        "provider_reference": "",
        "notes": "",
    }


def task_for(req, task_id="task-current", status="pending"):
    return {
        "task_id": task_id,
        "title": "Preserve the API contract",
        "description": "Verify the public output and its durable evidence.",
        "acceptance": list(req["acceptance_oracles"]),
        "status": status,
        "commit_message": "",
        "requirement_ids": [req["id"]],
        "requirement_proofs": [
            {
                "requirement_id": req["id"],
                "requirement_contract_sha256": requirement_contract_sha256(req),
                "oracle_index": index,
                "acceptance_oracle": oracle,
                "proof_type": "integration_test",
                "oracle_strength": "behavioral",
                "evidence_boundary": "system_boundary",
                "evidence_refs": ["tests/test_api.py::test_contract"],
                "forbidden_proxy_oracles": ["config-only checks"],
                "proxy_oracles": [],
                "status": "verified" if status == "done" else "planned",
            }
            for index, oracle in enumerate(req["acceptance_oracles"], 1)
        ],
    }


def scene():
    current = requirement("REQ-001", "specs/older.md; " + str(SPEC) + " §1")
    old = requirement("REQ-002", "specs/stopped-iteration.md §2")
    trace = {"version": 1, "requirements": [current, old]}
    plan = {
        "oracle_proof_schema_version": 2,
        "test_strategy": "Offline public API regression",
        "verification_commands": ["conda run -p ./.conda python -m pytest -q tests/test_api.py"],
        "tasks": [task_for(current)],
    }
    return trace, plan


def retained_scope_run(root, spec, plan, workflow_id=""):
    """Synthetic copy of the retained run's planning-only failure shape."""
    state = load_run_state(root)
    state.run_id = "retained-run"
    state.current_stage = "design"
    state.stage_summaries = {"clarify": "done", "prototype": "not requested", "design": "done"}
    state.agent_attempts["plan"] = 3
    state.status = "blocked"
    state.active_blocker = {
        "owner": "auto_agents", "category": "iteration_plan_scope_mismatch",
        "fingerprint": "retained-scope-conflict", "status": "blocked",
    }
    state.resume_context.update(spec_file=str(spec), workflow_id=workflow_id)
    conflict = {
        "blocker_id": "plan-cumulative-scope-conflict",
        "category": "requirement_scope_conflict",
        "status": "needs_input",
        "requirement_ids": ["REQ-002"],
        "reason": "Cumulative coverage conflicts with the independent iteration.",
    }
    blocked = copy.deepcopy(plan)
    blocked.update(stage_status="blocked", blockers=[conflict])
    write_json(task_plan_path(root), blocked)
    save_run_state(root, state)
    return state, conflict


class PlanningAdapter:
    def __init__(self, root, plans):
        self.root = root
        self.plans = plans
        self.prompts = []

    def run(self, request):
        assert request.stage == "plan", request.stage
        self.prompts.append(request.prompt)
        payload = self.plans[min(len(self.prompts) - 1, len(self.plans) - 1)]
        write_json(task_plan_path(self.root), copy.deepcopy(payload))
        write_text(request.output_path, "Current iteration coverage is complete.\n")
        return AgentResult(
            ok=True, command=["offline-planner"], output_path=request.output_path,
            summary="Current iteration coverage is complete.", returncode=0,
        )


@contextmanager
def project():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        # The fixture needs orchestrator documents/config, not a Git repository
        # or agent-instruction synchronization. Never change candidate Git metadata.
        with patch("auto_agents.orchestrator.ensure_repo"), patch(
            "auto_agents.orchestrator.sync_agent_instructions"
        ):
            Orchestrator.init_project(root, "scope-fixture", "mock")
        orch = Orchestrator(root)
        orch.config.retries.per_stage["plan"] = 3
        orch.config.approvals.enabled = []
        save_project_config(root, orch.config)
        spec = root / SPEC
        write_text(spec, "# Independent iteration\nPreserve the public API contract.\n")
        write_text(root / "tests/test_api.py", "def test_contract():\n    assert True\n")
        trace, plan = scene()
        write_json(requirements_trace_path(root), trace)
        write_json(task_plan_path(root), plan)
        orch._attach_run_logger(load_run_state(root).run_id)

        def entries(project_root, ignored_prefixes=()):
            return [
                ("??", str(path.relative_to(project_root)))
                for path in sorted(project_root.rglob("*"))
                if path.is_file()
                and not str(path.relative_to(project_root)).startswith(ignored_prefixes)
            ]

        # Supply a real file inventory for mutation snapshots without creating
        # a Git index. The production mutation-boundary checks still run.
        with (
            patch("auto_agents.orchestrator.changed_entries", side_effect=entries),
            patch("auto_agents.git_ops.changed_entries", side_effect=entries),
            patch.object(orch, "_cleanup_ephemeral_tooling_artifacts"),
            patch.object(orch, "_call_with_failover", side_effect=lambda request: orch.adapter.run(request)),
        ):
            yield root, orch, spec, trace, plan


@contextmanager
def continuation_probe_project(*, research_required=False):
    """Synthetic fixture for the controller's real continuation collector."""
    from auto_agents.repair_runtime_identity import observe_engine
    from auto_agents.repair_v2.boundary_driver import run_input_hashes

    with project() as (root, orch, spec, trace, plan):
        if research_required:
            reference = '.auto-agents/docs/provider_references/current.md'
            trace['requirements'][0].update(external_docs_required=True, provider_reference=reference)
            plan['tasks'] = [task_for(trace['requirements'][0])]
            write_json(requirements_trace_path(root), trace)
            write_text(root / reference, '# Retained provider reference\nRequires research for the new contract.\n')
            write_json(provider_references_lock_path(root), {
                'version': 1, 'references': {'current': {'path': reference, 'status': 'needs_refresh'}},
            })
        plan['tasks'][0]['expected_test_migrations'] = [{
            'ref': 'tests/test_api.py::test_contract', 'change': 'Preserve the current API assertion.',
        }]
        store = WorkflowStore(root)
        workflow = store.create_root(WorkflowRef('run', 'retained-run'))
        original, _ = retained_scope_run(root, spec, plan, workflow.workflow_id)
        original_plan = load_task_plan(root)
        frozen = run_input_hashes(root, original)
        identity = orch._auto_agents_runtime_identity()
        runtime = observe_engine(Path(identity['repository_root']), expected_commit=identity['repository_head'])
        assert runtime['ok'], runtime['mismatches']
        request = {'commit': runtime['commit'], 'invocation': {
            'run_id': original.run_id, 'workflow_id': workflow.workflow_id,
        }}
        state = orch.mark_self_repair_applied(runtime['commit'])
        assert orch._resume_blocked_run(state)
        save_run_state(root, state)
        with ExitStack() as stack:
            stack.enter_context(patch('auto_agents.orchestrator.ensure_repo'))
            for name in ('_ensure_agent_instructions_synced', '_start_health_supervision', 'stop_health_supervision'):
                stack.enter_context(patch.object(orch, name))
            provider = stack.enter_context(patch.object(
                orch, '_call_with_failover', side_effect=AssertionError('no provider allowed')
            ))
            yield root, orch, original, original_plan, request, runtime, frozen
            provider.assert_not_called()


class IterationPlanScopeTests(unittest.TestCase):
    def test_scope_reconciliation_does_not_depend_on_model_blocker_id(self):
        with project() as (root, orch, spec, trace, plan):
            retained_scope_run(root, spec, plan)
            payload = load_task_plan(root)
            payload['blockers'][0]['blocker_id'] = 'other-generated-identifier'
            write_json(task_plan_path(root), payload)
            state = orch.mark_self_repair_applied('verified-engine')
            self.assertTrue(orch._resume_blocked_run(state))
            self.assertEqual([t.task_id for t in state.tasks], ['task-current'])

    def test_planning_entrypoint_accepts_independent_iteration(self):
        """Differential: unchanged test fails on the base cumulative engine."""
        with project() as (root, orch, spec, trace, plan):
            before = requirements_trace_path(root).read_bytes()
            orch.adapter = PlanningAdapter(root, [plan])
            state = load_run_state(root)
            # Intentionally do not set _active_spec_file: stage-local authority
            # must reach validation even outside Orchestrator.run().
            result = orch._run_agent_stage("plan", state, spec)
            self.assertEqual(len(orch.adapter.prompts), 1)
            self.assertEqual(result.current_stage, "plan")
            self.assertEqual([t.task_id for t in result.tasks], ["task-current"])
            self.assertEqual(result.tasks[0].status, "pending")
            self.assertEqual(requirements_trace_path(root).read_bytes(), before)
            self.assertEqual(orch.config.retries.per_stage["plan"], 3)
            self.assertEqual(len(load_task_plan(root)["tasks"]), 1)
            prompt = orch.adapter.prompts[0]
            self.assertIn("same policy as requirements auditing", prompt)
            self.assertIn("Requirements sourced from the primary input spec: REQ-001.", prompt)
            self.assertIn("Unselected historical requirements remain unchanged", prompt)
            self.assertNotIn("All active mandatory requirements in requirements_trace.json", prompt)

    def test_current_coverage_and_oracle_are_still_mandatory(self):
        trace, plan = scene()
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC), [])
        for kind in ("binding", "proof"):
            with self.subTest(kind=kind):
                broken = copy.deepcopy(plan)
                if kind == "binding":
                    broken["tasks"][0]["requirement_ids"] = []
                    broken["tasks"][0]["requirement_proofs"] = []
                else:
                    broken["tasks"][0]["requirement_proofs"].pop()
                errors = validate_task_plan_with_requirements(broken, trace, current_spec=SPEC)
                self.assertTrue(any("REQ-001" in e for e in errors), errors)
                self.assertTrue(any("acceptance oracle #2" in e for e in errors), errors)
                self.assertFalse(any("REQ-002" in e for e in errors), errors)

    def test_explicit_historical_adoption_requires_all_oracles(self):
        trace, plan = scene()
        plan["tasks"].append(task_for(trace["requirements"][1], "task-adopted"))
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC), [])
        plan["tasks"][-1]["requirement_proofs"].pop()
        errors = validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)
        self.assertIn("mandatory requirement REQ-002 acceptance oracle #2 is not covered by requirement_proofs", errors)

    def test_scoped_validation_preserves_proof_contract_checks(self):
        for owner in ("current", "historical"):
            for field, value, message in (
                ("requirement_id", "REQ-404", "unknown requirement_id"),
                ("requirement_contract_sha256", "sha256:" + "0" * 64, "requirement_contract_sha256"),
                ("oracle_strength", "structural", "oracle_strength"),
                ("evidence_boundary", "internal_state", "evidence_boundary"),
                ("proxy_oracles", ["config-only checks"], "forbidden proxy"),
            ):
                with self.subTest(owner=owner, field=field):
                    trace, plan = scene()
                    if owner == "historical":
                        plan["tasks"].append(task_for(trace["requirements"][1], "task-adopted"))
                    plan["tasks"][-1]["requirement_proofs"][0][field] = value
                    errors = validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)
                    self.assertTrue(any(message in e for e in errors), errors)
        trace, plan = scene()
        plan["tasks"][0]["requirement_proofs"][0].update(oracle_index=99, acceptance_oracle="wrong oracle")
        self.assertTrue(any("must identify an acceptance oracle" in e for e in
                            validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)))
        plan["tasks"][0]["requirement_ids"].append("REQ-404")
        self.assertTrue(any("unknown requirement_ids" in e for e in
                            validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)))

    def test_absent_scope_is_cumulative_at_every_validation_entrypoint(self):
        trace, plan = scene()
        # Model-authored scope notes cannot establish validator authority.
        plan["scope_notes"] = ["Only REQ-001 is in scope"]
        plan["current_spec"] = str(SPEC)
        for validator in (validate_task_plan_with_requirements,
                          validate_task_requirement_coverage, validate_task_requirement_proofs):
            with self.subTest(validator=validator.__name__):
                errors = validator(plan, trace)
                self.assertTrue(any("REQ-002 acceptance oracle #1" in e for e in errors), errors)
                self.assertEqual(validator(plan, trace, current_spec=SPEC), [])

    def test_full_registry_and_inputs_are_preserved(self):
        trace, plan = scene()
        saved = copy.deepcopy((plan, trace))
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC), [])
        self.assertEqual((plan, trace), saved)
        self.assertEqual(trace["requirements"][1]["status"], "active")
        # An out-of-scope record is still schema/contract validated.
        trace["requirements"][1]["priority"] = "invalid"
        self.assertTrue(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC))

    def test_unselected_historical_proof_is_validated_against_full_registry(self):
        trace, plan = scene()
        orphan = task_for(trace["requirements"][1])["requirement_proofs"][0]
        plan["tasks"][0]["requirement_proofs"].append(orphan)
        errors = validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)
        self.assertTrue(any("requirement_id must also appear in task requirement_ids: REQ-002" in e for e in errors), errors)
        self.assertFalse(any("unknown requirement_id: REQ-002" in e for e in errors), errors)

    def test_archived_verified_proofs_and_explicit_reownership(self):
        trace, plan = scene()
        archived = task_for(trace["requirements"][1], "task-archived", "done")
        saved = copy.deepcopy(archived)
        self.assertEqual(validate_task_plan_with_requirements(
            plan, trace, historical_tasks=[archived]), [])
        self.assertEqual(validate_task_plan_with_requirements(
            plan, trace, current_spec=SPEC, historical_tasks=[archived]), [])
        self.assertEqual(archived, saved)
        # Current ownership cannot hide behind an incomplete historical snapshot.
        archived["requirement_proofs"].pop()
        adopted = task_for(trace["requirements"][1], "task-adopted")
        adopted["requirement_proofs"].pop()
        plan["tasks"].append(adopted)
        errors = validate_task_plan_with_requirements(
            plan, trace, current_spec=SPEC, historical_tasks=[archived])
        self.assertTrue(any("REQ-002 acceptance oracle #2" in e for e in errors), errors)

    def test_scope_matches_audit_and_spec_path_forms(self):
        from auto_agents.requirements import requirement_scope_ids

        with project() as (root, orch, spec, trace, plan):
            for path in (SPEC, spec):
                self.assertEqual(requirement_scope_ids(trace, current_spec=path), {"REQ-001"})
                self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=path), [])
            report = run_requirements_audit(root, [], current_spec=spec)
            issues = {i["requirement_id"]: i for i in report["issues"]}
            self.assertEqual(issues["REQ-001"]["result"], "fail")
            self.assertEqual(issues["REQ-002"]["result"], "advisory")
            self.assertTrue(issues["REQ-002"]["out_of_scope_backlog"])
            adopted = TaskSpec.from_dict(task_for(trace["requirements"][1], "task-adopted"))
            report = run_requirements_audit(root, [adopted], current_spec=spec)
            self.assertEqual(next(i for i in report["issues"] if i["requirement_id"] == "REQ-002")["result"], "fail")

    def test_blocked_plan_must_be_revised_before_tasks_load(self):
        with project() as (root, orch, spec, trace, plan):
            blocked = copy.deepcopy(plan)
            blocked.update(stage_status="blocked", blockers=[{"blocker_id": "old-scope-conflict"}])
            orch.adapter = PlanningAdapter(root, [blocked, plan])
            result = orch._run_agent_stage("plan", load_run_state(root), spec)
            self.assertEqual(len(orch.adapter.prompts), 2)
            self.assertIn("task plan has active blockers", orch.adapter.prompts[1])
            self.assertEqual([t.task_id for t in result.tasks], ["task-current"])

    def test_feedback_uses_active_spec_and_rejects_a_blocked_stage_marker(self):
        with project() as (root, orch, spec, trace, plan):
            result = AgentResult(ok=True, command=[], output_path=root / "result.txt", summary="planned")
            self.assertIn("REQ-002", orch._plan_validation_feedback(result))
            orch._active_spec_file = spec
            self.assertIsNone(orch._plan_validation_feedback(result))
            plan["stage_status"] = "blocked"
            write_json(task_plan_path(root), plan)
            self.assertIn("stage_status is blocked", orch._plan_validation_feedback(result))

    def test_successful_plan_does_not_clear_unrelated_run_blocker(self):
        with project() as (root, orch, spec, trace, plan):
            orch.adapter = PlanningAdapter(root, [plan])
            state = load_run_state(root)
            blocker = {
                "owner": "auto_agents", "category": "unrelated-recovery",
                "status": "retrying", "self_repair_commit": "other-repair",
                "fingerprint": "unrelated-fingerprint",
            }
            state.active_blocker = copy.deepcopy(blocker)
            state = orch._run_agent_stage("plan", state, spec)
            self.assertEqual(state.active_blocker, blocker)
            self.assertNotEqual(state.last_recovery_route.get("outcome"), "iteration_plan_scope_reconciled")

    def test_unresolved_plan_blocker_remains_terminal(self):
        with project() as (root, orch, spec, trace, plan):
            plan["blockers"] = [{"blocker_id": "unrelated-user-decision"}]
            orch.adapter = PlanningAdapter(root, [plan])
            state = load_run_state(root)
            with self.assertRaisesRegex(RuntimeError, "plan exhausted retries"):
                orch._run_agent_stage("plan", state, spec)
            self.assertEqual(len(orch.adapter.prompts), 3)
            self.assertEqual(state.tasks, [])
            self.assertNotIn("plan", state.stage_summaries)
            self.assertEqual(load_task_plan(root)["blockers"], plan["blockers"])

    def test_same_workflow_resumes_planning_after_approved_engine_repair(self):
        with project() as (root, orch, spec, trace, plan):
            state = load_run_state(root)
            state.run_id = "original-run"
            state.current_stage = "design"
            state.stage_summaries = {"clarify": "done", "prototype": "not requested", "design": "done"}
            state.agent_attempts["plan"] = 3
            state.status = "blocked"
            state.active_blocker = {
                "owner": "auto_agents", "category": "iteration_plan_scope_mismatch",
                "fingerprint": "original-scope-conflict", "status": "blocked",
            }
            store = WorkflowStore(root)
            workflow = store.create_root(WorkflowRef("run", state.run_id))
            state.resume_context.update(spec_file=str(spec), workflow_id=workflow.workflow_id)
            save_run_state(root, state)
            blocked = copy.deepcopy(plan)
            blocked.update(stage_status="blocked", blockers=[{"blocker_id": "plan-cumulative-scope-conflict"}])
            write_json(task_plan_path(root), blocked)
            stopped = root / ".auto-agents/history/task_plans/stopped-run.json"
            write_json(stopped, {"tasks": [task_for(trace["requirements"][1], "task-stopped")]})
            unrelated = root / "unrelated.txt"
            write_text(unrelated, "preserve existing work\n")
            preserved = {p: p.read_bytes() for p in (requirements_trace_path(root), stopped, unrelated, spec)}
            orch.adapter = PlanningAdapter(root, [plan])
            # Same preparation used by the approved runtime launcher; this
            # records approval in the fixture and does not change Git metadata.
            orch.mark_self_repair_applied("synthetic-approved-engine", verification="offline candidate proof")
            reached = []

            def next_stage(resumed, spec_file):
                reached.append((resumed.run_id, list(resumed.tasks), spec_file))
                resumed.current_stage = "provider_research"
                resumed.status = "paused"
                return resumed

            with ExitStack() as stack:
                # Isolate repository/health infrastructure only. Scope,
                # validation, planning retries, resume routing and task loading
                # all execute their production implementations.
                stack.enter_context(patch("auto_agents.orchestrator.ensure_repo"))
                for name in ("_ensure_agent_instructions_synced", "_ensure_preconditions",
                             "_start_health_supervision", "stop_health_supervision"):
                    stack.enter_context(patch.object(orch, name))
                stack.enter_context(patch.object(orch, "_run_provider_research", side_effect=next_stage))
                result = WorkflowCoordinator(orch).resume_workflow(workflow.workflow_id)
            self.assertEqual(result.run_id, "original-run")
            self.assertEqual(result.resume_context["workflow_id"], workflow.workflow_id)
            self.assertEqual(result.current_stage, "provider_research")
            self.assertIn("plan", result.stage_summaries)
            self.assertEqual(result.active_blocker, {})
            self.assertEqual(result.last_recovery_route["outcome"], "iteration_plan_scope_reconciled")
            self.assertEqual(len(reached), 1)
            self.assertEqual([t.task_id for t in reached[0][1]], ["task-current"])
            self.assertEqual(len(orch.adapter.prompts), 1)
            self.assertEqual(orch.config.retries.per_stage["plan"], 3)
            self.assertEqual({p: p.read_bytes() for p in preserved}, preserved)
            self.assertEqual(store.load(workflow.workflow_id).root.native_id, "original-run")

    def test_offline_boundary_reconciles_retained_plan_without_provider(self):
        """Use the pinned boundary driver's exact run preparation/resume calls."""
        with project() as (root, orch, spec, trace, plan):
            original, conflict = retained_scope_run(root, spec, plan)
            trace_before = requirements_trace_path(root).read_bytes()
            attempts_before = dict(original.agent_attempts)
            before = dict(original.active_blocker)
            with patch.object(orch, "_call_with_failover", side_effect=AssertionError("no provider allowed")) as provider:
                state = orch.mark_self_repair_applied("synthetic-approved-engine")
                changed = orch._resume_blocked_run(state)
                save_run_state(root, state)
            provider.assert_not_called()
            after = state.active_blocker or {}
            same_blocker = bool(after and any(after.get(k) and after.get(k) == before.get(k)
                                            for k in ("fingerprint", "category")))
            self.assertTrue(changed)
            self.assertFalse(same_blocker)
            self.assertEqual(state.current_stage, "plan")
            self.assertEqual(state.run_id, original.run_id)
            self.assertIn("plan", state.stage_summaries)
            self.assertEqual([t.task_id for t in state.tasks], ["task-current"])
            self.assertEqual(state.agent_attempts, attempts_before)
            self.assertEqual(orch._pending_stages(state)[0], "provider_research")
            accepted = load_task_plan(root)
            self.assertEqual(accepted["blockers"], [])
            self.assertEqual(accepted["stage_status"], "ready")
            self.assertEqual(accepted["verification_commands"], plan["verification_commands"])
            self.assertEqual(accepted["tasks"][0]["requirement_proofs"], plan["tasks"][0]["requirement_proofs"])
            self.assertEqual(state.last_recovery_route["resolved_plan_blockers"], [conflict])
            self.assertEqual(requirements_trace_path(root).read_bytes(), trace_before)

    def test_same_workflow_advances_with_revalidated_retained_plan(self):
        with project() as (root, orch, spec, trace, plan):
            store = WorkflowStore(root)
            workflow = store.create_root(WorkflowRef("run", "retained-run"))
            original, conflict = retained_scope_run(root, spec, plan, workflow.workflow_id)
            orch.mark_self_repair_applied("synthetic-approved-engine")
            reached = []

            def next_stage(state, spec_file):
                reached.append([t.task_id for t in state.tasks])
                state.current_stage = "provider_research"
                state.status = "paused"
                return state

            with ExitStack() as stack:
                stack.enter_context(patch("auto_agents.orchestrator.ensure_repo"))
                for name in ("_ensure_agent_instructions_synced", "_ensure_preconditions",
                             "_start_health_supervision", "stop_health_supervision"):
                    stack.enter_context(patch.object(orch, name))
                provider = stack.enter_context(patch.object(
                    orch, "_call_with_failover", side_effect=AssertionError("no provider allowed")
                ))
                stack.enter_context(patch.object(orch, "_run_provider_research", side_effect=next_stage))
                state = WorkflowCoordinator(orch).resume_workflow(workflow.workflow_id)
            provider.assert_not_called()
            self.assertEqual(state.run_id, original.run_id)
            self.assertEqual(state.resume_context["workflow_id"], workflow.workflow_id)
            self.assertEqual(state.current_stage, "provider_research")
            self.assertEqual(state.active_blocker, {})
            self.assertEqual(state.agent_attempts["plan"], 3)
            self.assertEqual(reached, [["task-current"]])
            self.assertEqual(state.last_recovery_route["resolved_plan_blockers"], [conflict])

    def test_offline_reconciliation_preserves_current_contracts_and_other_blockers(self):
        cases = (
            "missing-current-binding", "missing-current-oracle", "stale-proof-hash",
            "weak-proof", "unknown-task-requirement", "untrusted-done-task", "legacy-proof-mode",
            "invalid-verification-command", "other-plan-blocker", "current-conflict",
            "adopted-history", "unknown-conflict", "missing-spec", "unmatched-spec",
            "other-run-blocker", "unapproved-repair",
        )
        for case in cases:
            with self.subTest(case=case), project() as (root, orch, spec, trace, plan):
                state, conflict = retained_scope_run(root, spec, plan)
                if case != "unapproved-repair":
                    state = orch.mark_self_repair_applied("synthetic-approved-engine")
                candidate = load_task_plan(root)
                task = candidate["tasks"][0]
                if case == "missing-current-binding":
                    task.update(requirement_ids=[], requirement_proofs=[])
                elif case == "missing-current-oracle":
                    task["requirement_proofs"].pop()
                elif case == "stale-proof-hash":
                    task["requirement_proofs"][0]["requirement_contract_sha256"] = "sha256:" + "0" * 64
                elif case == "weak-proof":
                    task["requirement_proofs"][0]["oracle_strength"] = "structural"
                elif case == "unknown-task-requirement":
                    task["requirement_ids"].append("REQ-404")
                elif case == "untrusted-done-task":
                    task["status"] = "done"
                    for proof in task["requirement_proofs"]:
                        proof["status"] = "verified"
                elif case == "legacy-proof-mode":
                    candidate.pop("oracle_proof_schema_version")
                    task.pop("requirement_proofs")
                elif case == "invalid-verification-command":
                    candidate["verification_commands"] = ["conda run -p ./.conda python -m pytest tests/missing.py"]
                elif case == "other-plan-blocker":
                    candidate["blockers"].append({"category": "missing_authorization", "status": "needs_input"})
                elif case == "current-conflict":
                    candidate["blockers"][0]["requirement_ids"] = ["REQ-001"]
                elif case == "adopted-history":
                    candidate["tasks"].append(task_for(trace["requirements"][1], "task-adopted"))
                elif case == "unknown-conflict":
                    candidate["blockers"][0]["requirement_ids"] = ["REQ-404"]
                elif case == "missing-spec":
                    state.resume_context.pop("spec_file")
                elif case == "unmatched-spec":
                    another = root / "specs/unrelated.md"
                    write_text(another, "# Unrelated scope\n")
                    state.resume_context["spec_file"] = str(another)
                elif case == "other-run-blocker":
                    state.active_blocker["category"] = "unrelated_recovery"
                write_json(task_plan_path(root), candidate)
                before = task_plan_path(root).read_bytes()
                trace_before = requirements_trace_path(root).read_bytes()
                with patch.object(orch, "_call_with_failover", side_effect=AssertionError("no provider allowed")) as provider:
                    orch._resume_blocked_run(state)
                provider.assert_not_called()
                self.assertNotIn("plan", state.stage_summaries)
                self.assertTrue(state.active_blocker)
                self.assertEqual(state.active_blocker["fingerprint"], "retained-scope-conflict")
                self.assertEqual(state.agent_attempts["plan"], 3)
                self.assertEqual(task_plan_path(root).read_bytes(), before)
                self.assertEqual(requirements_trace_path(root).read_bytes(), trace_before)

    def test_retained_plan_cannot_bypass_unbound_persistence_readiness(self):
        with project() as (root, orch, spec, trace, plan):
            trace["persistence_decisions"] = [{
                "id": "PERSIST-002", "strategy": "clean_break",
                "target_ids": ["local-sqlite-test"], "status": "active",
                "source": "specs/stopped-iteration.md; explicit persistence decision",
            }]
            orch.config.persistence.targets = [PersistenceTargetConfig(
                target_id="local-sqlite-test", environment="test", kind="local_file",
                locator={"path": ".tmp-tests/app.sqlite3"},
            )]
            save_project_config(root, orch.config)
            write_json(requirements_trace_path(root), trace)
            original, _ = retained_scope_run(root, spec, plan)
            before_plan = task_plan_path(root).read_bytes()
            before_trace = requirements_trace_path(root).read_bytes()
            before_config = (root / ".auto-agents/config.json").read_bytes()
            self.assertNotIn("persistence_change", plan["tasks"][0])
            with patch.object(orch, "_call_with_failover", side_effect=AssertionError("no provider allowed")) as provider:
                state = orch.mark_self_repair_applied("synthetic-approved-engine")
                orch._resume_blocked_run(state)
                if "plan" not in state.stage_summaries and state.status == "pending":
                    state = orch._run_agent_stage("plan", state, spec)
            provider.assert_not_called()
            self.assertNotIn("plan", state.stage_summaries)
            self.assertEqual(state.tasks, [])
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.active_blocker["category"], "persistence_configuration_required")
            self.assertEqual(state.active_blocker["owner"], "target_project")
            self.assertIn("initialize_argv and verify_argv", state.last_error)
            self.assertEqual(state.agent_attempts, original.agent_attempts)
            self.assertEqual(task_plan_path(root).read_bytes(), before_plan)
            self.assertEqual(requirements_trace_path(root).read_bytes(), before_trace)
            self.assertEqual((root / ".auto-agents/config.json").read_bytes(), before_config)

    def test_ready_unbound_persistence_decision_allows_retained_plan(self):
        with project() as (root, orch, spec, trace, plan):
            trace["persistence_decisions"] = [{
                "id": "PERSIST-002", "strategy": "clean_break",
                "target_ids": ["local-sqlite-test"], "status": "active",
                "source": "specs/stopped-iteration.md; explicit persistence decision",
            }]
            orch.config.persistence.targets = [PersistenceTargetConfig(
                target_id="local-sqlite-test", environment="test", kind="local_file",
                locator={"path": ".tmp-tests/app.sqlite3"},
                initialize_argv=["tool", "init"], verify_argv=["tool", "verify"],
            )]
            save_project_config(root, orch.config)
            write_json(requirements_trace_path(root), trace)
            original, _ = retained_scope_run(root, spec, plan)
            with patch.object(orch, "_call_with_failover", side_effect=AssertionError("no provider allowed")) as provider:
                state = orch.mark_self_repair_applied("synthetic-approved-engine")
                self.assertTrue(orch._resume_blocked_run(state))
            provider.assert_not_called()
            self.assertIn("plan", state.stage_summaries)
            self.assertEqual([t.task_id for t in state.tasks], ["task-current"])
            self.assertEqual(state.active_blocker, {})
            self.assertEqual(state.agent_attempts, original.agent_attempts)
            self.assertFalse((root / ".tmp-tests/app.sqlite3").exists())

    def test_workflow_records_real_implementation_entry_after_reference_reuse(self):
        class StopAfterEntry(BaseException):
            pass

        with project() as (root, orch, spec, trace, plan):
            reference = ".auto-agents/docs/provider_references/current.md"
            trace["requirements"][0].update(
                external_docs_required=True, provider_reference=reference
            )
            plan["tasks"] = [task_for(trace["requirements"][0])]
            write_json(requirements_trace_path(root), trace)
            write_text(root / reference, "# Synthetic provider reference\nOffline contract fixture.\n")
            lock, _ = stamp_provider_reference_consumer_hashes({
                "version": 1,
                "references": {"current": {"path": reference, "status": "verified"}},
            }, trace)
            write_json(provider_references_lock_path(root), lock)
            store = WorkflowStore(root)
            workflow = store.create_root(WorkflowRef("run", "retained-run"))
            original, _ = retained_scope_run(root, spec, plan, workflow.workflow_id)
            trace_before = requirements_trace_path(root).read_bytes()
            lock_before = provider_references_lock_path(root).read_bytes()
            orch.mark_self_repair_applied("synthetic-approved-engine")
            captured = []
            write_event = orch.reporter.event

            def observe(kind, data, **options):
                write_event(kind, data, **options)
                if kind == "implementation.entered":
                    captured.append(copy.deepcopy(data))
                    raise StopAfterEntry()

            with ExitStack() as stack:
                # Repository/health setup is isolated. The coordinator,
                # project preconditions, provider research, implementation loop,
                # ownership checks, and event writer all execute real code.
                stack.enter_context(patch("auto_agents.orchestrator.ensure_repo"))
                for name in ("_ensure_agent_instructions_synced", "_start_health_supervision",
                             "stop_health_supervision"):
                    stack.enter_context(patch.object(orch, name))
                provider = stack.enter_context(patch.object(
                    orch, "_call_with_failover", side_effect=AssertionError("no provider allowed")
                ))
                stack.enter_context(patch.object(orch.reporter, "event", side_effect=observe))
                with self.assertRaises(StopAfterEntry):
                    WorkflowCoordinator(orch).resume_workflow(workflow.workflow_id)
            provider.assert_not_called()
            self.assertEqual(len(captured), 1)
            entry = captured[0]
            state = load_run_state(root)
            self.assertEqual(state.current_stage, "implement")
            self.assertEqual(state.active_blocker, {})
            self.assertIn("plan", state.stage_summaries)
            self.assertIn("already verified", state.stage_summaries["provider_research"])
            self.assertNotIn("implement", state.stage_summaries)
            self.assertEqual(entry["run_id"], original.run_id)
            self.assertEqual(entry["workflow_id"], workflow.workflow_id)
            self.assertEqual(entry["spec_file"], str(spec.resolve()))
            self.assertEqual(entry["spec_sha256"], hashlib.sha256(spec.read_bytes()).hexdigest())
            self.assertEqual(entry["plan_sha256"], hashlib.sha256(task_plan_path(root).read_bytes()).hexdigest())
            self.assertEqual(entry["task_ids"], ["task-current"])
            self.assertEqual(entry["pending_task_ids"], ["task-current"])
            self.assertEqual(entry["agent_attempts"], original.agent_attempts)
            self.assertEqual(entry["engine_runtime"], orch._auto_agents_runtime_identity())
            event_path = root / ".auto-agents/runs" / original.run_id / "events.jsonl"
            events = [json.loads(line) for line in event_path.read_text().splitlines()]
            persisted = [e for e in events if e["type"] == "implementation.entered"]
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0]["data"], entry)
            self.assertTrue(persisted[0]["event_id"])
            self.assertEqual(requirements_trace_path(root).read_bytes(), trace_before)
            self.assertEqual(provider_references_lock_path(root).read_bytes(), lock_before)
            self.assertEqual(load_task_plan(root)["tasks"][0]["requirement_proofs"], plan["tasks"][0]["requirement_proofs"])

    def test_controller_collects_workflow_entry_and_supplies_it_to_review(self):
        from auto_agents.repair_v2.boundary_driver import observe_run_continuation
        from auto_agents.repair_v2.review_evidence import recovery_evidence

        with continuation_probe_project() as (root, orch, original, plan, request, runtime, frozen):
            result = observe_run_continuation(orch, original, plan, request, runtime, frozen)
            self.assertTrue(result['ok'])
            self.assertEqual(result['current_stage'], 'implement')
            receipt = result['recovery_observation']
            self.assertEqual(receipt['run_id'], original.run_id)
            self.assertEqual(receipt['workflow_id'], original.resume_context['workflow_id'])
            self.assertEqual(receipt['accepted_task_ids'], ['task-current'])
            self.assertEqual(receipt['oracle_proof_count'], 2)
            self.assertEqual(receipt['spec_sha256'], frozen['spec_sha256'])
            self.assertEqual(receipt['requirements_trace_sha256'], frozen['requirements_trace_sha256'])
            self.assertTrue(receipt['implementation_entry']['event_id'])
            report = {'ok': True, 'snapshot': 'candidate-source', 'runtime': 'pinned-verifier',
                      'target': 'frozen-scene', 'observed': result}
            controller = SimpleNamespace(
                state={'boundary_preflight': 'receipt.json', 'verification_runtime': 'pinned-verifier'},
                store=SimpleNamespace(read=lambda ref: report),
            )
            evidence = recovery_evidence(controller, 'candidate-source')
            self.assertEqual(evidence['cases'][0]['run_id'], original.run_id)
            self.assertEqual(evidence['cases'][0]['workflow_id'], receipt['workflow_id'])
            self.assertEqual(evidence['cases'][0]['current_stage'], 'implement')
            projected = evidence['cases'][0]['recovery_observation']
            for key, value in receipt.items():
                self.assertEqual(projected[key], value, key)
            self.assertIsNone(recovery_evidence(controller, 'other-candidate'))

    def test_controller_records_actual_research_prerequisite_without_claiming_implementation(self):
        from auto_agents.repair_v2.boundary_driver import observe_run_continuation
        with continuation_probe_project(research_required=True) as (root, orch, original, plan, request, runtime, frozen):
            lock_before = provider_references_lock_path(root).read_bytes()
            result = observe_run_continuation(orch, original, plan, request, runtime, frozen)
            receipt = result['recovery_observation']
            self.assertTrue(result['ok'])
            self.assertEqual(receipt['boundary_kind'], 'provider_research')
            self.assertEqual(receipt['continuation_status'], 'prerequisite_required')
            self.assertFalse(receipt['implementation_entered'])
            self.assertIsNone(receipt['implementation_entry'])
            self.assertEqual(receipt['continuation_entry']['type'], 'provider_research.required')
            self.assertEqual(receipt['prerequisites'][0]['status'], 'needs_refresh')
            self.assertEqual(receipt['accepted_task_ids'], ['task-current'])
            self.assertEqual(provider_references_lock_path(root).read_bytes(), lock_before)
            self.assertEqual(load_run_state(root).agent_attempts, original.agent_attempts)
            self.assertNotIn('provider_research', load_run_state(root).stage_summaries)

    def test_controller_rejects_fabricated_prerequisite_event(self):
        from auto_agents.repair_v2.boundary_driver import observe_run_continuation
        with continuation_probe_project(research_required=True) as (root, orch, original, plan, request, runtime, frozen):
            write_event = orch.reporter.event
            def tamper(kind, data, **options):
                if kind == 'provider_research.required':
                    data = {**data, 'prerequisites': [{'status': 'invented'}]}
                return write_event(kind, data, **options)
            with patch.object(orch.reporter, 'event', side_effect=tamper):
                with self.assertRaisesRegex(RuntimeError, 'identity or preservation'):
                    observe_run_continuation(orch, original, plan, request, runtime, frozen)

    def test_controller_rejects_old_entry_when_workflow_stops_before_implementation(self):
        from auto_agents.repair_v2.boundary_driver import observe_run_continuation

        with continuation_probe_project() as (root, orch, original, plan, request, runtime, frozen):
            first = observe_run_continuation(orch, original, plan, request, runtime, frozen)
            self.assertTrue(first['ok'])
            state = load_run_state(root)
            state.pending_approval = 'implementation-confirmation'
            save_run_state(root, state)
            with self.assertRaisesRegex(RuntimeError, 'fresh implementation entry'):
                observe_run_continuation(orch, original, plan, request, runtime, frozen)
            self.assertEqual(load_run_state(root).pending_approval, 'implementation-confirmation')

    def test_controller_rejects_foreign_identity_or_changed_retained_inputs(self):
        from auto_agents.repair_v2.boundary_driver import observe_run_continuation

        for mismatch in ('run', 'workflow', 'runtime', 'spec', 'trace', 'proof'):
            with self.subTest(mismatch=mismatch), continuation_probe_project() as (root, orch, original, plan, request, runtime, frozen):
                if mismatch == 'run':
                    request['invocation']['run_id'] = 'different-run'
                elif mismatch == 'workflow':
                    request['invocation']['workflow_id'] = 'different-workflow'
                elif mismatch == 'runtime':
                    runtime['commit'] = 'different-engine'
                elif mismatch == 'spec':
                    Path(original.resume_context['spec_file']).write_text('# Different scope\n')
                elif mismatch == 'trace':
                    changed = json.loads(requirements_trace_path(root).read_text())
                    changed['requirements'][1]['notes'] = 'changed historical backlog'
                    write_json(requirements_trace_path(root), changed)
                else:
                    # The expected contract is sealed before reconciliation;
                    # even a correctly identified entry cannot waive its loss.
                    plan['tasks'][0]['requirement_proofs'].pop()
                with self.assertRaises(RuntimeError):
                    observe_run_continuation(orch, original, plan, request, runtime, frozen)


if __name__ == "__main__":
    unittest.main()
