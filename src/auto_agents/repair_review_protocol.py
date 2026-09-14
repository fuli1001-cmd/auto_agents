"""One independent review supplies necessity, counterexamples and correction scope."""
from copy import deepcopy

from .repair_control import digest
from .repair_memory import read_record, save_record
from .verification_ledger import source_identity

INSTRUCTION = (
    'For each finding independently classify necessity in scope:{verdict:required|not_applicable|follow_up|unknown,'
    'obligation_id,trigger,consequence,support_basis,reason,evidence:[...],disproof}. '
    'A required finding needs a supported trigger and consequence under the original contract. '
    'A safety violation or introduced regression cannot be deferred; excluding it requires concrete disproof. '
    'This is the independent necessity decision, not a request for a second agent to repeat it. '
    'Provide controls:{negative:{command,scenario_ids:[...],purpose},positive:{command,scenario_ids:[...],purpose}} '
    'when executable checks are possible. Commands must be exact python -m pytest node invocations; '
    'the negative reproduces the defect and the positive preserves valid behavior of the same mechanism. '
    'Use existing scenarios when covered; a new scenario or test path requires plan_gap. '
    'If a runnable counterexample is unavailable, supply concrete manual evidence and explain why; '
    'an import/setup/permission failure is not a behavioral counterexample. '
    'Inspect related positive and inverse cases now so a local correction does not break them.'
)


def admit_scope(runner, workspace, group, findings, *, review_input, reviewed_source, reviewed_environment):
    """Admit only explicit, complete independent decisions on the frozen input.

    Older provider replies remain supported through the ordinary scope reviewer.
    This function never infers nonblocking scope from labels or changed paths.
    """
    from .git_ops import head_ref
    from .repair_planning import _validate_scope, record_scope_decisions, PlanningBlocked
    original = read_record(runner, review_input)
    if (not original or original.get('kind') != 'code_review_input'
            or original.get('source') != reviewed_source or original.get('environment') != reviewed_environment
            or original.get('contract_fingerprint') != runner._experiment.contract_fingerprint):
        return []
    if (source_identity(workspace) != reviewed_source
            or digest(runner._full_suite_environment_fingerprint()) != reviewed_environment):
        return []
    admitted, decisions = [], []
    for finding in findings:
        scope = finding.get('scope')
        if not isinstance(scope, dict):
            continue
        if scope.get('finding_id', finding['finding_id']) != finding['finding_id']:
            continue
        row = {**deepcopy(scope), 'finding_id': finding['finding_id']}
        try:
            _validate_scope({'decisions': [row]}, [finding], runner._experiment.contract_obligation_ids)
        except PlanningBlocked:
            continue
        if row['verdict'] == 'unknown':
            continue  # Diagnosis remains required; no waiver is created.
        admitted.append(finding)
        decisions.append(row)
    if not admitted:
        return []
    context = {'source': reviewed_source, 'source_commit': head_ref(workspace),
               'environment': reviewed_environment, 'findings': admitted, 'component': group}
    reference = save_record(runner, 'code_review_scope', {
        **context, 'contract': runner._experiment.contract_fingerprint,
        'engine_base': runner._experiment.base_commit, 'review_input': review_input, 'decisions': decisions,
    })
    records = record_scope_decisions(runner, workspace, context, reference['id'], decisions, persist=False)
    for value in records.values():
        value['code_review_scope'] = reference
    runner._experiment_store.save(runner._experiment)
    return list(records)


def retained_scope(runner, receipt, finding):
    from .repair_planning import finding_key, _validate_scope, PlanningBlocked
    record = read_record(runner, receipt.get('code_review_scope', {}))
    if (not record or record.get('kind') != 'code_review_scope'
            or record.get('id') != receipt.get('request_id')
            or record.get('contract') != runner._experiment.contract_fingerprint
            or record.get('engine_base') != receipt.get('engine_base')):
        return False
    original = read_record(runner, record.get('review_input', {}))
    if (not original or original.get('kind') != 'code_review_input'
            or original.get('source') != record.get('source')
            or original.get('environment') != record.get('environment')
            or original.get('contract_fingerprint') != record.get('contract')):
        return False
    try:
        matching = [row for row in record['findings'] if finding_key(row) == finding_key(finding)]
        decisions = [row for row in record['decisions'] if row.get('finding_id') == finding['finding_id']]
        if len(matching) != 1 or len(decisions) != 1:
            return False
        _validate_scope({'decisions': decisions}, [finding], runner._experiment.contract_obligation_ids)
        return (all(record.get(key) == receipt.get(key) for key in ('source', 'source_commit', 'environment'))
                and all(decisions[0].get(key) == receipt.get(key) for key in (
                    'verdict', 'obligation_id', 'trigger', 'consequence', 'support_basis', 'evidence', 'reason', 'disproof')))
    except (KeyError, TypeError, ValueError, PlanningBlocked):
        return False


def normalized_controls(finding, group):
    from .repair_planning import _test_command, _texts, _text
    scenarios = {row['scenario_id']: row for row in group.get('scenarios', [])}
    controls = finding.get('controls')
    if not isinstance(controls, dict):
        return None
    result = {}
    for key in ('negative', 'positive'):
        row = controls.get(key)
        if (not isinstance(row, dict) or not _test_command(row.get('command'))
                or not _text(row.get('purpose')) or not _texts(row.get('scenario_ids'))
                or not set(row['scenario_ids']).issubset(scenarios)
                or not all(finding.get('causal_obligation_id') in scenarios[s]['obligation_ids'] for s in row['scenario_ids'])):
            return None
        if key == 'positive' and not any(scenarios[s]['kind'] == 'compatibility' for s in row['scenario_ids']):
            return None
        result[key] = deepcopy(row)
    return result


def remember_controls(runner, group, payload, reference):
    from .repair_planning import finding_key
    from .repair_work import memory
    values = memory(runner, group)
    known = values.setdefault('review_controls', {})
    inventory = values.setdefault('acceptance_inventory', [])
    for finding in payload.get('findings', []):
        controls = normalized_controls(finding, group)
        if controls:
            known[finding['finding_id']] = {'finding_key': finding_key(finding), 'controls': controls,
                                           'review': reference}
            for row in controls.values():
                if row['command'] not in inventory:
                    inventory.append(row['command'])


def controls_for(experiment, group):
    from .repair_planning import finding_key
    from .repair_work import stored_memory
    known = stored_memory(experiment, group).get('review_controls', {})
    result = []
    for identity in group.get('finding_ids', []):
        finding = getattr(experiment, 'findings', {}).get(identity)
        row = known.get(identity, {})
        if finding and finding.status in {'confirmed', 'reopened'} and row.get('finding_key') == finding_key(finding):
            result.append({'finding_id': identity, **row})
    return result
