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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.config import load_run_state, load_task_plan, provider_references_lock_path, requirements_trace_path, save_project_config, save_run_state, task_plan_path
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, PersistenceTargetConfig, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.requirements import requirement_contract_sha256, stamp_provider_reference_consumer_hashes, run_requirements_audit, validate_task_requirement_coverage, validate_task_requirement_proofs
from auto_agents.validation import validate_task_plan_with_requirements
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
from auto_agents.workflow_runtime import WorkflowCoordinator
SPEC = Path('specs/current-iteration.md')

def requirement(req_id, source):
    return {'id': req_id, 'text': 'Preserve the public API contract.', 'source': source, 'status': 'active', 'priority': 'mandatory', 'acceptance_oracles': ['The public API returns normalized provider output.', 'The public API records durable provider evidence.'], 'oracle_type': 'integration_test', 'oracle_strength': 'behavioral', 'evidence_boundary': 'system_boundary', 'forbidden_proxy_oracles': ['config-only checks'], 'forbidden_patterns': [], 'external_docs_required': False, 'provider_reference': '', 'notes': ''}

def task_for(req, task_id='task-current', status='pending'):
    return {'task_id': task_id, 'title': 'Preserve the API contract', 'description': 'Verify the public output and its durable evidence.', 'acceptance': list(req['acceptance_oracles']), 'status': status, 'commit_message': '', 'requirement_ids': [req['id']], 'requirement_proofs': [{'requirement_id': req['id'], 'requirement_contract_sha256': requirement_contract_sha256(req), 'oracle_index': index, 'acceptance_oracle': oracle, 'proof_type': 'integration_test', 'oracle_strength': 'behavioral', 'evidence_boundary': 'system_boundary', 'evidence_refs': ['tests/test_api.py::test_contract'], 'forbidden_proxy_oracles': ['config-only checks'], 'proxy_oracles': [], 'status': 'verified' if status == 'done' else 'planned'} for index, oracle in enumerate(req['acceptance_oracles'], 1)]}

def scene():
    current = requirement('REQ-001', 'specs/older.md; ' + str(SPEC) + ' §1')
    old = requirement('REQ-002', 'specs/stopped-iteration.md §2')
    trace = {'version': 1, 'requirements': [current, old]}
    plan = {'oracle_proof_schema_version': 2, 'test_strategy': 'Offline public API regression', 'verification_commands': ['conda run -p ./.conda python -m pytest -q tests/test_api.py'], 'tasks': [task_for(current)]}
    return (trace, plan)

def retained_scope_run(root, spec, plan, workflow_id=''):
    """Synthetic copy of the retained run's planning-only failure shape."""
    state = load_run_state(root)
    state.run_id = 'retained-run'
    state.current_stage = 'design'
    state.stage_summaries = {'clarify': 'done', 'prototype': 'not requested', 'design': 'done'}
    state.agent_attempts['plan'] = 3
    state.status = 'blocked'
    state.active_blocker = {'owner': 'auto_agents', 'category': 'iteration_plan_scope_mismatch', 'fingerprint': 'retained-scope-conflict', 'status': 'blocked'}
    state.resume_context.update(spec_file=str(spec), workflow_id=workflow_id)
    conflict = {'blocker_id': 'plan-cumulative-scope-conflict', 'category': 'requirement_scope_conflict', 'status': 'needs_input', 'requirement_ids': ['REQ-002'], 'reason': 'Cumulative coverage conflicts with the independent iteration.'}
    blocked = copy.deepcopy(plan)
    blocked.update(stage_status='blocked', blockers=[conflict])
    write_json(task_plan_path(root), blocked)
    save_run_state(root, state)
    return (state, conflict)

class PlanningAdapter:

    def __init__(self, root, plans):
        self.root = root
        self.plans = plans
        self.prompts = []

    def run(self, request):
        assert request.stage == 'plan', request.stage
        self.prompts.append(request.prompt)
        payload = self.plans[min(len(self.prompts) - 1, len(self.plans) - 1)]
        write_json(task_plan_path(self.root), copy.deepcopy(payload))
        write_text(request.output_path, 'Current iteration coverage is complete.\n')
        return AgentResult(ok=True, command=['offline-planner'], output_path=request.output_path, summary='Current iteration coverage is complete.', returncode=0)

