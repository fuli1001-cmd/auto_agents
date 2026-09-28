"""Bind an accepted engine patch to independently adopted runtime content."""
from pathlib import Path

from .model import digest, require
from .runtime_source import inventory


def _key(stream, incident):
    return 'engine_adoption:' + digest([stream, incident['incident_id']])


def record(store, stream, incident, base, candidate, runtime):
    require(store.meta('active_runtime') == runtime, 'adoption_required',
            'Engine delivery has not been independently adopted')
    activation = store.meta('activation_receipt')
    require(activation in store.meta('verified_upgrades', []), 'adoption_required',
            'Engine delivery has no sealed adoption receipt')
    receipt = store.read(activation)
    require(receipt.get('source') == runtime['source'] and receipt.get('runtime') == runtime['artifact_id'],
            'adoption_required', 'Engine delivery adoption receipt belongs to another runtime')
    before, after = inventory(base), inventory(candidate['path'])
    require(candidate['source'] == incident['required_runtime'] == digest(after),
            'engine_evidence', 'Delivered candidate differs from the resolved engine repair')
    changes = {name: after.get(name) for name in before.keys() | after.keys()
               if before.get(name) != after.get(name)}
    actual = inventory(runtime['path'])
    require(digest(actual) == runtime['source'] and all(actual.get(name) == row for name, row in changes.items()),
            'adoption_required', 'Adopted runtime does not contain the accepted engine patch')
    reference = store.put({'stream': stream, 'incident': incident['incident_id'],
        'resolution': incident['resolution_ref'], 'candidate': candidate['source'],
        'source': runtime['source'], 'runtime': runtime['artifact_id'],
        'activation': activation, 'changes': changes})
    store.set_meta(_key(stream, incident), reference)


def adopted(store, stream, incident, runtime=None):
    runtime = runtime or store.meta('active_runtime') or {}
    required = incident.get('required_runtime')
    if not required or required == runtime.get('source'): return True
    reference = store.meta(_key(stream, incident))
    if not reference: return False
    proof = store.read(reference)
    if (proof.get('stream') != stream or proof.get('incident') != incident['incident_id']
            or proof.get('resolution') != incident.get('resolution_ref') or proof.get('candidate') != required
            or proof.get('activation') not in store.meta('verified_upgrades', [])):
        return False
    receipt = store.read(proof['activation'])
    if receipt.get('source') != proof['source'] or receipt.get('runtime') != proof['runtime']: return False
    if proof['source'] == runtime.get('source'): return True
    # Later independently adopted versions may keep the patch while changing
    # unrelated files. Keep the compact patch evidence after snapshot cleanup.
    activation = store.meta('activation_receipt')
    if activation not in store.meta('verified_upgrades', []) or not runtime.get('path'): return False
    receipt = store.read(activation)
    if receipt.get('source') != runtime.get('source') or receipt.get('runtime') != runtime.get('artifact_id'): return False
    actual = inventory(runtime['path'])
    return (digest(actual) == runtime['source']
            and all(actual.get(name) == row for name, row in proof['changes'].items()))


def retained_payload(store, project, orchestrator, error):
    """Select only the sealed completed repair belonging to this cold route."""
    stream, identifier = error.resume_incident
    state = store.load(stream)
    invocation = getattr(orchestrator, '_invocation_context', {}) or {}
    subject = getattr(orchestrator, '_kernel_subject', None)
    if not subject and invocation.get('session_id'): subject = 'session:' + invocation['session_id']
    if not subject and invocation.get('run_id'): subject = 'run:' + invocation['run_id']
    incident = state['incidents'].get(identifier, {})
    from ..repair_control import digest as route_digest
    require(state['project'] == str(Path(project).resolve()) and subject
            and store.binding(project, subject) == stream
            and incident.get('status') == 'resolved' and incident.get('payload_ref')
            and incident.get('route_digest') == route_digest(error.route_payload),
            'engine_evidence', 'Pending engine adoption belongs to another route or workflow')
    task = state['tasks'].get(incident['task_id'], {})
    require(task.get('status') == 'completed' and task.get('contract', {}).get('kind') == 'engine_repair',
            'engine_evidence', 'Pending engine adoption has no completed repair')
    return store.read(incident['payload_ref'])
