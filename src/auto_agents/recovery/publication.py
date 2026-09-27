"""Publication reuses the accepted revision and never opens a repair task."""
import json
import subprocess
from pathlib import Path

from .executor import Executor, FunctionExecutor
from .model import Command, Contract, Event, Evidence, Outcome, OutcomeKind, digest, require


def retry(store, state, selected):
    incidents = [i for i in state['incidents'].values() if i['status'] == 'resolved' and
        (selected == i['incident_id'] or selected in i.get('legacy_jobs', [i.get('legacy_job')]))]
    require(len(incidents) == 1, 'publication', 'Publication requires one resolved incident')
    incident = incidents[0]
    record = store.read(incident['record_ref'])
    approved = json.loads(record['result'])
    from ..repair_v2.integration import verify_receipt
    verify_receipt(approved, expected_root=Path(approved['v2_transaction']))
    config = json.loads((store.root/'operator.json').read_text())
    from ..repair_control import Repository, publication_policy
    repository = Repository(config)
    stream = state['workflow_id']
    key = 'publish:' + digest([incident['incident_id'], approved['commit']])[:40]
    task = Contract(state['goal_id'], key, 'engine_repair', incident['record_ref'], incident['record_ref'],
        (), ('accepted_revision_is_remote_ancestor',), (config['source_root'],), store.put({'operation':'retry-publish'}),
        'phase_completed', ('publish',))
    def emit(kind, data, identity):
        return store.apply(stream, store.load(stream)['revision'], Event(identity, kind, data))
    emit('task_bound', {'contract':task.to_dict()}, key + ':bind')
    command = Command(key, stream, key, 'publish', approved['runtime_artifact']['source'], task.identity,
        digest({'remote':config['remote'],'ref':config['ref']}), store.meta('active_runtime')['source'], key)
    prior = store.load(stream)['commands'].get(key)
    if prior is None: emit('command_reserved', command.to_dict(), key + ':reserve')
    def observed(request, push):
        revision, fresh = repository.fetch()
        require(fresh, 'publication_environment', 'Cannot observe the remote revision')
        def ancestor(left, right):
            return subprocess.run(['git','-C',str(repository.cache),'merge-base','--is-ancestor',left,right],
                                  capture_output=True).returncode == 0
        contained = ancestor(approved['commit'], revision)
        if not contained and not push: return None
        if not contained:
            require(ancestor(revision, approved['commit']), 'publication_diverged', 'Remote advanced independently; retain publication for reconciliation')
            publication_policy(config)
            repository.push(approved['commit'])
        proof = Evidence(store.put({'remote':config['remote'],'commit':approved['commit']}), key, request.source,
            request.contract, request.environment, digest('publication-observer-v1'),'publish','phase_completed')
        return Outcome(OutcomeKind.SUCCESS, 'Accepted revision published', (proof,))
    executor = Executor(store, {'publish':FunctionExecutor(lambda c: observed(c, True), lambda c: observed(c, False))})
    result = executor.reconcile(stream, key) if prior and prior['status'] in {'running','unknown'} else executor.execute(stream, key)
    return {'ok':result.kind == OutcomeKind.SUCCESS,'status':result.kind.value,'reason':result.reason,'command':key}