@contextmanager
def project():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'project'
        with patch('auto_agents.orchestrator.ensure_repo'), patch('auto_agents.orchestrator.sync_agent_instructions'):
            Orchestrator.init_project(root, 'scope-fixture', 'mock')
        orch = Orchestrator(root)
        orch.config.retries.per_stage['plan'] = 3
        orch.config.approvals.enabled = []
        save_project_config(root, orch.config)
        spec = root / SPEC
        write_text(spec, '# Independent iteration\nPreserve the public API contract.\n')
        write_text(root / 'tests/test_api.py', 'def test_contract():\n    assert True\n')
        trace, plan = scene()
        write_json(requirements_trace_path(root), trace)
        write_json(task_plan_path(root), plan)
        orch._attach_run_logger(load_run_state(root).run_id)

        def entries(project_root, ignored_prefixes=()):
            return [('??', str(path.relative_to(project_root))) for path in sorted(project_root.rglob('*')) if path.is_file() and (not str(path.relative_to(project_root)).startswith(ignored_prefixes))]
        with patch('auto_agents.orchestrator.changed_entries', side_effect=entries), patch('auto_agents.git_ops.changed_entries', side_effect=entries), patch.object(orch, '_cleanup_ephemeral_tooling_artifacts'), patch.object(orch, '_call_with_failover', side_effect=lambda request: orch.adapter.run(request)):
            yield (root, orch, spec, trace, plan)

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
            write_json(provider_references_lock_path(root), {'version': 1, 'references': {'current': {'path': reference, 'status': 'needs_refresh'}}})
        plan['tasks'][0]['expected_test_migrations'] = [{'ref': 'tests/test_api.py::test_contract', 'change': 'Preserve the current API assertion.'}]
        store = WorkflowStore(root)
        workflow = store.create_root(WorkflowRef('run', 'retained-run'))
        original, _ = retained_scope_run(root, spec, plan, workflow.workflow_id)
        original_plan = load_task_plan(root)
        frozen = run_input_hashes(root, original)
        identity = orch._auto_agents_runtime_identity()
        runtime = observe_engine(Path(identity['repository_root']), expected_commit=identity['repository_head'])
        assert runtime['ok'], runtime['mismatches']
        request = {'commit': runtime['commit'], 'invocation': {'run_id': original.run_id, 'workflow_id': workflow.workflow_id}}
        state = orch.mark_self_repair_applied(runtime['commit'])
        assert orch._resume_blocked_run(state)
        save_run_state(root, state)
        with ExitStack() as stack:
            stack.enter_context(patch('auto_agents.orchestrator.ensure_repo'))
            for name in ('_ensure_agent_instructions_synced', '_start_health_supervision', 'stop_health_supervision'):
                stack.enter_context(patch.object(orch, name))
            provider = stack.enter_context(patch.object(orch, '_call_with_failover', side_effect=AssertionError('no provider allowed')))
            yield (root, orch, original, original_plan, request, runtime, frozen)
            provider.assert_not_called()

