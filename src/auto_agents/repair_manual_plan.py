"""Record an explicit manual plan revision as a draft requiring independent review."""
from copy import deepcopy
import json
import uuid

from .git_ops import head_ref
from .repair_control import atomic_json, digest
from .repair_feedback import sanitize_evidence
from .repair_memory import latest_revision, remember_revision
from .repair_work import memory, work_id
from .verification_ledger import source_identity

STAGE = 'self_repair_manual_plan'


def validate_revision(runner, workspace, document):
    from .repair_planning import validate_plan, PlanningBlocked, nonblocking_scope, _retained_scope
    state = runner._experiment
    group = next((g for g in state.finding_groups if g['group_id'] == document.get('group_id')), None)
    if (document.get('version') != 1 or not group
            or group.get('status') == 'completed'
            or document.get('contract_fingerprint') != state.contract_fingerprint
            or document.get('source_commit') != head_ref(workspace)
            or document.get('source') != source_identity(workspace)):
        raise PlanningBlocked('manual revision does not match retained source, component or contract')
    previous = latest_revision(runner, group)
    if not previous or digest(previous.get('draft')) != document.get('parent_plan_digest'):
        raise PlanningBlocked('manual revision has a stale or missing parent proposal')
    original = previous['draft']
    # Canonical groups retain historical observations too. Reuse their
    # authenticated exclusions only to assemble this *unapproved* proposal;
    # ordinary scope admission still runs before a writer may use it.
    group = deepcopy(group)
    findings = [f for f in state.findings.values() if f.status in {'confirmed', 'reopened'}
                and (f.finding_id in group.get('finding_ids', []) or f.repair_group_id == group['group_id'])]
    group['finding_ids'] = [f.finding_id for f in findings if not (
        nonblocking_scope(state, f) and _retained_scope(runner, state.scope_decisions[f.finding_id], f.to_dict()))]
    plan = validate_plan(document.get('plan'), group, set(state.contract_obligation_ids))
    if plan.get('touched_paths') != original.get('touched_paths'):
        raise PlanningBlocked('manual revision cannot expand or discard retained write scope')
    if plan['quick_checks'] != original['quick_checks']:
        raise PlanningBlocked('manual revision must preserve every original quick-check cohort')
    old = {s['scenario_id']: s for s in original['scenarios']}
    current = {s['scenario_id']: s for s in plan['scenarios']}
    if set(old) != set(current) or any(
            old[identity].get(key) != current[identity].get(key)
            for identity in old for key in ('kind', 'obligation_ids', 'check', 'quick_check', 'quick_required')):
        raise PlanningBlocked('manual revision changed retained scenarios, obligations or acceptance')
    if not isinstance(document.get('reason'), str) or not document['reason'].strip():
        raise PlanningBlocked('manual revision requires its concrete reason')
    if digest(plan) != digest(sanitize_evidence(plan)):
        raise PlanningBlocked('redacted executable proposal cannot be recorded as an exact manual revision')
    return group, previous, plan


def propose(runner, workspace, document):
    """Only plan memory changes; attempts, scope decisions and approvals remain intact."""
    from .repair_planning import POLICY_VERSION
    group, previous, plan = validate_revision(runner, workspace, document)
    state = runner._experiment
    identity = uuid.uuid4().hex
    directory = runner._experiment_store.root / 'planning' / identity
    directory.mkdir(parents=True, exist_ok=False)
    incoming = {'source': document['source'], 'source_commit': document['source_commit'],
        'contract_fingerprint': state.contract_fingerprint, 'component': deepcopy(group),
        'parent_revision': previous['id'], 'parent_plan_digest': document['parent_plan_digest'],
        'reason': document['reason'], 'basis': deepcopy(document.get('basis', {})), 'plan': plan}
    request = {'request_id': identity, 'stage': STAGE, 'policy': POLICY_VERSION,
               'source': document['source'], 'origin': 'explicit_manual_revision',
               'requires_independent_review': True, 'manual_input_digest': digest(incoming)}
    atomic_json(directory / 'input.json', incoming)
    atomic_json(directory / 'request.json', request)
    atomic_json(directory / 'result.json', plan)
    # Retain the exact pre-edit state in case an operator needs to undo this
    # draft selection. This does not reset or replace the candidate checkpoint.
    atomic_json(directory / 'previous-experiment.json', state.to_dict())
    retained = memory(runner, group)
    retained['manual_plan'] = {'request_id': identity, 'document_digest': digest(document)}
    if retained.get('completion'):
        retained.setdefault('completion_history', []).append(retained.pop('completion'))
    retained['completion_assessment'] = {'state': 'needs_revalidation',
                                       'reason': 'explicit plan revision requires new independent review and acceptance'}
    reference = remember_revision(runner, group, {
        'parent_revision': previous['id'], 'draft': plan, 'source': document['source'],
        'source_commit': document['source_commit'], 'environment': previous.get('environment'),
        'component': group, 'planner_request': identity, 'status': 'draft',
        'requires_independent_review': True, 'manual_reason': document['reason'],
        'manual_document_digest': digest(document)})
    return {'request_id': identity, 'revision': reference, 'status': 'draft',
            'requires_independent_review': True, 'candidate_modified': False}


def selected_revision(runner, group):
    """An explicit amendment supersedes older approvals without erasing them."""
    from .repair_planning import PlanningBlocked
    if not memory(runner, group).get('manual_plan'):
        return None
    previous = latest_revision(runner, group)
    if not previous:
        raise PlanningBlocked('selected manual revision is missing')
    if previous.get('manual_document_digest'):
        identity = previous.get('planner_request', '')
        import re
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
            raise PlanningBlocked('selected manual revision has an invalid request identity')
        path = runner._experiment_store.root / 'planning' / identity / 'request.json'
        try:
            if path.is_symlink():
                raise ValueError('symbolic request')
            request = json.loads(path.read_text())
            valid = (request.get('stage') == STAGE and request.get('request_id') == identity
                     and retained_proposal(runner, request, {'contract': runner._experiment.contract_fingerprint,
                                                           'component': group, 'plan': previous['draft']}))
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise PlanningBlocked('selected manual proposal provenance is missing or changed')
    return previous


def retained_proposal(runner, request, receipt):
    """Authenticate manual origin; the separate reviewer still supplies approval."""
    from .repair_planning import _format_semantics_changed
    directory = runner._experiment_store.root / 'planning' / request['request_id']
    path = directory / 'input.json'
    if path.is_symlink() or directory.is_symlink() or directory.parent.is_symlink():
        return False
    try:
        context = json.loads(path.read_text())
        return (request.get('origin') == 'explicit_manual_revision'
                and request.get('requires_independent_review') is True
                and request.get('manual_input_digest') == digest(context)
                and context.get('contract_fingerprint') == receipt.get('contract')
                and work_id(runner._experiment, context['component']) == work_id(runner._experiment, receipt['component'])
                and not _format_semantics_changed(context['plan'], receipt['plan'])
                and context['plan']['quick_checks'] == receipt['plan']['quick_checks'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
