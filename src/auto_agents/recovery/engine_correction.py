"""Retain an explicitly supplied engine correction without renewing model credit."""
from pathlib import Path
import subprocess

from .model import Event, digest, require


def retain(store, stream, selected, source):
    from ..run_lock import ProjectRunLock
    state = store.load(stream)
    with ProjectRunLock(Path(state['project'])):
        return _retain(store, stream, selected, Path(source).resolve())


def _retain(store, stream, selected, source):
    from ..repair_v2.audit import test_protection_findings
    from ..repair_v2.runtime_artifact import build, verify
    from ..repair_v2.workspace import git, source_identity, inventory
    from .scope_amendments import product_paths
    state = store.load(stream)
    task_id = next((i['task_id'] for i in state['incidents'].values()
                    if selected == i['incident_id']), selected)
    task = state['tasks'].get(task_id) or {}
    require(task.get('contract', {}).get('kind') == 'engine_repair'
            and task.get('status') in {'ready', 'blocked'}
            and task.get('phase') in {'implement', 'verify', 'review'}
            and not task.get('active_command') and task.get('candidate'),
            'reverification_owner', 'Select one idle retained engine repair')
    require(not any(c['status'] in {'reserved', 'running', 'unknown'} for c in state['commands'].values())
            and not any(c['status'] == 'reserved'
                        for c in state.get('recovery', {}).get('auxiliary', {}).values()),
            'outcome_unknown', 'Reconcile outstanding operations before changing a candidate')
    old = task['candidate']
    result = store.read(old['receipt'])
    prior = result['artifact']
    verify(prior)
    require(digest(prior) == old['candidate_id'] and prior['source'] == old['source'],
            'candidate_evidence', 'Original candidate custody differs from its artifact')
    require(not git(source, 'status', '--porcelain'), 'runtime_dirty', 'Commit the complete correction first')
    # A new unrelated checkout cannot replace the retained history or its tests.
    try:
        git(source, 'merge-base', '--is-ancestor', prior['commit'], 'HEAD')
    except subprocess.CalledProcessError:
        require(False, 'candidate_lineage', 'Correction must descend from the retained candidate commit')
    payload = store.read(task['contract']['issue_ref'])
    findings = test_protection_findings(source, payload['base'], source)
    require(not findings, 'tests_weakened', 'Correction weakens retained tests', findings=findings)
    identity = source_identity(source)
    before, after = inventory(prior['path']), inventory(source)
    paths = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if identity == prior['source']:
        paths = result.get('operator_correction', {}).get('paths') or []
    require(product_paths(paths), 'scope_review_required', 'Correction must preserve protected control files')
    working = store.root / 'kernel-engine' / task_id.removesuffix(':repair') / 'workspace/candidate'
    require(source_identity(working) in {prior['source'], identity}
            and git(working, 'rev-parse', 'HEAD') in {prior['commit'], git(source, 'rev-parse', 'HEAD')}
            and not git(working, 'status', '--porcelain'),
            'candidate_changed', 'Working candidate contains unretained changes')
    artifact = build(store.root, source, identity, prior['environments'])
    # Restore the producer workspace from committed ancestry, never from a
    # model completion fabricated for this human-supplied correction.
    git(working, 'fetch', '--quiet', str(source), artifact['commit'])
    git(working, 'merge', '--ff-only', artifact['commit'])
    require(source_identity(working) == identity, 'candidate_changed', 'Imported correction differs from its artifact')
    retained = {'artifact': artifact, 'operator_correction': {
        'parent': old['candidate_id'], 'original_contract': task['contract_id'],
        'request': 'explicit_reverification', 'test_protection': 'preserved', 'paths': paths},
        'progress_credit': False}
    if identity == prior['source']:
        require(result.get('operator_correction', {}).get('request') == 'explicit_reverification',
                'candidate_unchanged', 'No corrected source was supplied')
        reference = old['receipt']
    else:
        reference = store.put(retained)
    data = {'task_id': task_id, 'candidate_id': digest(artifact), 'source': identity,
            'base': old['base'], 'receipt': reference, 'parent': old['candidate_id']}
    if identity != prior['source']:
        store.apply(stream, state['revision'], Event('engine-correction:' + reference, 'candidate_retained', data))
    current = store.load(stream)
    store.apply(stream, current['revision'], Event('engine-reverify:' + reference,
        'candidate_reverification_requested', {'task_id': task_id, 'receipt': reference,
            'source': identity, 'evidence_ref': reference, 'paths': paths}))
    return {'ok': True, 'status': 'ready', 'phase': 'verify', 'source': identity,
            'task_id': task_id, 'progress_credit': False}