class IterationPlanScopeTests(unittest.TestCase):

    def test_current_coverage_and_oracle_are_still_mandatory(self):
        trace, plan = scene()
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC), [])
        for kind in ('binding', 'proof'):
            with self.subTest(kind=kind):
                broken = copy.deepcopy(plan)
                if kind == 'binding':
                    broken['tasks'][0]['requirement_ids'] = []
                    broken['tasks'][0]['requirement_proofs'] = []
                else:
                    broken['tasks'][0]['requirement_proofs'].pop()
                errors = validate_task_plan_with_requirements(broken, trace, current_spec=SPEC)
                self.assertTrue(any(('REQ-001' in e for e in errors)), errors)
                self.assertTrue(any(('acceptance oracle #2' in e for e in errors)), errors)
                self.assertFalse(any(('REQ-002' in e for e in errors)), errors)

    def test_explicit_historical_adoption_requires_all_oracles(self):
        trace, plan = scene()
        plan['tasks'].append(task_for(trace['requirements'][1], 'task-adopted'))
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC), [])
        plan['tasks'][-1]['requirement_proofs'].pop()
        errors = validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)
        self.assertIn('mandatory requirement REQ-002 acceptance oracle #2 is not covered by requirement_proofs', errors)

    def test_scoped_validation_preserves_proof_contract_checks(self):
        for owner in ('current', 'historical'):
            for field, value, message in (('requirement_id', 'REQ-404', 'unknown requirement_id'), ('requirement_contract_sha256', 'sha256:' + '0' * 64, 'requirement_contract_sha256'), ('oracle_strength', 'structural', 'oracle_strength'), ('evidence_boundary', 'internal_state', 'evidence_boundary'), ('proxy_oracles', ['config-only checks'], 'forbidden proxy')):
                with self.subTest(owner=owner, field=field):
                    trace, plan = scene()
                    if owner == 'historical':
                        plan['tasks'].append(task_for(trace['requirements'][1], 'task-adopted'))
                    plan['tasks'][-1]['requirement_proofs'][0][field] = value
                    errors = validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)
                    self.assertTrue(any((message in e for e in errors)), errors)
        trace, plan = scene()
        plan['tasks'][0]['requirement_proofs'][0].update(oracle_index=99, acceptance_oracle='wrong oracle')
        self.assertTrue(any(('must identify an acceptance oracle' in e for e in validate_task_plan_with_requirements(plan, trace, current_spec=SPEC))))
        plan['tasks'][0]['requirement_ids'].append('REQ-404')
        self.assertTrue(any(('unknown requirement_ids' in e for e in validate_task_plan_with_requirements(plan, trace, current_spec=SPEC))))

    def test_absent_scope_is_cumulative_at_every_validation_entrypoint(self):
        trace, plan = scene()
        plan['scope_notes'] = ['Only REQ-001 is in scope']
        plan['current_spec'] = str(SPEC)
        for validator in (validate_task_plan_with_requirements, validate_task_requirement_coverage, validate_task_requirement_proofs):
            with self.subTest(validator=validator.__name__):
                errors = validator(plan, trace)
                self.assertTrue(any(('REQ-002 acceptance oracle #1' in e for e in errors)), errors)
                self.assertEqual(validator(plan, trace, current_spec=SPEC), [])

    def test_full_registry_and_inputs_are_preserved(self):
        trace, plan = scene()
        saved = copy.deepcopy((plan, trace))
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC), [])
        self.assertEqual((plan, trace), saved)
        self.assertEqual(trace['requirements'][1]['status'], 'active')
        trace['requirements'][1]['priority'] = 'invalid'
        self.assertTrue(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC))

    def test_unselected_historical_proof_is_validated_against_full_registry(self):
        trace, plan = scene()
        orphan = task_for(trace['requirements'][1])['requirement_proofs'][0]
        plan['tasks'][0]['requirement_proofs'].append(orphan)
        errors = validate_task_plan_with_requirements(plan, trace, current_spec=SPEC)
        self.assertTrue(any(('requirement_id must also appear in task requirement_ids: REQ-002' in e for e in errors)), errors)
        self.assertFalse(any(('unknown requirement_id: REQ-002' in e for e in errors)), errors)

    def test_archived_verified_proofs_and_explicit_reownership(self):
        trace, plan = scene()
        archived = task_for(trace['requirements'][1], 'task-archived', 'done')
        saved = copy.deepcopy(archived)
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, historical_tasks=[archived]), [])
        self.assertEqual(validate_task_plan_with_requirements(plan, trace, current_spec=SPEC, historical_tasks=[archived]), [])
        self.assertEqual(archived, saved)
        archived['requirement_proofs'].pop()
        adopted = task_for(trace['requirements'][1], 'task-adopted')
        adopted['requirement_proofs'].pop()
        plan['tasks'].append(adopted)
        errors = validate_task_plan_with_requirements(plan, trace, current_spec=SPEC, historical_tasks=[archived])
        self.assertTrue(any(('REQ-002 acceptance oracle #2' in e for e in errors)), errors)
if __name__ == '__main__':
    unittest.main()
