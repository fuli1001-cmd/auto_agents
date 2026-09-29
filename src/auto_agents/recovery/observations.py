"""Executor observations, distinct from a model's claims and final acceptance."""
from .model import digest, require

STATUSES = {'passed', 'failed', 'skipped', 'unexecuted', 'blocked'}


def compact(value):
    """Keep complete output in a blob, not in every subsequent journal snapshot."""
    if value.get('details_ref'): return value
    return {**value, 'details_ref': digest(value), 'checks': {
        key: {'id': key, 'status': row['status'],
              **({'detail': row.get('detail', '')[:1200]} if row['status'] == 'failed' else {})}
        for key, row in value['checks'].items()}}


def retained_progress_checks(session, state, commands):
    """Only frozen selectors may earn credit; new candidate tests cannot mint it."""
    from ..verification_context import current_context
    from ..pytest_selection import selected_nodes
    from ..session_verification import _mandatory_refs
    from ..execution_binding import RunnerContextError
    original = set(state.baseline_commands)
    if state.fix_verify_command:
        original.add(session._fix_verify_command_for_execution(state.fix_verify_command))
    allowed = {'command:' + digest(command) for command in commands if command in original}
    if not state.verification_binding: return sorted(allowed)
    for command in commands:
        try:
            for invocation in current_context(session, state).invocations(command):
                if invocation.runner == 'pytest' and invocation.repository_targets:
                    allowed.update(selected_nodes(session, state, invocation,
                                   expected_missing=_mandatory_refs(state))[0])
            allowed.update(ref for ref in _mandatory_refs(state) if '::' in ref)
        except (RunnerContextError, OSError, ValueError):
            # Missing provenance grants no progress, but does not discard the
            # actual verification failure or bypass its ordinary admission.
            continue
    return sorted(allowed)


def gate_checks(gate):
    """Use trusted phase receipts where available; otherwise keep command granularity."""
    from ..gates import extract_failure_info
    from ..diagnostic_output import redact
    rows = []
    for result in gate.commands:
        command = result.command
        blocked = bool(result.infrastructure_error or result.termination_reason or result.cleanup_incomplete)
        nodes = getattr(result, 'test_results', {})
        if nodes:
            for node, value in sorted(nodes.items()):
                phases = value.get('phases', {})
                status = ('blocked' if blocked else 'failed' if 'failed' in phases.values()
                          else 'skipped' if 'skipped' in phases.values()
                          else 'passed' if all(phases.get(p) == 'passed' for p in ('setup', 'call', 'teardown'))
                          else 'unexecuted')
                rows.append({'id': node, 'status': status, 'command': command,
                             'evidence': dict(result.artifacts), 'detail': redact(value.get('detail', ''))[:4000]})
        else:
            # Older signed certificates retain exact passed nodes even when
            # they predate the richer phase map. Preserve those proofs too.
            rows.extend({'id': node, 'status': 'blocked' if blocked else 'passed', 'command': command,
                         'evidence': dict(result.artifacts), 'detail': ''}
                        for node in result.executed_tests)
            # A failing command may expose stable test failures, but its other
            # tests are not implicitly passed. No parsing of a success summary.
            failures = extract_failure_info(type(gate)(False, [result]))
            rows.extend({'id': node, 'status': 'failed', 'command': command,
                         'evidence': dict(result.artifacts),
                         'detail': redact(result.stdout[-3000:] or result.stderr[-3000:])}
                        for node in failures.failure_ids if failures.comparable and not blocked)
        rows.append({'id': 'command:' + digest(command),
                     'status': 'blocked' if blocked else 'passed' if result.ok else 'failed',
                     'command': command, 'evidence': dict(result.artifacts),
                     'detail': '' if result.ok else redact(result.stdout[-3000:] or result.stderr[-3000:])})
    return rows


def observation(command, result, *, verifier):
    rows = result.get('verification_checks', [])
    units = result.get('checks') or (result.get('suite') or {}).get('checks')
    # Engine validation units carry their own immutable check identities.
    if not rows and isinstance(units, list):
        rows = [{'id': str(r.get('unit') or r.get('identity') or r.get('command')),
                 'status': 'passed' if r.get('ok') else 'blocked' if r.get('infrastructure') else 'failed',
                 'command': r.get('command', ''), 'detail': r.get('excerpt', ''), 'evidence': {}}
                for r in units if r.get('unit') or r.get('identity') or r.get('command')]
        if result.get('boundary'):
            rows.append({'id': 'original-boundary', 'status': 'passed' if result['boundary']['ok'] else 'failed',
                         'command': '', 'detail': result['boundary'].get('reason', ''), 'evidence': {}})
    checks = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id']
                and row.get('status') in STATUSES, 'verification_observation', 'Invalid check observation')
        key = row['id']
        prior = checks.get(key)
        # Repeated checks must agree; a flaky pass cannot close an obligation.
        checks[key] = {**row, 'status': 'blocked' if prior and prior['status'] != row['status'] else row['status']}
    return {'version': 1, 'command_id': command.command_id, 'task_id': command.task_id,
            'source': command.source, 'contract': command.contract, 'environment': command.environment,
            'verifier': verifier, 'checks': checks, 'baseline_failures': result.get('baseline_failures', []),
            'regressions': result.get('regression_ids', []),
            'complete': bool(result.get('ok')), 'scope': result.get('scope', 'verify'),
            'manifest': result.get('verification_manifest') or command.contract,
            'progress_checks': result.get('progress_checks', list(checks)),
            'reason': result.get('reason', ''), 'result_identity': result.get('execution_identity', '')}


def validate(value, command):
    require(value.get('version') == 1 and all(value.get(key) == command[key]
            for key in ('command_id', 'task_id', 'source', 'contract', 'environment')),
            'verification_observation', 'Observation belongs to different execution inputs')
    require(isinstance(value.get('checks'), dict) and type(value.get('complete')) is bool
            and value.get('manifest') and value.get('verifier'),
            'verification_observation', 'Observation has no verification identity')
    require(all(key == row.get('id') and row.get('status') in STATUSES
                for key, row in value['checks'].items()), 'verification_observation', 'Invalid check result')


def diagnosis_schema(failures, observation_id):
    return {'type': 'object', 'additionalProperties': False,
            'properties': {
                'observation': {'type': 'string', 'enum': [observation_id]},
                'hypothesis': {'type': 'string', 'minLength': 1},
                'failure_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                'items': {'type': 'string', 'enum': failures}},
                'paths': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                          'items': {'type': 'string', 'minLength': 1}},
                'expected_result': {'type': 'string', 'minLength': 1}},
            'required': ['observation', 'hypothesis', 'failure_ids', 'paths', 'expected_result']}
