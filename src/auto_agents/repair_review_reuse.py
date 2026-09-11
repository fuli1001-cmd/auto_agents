"""Reuse independent reviews only for identical admitted inputs and coverage."""
from pathlib import Path
import os
from types import SimpleNamespace

from .repair_control import digest
from .repair_memory import component_key, read_record, save_record
from .verification_ledger import source_identity


def reviewer_policy(runner, root):
    config = getattr(runner.target_orchestrator, 'config', None)
    provider = getattr(runner.target_orchestrator, '_current_provider', '') or getattr(config, 'active_provider', '')
    effort = runner._review_effort()
    selected = getattr(config, 'providers', {}).get(provider)
    if selected is None:
        return [provider, effort]
    try:
        from .prompting.runtime import _settings_fingerprint, binary_identity
        return [provider, effort, selected.kind, binary_identity(selected.binary),
                _settings_fingerprint(selected, SimpleNamespace(cwd=Path(root), effort=effort), dict(os.environ))]
    except (OSError, ValueError, TypeError, AttributeError):
        return None  # Unknown review policy cannot authorize reuse.


def review_identity(runner, root, group, phase):
    if phase == 'integration' or not runner._acceleration_enabled() or not hasattr(runner, '_experiment_store'):
        return ''
    reviewer = reviewer_policy(runner, root)
    if reviewer is None:
        return ''
    state = runner._experiment
    # Presentation and orchestration bookkeeping are not new review scope.
    coverage = {key: value for key, value in group.items() if key not in {
        'title', 'status', 'completed_at', 'completed_by', 'planning_receipt'}}
    from .prompting.core import policy_fingerprint
    return digest({'version': 1, 'source': source_identity(root), 'base': state.base_commit,
        'contract': state.contract_fingerprint, 'coverage': coverage, 'phase': phase,
        'policy': policy_fingerprint(), 'reviewer': digest([Path(__file__).read_text(),
                                                           Path(__file__).with_name('self_repair.py').read_text()]),
        'environment': runner._full_suite_environment_fingerprint(),
        'reviewer_policy': reviewer,
        'execution': getattr(runner, '_repair_control_binding', None),
        'invocation': getattr(runner, '_invocation_context', {}),
        'findings': [f.to_dict() for f in state.blocking_findings()],
        'next_action': getattr(runner, '_candidate_next_action', {}),
        'verification': getattr(runner, '_review_verification_binding', None)})


def reuse_review(runner, root, group, phase, identity=None):
    identity = review_identity(runner, root, group, phase) if identity is None else identity
    if not identity:
        return None
    memory = runner._experiment.component_memory.get(component_key(group), {})
    record = read_record(runner, memory.get('approved_review', {}))
    if not record or record.get('identity') != identity:
        return None
    payload = record.get('payload', {})
    if payload.get('decision') != 'APPROVE' or payload.get('findings') or payload.get('deferred_findings'):
        return None
    from .self_repair import _VerificationResult
    runner._candidate_review_completed = True
    callback = getattr(runner, '_control_phase_callback', None)
    if callback:
        callback('review_reused', {'candidate_id': runner._candidate_id, 'phase': phase,
                                  'receipt': record['id']})
    return _VerificationResult(True, 'candidate review=reused for identical coverage and evidence',
        payload={**payload, 'reused': True, 'review_identity': identity})


def remember_approval(runner, root, group, phase, payload, *, reviewed_identity):
    if payload.get('decision') != 'APPROVE' or payload.get('findings') or payload.get('deferred_findings'):
        return
    identity = review_identity(runner, root, group, phase)
    if not identity or identity != reviewed_identity:
        return
    reference = save_record(runner, 'approved_component_review', {'identity': identity, 'payload': payload})
    runner._experiment.component_memory.setdefault(component_key(group), {})['approved_review'] = reference
    runner._experiment_store.save(runner._experiment)


def related_reviews(runner, root, group):
    """Evidence for overlapping mechanisms, never approval of another component."""
    current = source_identity(root)
    environment = digest(runner._full_suite_environment_fingerprint())
    result = []
    for memory in runner._experiment.component_memory.values():
        record = read_record(runner, memory.get('code_review', {}))
        if (not record or record.get('source') != current or record.get('environment') != environment
                or record.get('contract') != runner._experiment.contract_fingerprint
                or record.get('result', {}).get('decision') != 'APPROVE'
                or record.get('result', {}).get('findings') or record.get('result', {}).get('deferred_findings')):
            continue
        previous = record.get('component', {})
        if previous.get('group_id') == group.get('group_id'):
            continue
        shared = set(group.get('contract_obligation_ids', [])) & set(previous.get('contract_obligation_ids', []))
        if shared:
            result.append({'component': previous.get('group_id'), 'shared_obligations': sorted(shared),
                'touched_paths': previous.get('touched_paths', []),
                'review_ref': str(runner._experiment_store.root / 'planning' / record['id'] / 'memory.json')})
    return result[-8:]
