"""Evidence-based repair feedback; symptom groups never waive acceptance checks."""
import re
import shlex

from .store import digest
from .types import ValidationUnit
from .dependencies import pytest_parts


def failure_keys(rows):
    keys = set()
    for row in rows:
        if row.get('failed'):
            keys.update(('test', node) for node in row['failed'])
        elif row.get('missing'):
            keys.update(('missing', node) for node in row['missing'])
        elif row.get('requirement'):
            keys.add(('review', row['requirement'], row.get('check', '')))
        else:
            keys.add(('unit', row.get('unit', row.get('command', row.get('reason', 'unknown')))))
    return keys


def covers(passed, node):
    return any(p == node or p.startswith(node + '[') or p.startswith(node + '::') for p in passed)


def assess_progress(state, failures, identity, *, passed_tests=(), review=None):
    current = failure_keys(failures)
    best = {tuple(item) for item in state.get('best_failure_keys', [])}
    resolved = {tuple(item) for item in state.get('resolved_failure_keys', [])}
    passed = set(passed_tests)
    failed = {key[1] for key in current if key[0] in ('test', 'missing')}
    passed.difference_update(failed)
    verified = {key for key in best if key[0] in ('test', 'missing') and key[1] in passed}
    # A completed independent approval earns credit only when its required
    # behavioral checks actually passed on this same candidate. Cancellation,
    # disappearance from a partial run, and changed prose are not resolution.
    if review and review.ok and not review.findings and review.snapshot == identity:
        approved = {row['requirement'] for row in review.coverage
                    if row.get('nodes') and all(covers(passed, n) and not covers(failed, n) for n in row['nodes'])}
        outstanding = {key[1] for key in current if key[0] == 'review'}
        verified.update(('review', key[1]) for key in best
                        if key[0] == 'review' and key[1] in approved - outstanding)
    fresh = verified - resolved
    progressed = bool(best and current < best) or bool(fresh)
    return {'progressed': progressed, 'best_failure_keys': sorted(current if not best or progressed else best),
            'resolved_failure_keys': sorted(resolved | verified), 'newly_verified': sorted(fresh)}


def _symptom(reason):
    errors = re.findall(r'^E\s+(.+)', reason, flags=re.MULTILINE)
    message = errors[0] if errors else reason.splitlines()[0] if reason else 'failure without structured detail'
    if message.startswith('AssertionError'):
        assertions = re.findall(r'^>\s+(assert .+)', reason, flags=re.MULTILINE)
        if assertions:
            message = 'AssertionError: ' + assertions[-1]
    message = re.sub(r'0x[0-9a-fA-F]+|\b[0-9a-f]{32,64}\b', '<identity>', message)
    message = re.sub(r'/tmp/[^\s\'\"]+', '<temporary-path>', message)
    return message[:1000]


def diagnose(failures):
    """Group observations, not assumed root causes; retain every failing node."""
    groups = {}
    for index, row in enumerate(failures):
        nodes = row.get('failed') or row.get('missing') or []
        symptoms = row.get('failure_details') or []
        details = {item.get('nodeid'): item for item in symptoms if isinstance(item, dict)}
        for node in nodes or ['']:
            detail = details.get(node, {})
            symptom = _symptom(detail.get('message') or row.get('reason', ''))
            kind = ('infrastructure' if row.get('infrastructure') else 'review' if row.get('requirement')
                    else 'missing' if row.get('missing') and not row.get('failed') else 'test' if node else 'boundary')
            owner = row.get('requirement') or (node.split('::', 1)[0] if node else row.get('unit', 'unknown'))
            key = (kind, owner, detail.get('phase', ''), symptom)
            group = groups.setdefault(key, {'kind': kind, 'owner': owner, 'symptom': symptom,
                'phase': detail.get('phase', 'unknown'),
                'nodes': [], 'failure_indices': [], 'evidence': row.get('reason', '')[:1800],
                'hypothesis_only': True})
            if node and node not in group['nodes']: group['nodes'].append(node)
            if index not in group['failure_indices']: group['failure_indices'].append(index)
    result = list(groups.values())
    for group in result:
        group['representatives'] = group['nodes'][:1]
    return {'version': 1, 'failed_nodes': len({n for g in result for n in g['nodes']}), 'groups': result}


def diagnostic_units(failures, units, *, limit=8):
    """Select bounded representatives from the current mandatory collection."""
    available = {node: unit for unit in units for node in unit.expected_nodes}
    selected = []
    for group in diagnose(failures)['groups']:
        matches = [n for n in group['nodes'] if n in available]
        if not matches and group['kind'] == 'review':
            text = '\n'.join(str(failures[i].get(k, '')) for i in group['failure_indices']
                             for k in ('reason', 'counterexample', 'check'))
            matches = [n for n in available if n in text or n.split('::')[-1].split('[', 1)[0] in text]
        if matches and matches[0] not in selected: selected.append(matches[0])
        if len(selected) >= limit: break
    result = []
    for node in selected:
        path = node.split('::', 1)[0]
        if not path.startswith('tests/') or '..' in path.split('/') or not path.endswith('.py'):
            continue
        # Do not turn a custom environment, plugin or selector invocation into
        # a different command. Those obligations keep their full original run.
        parts = pytest_parts(available[node].command)
        flags = ('-q', '-qq', '-v', '-vv', '-x', '--disable-warnings')
        if parts is None or any(p.startswith('-') and p not in flags for p in parts):
            continue
        result.append(ValidationUnit('diagnostic:' + digest(node)[:16],
            shlex.join(['python', '-m', 'pytest', '-q', node]), expected_nodes=(node,),
            fresh=True, profile=available[node].profile))
    return result


def retain_unchecked(previous, observed, passed):
    """A representative probe cannot silently erase unexecuted obligations."""
    covered = set(passed) | {n for row in observed for field in ('failed', 'missing') for n in row.get(field, [])}
    remaining = []
    for row in previous:
        field = 'failed' if row.get('failed') else 'missing' if row.get('missing') else None
        if field:
            nodes = [n for n in row[field] if n not in covered]
            if nodes: remaining.append({**row, field: nodes})
        else:
            remaining.append(row)
    return [*observed, *remaining]
