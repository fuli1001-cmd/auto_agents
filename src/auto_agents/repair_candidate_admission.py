"""A writer's explicit not-ready handoff is not a candidate approval."""
import json
import re


def declared_not_ready(text, component):
    objects = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    objects.append(text.strip())
    for raw in objects:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (isinstance(value, dict) and value.get('component') == component
                and value.get('candidate_ready') is False
                and value.get('status') in {'capability_blocked', 'not_ready', 'blocked'}):
            return value
    return None


def admission_blocker(runner, workspace, summary):
    from .repair_memory import save_record
    from .repair_capability_checks import namespace_observation
    from .verification_ledger import source_identity
    from .repair_control import digest
    group = runner._candidate_group
    declaration = declared_not_ready(summary, group.get('group_id'))
    if declaration is None:
        return None
    namespace_requested = (declaration['status'] == 'capability_blocked'
        and (declaration.get('capability') == 'nested_user_mount_namespace'
             or declaration.get('reference') == 'nested_private_metadata_compatibility'))
    observed = (namespace_observation(runner, workspace) if namespace_requested
                else {'status': 'not_requested', 'supported': None, 'acceptance_proof': False})
    verified = observed.get('supported') is False
    reason = ('candidate not ready; production namespace preflight failed: ' + observed.get('reason', '')
              if verified else 'writer declared candidate not ready; capability cause remains unconfirmed')
    reference = save_record(runner, 'candidate_admission', {
        'component': group['group_id'], 'candidate_id': runner._candidate_id,
        'source': source_identity(workspace), 'declaration': declaration,
        'observation': observed, 'candidate_ready': False, 'reason': reason})
    evidence = {'evidence_id': reference['id'], 'phase': 'candidate_admission',
        'candidate_id': runner._candidate_id, 'command': 'candidate readiness',
        'failure_kind': 'capability' if verified else 'candidate_not_ready',
        'admission_source': source_identity(workspace),
        'admission_environment': digest(runner._full_suite_environment_fingerprint()),
        'capability_requested': namespace_requested,
        'next_action': 'blocked', 'excerpt': reason,
        'artifacts': {'result': str(runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json')}}
    runner._candidate_failure_evidence = [*getattr(runner, '_candidate_failure_evidence', []), evidence]
    return {'kind': 'blocked', 'reason': reason, 'evidence_ids': [reference['id']],
            'capability_verified': verified, 'candidate_ready': False, 'retry_fix': False}
