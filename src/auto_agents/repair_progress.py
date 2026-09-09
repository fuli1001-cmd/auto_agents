"""Historical achievement is independent of candidate selection and proof reuse."""
import re

from .repair_control import digest
from .repair_feedback import normalized_failure


def canonical_obligation(experiment, key):
    visited = set()
    while key not in visited:
        visited.add(key)
        parent = experiment.obligations.get(key, {}).get('parent_obligation_id')
        if not parent:
            break
        key = parent
    return key


def finding_identity(finding, experiment):
    obligation = canonical_obligation(experiment, finding.causal_obligation_id or finding.obligation_id)
    checks = re.findall(r'tests/[^\s`\"\x27,;]+\.py(?:::[\w\[\]./-]+)*', finding.required_test)
    return digest([obligation, sorted(checks) if checks else
                   normalized_failure(finding.counterexample or finding.required_test or finding.reason)])


def achievements(experiment, record):
    """Only completed verifier/reviewer results can issue achievement identities."""
    result = {'obligation:' + canonical_obligation(experiment, key) for key in record.passed_obligations if key.startswith('root:')}
    result.update("environment:" + key for key in record.prepared_dependencies)
    parent = experiment.candidates.get(record.parent_candidate_id)
    if parent:
        result.update('obligation:' + key for key in record.passed_obligations
                      if key.startswith('safety:') and key in parent.failed_obligations)
    result.update('verification:' + key for key in record.passed_obligations
                  if key == 'validation:full_suite')
    if record.review_completed:
        for key in record.resolved_finding_ids:
            finding = experiment.findings.get(key)
            if finding and finding.disposition == 'contract_violation':
                checks = re.findall(r'tests/[^\s`"\x27,;]+\.py(?:::[\w\[\]./-]+)*', finding.required_test)
                if checks and set(checks).issubset(record.verified_check_ids):
                    result.add('finding:' + finding_identity(finding, experiment))
    for group in experiment.finding_groups:
        if (record.status == 'candidate_group_completed' and group.get('group_id') == record.finding_group_id
                or group.get('group_id') in record.component_receipts):
            obligations = sorted({canonical_obligation(experiment, key) for key in group.get('contract_obligation_ids', [])})
            for command in group.get('focused_tests', []):
                # Split the stable, explicit check identities, not group labels.
                checks = re.findall(r'tests/[^\s`\"\x27,;]+\.py(?:::[\w\[\]./-]+)*', command)
                for check in checks or [normalized_failure(command)]:
                    result.add('check:' + digest([obligations, check]))
    return result


def remember_history(experiment):
    for record in experiment.candidates.values():
        if (record.candidate_id == 'base' or record.fatal
                or record.status not in {'candidate_group_completed', 'approved_candidate'}
                or any(key.startswith('candidate_regression:') for key in record.failed_obligations)):
            continue
        for identity in achievements(experiment, record):
            experiment.progress_credits.setdefault(identity, record.candidate_id)


def credit(experiment, record, *, regressions):
    new = sorted(achievements(experiment, record) - set(experiment.progress_credits))
    # A candidate cannot renew its search by trading a resolved issue for a regression.
    if regressions or record.fatal or record.infrastructure_failure:
        new = []
    for identity in new:
        experiment.progress_credits[identity] = record.candidate_id
    record.progress_keys = new
    return len(new) - len(regressions)
