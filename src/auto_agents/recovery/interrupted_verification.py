"""Settle abandoned native candidate verification without claiming test success."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

from .model import Outcome, OutcomeKind
from .convergence import owner


def _read(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _identity(snapshot, command, subject):
    match = re.fullmatch(r'native:(\d+):[0-9a-f]+', command['owner'])
    if not match: return None
    pid = int(match[1])
    identity = command.get('dispatch_identity')
    if identity:
        if identity.get('pid') == pid and identity.get('ticks') not in {None, '', 'unknown'} and identity.get('boot') not in {None, '', 'unknown'}:
            return identity
        return None
    # Legacy commands did not retain dispatch identity. Their health record
    # binds the same project, subject, PID and start ticks to verification.
    health = _read(Path(snapshot['project']) / '.auto-agents/state/health-watch-control.json')
    if (health.get('project') != snapshot['project'] or health.get('subject_id') != subject
            or health.get('owner_pid') != pid or not health.get('owner_start_ticks')
            or (health.get('active_operation') or {}).get('kind') != 'verification'):
        return None
    try: boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError: return None
    return {'pid': pid, 'ticks': str(health['owner_start_ticks']), 'boot': boot}


def prepare(store, stream, command_id):
    """Read and validate custody first; return an interruption, never pass credit."""
    from ..artifact_store import alive
    from .authority import unpack
    from ..models import SessionState
    from ..session_candidate import validate_receipt
    from ..session_source import validate_checkout
    from .native import _source
    snapshot = store.load(stream)
    command = snapshot['commands'][command_id]
    if command['status'] not in {'running', 'unknown'} or command['phase'] != 'verify' or command['model_call']:
        return None
    owner_id = owner(snapshot, command['task_id'])
    parent = snapshot['tasks'][owner_id]
    if parent['contract']['kind'] != 'fix': return None
    if not owner_id.startswith('fix:'): return None
    subject = owner_id.removeprefix('fix:')
    identity = _identity(snapshot, command, subject)
    if not identity or alive(identity): return None
    # Prefer an actual saved outcome; its evidence must be reconciled normally.
    with store.connect() as db:
        if db.execute('SELECT 1 FROM kernel_results WHERE command_id=?', (command_id,)).fetchone(): return None
    projection = snapshot['projections'].get('session:' + subject)
    if not projection: return None
    state = SessionState.from_dict(unpack(store, store.read(projection['blob'])))
    custody = state.candidate_custody
    if (state.mode != 'fix' or state.session_id != subject or custody.get('repository') != snapshot['project']
            or not custody.get('receipt') or not custody.get('checkout')):
        return None
    validate_checkout(Path(snapshot['project']), state, Path(custody['checkout']))
    validate_receipt(state)
    source = _source(SimpleNamespace(project_root=Path(custody['checkout']), _recovery_policy_active='recovery' in snapshot), state)
    if source != command['source']: return None
    return {'version': 1, 'command_id': command_id, 'owner': command['owner'], 'identity': identity, 'subject': subject,
            'source': source, 'projection': projection['blob'], 'candidate': custody['receipt']['fingerprint'],
            'conclusion': 'interrupted_without_result', 'progress_credit': False}


def reconcile(store, stream):
    """Caller holds the project run lock. No model or verification is redispatched."""
    from .executor import Executor, FunctionExecutor
    settled = []
    for command in list(store.load(stream)['commands'].values()):
        if command['phase'] != 'verify' or command['model_call'] or command['status'] not in {'running', 'unknown'}:
            continue
        with store.connect() as db:
            saved = db.execute('SELECT 1 FROM kernel_results WHERE command_id=?', (command['command_id'],)).fetchone()
        if command['status'] in {'running', 'unknown'} and saved:
            Executor(store, {}).reconcile(stream, command['command_id'])
            continue
        proof = prepare(store, stream, command['command_id'])
        if proof is None: continue
        # Recheck owner liveness immediately before publishing the receipt.
        from ..artifact_store import alive
        if alive(proof['identity']): continue
        reason = 'Verification owner exited without a final result; candidate custody is unchanged'
        result = {'ok': False, 'retry_fix': False, 'reason': reason, 'error_code': 'verification_interrupted',
                  'interrupted_verification': True, 'diagnostic': proof}
        outcome = Outcome(OutcomeKind.ENVIRONMENT_BLOCKED, reason, details={
            'native_result': store.put(result), 'subject': proof['subject'],
            'post_source': proof['source'], 'interrupted_verification': store.put(proof)})
        executor = Executor(store, {'verify': FunctionExecutor(lambda c: None, lambda c, value=outcome: value)})
        executor.reconcile(stream, command['command_id'])
        settled.append(command['command_id'])
    return settled


def reconcile_installation(store):
    """Fence each project before examining dead owners during public bootstrap."""
    from ..run_lock import ProjectRunLock, RunAlreadyActiveError
    with store.connect() as db:
        streams = [row['id'] for row in db.execute('SELECT id FROM kernel_streams')]
    settled = []
    for stream in streams:
        snapshot = store.load(stream)
        if not any(c['phase'] == 'verify' and not c['model_call'] and c['status'] in {'running', 'unknown'}
                   for c in snapshot['commands'].values()): continue
        try:
            with ProjectRunLock(Path(snapshot['project'])):
                settled.extend(reconcile(store, stream))
        except RunAlreadyActiveError:
            continue
    return settled
