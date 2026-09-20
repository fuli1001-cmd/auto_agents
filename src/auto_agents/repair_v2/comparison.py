"""Keep raw suite failures; attest only unchanged, reproducible baseline failures."""
from dataclasses import asdict
from pathlib import Path
import re
import shlex
import tempfile

from .store import digest
from .types import ValidationUnit
from .workspace import Workspace, source_identity

POLICY = 'no-new-failures-v1'


def signatures(report):
    if report.get('cancelled') or report.get('infrastructure'):
        return None
    result = {}
    for check in report.get('checks', []):
        if check.get('ok'):
            continue
        failed = set(check.get('failed', []))
        if (check.get('returncode') != 1 or check.get('source_unchanged') is not True
                or str(check.get('unit', '')).startswith('required:')
                or check.get('cancelled') or check.get('timed_out') or check.get('infrastructure')
                or check.get('skipped') or not failed
                or not failed <= set(check.get('call_failed', []))
                or set(check.get('missing', [])) - failed
                or not failed <= set(check.get('collected', []))):
            return None
        details = check.get('failure_details', [])
        for node in failed:
            rows = [r for r in details if r.get('nodeid') == node]
            # Setup/collection errors leave unobserved behavior, not proof of
            # an unchanged old failure. Do not normalize assertions or counts.
            if (len(rows) != 1 or rows[0].get('phase') != 'call' or not rows[0].get('message')
                    or len(rows[0]['message']) >= 2000):
                return None
            signature = (rows[0]['phase'], rows[0]['message'])
            if node in result and result[node] != signature:
                return None
            result[node] = signature
    return result


def unchanged(candidate, baseline):
    current, original = signatures(candidate), signatures(baseline)
    return bool(current and original is not None and
                all(original.get(node) == value for node, value in current.items()))


def matched(proof, validation):
    if (not proof or proof.get('policy') != POLICY or proof.get('snapshot') != validation.get('snapshot')
            or proof.get('validation_digest') != digest(validation)):
        return set()
    original = signatures(proof.get('baseline_report', {}))
    if original is None:
        return set()
    result = set()
    for check in validation.get('checks', []):
        current = signatures({'checks': [check]}) or {}
        result.update(node for node, value in current.items() if original.get(node) == value
                      and node in proof.get('unchanged_tests', []))
    return result


def relevant_failures(rows, old_nodes):
    result = []
    for row in rows:
        if not (row.get('failed') or row.get('missing')):
            result.append(row)
            continue
        kept = {field: [n for n in row.get(field, []) if n not in old_nodes] for field in ('failed', 'missing')}
        if kept['failed'] or kept['missing']:
            result.append({**row, **{k: v for k, v in kept.items() if k in row}})
    return result


def verify(comparison, validation, *, base=None):
    return bool(comparison and comparison.get('policy') == POLICY
                and comparison.get('ok') is True
                and not comparison.get('classification_incomplete') and not comparison.get('infrastructure')
                and comparison.get('snapshot') == validation.get('snapshot')
                and comparison.get('validation_digest') == digest(validation)
                and (base is None or comparison.get('base') == base)
                and unchanged(validation, comparison.get('baseline_report', {}))
                and set(signatures(validation) or {}) <= set(comparison.get('unchanged_tests', [])))


def accepted(store, receipt, base=None):
    validation = store.read(receipt['validation'])
    if validation.get('ok'):
        return True
    selected = receipt.get('comparison_base', base)
    if selected != base and selected not in receipt.get('integration_parents', []):
        return False
    reference = receipt.get('comparison')
    return bool(reference and verify(store.read(reference), validation, base=selected))


def compare(verifier, snapshot_id, snapshot, base_repository, base_commit, validation, cancel):
    raw = asdict(validation)
    proof = {'policy': POLICY, 'ok': False, 'snapshot': snapshot_id, 'base': base_commit,
             'runtime': verifier.runtime, 'validation_digest': digest(raw),
             'baseline_report': {}, 'unchanged_tests': [], 'classification_incomplete': False}
    failures = {}
    unknown = []
    for check in raw.get('checks', []):
        observed = signatures({'checks': [check]})
        if observed is None and not str(check.get('unit', '')).startswith('required:'):
            unknown.append(check)
        failures.update(observed or {})
    import subprocess
    eligible = []
    for node in failures:
        name = node.split('::')[0]
        if not re.fullmatch(r'tests/(?:[\w-]+/)*test_[\w-]+\.py', name):
            continue
        try:
            old = subprocess.run(['git', '-C', str(base_repository), 'show', base_commit + ':' + name],
                                 check=True, capture_output=True).stdout
        except subprocess.CalledProcessError:
            continue
        if (Path(snapshot) / name).read_bytes() != old:
            # An edited old test is not a comparable baseline. Required
            # behavior remains mandatory; do not silently label it an old bug.
            proof['classification_incomplete'] = True
            continue
        eligible.append(node)
    if not eligible and not unknown:
        return proof
    with tempfile.TemporaryDirectory(prefix='suite-baseline-', dir=verifier.root) as temporary:
        workspace = Workspace(Path(temporary) / 'base', base_repository, base_commit)
        baseline = workspace.prepare()
        nodes = sorted(eligible)
        units = [ValidationUnit('baseline:' + digest(node)[:20],
                    'python -m pytest -q ' + shlex.quote(node), (node,), fresh=True, profile='sandbox')
                 for node in nodes]
        from .types import ValidationResult
        result = (verifier.validate(source_identity(baseline), baseline, units, cancel, collect_all=True)
                  if units else ValidationResult(True, source_identity(baseline)))
        proof.update(baseline_report=asdict(result), unchanged_tests=nodes,
                     infrastructure=result.infrastructure, ok=unchanged(raw, asdict(result)))
        for check in unknown:
            # A baseline pass proves a candidate regression even for setup or
            # collection errors. A baseline failure does not prove equivalence.
            command = check.get('command', '')
            from .dependencies import pytest_parts
            if pytest_parts(command) is None:
                proof['classification_incomplete'] = True
                continue
            unit = ValidationUnit('baseline-unknown:' + digest(command)[:20], command,
                                  fresh=True, profile='sandbox')
            observed = verifier.validate(source_identity(baseline), baseline, [unit], cancel, collect_all=True)
            proof.setdefault('additional_checks', []).append(asdict(observed))
            proof['infrastructure'] |= observed.infrastructure
            if not observed.ok:
                proof['classification_incomplete'] = True
        if proof['classification_incomplete']:
            proof['ok'] = False
    return proof
