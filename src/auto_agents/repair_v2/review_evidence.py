"""Controller-produced recovery observations supplied to independent review."""


def recovery_evidence(controller, identity):
    reference = controller.state.get('boundary_preflight')
    if not reference:
        return None
    report = controller.store.read(reference)
    if (report.get('snapshot') != identity
            or report.get('runtime', '') != controller.state.get('verification_runtime', '')):
        return None
    cases = []
    for case in report.get('cases', [report]):
        observed = case.get('observed', {})
        runtime = observed.get('engine_runtime', {})
        recovery = observed.get('recovery_observation', {})
        cases.append({
            'ok': case.get('ok'), 'snapshot': case.get('snapshot'),
            'target_digest': case.get('target'), 'runtime': case.get('runtime'),
            'proof_directory': case.get('proof_directory'),
            'route_consumed': observed.get('route_consumed'),
            'error': observed.get('error'),
            'run_id': observed.get('run_id'),
            'workflow_id': observed.get('workflow_id'),
            'current_stage': observed.get('current_stage'),
            'engine_runtime': {key: runtime.get(key) for key in ('ok', 'commit', 'runtime_root', 'mismatches')},
            'recovery_observation': {key: recovery.get(key) for key in (
                'workflow_id', 'original_handoff_id', 'child_session_id', 'parent_session_id',
                'boundary_kind', 'boundary_session_id', 'preflight_started', 'preflight_rechecked',
                'preflight_outcome', 'new_preflight_events', 'diagnostic_origin',
                'parent_budget', 'child_budget', 'parent_constraints_preserved',
                'child_constraints_preserved', 'retained_constraints', 'diagnostic_provider_calls',
                'run_id', 'implementation_entry', 'continuation_entry', 'implementation_entered',
                'continuation_status', 'prerequisites', 'provider_admission', 'entry_event_ref', 'spec_sha256',
                'accepted_plan_sha256', 'requirements_trace_sha256', 'accepted_task_ids',
                'oracle_proof_count', 'verification_step_count', 'submission_receipt',
                'task_id', 'verification_completed', 'verification_entry', 'verification_event',
                'review_entered', 'review_entry',
                'verification_receipt', 'event_order',
                'execution_reports',
                'task_scope', 'ok')},
        })
    return {'artifact': reference, 'ok': report.get('ok'), 'snapshot': identity,
            'runtime': report.get('runtime'), 'cases': cases}
