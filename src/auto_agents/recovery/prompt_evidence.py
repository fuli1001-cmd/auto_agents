"""Bound model input while retaining complete, readable diagnostic evidence."""
import json
from pathlib import Path

from ..repair_v2.diagnostic_evidence import sanitized_json
from ..repair_v2.store import atomic_json
from .model import canonical, digest


INLINE_LIMIT = 16_000
READ_INSTRUCTION = (
    'Complete diagnostic evidence is mounted read-only at /repair-observations. '
    'Read the referenced files before deciding or editing; use targeted JSON queries '
    'and bounded excerpts instead of printing entire large reports. Inline summaries '
    'are navigation aids, not replacements for the sealed verification evidence.\n'
)


def result_summary(result):
    """Keep the failing recovery boundary visible beside passing suite counts."""
    summary = {key: result[key] for key in ('ok', 'error', 'reason', 'code', 'snapshot',
               'cancelled', 'infrastructure', 'missing_dependencies') if key in result}
    suite = result.get('suite', result)
    if isinstance(suite, dict) and isinstance(suite.get('checks'), list):
        checks = suite['checks']
        summary['suite'] = {key: suite[key] for key in
            ('ok', 'snapshot', 'cancelled', 'infrastructure') if key in suite}
        summary['suite'].update(checks=len(checks), failed_checks=sum(not row.get('ok') for row in checks))
        for name in ('collected', 'passed', 'failed', 'skipped', 'missing'):
            summary['suite'][name] = len({node for row in checks for node in row.get(name, [])})
    if result.get('failures'):
        from ..repair_v2.feedback import diagnose
        summary['diagnosis'] = diagnose(result['failures'])
    boundary = result.get('boundary')
    if isinstance(boundary, dict):
        summary['boundary'] = {key: boundary[key] for key in
            ('ok', 'snapshot', 'infrastructure', 'reason', 'error') if key in boundary}
        summary['boundary']['cases'] = []
        for case in boundary.get('cases', [boundary]):
            observed = case.get('observed') or {}
            recovery = observed.get('recovery_observation') or {}
            summary['boundary']['cases'].append({
                'ok': case.get('ok'),
                'error': observed.get('error') or case.get('reason') or case.get('error'),
                'route_consumed': observed.get('route_consumed'),
                'recovery_observation': {key: recovery[key] for key in (
                    'ok', 'boundary_kind', 'original_handoff_id', 'child_session_id',
                    'parent_session_id', 'preflight_outcome', 'preflight_rechecked',
                    'current_failure', 'parent_constraints_preserved', 'child_constraints_preserved',
                    'implementation_entered', 'verification_completed') if key in recovery},
            })
    return summary


class PromptEvidence:
    def __init__(self, root, driver):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        driver.set_prompt_evidence(self.root)

    def section(self, value, *, summary=None):
        """Publish lossless sanitized data; oversized sections become file references."""
        value = sanitized_json(value)
        identity = digest(value)
        atomic_json(self.root / (identity + '.json'), value)
        reference = {'file': '/repair-observations/' + identity + '.json', 'content_digest': identity}
        text = canonical(value)
        if len(text) <= INLINE_LIMIT:
            reference['data'] = value
        else:
            reference['characters'] = len(text)
            if summary is not None:
                # The summary itself may contain a large failure or test matrix.
                # Publish that intact too, rather than silently truncating it.
                reference['summary'] = self.section(summary)
        return reference

    def render(self, value, *, summary=None):
        return json.dumps(self.section(value, summary=summary), ensure_ascii=False)
