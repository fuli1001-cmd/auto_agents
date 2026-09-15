"""Read legacy code/evidence without importing its execution state machine."""
import json
from pathlib import Path

from .store import digest
from .types import Acceptance, RepairBlocked, RepairRequest, ValidationUnit


def request_from_payload(payload, identity, *, history=None):
    invocation = payload.get('invocation', {})
    route = invocation.get('engine_route') or {}
    if route:
        from ..repair_contract import obligations
        requirements = obligations(route)
        goal = (route.get('issue_seed') or route.get('spec_seed') or {}).get('summary') or payload.get('error', '')
        commands = ()
    else:
        final = (payload.get('diagnosis') or {}).get('final') or {}
        requirements = final.get('expected_postconditions') or []
        goal = '\n'.join(final.get('causal_chain') or []) or payload.get('error', '')
        commands = tuple(final.get('verification_commands') or [])
    if not goal or not requirements:
        raise RepairBlocked('contract_missing', 'repair needs an authorized goal and explicit acceptance requirements')
    acceptance = tuple(Acceptance('requirement-' + digest(text)[:16], text, commands) for text in dict.fromkeys(requirements))
    evidence = []
    if history:
        # Completed flags, budgets and model-format bookkeeping are not authority.
        facts = [f for f in history.get('findings', {}).values()
                 if f.get('status') in ('confirmed', 'reopened') and not f.get('resolved_by')]
        evidence = [{key: f.get(key) for key in ('reason', 'counterexample', 'required_test', 'affected_paths')}
                    for f in facts]
    return RepairRequest(identity, payload['base'], goal, acceptance, payload.get('provider') or 'codex',
                         invocation, tuple(evidence))


def latest_legacy_job(control_root, job_id, payload):
    """Only stopped, same-session jobs may supply a private candidate."""
    import sqlite3
    root = Path(control_root)
    with sqlite3.connect('file:' + str(root / 'control.sqlite3') + '?mode=ro', uri=True) as database:
        rows = database.execute('select id,state,payload from jobs where id<>? order by updated desc', (job_id,)).fetchall()
    for previous_id, state, raw in rows:
        if state not in ('cancelled', 'blocked', 'failed'): continue
        previous = json.loads(raw)
        if (previous.get('project') != payload.get('project') or previous.get('fingerprint') != payload.get('fingerprint')
                or previous.get('invocation', {}).get('session_id') != payload.get('invocation', {}).get('session_id')):
            continue
        directory = root / 'jobs' / previous_id
        source = directory / 'continuous/repair'
        if not source.is_dir(): continue
        paths = list(directory.glob('working-evidence/**/experiment.json'))
        if not paths: continue
        history = json.loads(paths[0].read_text())
        return {'job': previous_id, 'source': str(source), 'history': history, 'experiment': str(paths[0])}
    return None


def acceptance_units(snapshot, request):
    """One centralized suite, with explicit standalone commands preserved."""
    root = Path(snapshot)
    tests = sorted((root / 'tests').glob('test_*.py'))
    if not tests: raise RepairBlocked('acceptance_missing', 'engine has no executable regression suite')
    # A test file is an execution shard, never a planning/approval group.
    units = [ValidationUnit('suite:' + str(path.relative_to(root)),
                'python -m pytest -q ' + str(path.relative_to(root)), profile='sandbox') for path in tests]
    mandatory = dict.fromkeys(command for item in request.acceptance for command in item.commands)
    units.extend(ValidationUnit('required:' + digest(command)[:20], command, profile='sandbox') for command in mandatory)
    return units
