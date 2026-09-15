"""Fixed controller entry, centralized acceptance, delivery and workflow handoff."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading

from .controller import Controller
from .docker import DockerVerifier
from .migration import acceptance_units, latest_legacy_job, request_from_payload
from .providers import AgentSandbox, NativeDriver
from .store import Store, atomic_json, digest
from .transaction import bind_controller, frozen_request, transaction_lock, transaction_root
from .types import RepairBlocked
from .workspace import Workspace, git, source_identity


def _assert_owner(request):
    from ..repair_control import Store as ControlStore
    current = ControlStore(request['config']['root']).job(request['job']['id'])
    if current['generation'] != request['job']['generation'] or current['state'] == 'cancelled':
        raise RepairBlocked('job_cancelled', 'repair generation lost ownership; source was not updated')


def _fixed_controller(request, root):
    job = request['job']
    pinned = bind_controller(root, request['config'], f"{job['id']}:{job['generation']}")
    current = Path(__file__).resolve().parents[3]
    if current == Path(pinned['root']).resolve(): return pinned
    if not request.get('_request_path'): return pinned  # in-process tests
    path = Path(request['_request_path'])
    atomic_json(path, request)
    fd = os.environ.get('AUTO_AGENTS_REPAIR_LOCK_FD')
    if fd: os.set_inheritable(int(fd), True)
    python = request['config']['python']
    entry = Path(pinned['root']) / 'src/auto_agents/repair_worker.py'
    os.execve(python, [python, str(entry), str(path)],
              {**os.environ, 'PYTHONPATH': str(entry.parents[1])})


def _boundaries(verifier, root, identity, source, payload, cancel):
    results = [verifier.boundary(identity, source, Path(root) / 'target-evidence', payload, cancel)]
    for case in sorted((Path(root) / 'counterexamples').glob('*/payload.json')):
        if cancel.is_set(): raise KeyboardInterrupt()
        results.append(verifier.boundary(identity, source, case.parent / 'target',
                                         json.loads(case.read_text()), cancel))
    return {'ok': all(r['ok'] for r in results), 'snapshot': identity, 'runtime': verifier.runtime,
            'cases': results, 'observed': [r.get('observed', {}) for r in results]}


def _components(request, root, accepted, workspace, python):
    from ..config import load_project_config
    from ..repair_control import Store as ControlStore
    config, job = request['config'], request['job']
    public = ControlStore(config['root'])
    def event(kind, details):
        public.event(job['id'], kind, {**details, 'engine': 'v2', 'generation': job['generation']})
    configured = load_project_config(Path(job['payload']['project']))
    store = Store(root, event)
    provider = job['payload'].get('provider') or configured.active_provider
    if provider not in configured.providers:
        raise RepairBlocked('provider_configuration', 'requested provider is not configured: ' + provider)
    selected = configured.providers[provider]
    verifier = DockerVerifier(Path(config['root']) / 'v2-verification', python=python,
                              callback=event, workers=config.get('verification_workers'),
                              codex_binary=selected.binary if selected.kind == 'codex' else None)
    verifier.prepare()
    from . import images
    images.pin(verifier.image, root)
    images.maintain()
    sandbox = AgentSandbox(root / 'provider-state', verifier.image)
    driver = NativeDriver(configured.providers[provider], sandbox,
                          effort=configured.efforts.get('self_repair', 'deep'),
                          review_effort=configured.efforts.get('self_repair_review', 'max'))
    return store, verifier, driver


def repair_entry(request):
    """No candidate runtime executes controller code or installs host dependencies."""
    from ..repair_control import Repository
    from ..repair_worker import engine_environment
    from ..root_cause import RootCauseCoordinator
    from .evidence import identity as evidence_identity, dissociate
    config, job = request['config'], request['job']
    _assert_owner(request)
    if job['payload'].get('autonomy') == 'off':
        return {'ok': False, 'engine': 'v2', 'status': 'disabled', 'error': 'autonomous self-repair is disabled'}
    root = transaction_root(config, job['payload'])
    pinned = _fixed_controller(request, root)
    with transaction_lock(root):
        from ..artifact_runtime import track
        track(root, 'recovery', scope='repair:' + config['root'],
              metadata={'repair_root': config['root'], 'v2_transaction': str(root)})
        repository = Repository(config)
        binding = Path(config['root']) / 'jobs' / job['id'] / 'v2-transaction.json'
        atomic_json(binding, {'root': str(root)})
        selected_file = root / 'source-selection.json'
        if selected_file.exists():
            selected = json.loads(selected_file.read_text())
        else:
            selected = repository.select_source(job['payload']['base'])
            atomic_json(selected_file, selected)
        atomic_json(binding.parent / 'source-selection.json', selected)
        revision = selected['revision']
        checkout = repository.worktree(revision, 'v2-base-' + revision[:20])
        # Build the host Python environment only from the trusted controller.
        implementation = Path(pinned['root'])
        python, environment = engine_environment(config, implementation)
        old_file = root / 'legacy-import.json'
        old = json.loads(old_file.read_text()) if old_file.exists() else latest_legacy_job(config['root'], job['id'], job['payload'], repository)
        accepted = frozen_request(root, job['payload'], lambda: request_from_payload(
            {**job['payload'], 'base': revision}, root.name, history=old['history'] if old else None))
        if old and not old_file.exists():
            from .migration import materialize_legacy
            old = materialize_legacy(root, old, repository)
            atomic_json(old_file, old)
        if old:
            atomic_json(binding.parent / 'prior-repair-import.json', {
                'source_job': old['job'], 'source_commit': old['revision'], 'engine': 'v2'})
        target = root / 'target-evidence'
        target_marker = root / 'target.json'
        if not target_marker.exists():
            staging = root / 'target.preparing'
            if staging.exists():
                import shutil
                shutil.rmtree(staging)  # never a live project or a candidate
            RootCauseCoordinator._copy_diagnostic_tree(Path(job['payload']['project']), staging)
            if target.exists():
                raise RepairBlocked('incomplete_evidence', 'unsealed target evidence must not be overwritten')
            dissociate(staging)
            staging.rename(target)
            atomic_json(target_marker, {'digest': evidence_identity(target)})
        if evidence_identity(target) != json.loads(target_marker.read_text())['digest']:
            raise RepairBlocked('target_changed', 'original frozen recovery evidence was modified')
        workspace = Workspace(root / 'workspace', checkout, accepted.engine_base,
                              retained=old['source'] if old else None)
        store, verifier, driver = _components(request, root, accepted, workspace, python)
        controller = Controller(accepted, store, workspace, driver, verifier,
            units=lambda source: verifier.suite_units(source, accepted) if hasattr(verifier, 'suite_units') else acceptance_units(source, accepted), resume_token=f"{job['id']}:{job['generation']}",
            allow_implementation=job['payload'].get('autonomy') == 'max',
            regression=lambda i, s, coverage, c: verifier.regression(i, s, checkout, accepted.engine_base, coverage, c),
            boundary=lambda identity, source, cancel: _boundaries(verifier, root, identity, source,
                json.loads((root / 'original-payload.json').read_text()), cancel))
        controller.recover_corrected_source(implementation, pinned['commit'])
        state = controller.run()
        if state['status'] != 'ready':
            return {'ok': False, 'engine': 'v2', 'status': 'v2_' + state['status'],
                    'error': state.get('blocker', {}).get('message', 'repair is not accepted'),
                    'v2_state': str(root / 'state.json'), 'v2_transaction': str(root)}
        approved = _approved(request, root, state, store, python, environment)
        return deliver(request, approved, controller=controller)


def _approved(request, root, state, store, python, environment):
    from ..repair_control import Repository
    snapshot = Path(state['snapshot_path'])
    commit = git(snapshot, 'rev-parse', 'HEAD')
    repository = Repository(request['config'])
    repository.import_commit(str(snapshot), commit)
    runtime = repository.worktree(commit, 'v2-approved-' + commit[:24])
    if source_identity(runtime) != state['snapshot']:
        raise RepairBlocked('delivery_source_changed', 'approved runtime differs from the accepted snapshot')
    return {'ok': True, 'status': 'repaired', 'engine': 'v2', 'commit': commit,
            'runtime': str(runtime), 'python': python, 'environment': environment,
            'base': json.loads((root / 'request.json').read_text())['engine_base'],
            'source_delivery_needed': True,
            'v2_transaction': str(root),
            'v2_receipt': {'request': state['request_digest'], 'snapshot': state['snapshot'],
                           'reference': state['receipt'], 'store': str(root)},
            'proof': 'V2 mandatory tests, independent review and original-boundary replay passed.',
            'engine_full_proof': {'policy': 1, 'commit': commit, 'environment': environment, 'ok': True}}


def verify_receipt(approved, *, expected_root=None):
    marker = approved.get('v2_receipt') or {}
    root = Path(marker.get('store', '/__missing_v2_receipt__'))
    if expected_root is not None and root.resolve() != Path(expected_root).resolve():
        raise RepairBlocked('invalid_acceptance', 'receipt belongs to another repair transaction')
    store = Store(root)
    revoked = root / 'revocations.json'
    if revoked.is_file() and marker['reference']['digest'] in json.loads(revoked.read_text()):
        raise RepairBlocked('invalid_acceptance', 'a live recovery counterexample invalidated this receipt')
    receipt = store.read(marker['reference'])
    validation, review = store.read(receipt['validation']), store.read(receipt['review'])
    accepted = json.loads((root / 'request.json').read_text())
    if (digest(accepted) != marker['request'] or receipt['request'] != marker['request']
            or receipt['snapshot'] != marker['snapshot'] or not validation['ok'] or not review['ok']
            or review['findings'] or validation['snapshot'] != marker['snapshot']
            or review['snapshot'] != marker['snapshot']
            or source_identity(Path(approved['runtime'])) != marker['snapshot']):
        raise RepairBlocked('invalid_acceptance', 'acceptance must bind the exact request, runtime and source')
    requirements = {r['identity'] for r in accepted['acceptance']}
    from .controller import review_result
    verdict = review_result(review['text'], marker['snapshot'], requirements)
    passed = {node for check in validation['checks'] for node in check.get('passed', [])}
    if not verdict.ok or any(not any(p == n or p.startswith(n + '[') or p.startswith(n + '::') for p in passed)
                             for row in verdict.coverage for n in row['nodes']):
        raise RepairBlocked('invalid_acceptance', 'behavioral acceptance coverage is incomplete')
    regression = store.read(receipt['regression'])
    if not regression['ok'] or regression['snapshot'] != marker['snapshot']:
        raise RepairBlocked('invalid_acceptance', 'behavioral regression proof does not match')
    boundary = store.read(receipt['boundary'])
    if not boundary['ok'] or boundary['snapshot'] != marker['snapshot']:
        raise RepairBlocked('invalid_acceptance', 'original boundary was not verified for this source')
    return receipt


def deliver(request, approved, *, controller=None, _passes=0):
    """Integrate advances privately; validate the merged bytes before updating master."""
    from ..repair_control import Repository
    from .types import RepairRequest
    from ..repair_control import operator_policy
    config, job = operator_policy(request['config']), request['job']
    root = Path(approved['v2_transaction'])
    if root != transaction_root(config, job['payload']):
        raise RepairBlocked('invalid_acceptance', 'publication belongs to another transaction')
    saved = Store(root).load()
    if saved and saved['status'] == 'ready' and saved.get('receipt') != approved['v2_receipt']['reference']:
        approved = _approved(request, root, saved, Store(root), approved['python'], approved['environment'])
    verify_receipt(approved, expected_root=root)
    repository = Repository(config)
    snapshot = repository.delivery_snapshot(job['id'])
    remote, fresh = repository.fetch()
    if not fresh: raise RepairBlocked('upstream_unavailable', 'delivery needs a successful upstream refresh')
    repository.import_commit(config['source_root'], snapshot['commit'])
    parents = [snapshot['commit'], remote]
    # All reconciliation uses the retained candidate and the same repair budget.
    advanced = any(__import__('subprocess').run(['git', '-C', str(repository.cache), 'merge-base',
                    '--is-ancestor', p, approved['commit']], capture_output=True).returncode for p in parents)
    if advanced:
        if _passes >= 3:
            raise RepairBlocked('upstream_churn', 'upstream kept advancing; retained candidate and acceptance remain available')
        if controller is None:
            accepted = RepairRequest.from_dict(json.loads((root / 'request.json').read_text()))
            base = repository.worktree(accepted.engine_base, 'v2-base-' + accepted.engine_base[:20])
            workspace = Workspace(root / 'workspace', base, accepted.engine_base)
            store, verifier, driver = _components(request, root, accepted, workspace, approved['python'])
            controller = Controller(accepted, store, workspace, driver, verifier,
                units=lambda s: verifier.suite_units(s, accepted) if hasattr(verifier, 'suite_units') else acceptance_units(s, accepted), resume_token=f"{job['id']}:{job['generation']}",
            allow_implementation=job['payload'].get('autonomy') == 'max',
                regression=lambda i, s, coverage, c: verifier.regression(i, s, base, accepted.engine_base, coverage, c),
                boundary=lambda i, s, c: _boundaries(verifier, root, i, s,
                    json.loads((root / 'original-payload.json').read_text()), c))
        controller.integrate(repository.cache, parents)
        state = controller.run()
        if state['status'] != 'ready':
            return {'ok': False, 'engine': 'v2', 'status': 'integration_blocked',
                    'error': state.get('blocker', {}).get('message', 'integrated source failed acceptance'),
                    'v2_transaction': str(root)}
        approved = _approved(request, root, state, controller.store, approved['python'], approved['environment'])
        verify_receipt(approved, expected_root=root)
        if any(__import__('subprocess').run(['git', '-C', str(repository.cache), 'merge-base',
               '--is-ancestor', p, approved['commit']], capture_output=True).returncode for p in parents):
            return deliver(request, approved, controller=controller, _passes=_passes + 1)
    _assert_owner(request)
    repository.record_delivery(job['id'], snapshot, approved['commit'])
    atomic_json(root / 'delivery.json', {'commit': approved['commit'], 'receipt': approved['v2_receipt'],
                                       'source': snapshot['root'], 'upstream': remote})
    return approved


def validate_subscriber(request):
    from ..root_cause import RootCauseCoordinator
    config, job, subscriber = request['config'], request['job'], request['subscriber']
    approved = job['result']
    root = transaction_root(config, subscriber['payload']['repair'])
    _fixed_controller(request, root)
    with transaction_lock(root):
        verify_receipt(approved, expected_root=root)
        verifier = DockerVerifier(Path(config['root']) / 'v2-verification', python=approved['python'])
        verifier.prepare()
        old = Store(root).read(Store(root).read(approved['v2_receipt']['reference'])['validation'])
        if any(check.get('inputs', {}).get('runtime') != verifier.runtime for check in old['checks']):
            raise RepairBlocked('verification_environment_changed', 'verification environment changed before workflow resume')
        # Re-read current project state in a fresh private copy. This proves the
        # subscriber still matches; historical boundary success alone is insufficient.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='v2-subscriber-', dir=root) as temporary:
            evidence = Path(temporary) / 'target'
            RootCauseCoordinator._copy_diagnostic_tree(Path(subscriber['project']), evidence)
            result = verifier.boundary(approved['v2_receipt']['snapshot'], Path(approved['runtime']), evidence,
                                       subscriber['payload']['repair'], threading.Event())
            if not result['ok']:
                from .evidence import dissociate
                case = root / 'counterexamples' / digest(result.get('observed', {}))
                case.mkdir(parents=True, exist_ok=True)
                if not (case / 'target').exists():
                    dissociate(evidence)
                    evidence.rename(case / 'target')
                    atomic_json(case / 'payload.json', subscriber['payload']['repair'])
                store = Store(root)
                with store.locked():
                    state = store.load()
                    state.update(status='active', phase='implement', failures=[{
                        'unit': 'subscriber-boundary', 'reason': json.dumps(result.get('observed', {}), ensure_ascii=False)}])
                    store.save(state)
                    revoked = root / 'revocations.json'
                    values = json.loads(revoked.read_text()) if revoked.exists() else []
                    values.append(approved['v2_receipt']['reference']['digest'])
                    atomic_json(revoked, sorted(set(values)))
        atomic_json(root / ('subscriber-' + subscriber['id'] + '.json'), result)
        return {'ok': result['ok'], 'proof': json.dumps(result), 'engine_full_proof': approved['engine_full_proof']}


def publish(request):
    from ..repair_control import Repository, publication_policy
    config, job = request['config'], request['job']
    approved = job['result']
    root = Path(approved['v2_transaction'])
    _fixed_controller(request, root)
    with transaction_lock(root):
        _assert_owner(request)
        from ..repair_control import Store as ControlStore
        current = ControlStore(config['root']).job(job['id'])
        if current['state'] != 'completed' or not current['result'].get('ok'):
            raise RepairBlocked('publication_state', 'workflow acceptance must complete before publication')
        approved = deliver(request, approved)
        if not approved.get('ok'): return approved
        _assert_owner(request)
        repository = Repository(config)
        remote, fresh = repository.fetch()
        if not fresh: raise RepairBlocked('upstream_unavailable', 'publication refresh failed')
        import subprocess
        contained = subprocess.run(['git', '-C', str(repository.cache), 'merge-base', '--is-ancestor',
                                    approved['commit'], remote], capture_output=True).returncode == 0
        if not contained:
            publication_policy(config)
            repository.push(approved['commit'])
        from . import images
        images.release(root)
        images.maintain()
        return {'ok': True, 'engine': 'v2', 'commit': approved['commit'], 'status': 'already_published' if contained else 'published'}
