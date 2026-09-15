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
        requirements = (final.get('expected_postconditions') or
                        (payload.get('repair_case') or {}).get('expected_postconditions') or
                        (payload.get('contract') or {}).get('expected_postconditions') or [])
        goal = '\n'.join(final.get('causal_chain') or []) or payload.get('error', '')
        commands = tuple(final.get('verification_commands') or [])
    if not goal or not requirements:
        raise RepairBlocked('contract_missing', 'repair needs an authorized goal and explicit acceptance requirements')
    acceptance = tuple(Acceptance('requirement-' + digest(text)[:16], text, commands) for text in dict.fromkeys(requirements))
    evidence = [{'error': payload.get('error', ''), 'boundary': payload.get('boundary', {}),
                 'diagnosis': payload.get('diagnosis') or {},
                 'authorization': (payload.get('repair_case') or {}).get('authorization_policy', {})}]
    if history:
        # Completed flags, budgets and model-format bookkeeping are not authority.
        facts = [f for f in history.get('findings', {}).values()
                 if f.get('status') in ('confirmed', 'reopened') and not f.get('resolved_by')]
        evidence += [{key: f.get(key) for key in ('reason', 'counterexample', 'required_test', 'affected_paths')}
                    for f in facts]
    return RepairRequest(identity, payload['base'], goal, acceptance, payload.get('provider') or 'codex',
                         invocation, tuple(evidence))


def latest_legacy_job(control_root, job_id, payload, repository=None):
    """Only stopped, same-session jobs may supply a private candidate."""
    import sqlite3
    root = Path(control_root)
    with sqlite3.connect('file:' + str(root / 'control.sqlite3') + '?mode=ro', uri=True) as database:
        rows = database.execute('select id,state,payload from jobs where id<>? order by updated desc', (job_id,)).fetchall()
    for previous_id, state, raw in rows:
        if state not in ('cancelled', 'blocked', 'failed'): continue
        previous = json.loads(raw)
        from .transaction import intent
        if digest(intent(previous)) != digest(intent(payload)):
            continue
        directory = root / 'jobs' / previous_id
        source = directory / 'continuous/repair'
        if not source.is_dir() or source.is_symlink(): continue
        from ..repair_restart import _quiescent
        if not _quiescent(directory):
            raise RepairBlocked('legacy_busy', 'previous candidate still has an active process')
        from ..self_repair_search import SelfRepairExperimentStore
        invocation = payload.get('invocation', {})
        subject = invocation.get('run_id') or ('session-' + invocation['session_id'] if invocation.get('session_id') else '')
        if not subject: continue
        legacy = SelfRepairExperimentStore(directory / 'working-evidence', subject, previous['fingerprint'])
        if not legacy.path.is_file(): continue
        history = json.loads(legacy.path.read_text())
        result = {'job': previous_id, 'source': str(source), 'history': history, 'experiment': str(legacy.path)}
        if repository is not None:
            from ..repair_restart import _restart_snapshot
            snapshot, candidate = _restart_snapshot(directory, source, legacy, legacy.load(), repository)
            result.update(revision=snapshot[0], patch=snapshot[1], untracked=snapshot[2], candidate=candidate)
        return result
    return None


def acceptance_units(snapshot, request):
    """One centralized suite, with explicit standalone commands preserved."""
    root = Path(snapshot)
    from .workspace import inventory
    import shlex
    tests = [root / name for name in inventory(root) if name.startswith('tests/')
             and Path(name).name.startswith('test_') and name.endswith('.py')]
    if not tests: raise RepairBlocked('acceptance_missing', 'engine has no executable regression suite')
    # A test file is an execution shard, never a planning/approval group.
    units = [ValidationUnit('suite:' + str(path.relative_to(root)),
                'python -m pytest -q ' + shlex.quote(str(path.relative_to(root))), profile='sandbox') for path in tests]
    mandatory = dict.fromkeys(command for item in request.acceptance for command in item.commands)
    units.extend(ValidationUnit('required:' + digest(command)[:20], command, profile='sandbox') for command in mandatory)
    return units


def materialize_legacy(root, old, repository):
    """Import the latest committed or partial candidate, not an obsolete fallback HEAD."""
    import hashlib
    import shutil
    import subprocess
    from .workspace import git, inventory
    from .storage import require_space, tree_bytes
    destination = Path(root) / 'legacy-source'
    source = Path(old['source'])
    if destination.exists():
        if (Path(root) / 'state.json').exists():
            raise RepairBlocked('legacy_import_incomplete', 'active transaction lost its legacy import receipt')
        shutil.rmtree(destination)
    require_space(root, tree_bytes(source) * 2)
    subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks', str(source), str(destination)], check=True)
    revision = old.get('revision') or git(source, 'rev-parse', 'HEAD')
    git(destination, 'fetch', '--quiet', str(repository.cache), revision)
    git(destination, 'checkout', '--quiet', '--detach', revision)
    if old.get('patch'):
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(destination), 'apply', '--binary', '-'],
                       input=old['patch'], text=True, check=True)
    for name, expected in old.get('untracked', {}).items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
            raise RepairBlocked('legacy_path', 'unsafe legacy untracked path')
        original, target = source / name, destination / name
        if any(parent.is_symlink() for parent in original.parents if source in parent.parents):
            raise RepairBlocked('legacy_path', 'legacy source traverses a link')
        data = str(original.readlink()).encode() if original.is_symlink() else original.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RepairBlocked('legacy_changed', 'legacy untracked work changed during migration')
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink(): target.symlink_to(original.readlink())
        else: shutil.copy2(original, target)
    inventory(destination)
    git(destination, 'add', '-A')
    if git(destination, 'status', '--porcelain'):
        git(destination, 'commit', '-qm', 'Preserve legacy partial candidate')
    return {'job': old['job'], 'source': str(destination), 'experiment': old['experiment'],
            'revision': git(destination, 'rev-parse', 'HEAD'), 'candidate': old.get('candidate', ''),
            'history': {'findings': old['history'].get('findings', {})}}
