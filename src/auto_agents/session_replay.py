"""Subprocess probe of a session's deterministic resume boundary.

The engine root is an explicit argument so the identical driver can exercise
both the base revision and its candidate. No model call is executed by a probe.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from contextlib import ExitStack
import runpy
from unittest.mock import patch


def main() -> dict[str, object]:
    engine, target, session_id, mode = sys.argv[1:5]
    sys.path.insert(0, str(Path(engine) / "src"))
    from auto_agents.config import load_session_state
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.workflow_runtime import WorkflowCoordinator
    from auto_agents import repair_client
    from auto_agents import session_verification
    from auto_agents.execution_binding import route_sources

    # This sibling, like the replay harness, belongs to the trusted controller.
    identity = runpy.run_path(str(Path(__file__).with_name('repair_runtime_identity.py')))
    runtime_report = {}
    recovery = {'required': False, 'diagnostic_provider_calls': 0}
    probe = os.environ.get('AUTO_AGENTS_REPAIR_ROUTE_PROBE')
    approved = json.loads(Path(probe).read_text()) if probe else {}
    expected_route = approved.get('engine_route', {})
    recovery['required'] = any(source.get('child_session_id') or source.get('failed_handoff_id')
                               for source in route_sources(expected_route))
    frames = []
    boundary_state = {}
    boundary_kind = ''
    before_child = {}
    before_parent = {}
    parent_boundary_state = {}
    classifications = []
    binding_checks = []
    bind_session = session_verification.bind_session

    def observe_binding(session, state, *args, **kwargs):
        result = bind_session(session, state, *args, **kwargs)
        binding_checks.append({'session_id': state.session_id,
                               'fingerprint': state.verification_binding.get('binding_fingerprint')})
        return result

    classify = session_verification._session_reference_kind
    def observe_reference(session, state, gates, ref):
        kind = classify(session, state, gates, ref)
        item = {'session_id': state.session_id, 'reference': ref, 'kind': kind,
                'contract_revision': state.verification_binding.get('contract_revision')}
        if item not in classifications:
            classifications.append(item)
        return kind

    class NextProviderBoundary(BaseException):
        pass

    def no_provider(*args, **kwargs):
        nonlocal boundary_kind
        if not boundary_kind:
            boundary_kind = 'provider'
        request = args[1] if len(args) > 1 else kwargs.get('request')
        recovery['provider_purpose'] = getattr(request, 'purpose', '')
        if boundary_kind == 'implementation' and recovery['provider_purpose'] != 'fix':
            boundary_kind = 'diagnosis'
        recovery['provider_boundary_calls'] = recovery.get('provider_boundary_calls', 0) + 1
        if frames:
            boundary_state.update(frames[-1].to_dict())
        raise NextProviderBoundary()

    call_agent = Session._call_agent
    def before_agent(self, state, label, prompt):
        nonlocal boundary_kind
        import re
        boundary_kind = ('implementation' if state.mode == 'fix' and re.fullmatch(r'fix-\d+', label)
                         else 'diagnosis')
        if boundary_kind == 'implementation':
            recovery['activation_binding_fingerprint'] = state.verification_binding.get('binding_fingerprint')
        return call_agent(self, state, label, prompt)

    acknowledge = getattr(Session, '_ack_engine_recovery', None)
    def observe_acknowledgement(self, state, stage, **details):
        nonlocal boundary_kind
        if (stage == 'verification' and recovery['required']
                and state.session_id == before_child.get('session_id')
                and before_child.get('candidate_custody', {}).get('receipt')):
            boundary_kind = 'verification'
            boundary_state.update(state.to_dict())
            recovery.update(verification_identity=details.get('verification_identity'),
                activation_binding_fingerprint=state.verification_binding.get('binding_fingerprint'),
                candidate_fingerprint=state.candidate_custody.get('receipt', {}).get('fingerprint'))
            raise NextProviderBoundary()
        return acknowledge(self, state, stage, **details)

    drive = WorkflowCoordinator._drive_session
    def observe_drive(self, session, state, workflow, *, root):
        frames.append(state)
        try:
            return drive(self, session, state, workflow, root=root)
        finally:
            if state.session_id == before_parent.get('session_id'):
                parent_boundary_state.update(state.to_dict())
            frames.pop()

    parent_phase = Session._phase_collab_loop
    def observe_parent_phase(self, state):
        nonlocal boundary_kind
        if recovery['required'] and before_child:
            child = load_session_state(project, before_child['session_id'])
            if child.status in {'blocked', 'failed'}:
                # The bound child's failure is the observation. Do not let
                # the diagnostic create a new parent baseline before the
                # parent's next provider call. This boundary cannot pass the
                # child-entry and fresh-preflight checks below.
                boundary_kind = 'blocked_child_return'
                boundary_state.update(state.to_dict())
                raise NextProviderBoundary()
        return parent_phase(self, state)

    route = repair_client.engine_route
    def observe_route(orchestrator, payload):
        accepted = route(orchestrator, payload)
        required = any(source.get('child_session_id') or source.get('failed_handoff_id')
                       for source in route_sources(payload))
        recovery['required'] |= required
        if accepted and required:
            try:
                parent = load_session_state(project, session_id)
                coordinator = WorkflowCoordinator(orchestrator)
                snapshot = coordinator.store.load(parent.workflow_id)
                child_id = coordinator._engine_child_id(payload, snapshot)
                child = load_session_state(project, child_id)
                if not before_child:
                    before_child.update(child.to_dict())
                from auto_agents.repair_v2.incidents import latest_failure
                failure = latest_failure(before_child)
                recovery.update(child_session_id=child_id, workflow_id=child.workflow_id,
                                original_handoff_id=child.parent_handoff_id,
                                previous_failure=failure[1] if failure else {})
            except Exception as error:
                # Observe the error without bypassing the real recovery path.
                recovery['identity_error'] = str(error)
        return accepted

    project = Path(target)
    try:
        runtime_report = identity['observe_engine'](engine, expected_commit=approved.get('engine_commit'))
        from auto_agents.workflow_chain import WorkflowStore
        initial = load_session_state(project, session_id)
        root_ref = WorkflowStore(project).load(initial.workflow_id).root if initial.workflow_id else None
        parent_id = root_ref.native_id if root_ref and root_ref.kind in {'collab', 'fix'} else session_id
        before_parent.update(load_session_state(project, parent_id).to_dict())
        # Older revisions have no recovery preflight; exercise their real load.
        import auto_agents.cli as cli
        prepare = getattr(cli, "_prepare_explicit_session", None)
        if prepare is not None:
            prepare(project, session_id, mode)
        else:
            load_session_state(project, session_id)
        # Child recovery may construct another Orchestrator. Keep every such
        # instance at the same offline provider boundary within this process.
        retained = load_session_state(project, session_id)
        with ExitStack() as patches:
            patches.enter_context(patch.object(Orchestrator, '_call_with_failover', no_provider))
            patches.enter_context(patch.object(Session, '_call_agent', before_agent))
            if acknowledge is not None:
                patches.enter_context(patch.object(Session, '_ack_engine_recovery', observe_acknowledgement))
            patches.enter_context(patch.object(WorkflowCoordinator, '_drive_session', observe_drive))
            patches.enter_context(patch.object(Session, '_phase_collab_loop', observe_parent_phase))
            patches.enter_context(patch.object(repair_client, 'engine_route', observe_route))
            patches.enter_context(patch.object(session_verification, '_session_reference_kind', observe_reference))
            patches.enter_context(patch.object(session_verification, 'bind_session', observe_binding))
            orchestrator = Orchestrator(project)
            session = Session(orchestrator, mode=mode, auto_approve=retained.auto_approve)
            coordinator = WorkflowCoordinator(orchestrator, auto_approve=retained.auto_approve)
            session._coordinator = coordinator
            session._coordinator_managed = True
            state = session.resume(session_id)
        payload = {"ok": state.status == "completed", "status": state.status,
                   "error": state.resolution, "session_id": state.session_id}
    except NextProviderBoundary:
        payload = {"ok": True, "status": "next_provider_boundary", "session_id": session_id}
    except Exception as error:
        payload = {"ok": False, "status": "failed", "error": str(error), "error_type": type(error).__name__}
        runtime_report = getattr(error, 'report', runtime_report)
        if hasattr(error, 'diagnostic'):
            payload['diagnostic'] = error.diagnostic
    if probe:
        expected = approved.get('route_digest')
        consumed = getattr(locals().get("orchestrator"), "_repair_route_probe_consumed", None)
        payload["route_consumed"] = bool(expected and consumed == expected)
        if not payload["route_consumed"]:
            payload['ok'] = False
            if not payload.get('error'):
                payload['error'] = 'original engine request was not consumed before the next boundary'
        recovery['route_digest'] = expected
    if recovery['required']:
        try:
            child_id = recovery['child_session_id']
            after = load_session_state(project, child_id).to_dict()
            if boundary_state.get('session_id') == child_id:
                after = boundary_state
            binding = after.get('verification_binding', {})
            history_preserved = after['execution_log'][:len(before_child['execution_log'])] == before_child['execution_log']
            new_events = after['execution_log'][len(before_child['execution_log']):] if history_preserved else []
            recovery.update(child_status=after['status'],
                            boundary_session_id=boundary_state.get('session_id'),
                            contract_revision=binding.get('contract_revision'),
                            binding_fingerprint=binding.get('binding_fingerprint'),
                            task_scope=binding.get('task_scope'),
                            reference_decisions=binding.get('required_references', {}),
                            current_failure=next((entry for entry in reversed(new_events)
                                if entry.get('action') == 'execution_preflight_blocked'), {}))
            preserved = history_preserved and all(after.get(key) == before_child.get(key) for key in (
                'session_id', 'parent_handoff_id', 'workflow_id', 'goal',
                'goal_execution_environment', 'authorization_policy', 'hard_ceiling'))
            rechecks = [entry for entry in new_events if entry.get('action') == 'engine_preflight_recheck'
                            and entry.get('route_digest') == recovery.get('route_digest')
                            and entry.get('child_session_id') == child_id
                            and entry.get('handoff_id') == recovery['original_handoff_id']]
            freshly_bound = any(row['session_id'] == child_id and row['fingerprint']
                                and row['fingerprint'] == binding.get('binding_fingerprint') for row in binding_checks)
            active_failure = (before_child.get('status') == 'blocked'
                              and recovery.get('previous_failure', {}).get('action') == 'execution_preflight_blocked')
            rechecked = bool(rechecks) or bool(not active_failure and freshly_bound)
            started = [entry for entry in new_events if entry.get('action') == 'engine_preflight_recheck_started'
                       and entry.get('route_digest') == recovery.get('route_digest')]
            recovery.update(preflight_started=bool(started), new_preflight_events=[*started, *rechecks],
                            diagnostic_origin=('fresh' if recovery['current_failure'] else
                                               'rechecked' if rechecked else 'historical_replay'))
            preserved &= all(after.get(key) == before_child.get(key) for key in ('attempt_epoch', 'max_attempts'))
            reservations = recovery.get('provider_boundary_calls', 0) if boundary_kind == 'implementation' else 0
            preserved &= reservations in (0, 1) and all(
                after.get(key) == before_child.get(key, 0) + reservations
                for key in ('current_attempt', 'attempts_since_progress'))
            if active_failure:
                preserved &= bool(rechecked and binding)
                preserved &= recovery['previous_failure'] in after['execution_log']
            budgets = ('current_attempt', 'attempt_epoch', 'attempts_since_progress', 'max_attempts', 'hard_ceiling')
            parent_after = parent_boundary_state or load_session_state(project, before_parent['session_id']).to_dict()
            parent_preserved = bool(before_parent) and all(parent_after.get(key) == before_parent.get(key)
                for key in (*budgets, 'session_id', 'workflow_id', 'goal', 'goal_execution_environment',
                            'authorization_policy', 'auto_approve', 'full_verify'))
            parent_preserved &= (parent_after['execution_log'][:len(before_parent['execution_log'])]
                                 == before_parent['execution_log'])
            recovery.update(parent_session_id=before_parent['session_id'],
                parent_constraints_preserved=parent_preserved, child_constraints_preserved=bool(preserved),
                parent_budget={'before': {k: before_parent.get(k) for k in budgets},
                               'after': {k: parent_after.get(k) for k in budgets}},
                child_budget={'before': {k: before_child.get(k) for k in budgets},
                              'after': {k: after.get(k) for k in budgets}},
                budget_reserved=after.get('current_attempt', 0) - before_child.get('current_attempt', 0),
                full_dispatch=True)
            preserved &= parent_preserved
            entered = (boundary_state.get('session_id') == child_id
                       and boundary_kind == 'implementation' and after['status'] == 'executing'
                       and bool(binding) and freshly_bound and reservations == 1)
            completed = (after['status'] == 'completed' and binding
                         and after.get('candidate_custody', {}).get('receipt'))
            verified = (boundary_kind == 'verification' and recovery.get('verification_identity')
                        and recovery.get('candidate_fingerprint') == before_child.get('candidate_custody', {}).get('receipt', {}).get('fingerprint')
                        and any(row.get('action') == 'receipt_verification'
                                and row.get('verification', {}).get('ok')
                                and row.get('verification', {}).get('execution_identity') == recovery['verification_identity']
                                for row in after['execution_log']))
            recovery.update(preflight_rechecked=rechecked, retained_constraints=bool(preserved),
                            boundary_kind=boundary_kind,
                            preflight_outcome='passed' if rechecked and not recovery['current_failure'] else 'not_passed',
                            ok=bool(preserved and (entered or completed or verified)
                                    and not recovery.get('identity_error')))
        except (OSError, ValueError, KeyError) as error:
            recovery.update(ok=False, error=str(error))
        if not recovery.get('ok'):
            payload['ok'] = False
            if not payload.get('error'):
                payload['error'] = 'bound child did not pass its retained recovery preflight'
    recovery['reference_classifications'] = classifications
    payload.update(engine_runtime=runtime_report, recovery_observation=recovery)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


if __name__ == "__main__":
    main()
