"""Synthetic lifecycle tests; sealed-scene replay remains a separate acceptance proof."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.config import load_run_state, load_task_plan, save_run_state, task_plan_path
from auto_agents.models import TaskSpec
from auto_agents.repair_v2.boundary_driver import (
    DIAGNOSTIC_BINDING_CATEGORY, diagnostic_continuation_complete, observe_run_continuation, run_input_hashes,
)
from auto_agents.repair_v2.diagnostic_replay import observe_submission, retained_diagnosis
from auto_agents.repair_v2.store import atomic_json
from auto_agents.root_cause import RootCauseDiagnosis, RootCauseReport
from auto_agents.workflow_chain import WorkflowStore, WorkflowRef
from test_iteration_plan_scope import project, task_for
from test_root_cause import _report


@contextmanager
def retained_submission_scene(*, research_required=False):
    from auto_agents.repair_runtime_identity import observe_engine

    with project() as (root, orch, spec, trace, plan), ExitStack() as stack:
        if research_required:
            from auto_agents.config import requirements_trace_path, provider_references_lock_path
            reference = '.auto-agents/docs/provider_references/current.md'
            trace['requirements'][0].update(external_docs_required=True, provider_reference=reference)
            plan['tasks'] = [task_for(trace['requirements'][0])]
            atomic_json(requirements_trace_path(root), trace)
            (root / reference).parent.mkdir(parents=True, exist_ok=True)
            (root / reference).write_text('# Retained prerequisite requiring review\n')
            atomic_json(provider_references_lock_path(root), {'version': 1, 'references': {
                'current': {'path': reference, 'status': 'needs_refresh'}}})
        run = '82288622684f'
        workflow = WorkflowStore(root).create_root(WorkflowRef('run', run))
        plan['tasks'][0]['status'] = 'blocked'
        atomic_json(task_plan_path(root), plan)
        state = load_run_state(root)
        state.run_id, state.current_stage, state.status = run, 'implement', 'blocked'
        state.stage_summaries = {key: 'Retained evidence' for key in
                                ('clarify', 'design', 'prototype', 'plan', 'provider_research')}
        if research_required:
            state.stage_summaries.pop('provider_research')
        state.active_blocker = {'owner': 'auto_agents', 'category': DIAGNOSTIC_BINDING_CATEGORY,
                                'status': 'blocked', 'fingerprint': 'retained-binding-failure',
                                'reason': 'diagnostic admission prevents publication repair'}
        state.last_error = state.active_blocker['reason']
        state.tasks = [TaskSpec.from_dict(task) for task in plan['tasks']]
        state.tasks[0].verify_history = [{'decision': 'pass', 'candidate_fingerprint': 'retained'}]
        state.localized_blockers = [{'task_id': 'task-current', 'category': 'publication_failure', 'status': 'localized'}]
        state.resume_context.update(spec_file=str(spec), workflow_id=workflow.workflow_id, auto_approve=True)
        save_run_state(root, state)
        orch._attach_run_logger(run)
        evidence = root / f'.auto-agents/runs/{run}/root-cause/retained/evidence.json'
        atomic_json(evidence, {'repair_case': {'run_id': run, 'synthetic': True}})
        report = RootCauseReport.from_dict({**_report(role='investigator', verdict='ROOT_CAUSE'),
            'necessity': {'decision': 'required', 'blocked_step': 'Publish the retained task',
                'consequence': 'publication is blocked', 'recovery_check': 'submission and implementation entry',
                'evidence_refs': [{'origin': 'source', 'path': '.root-cause-evidence.json#/repair_case'}]}},
            role='investigator')
        reviewer = RootCauseReport.from_dict(_report(role='reviewer', verdict='AGREE'), role='reviewer')
        diagnosis = RootCauseDiagnosis('retained', str(evidence), report, reviewer, report, None, True, '')
        diagnosis = RootCauseDiagnosis.from_dict(json.loads(json.dumps(diagnosis.to_dict())))
        identity = orch._auto_agents_runtime_identity()
        runtime = observe_engine(Path(identity['repository_root']), expected_commit=identity['repository_head'])
        assert runtime['ok']
        request = {'project': str(root), 'commit': runtime['commit'], 'diagnosis': diagnosis.to_dict(),
                   'invocation': {'run_id': run, 'workflow_id': workflow.workflow_id}}
        frozen = run_input_hashes(root, state)
        stack.enter_context(patch('auto_agents.orchestrator.ensure_repo'))
        for name in ('_ensure_agent_instructions_synced', '_start_health_supervision', 'stop_health_supervision'):
            stack.enter_context(patch.object(orch, name))
        stack.enter_context(patch.object(orch, '_changed_paths_excluding_agent_instructions', return_value=[]))
        provider = stack.enter_context(patch.object(orch, '_call_with_failover', side_effect=AssertionError('no provider allowed')))
        yield root, orch, deepcopy(state), plan, request, runtime, frozen, diagnosis
        provider.assert_not_called()


def test_diagnostic_repair_submits_and_reenters_saved_workflow(tmp_path):
    from auto_agents.repair_v2.review_evidence import recovery_evidence

    with retained_submission_scene() as (root, orch, original, plan, request, runtime, frozen, diagnosis):
        unchanged = deepcopy(diagnosis.to_dict())
        submission = observe_submission(orch, original, diagnosis, request, runtime, tmp_path / 'proof')
        assert submission['accepted'] and submission['job_state'] == 'queued'
        assert submission['run_id'] == original.run_id
        assert submission['workflow_id'] == original.resume_context['workflow_id']
        assert submission['event']['event_id']
        assert (tmp_path / 'proof/submission-control/control.sqlite3').is_file()
        assert json.loads((tmp_path / 'proof/submission.json').read_text()) == submission
        assert diagnosis.to_dict() == unchanged
        state = orch.mark_self_repair_applied(runtime['commit'])
        assert orch._resume_blocked_run(state)
        save_run_state(root, state)
        result = observe_run_continuation(orch, original, plan, request, runtime, frozen, submission=submission)
        receipt = result['recovery_observation']
        assert result['ok'] and receipt['implementation_entered'] is True
        assert diagnostic_continuation_complete(result, original.to_dict())
        assert receipt['run_id'] == original.run_id and receipt['workflow_id'] == original.resume_context['workflow_id']
        assert receipt['implementation_entry']['event_id'] != submission['event']['event_id']
        assert receipt['implementation_entry']['data']['pending_task_ids'] == ['task-current']
        assert receipt['submission_receipt'] == submission and receipt['entry_event_ref']
        assert load_task_plan(root)['tasks'][0]['status'] != 'done'
        assert load_run_state(root).localized_blockers == original.localized_blockers
        control = SimpleNamespace(state={'boundary_preflight': 'boundary', 'verification_runtime': 'trusted'},
            store=SimpleNamespace(read=lambda ref: {'snapshot': 'candidate', 'runtime': 'trusted', 'ok': True,
                                                  'observed': result}))
        projected = recovery_evidence(control, 'candidate')['cases'][0]['recovery_observation']
        assert projected['submission_receipt'] == submission
        assert projected['implementation_entered'] and projected['entry_event_ref'] == receipt['entry_event_ref']


def test_clearance_only_report_cannot_satisfy_diagnostic_boundary():
    original = {'run_id': '82288622684f', 'resume_context': {'workflow_id': 'wf-cea9506a7499'}}
    counterexample = {'ok': True, 'run_id': original['run_id'], 'status': 'pending',
                      'remaining_blocked': [], 'same_blocker': False}
    assert not diagnostic_continuation_complete(counterexample, original)


def test_diagnostic_recovery_rejects_clearance_without_submission(tmp_path):
    with retained_submission_scene() as (root, orch, original, plan, request, runtime, frozen, diagnosis):
        state = orch.mark_self_repair_applied(runtime['commit'])
        assert orch._resume_blocked_run(state)
        save_run_state(root, state)
        assert state.status == 'pending' and not state.active_blocker
        with pytest.raises(RuntimeError, match='supervisor-accepted submission'):
            observe_run_continuation(orch, original, plan, request, runtime, frozen)


def test_retained_submission_requires_its_own_evidence():
    with retained_submission_scene() as (root, _, _, _, request, _, _, diagnosis):
        parsed, relative = retained_diagnosis(root, request)
        assert parsed.to_dict() == diagnosis.to_dict()
        (root / relative).unlink()
        with pytest.raises(FileNotFoundError, match='retained diagnosis evidence is absent'):
            retained_diagnosis(root, request)


def test_submission_uses_the_referenced_retained_certificate():
    with retained_submission_scene() as (root, _, _, _, request, _, _, diagnosis):
        relative = '.auto-agents/state/root_cause_certificates/' + 'a' * 64 + '.json'
        certificate = {'certificate_key': 'a' * 64, 'diagnosis': diagnosis.to_dict()}
        atomic_json(root / relative, certificate)
        outer = deepcopy(request)
        outer['diagnosis']['final']['necessity']['evidence_refs'] = [{'origin': 'target', 'path': relative}]
        before = deepcopy(outer)
        parsed, evidence = retained_diagnosis(root, outer)
        assert parsed.to_dict() == diagnosis.to_dict()
        assert evidence == Path(diagnosis.evidence_path).relative_to(root)
        assert outer == before
        atomic_json(root / relative, {**certificate, 'certificate_key': 'b' * 64})
        with pytest.raises(ValueError, match='certificate identity mismatch'):
            retained_diagnosis(root, outer)


@pytest.mark.parametrize('mismatch', ['run', 'workflow', 'runtime', 'digest', 'requeue', 'contract', 'approval', 'no_entry'])
def test_diagnostic_recovery_rejects_incomplete_or_changed_proof(tmp_path, mismatch):
    with retained_submission_scene() as (root, orch, original, plan, request, runtime, frozen, diagnosis):
        submission = observe_submission(orch, original, diagnosis, request, runtime, tmp_path / 'proof')
        state = orch.mark_self_repair_applied(runtime['commit'])
        assert orch._resume_blocked_run(state)
        if mismatch == 'run':
            submission['run_id'] = 'foreign'
        elif mismatch == 'workflow':
            submission['workflow_id'] = 'foreign'
        elif mismatch == 'runtime':
            submission['engine_commit'] = 'foreign'
        elif mismatch == 'digest':
            submission['scope_receipt_digest'] = '0' * 64
        elif mismatch == 'requeue':
            state.last_recovery_route['diagnostic_evidence_repair']['repaired_blocker']['requeued_task_ids'] = ['foreign']
        elif mismatch == 'contract':
            changed = load_task_plan(root)
            changed['tasks'][0]['acceptance'].append('Unapproved extra requirement')
            atomic_json(task_plan_path(root), changed)
        elif mismatch == 'approval':
            state.pending_approval = 'implement'
        save_run_state(root, state)
        with ExitStack() as stack:
            if mismatch == 'no_entry':
                # A prior valid event is not evidence of this workflow resume.
                first = observe_run_continuation(orch, original, plan, request, runtime, frozen, submission=submission)
                assert first['recovery_observation']['implementation_entered']
                stack.enter_context(patch('auto_agents.workflow_runtime.WorkflowCoordinator.resume_workflow',
                                          return_value=load_run_state(root)))
            with pytest.raises(RuntimeError):
                observe_run_continuation(orch, original, plan, request, runtime, frozen, submission=submission)


def test_diagnostic_snapshot_retains_only_its_owned_document(tmp_path):
    from auto_agents.root_cause import RootCauseCoordinator
    from auto_agents.repair_v2.diagnostic_replay import copy_submission_evidence

    with retained_submission_scene() as (root, _, _, _, request, _, _, diagnosis):
        unrelated = root / '.auto-agents/runs/unrelated/private.json'
        atomic_json(unrelated, {'private': 'unrelated run'})
        snapshot = tmp_path / 'snapshot'
        RootCauseCoordinator._copy_diagnostic_tree(root, snapshot)
        relative = Path(diagnosis.evidence_path).relative_to(root)
        assert not (snapshot / relative).exists()
        copy_submission_evidence(root, snapshot, request)
        assert (snapshot / relative).read_bytes() == (root / relative).read_bytes()
        assert not (snapshot / unrelated.relative_to(root)).exists()
        parsed, copied_relative = retained_diagnosis(snapshot, request)
        assert parsed.to_dict() == diagnosis.to_dict() and copied_relative == relative


def test_diagnostic_recovery_does_not_accept_only_a_prerequisite(tmp_path):
    with retained_submission_scene(research_required=True) as (root, orch, original, plan, request, runtime, frozen, diagnosis):
        submission = observe_submission(orch, original, diagnosis, request, runtime, tmp_path / 'proof')
        state = orch.mark_self_repair_applied(runtime['commit'])
        assert orch._resume_blocked_run(state)
        save_run_state(root, state)
        with pytest.raises(RuntimeError, match='prerequisite, not implementation'):
            observe_run_continuation(orch, original, plan, request, runtime, frozen, submission=submission)
        events = [json.loads(line) for line in (root / submission['event_ref']).read_text().splitlines()]
        assert any(event['type'] == 'provider_research.required' for event in events)
        assert not any(event['type'] == 'implementation.entered' for event in events)
