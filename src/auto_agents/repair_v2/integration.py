"""Control-plane seam. Git custody/publication stay outside model processes."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

from .controller import Controller
from .docker import DockerVerifier
from .migration import acceptance_units, latest_legacy_job, request_from_payload
from .providers import AgentSandbox, NativeDriver
from .store import Store, atomic_json, digest
from .types import RepairBlocked
from .workspace import Workspace, git, source_identity


def repair(request, checkout, python, environment, revision):
    from ..config import load_project_config
    from ..repair_control import Repository, Store as ControlStore
    config, job = request['config'], request['job']
    payload = {**job['payload'], 'base': revision}
    directory = Path(config['root']) / 'jobs' / job['id'] / 'v2'
    old = latest_legacy_job(config['root'], job['id'], payload)
    accepted = request_from_payload(payload, job['id'], history=old['history'] if old else None)
    # Bind all migration inputs before any native provider sees them.
    directory.mkdir(parents=True, exist_ok=True)
    frozen = directory / 'request.json'
    if frozen.exists() and json.loads(frozen.read_text()) != accepted.to_dict():
        # JSON tuples normalize to arrays; compare canonical digests.
        if digest(json.loads(frozen.read_text())) != digest(accepted.to_dict()):
            raise RepairBlocked('request_changed', 'frozen V2 request differs from current input')
    atomic_json(frozen, accepted.to_dict())
    if old:
        atomic_json(directory / 'legacy-import.json', {k: old[k] for k in ('job', 'source', 'experiment')})
    public = ControlStore(config['root'])
    def event(kind, details):
        public.event(job['id'], kind, {**details, 'engine': 'v2', 'generation': job['generation']})
    store = Store(directory, event)
    workspace = Workspace(directory / 'workspace', checkout, revision, retained=old['source'] if old else None)
    configured = load_project_config(Path(payload['project']))
    verifier = DockerVerifier(Path(config['root']) / 'v2-verification', python=python,
                              callback=lambda kind, details: event(kind, details))
    verifier.prepare()
    sandbox = AgentSandbox(directory / 'provider-state', verifier.image)
    driver = NativeDriver(configured.providers[accepted.provider], sandbox,
                          effort=configured.efforts.get('self_repair', 'max'))
    controller = Controller(accepted, store, workspace, driver, verifier,
                            units=lambda root: acceptance_units(root, accepted))
    result = controller.run()
    if result['status'] != 'ready':
        return {'ok': False, 'status': 'v2_' + result['status'], 'engine': 'v2',
                'error': result.get('blocker', {}).get('message', 'repair is not yet accepted'),
                'v2_state': str(store.root / 'state.json')}
    snapshot = Path(result['snapshot_path'])
    commit = git(snapshot, 'rev-parse', 'HEAD')
    repository = Repository(config)
    repository.import_commit(str(snapshot), commit)
    runtime = repository.worktree(commit, job['id'] + '-v2-approved-' + commit[:12])
    if source_identity(runtime) != result['snapshot']:
        raise RepairBlocked('delivery_source_changed', 'materialized candidate differs from verified source')
    contract = {}
    if payload.get('invocation', {}).get('engine_route'):
        from ..repair_contract import EngineRequestContract
        reviewed = store.read(result['review'])
        descriptions = {a.identity: a.description for a in accepted.acceptance}
        checks = [{'obligation': descriptions[row['requirement']], 'nodeids': row['nodes'],
                   'reason': 'Independent V2 code review maps this behavior to executed tests.'}
                  for row in reviewed['coverage']]
        checks.sort(key=lambda row: [a.description for a in accepted.acceptance].index(row['obligation']))
        contract = EngineRequestContract(payload['invocation']['engine_route'], revision, checks).to_dict()
    return {'ok': True, 'status': 'repaired', 'engine': 'v2', 'commit': commit,
        'base': revision, 'runtime': str(runtime), 'python': python, 'environment': environment,
        'source_delivery_needed': True, 'request_contract': contract,
        'v2_receipt': {'job': job['id'], 'request': result['request_digest'], 'snapshot': result['snapshot'],
                       'reference': result['receipt'], 'store': str(store.root)},
        'proof': 'Unified V2 independent review and required validation passed.',
        'engine_full_proof': {'policy': 1, 'commit': commit, 'environment': environment, 'ok': True}}


def verify_receipt(approved):
    marker = approved.get('v2_receipt') or {}
    store = Store(marker['store'])
    receipt = store.read(marker['reference'])
    validation, review = store.read(receipt['validation']), store.read(receipt['review'])
    if (receipt['request'] != marker['request'] or receipt['snapshot'] != marker['snapshot']
            or not validation['ok'] or not review['ok'] or review['findings']
            or validation['snapshot'] != marker['snapshot'] or review['snapshot'] != marker['snapshot']
            or source_identity(Path(approved['runtime'])) != marker['snapshot']):
        raise RepairBlocked('invalid_acceptance', 'delivery needs valid evidence for the exact current snapshot')
    return receipt
