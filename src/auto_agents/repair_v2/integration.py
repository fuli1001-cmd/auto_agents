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
from .chain import RepairChain
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
    results = [_boundary_once(verifier, root, identity, source, Path(root) / 'target-evidence', payload, cancel)]
    exclusions = _excluded_cases(root)
    for case in sorted((Path(root) / 'counterexamples').glob('*/payload.json')):
        if cancel.is_set(): raise KeyboardInterrupt()
        if case.parent.name in exclusions: continue
        results.append(_boundary_once(verifier, root, identity, source, case.parent / 'target',
                                      json.loads(case.read_text()), cancel))
    from .recovery import classify
    for result in results:
        if result.get('ok'): continue
        failure = classify(result, payload)
        if failure['domain'] != 'candidate':
            error = RepairBlocked(failure['code'], failure['message'])
            error.failure = failure
            raise error
    return {'ok': all(r['ok'] for r in results), 'snapshot': identity, 'runtime': verifier.runtime,
            'infrastructure': any(r.get('infrastructure') for r in results),
            'reason': '; '.join(r.get('reason', '') for r in results if r.get('infrastructure')),
            'cases': results, 'observed': [r.get('observed', {}) for r in results]}


def _boundary_once(verifier, root, identity, source, target, payload, cancel):
    from .evidence import identity as evidence_identity
    from .proofs import once
    anchor_file = Path(root) / 'budget-anchors.json'
    if anchor_file.exists():
        payload = {**payload, '_budget_anchors': Store(root).read(json.loads(anchor_file.read_text()))}
    if not hasattr(verifier, 'boundary_inputs'):
        return verifier.boundary(identity, source, target, payload, cancel)
    inputs = {'source': identity, 'commit': git(source, 'rev-parse', 'HEAD'),
              'format': 'standalone-git-v1', 'runtime': verifier.runtime,
              'target': evidence_identity(target), 'payload': digest(payload),
              'environments': verifier.boundary_inputs(target, payload)}
    return once(Store(root), 'recovery-boundary', inputs,
                lambda: verifier.boundary(identity, source, target, payload, cancel))


def _excluded_cases(root):
    from .recovery import classify
    marker = Path(root) / 'artifact-migration.json'
    if not marker.exists(): return set()
    result = set()
    saved = json.loads(marker.read_text())
    for reference in saved.get('packaging_failures', []):
        evidence = Store(root).read(reference)
        if classify(evidence, {}).get('domain') != 'artifact':
            raise RepairBlocked('invalid_migration', '旧打包故障的迁移依据已改变')
        result.add(digest(evidence.get('observed', {})))
    return result


def _migrate_artifact_failure(controller):
    """Retain raw/revoked receipts; only proven packaging failures are superseded."""
    from .recovery import classify
    store = controller.store
    with store.locked():
        state = store.load()
        if not state or state.get('runtime_artifact') or not state.get('receipt'):
            return False
        failures = state.get('failures', [])
        if not failures or any(f.get('unit') != 'subscriber-boundary' for f in failures):
            return False
        accepted = store.read(state['receipt'])
        if accepted.get('snapshot') != state.get('snapshot'):
            return False
        references, known = [], set()
        for path in sorted(store.root.glob('subscriber-*.json')):
            evidence = json.loads(path.read_text())
            observed = evidence.get('observed', {})
            runtime = observed.get('engine_runtime', {}) if isinstance(observed, dict) else {}
            if (evidence.get('snapshot') == state['snapshot'] and not evidence.get('ok')
                    and 'commit_unavailable' in runtime.get('mismatches', [])
                    and set(runtime['mismatches']) <= {'commit', 'commit_unavailable'}
                    and classify(evidence, {})['domain'] == 'artifact'):
                references.append(store.artifact('packaging-failure', evidence))
                known.add(digest(observed))
        cases = {p.parent.name for p in (store.root / 'counterexamples').glob('*/payload.json')}
        if not references or not cases or not cases <= known:
            return False
        if source_identity(controller.workspace.candidate) != state['snapshot']:
            return False
        previous = store.artifact('pre-artifact-migration', state)
        atomic_json(store.root / 'artifact-migration.json', {'version': 1, 'previous': previous,
                    'revoked_receipt': state['receipt'], 'packaging_failures': references})
        store.transition(state, status='active', phase='validate', failures=[], blocker={})
        store.event('artifact_failure_migrated', previous=previous, calls=state['calls'], attempts=state['attempts'])
        return True


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
    from .diagnostic_evidence import prepare
    evidence, evidence_context = prepare(root, json.loads((root / 'original-payload.json').read_text()))
    sandbox = AgentSandbox(root / 'provider-state', verifier.image, evidence=evidence)
    driver = NativeDriver(configured.providers[provider], sandbox,
                          effort=configured.efforts.get('self_repair', 'deep'),
                          review_effort=configured.efforts.get('self_repair_review', 'max'))
    driver.evidence_context = evidence_context
    return store, verifier, driver


def repair_entry(request):
    """Keep delivery/controller exceptions out of candidate implementation."""
    try:
        return _repair_entry(request)
    except Exception as error:
        root = transaction_root(request['config'], request['job']['payload'])
        code = getattr(error, 'code', 'recovery_unclassified')
        if code in {'job_cancelled', 'transaction_busy', 'stale_transition'}:
            raise
        with transaction_lock(root):
            _assert_owner(request)
            store = Store(root)
            with store.locked():
                state = store.load()
                if state:
                    domain = 'artifact' if code == 'runtime_artifact_invalid' else 'controller'
                    from .types import RepairFailure
                    from .recovery import block
                    failure = asdict(RepairFailure(domain, code, domain, state.get('phase', 'prepare'),
                        str(error), {'error_type': type(error).__name__, 'error': str(error)}))
                    block(store, state, failure, operation='repair:' + request['job']['id'])
        raise


def _repair_entry(request):
    """No candidate runtime executes controller code or installs host dependencies."""
    from ..repair_control import Repository
    from ..repair_worker import engine_environment
    from ..root_cause import RootCauseCoordinator
    from .evidence import identity as evidence_identity, dissociate
    config, job = request['config'], request['job']
    _assert_owner(request)
    if job['payload'].get('autonomy') == 'off':
        return {'ok': False, 'engine': 'v2', 'status': 'disabled', 'error': 'autonomous self-repair is disabled'}
    from .incidents import migrate as migrate_incidents
    migrate_incidents(config, job['payload'])
    root = transaction_root(config, job['payload'])
    chain = RepairChain(config, job['payload'], root)
    chain.admit()
    pinned = _fixed_controller(request, root)
    with transaction_lock(root):
        from ..artifact_runtime import track
        track(root, 'recovery', scope='repair:' + config['root'],
              metadata={'repair_root': config['root'], 'v2_transaction': str(root)})
        repository = Repository(config)
        binding = Path(config['root']) / 'jobs' / job['id'] / 'v2-transaction.json'
        atomic_json(binding, {'root': str(root)})
        selected_file = root / 'source-selection.json'
        first_selection = not selected_file.exists()
        if selected_file.exists():
            selected = json.loads(selected_file.read_text())
        else:
            selected = repository.select_source(job['payload']['base'])
            atomic_json(selected_file, selected)
        # The transaction's selection is its immutable acceptance baseline.
        # Each new job generation must separately observe the current engine.
        current_file = binding.parent / f"source-selection-g{job['generation']}.json"
        if current_file.exists():
            current = json.loads(current_file.read_text())
        else:
            current = selected if first_selection else repository.select_source(job['payload']['base'])
            atomic_json(current_file, current)
        atomic_json(binding.parent / 'source-selection.json', current)
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
            from .diagnostic_replay import copy_submission_evidence
            copy_submission_evidence(Path(job['payload']['project']), staging, job['payload'])
            if target.exists():
                raise RepairBlocked('incomplete_evidence', 'unsealed target evidence must not be overwritten')
            dissociate(staging)
            staging.rename(target)
            atomic_json(target_marker, {'digest': evidence_identity(target)})
        if evidence_identity(target) != json.loads(target_marker.read_text())['digest']:
            raise RepairBlocked('target_changed', 'original frozen recovery evidence was modified')
        workspace = Workspace(root / 'workspace', checkout, accepted.engine_base,
                              retained=old['source'] if old else None)
        from .scope import ScopeGuard, context as goal_context
        # Repeated observations of one unresolved fault retain its contract
        # and budget, but the current scene is a separate immutable witness.
        scope_target = target
        if (job['payload'].get('scope_receipt') or {}).get('context', {}).get('policy') == 'goal-scope-v2':
            current_context = goal_context(Path(job['payload']['project']), job['payload'])
            original_context = goal_context(target, json.loads((root / 'original-payload.json').read_text()))
            current_incident, original_incident = current_context.get('incident'), original_context.get('incident')
            if current_incident and original_incident and current_incident['identity'] != original_incident['identity']:
                raise RepairBlocked('recovery_context_changed', '当前故障已变化，旧事务不能接管新的阻塞。')
            if current_incident and current_incident != original_incident:
                case = root / 'counterexamples' / digest(current_incident)
                if not (case / 'target').exists():
                    RootCauseCoordinator._copy_diagnostic_tree(Path(job['payload']['project']), case / 'target')
                    from .diagnostic_replay import copy_submission_evidence
                    copy_submission_evidence(Path(job['payload']['project']), case / 'target', job['payload'])
                    dissociate(case / 'target')
                    atomic_json(case / 'payload.json', job['payload'])
                scope_target = case / 'target'
        store, verifier, driver = _components(request, root, accepted, workspace, python)
        current_source = repository.worktree(current['revision'], 'v2-scope-' + current['revision'][:20])
        scope = ScopeGuard(root, job['payload'], scope_target, current_source)
        from .incidents import record as record_incident
        record_incident(store, scope.context)
        scope.import_receipt(job['payload'].get('scope_receipt'))
        controller = Controller(accepted, store, workspace, driver, verifier,
            chain=chain, preflight_boundary=True, scope=scope,
            units=lambda source: verifier.suite_units(source, accepted) if hasattr(verifier, 'suite_units') else acceptance_units(source, accepted), resume_token=f"{job['id']}:{job['generation']}",
            allow_implementation=job['payload'].get('autonomy') == 'max',
            regression=lambda i, s, coverage, c: verifier.regression(i, s, checkout, accepted.engine_base, coverage, c),
            boundary=lambda identity, source, cancel: _boundaries(verifier, root, identity, source,
                json.loads((root / 'original-payload.json').read_text()), cancel))
        controller.runtime_environments = {'python': python, 'environment': environment}
        from .budget_recovery import anchors
        controller.budget_anchors = store.artifact('budget-anchors', anchors(target, accepted.invocation))
        atomic_json(root / 'budget-anchors.json', controller.budget_anchors)
        from .recovery import context
        recovery_context = context(job['payload'], digest(accepted.to_dict()), chain.store.root)
        recovery_context.update(scope=scope.context,
            incident_id=accepted.incident_id or (scope.context.get('incident') or {}).get('identity', ''),
            incident_revision=(scope.context.get('incident') or {}).get('revision', accepted.incident_revision),
            contract_revision=accepted.contract_revision or digest([accepted.goal, [asdict(a) for a in accepted.acceptance]]))
        controller.recovery_context = store.artifact('recovery-context', recovery_context)
        atomic_json(root / 'recovery-context.json', recovery_context)
        _migrate_artifact_failure(controller)
        controller.recover_verified_progress()
        corrected = controller.recover_corrected_source(repository.cache, current['revision'])
        from .source_refresh import prepare
        prepare(controller, repository.cache, current['revision'], force_check=corrected)
        state = controller.run()
        if state['status'] not in ('ready', 'complete'):
            return {'ok': False, 'engine': 'v2', 'status': 'v2_' + state['status'],
                    'return_goal': scope.context['owner'] if state['status'] == 'skipped' else None,
                    'pending_decision': state.get('pending_decision', '') if state['status'] == 'waiting_user' else '',
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
    from .runtime_artifact import build, verify
    artifact = state.get('runtime_artifact')
    if not artifact:
        artifact = build(root, snapshot, state['snapshot'],
                         {'python': python, 'environment': environment,
                          'verifier': state.get('verification_runtime', '')})
    verify(artifact)
    runtime = Path(artifact['path'])
    if source_identity(runtime) != state['snapshot']:
        raise RepairBlocked('delivery_source_changed', 'approved runtime differs from the accepted snapshot')
    return {'ok': True, 'status': 'accepted', 'engine': 'v2', 'commit': commit,
            'runtime_artifact': artifact, 'recovery_protocol': 2,
            'runtime': str(runtime), 'python': python, 'environment': environment,
            'base': json.loads((root / 'request.json').read_text())['engine_base'],
            'source_delivery_needed': True,
            'v2_transaction': str(root),
            'v2_receipt': {'request': state['request_digest'], 'snapshot': state['snapshot'],
                           'reference': state['receipt'], 'store': str(root)},
            'proof': 'Required behavior, no-new-failures acceptance, independent review and original-boundary replay passed.',
            'engine_full_proof': {'policy': 2, 'commit': commit, 'environment': environment,
                                  'artifact_id': artifact['artifact_id'], 'code_accepted': True,
                                  'recovered': False, 'ok': False}}


def verify_receipt(approved, *, expected_root=None):
    if approved.get('recovery_protocol') not in (None, 1, 2):
        raise RepairBlocked('runtime_protocol', '恢复协议版本不受当前控制器支持；需要兼容的控制器后才能继续')
    from .runtime_artifact import verify
    if approved.get('runtime_artifact'):
        verify(approved['runtime_artifact'])
        if (Path(approved['runtime']).resolve() != Path(approved['runtime_artifact']['path']).resolve()
                or approved['commit'] != approved['runtime_artifact']['commit']):
            raise RepairBlocked('runtime_artifact_invalid', '交付路径与产物清单不一致')
    from .comparison import accepted as validation_accepted
    marker = approved.get('v2_receipt') or {}
    root = Path(marker.get('store', '/__missing_v2_receipt__'))
    if expected_root is not None and root.resolve() != Path(expected_root).resolve():
        raise RepairBlocked('invalid_acceptance', 'receipt belongs to another repair transaction')
    store = Store(root)
    revoked = root / 'revocations.json'
    if revoked.is_file() and marker['reference']['digest'] in json.loads(revoked.read_text()):
        raise RepairBlocked('invalid_acceptance', 'a live recovery counterexample invalidated this receipt')
    receipt = store.read(marker['reference'])
    if approved.get('recovery_protocol') in (1, 2):
        if receipt.get('runtime_artifact') != approved.get('runtime_artifact'):
            raise RepairBlocked('invalid_acceptance', '验收回执没有绑定当前运行产物')
        if receipt.get('budget_anchors'):
            store.read(receipt['budget_anchors'])
        if not receipt.get('recovery_context'):
            raise RepairBlocked('invalid_acceptance', '验收回执缺少原任务恢复上下文')
        context = store.read(receipt['recovery_context'])
        if context.get('version') != approved['recovery_protocol'] or context.get('request') != marker['request']:
            raise RepairBlocked('invalid_acceptance', '恢复上下文与验收目标不一致')
        if approved['recovery_protocol'] == 2:
            incident = context.get('scope', {}).get('incident')
            if incident and context.get('incident_id') != incident['identity']:
                raise RepairBlocked('invalid_acceptance', '恢复回执没有绑定对应失败事件')
    if (root / 'scope.json').exists() and not receipt.get('scope'):
        raise RepairBlocked('invalid_acceptance', 'candidate lacks the current goal scope proof')
    validation, review = store.read(receipt['validation']), store.read(receipt['review'])
    accepted = json.loads((root / 'request.json').read_text())
    if (digest(accepted) != marker['request'] or receipt['request'] != marker['request']
            or receipt['snapshot'] != marker['snapshot'] or not validation_accepted(store, receipt, accepted['engine_base']) or not review['ok']
            or review['findings'] or validation['snapshot'] != marker['snapshot']
            or review['snapshot'] != marker['snapshot']
            or source_identity(Path(approved['runtime'])) != marker['snapshot']):
        raise RepairBlocked('invalid_acceptance', 'acceptance must bind the exact request, runtime and source')
    requirements = {r['identity'] for r in accepted['acceptance']}
    from .controller import review_result
    verdict = review_result(review['text'], marker['snapshot'], requirements)
    if receipt.get('scope'):
        from .scope import POLICY, changes
        scoped = store.read(receipt['scope'])
        if scoped.get('policy') not in ({POLICY} if approved.get('recovery_protocol') == 2 else {POLICY, 'goal-scope-v1'}) or scoped.get('proposal', {}).get('decision') != 'required':
            raise RepairBlocked('invalid_acceptance', 'repair scope receipt is invalid')
        verdict = review_result(review['text'], marker['snapshot'], requirements,
                                changes(Path(approved['runtime']), accepted['engine_base'], receipt.get('integration_parents', [])))
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
            chain = RepairChain(config, job['payload'], root)
            chain.admit()
            from .scope import ScopeGuard
            controller = Controller(accepted, store, workspace, driver, verifier,
                chain=chain, preflight_boundary=True,
                scope=ScopeGuard(root, job['payload'], root / 'target-evidence', base),
                units=lambda s: verifier.suite_units(s, accepted) if hasattr(verifier, 'suite_units') else acceptance_units(s, accepted), resume_token=f"{job['id']}:{job['generation']}",
            allow_implementation=job['payload'].get('autonomy') == 'max',
                regression=lambda i, s, coverage, c: verifier.regression(i, s, base, accepted.engine_base, coverage, c),
                boundary=lambda i, s, c: _boundaries(verifier, root, i, s,
                    json.loads((root / 'original-payload.json').read_text()), c))
            controller.runtime_environments = {'python': approved['python'], 'environment': approved['environment']}
            controller.budget_anchors = json.loads((root / 'budget-anchors.json').read_text())
            controller.recovery_context = store.artifact('recovery-context', json.loads((root / 'recovery-context.json').read_text()))
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
    from .recovery import block
    from .types import RepairFailure
    try:
        return _validate_subscriber(request)
    except Exception as error:
        code = getattr(error, 'code', 'recovery_unclassified')
        if code in {'job_cancelled', 'transaction_busy', 'stale_transition'}:
            raise
        artifact = code in {'runtime_artifact_invalid', 'delivery_source_changed'}
        environment = code in {'verification_infrastructure', 'verification_environment_changed',
                               'docker_unavailable', 'disk_space', 'image_unavailable'}
        domain = 'artifact' if artifact else 'environment' if environment else 'controller'
        stage = 'artifact' if artifact else 'subscriber_validation'
        failure = asdict(RepairFailure(domain, code, domain, stage, str(error),
                                      {'error_type': type(error).__name__, 'error': str(error),
                                       'inputs': getattr(error, 'inputs', {})}))
        root = transaction_root(request['config'], request['subscriber']['payload']['repair'])
        with transaction_lock(root):
            _assert_owner(request)
            store = Store(root)
            with store.locked():
                state = store.load()
                if state:
                    if code in {'verification_environment_changed', 'image_unavailable'}:
                        artifact = request['job']['result'].get('runtime_artifact', {})
                        signature = digest([code, artifact.get('source'), getattr(error, 'controller_source', '')])
                        previous = state.get('revalidation_signatures', [])
                        if signature not in previous:
                            store.transition(state, status='active', phase='validate', blocker={},
                                revalidation_signatures=[*previous, signature])
                            store.event('acceptance_revalidation_required', signature=signature, code=code)
                            return {'ok': False, 'revalidate': True, 'failure': failure,
                                    'error': '验证环境已变化，保留代码结果并补验受影响的检查。'}
                    block(store, state, failure, operation='validation:' + request['subscriber']['id'])
        return {'ok': False, 'failure': failure, 'error': str(error), 'proof': json.dumps(failure)}


def _validate_subscriber(request):
    from ..root_cause import RootCauseCoordinator
    from .recovery import block, classify, context, operation
    from .runtime_artifact import verify
    config, job, subscriber = request['config'], request['job'], request['subscriber']
    approved = job['result']
    root = transaction_root(config, subscriber['payload']['repair'])
    pinned = _fixed_controller(request, root)
    with transaction_lock(root):
        _assert_owner(request)
        acceptance = verify_receipt(approved, expected_root=root)
        artifact = approved.get('runtime_artifact')
        if not artifact:
            raise RepairBlocked('runtime_protocol', '旧运行目录需要迁移为独立产物，不能直接接管任务')
        verify(artifact)
        recovery_context = Store(root).read(acceptance['recovery_context'])
        current_payload = subscriber['payload']['repair']
        if (recovery_context['invocation'] != current_payload.get('invocation', {})
                or recovery_context['boundary'] != current_payload.get('boundary', {})):
            raise RepairBlocked('recovery_context_changed', '当前交接与已验证的原任务不一致，需要重新核对恢复上下文')
        action = operation(job, subscriber, artifact, recovery_context)
        verifier = DockerVerifier(Path(config['root']) / 'v2-verification', python=approved['python'],
                                  image=artifact['environments'].get('image') or None)
        try:
            verifier.prepare()
        except RepairBlocked as error:
            error.controller_source = pinned['source']
            raise
        old = Store(root).read(Store(root).read(approved['v2_receipt']['reference'])['validation'])
        if any(check.get('inputs', {}).get('runtime') != verifier.runtime for check in old['checks']):
            error = RepairBlocked('verification_environment_changed', '恢复检查与已有验收的验证环境不同，需要补验。')
            error.inputs = {'accepted': sorted({check.get('inputs', {}).get('runtime', '') for check in old['checks']}),
                            'observed': verifier.runtime}
            error.controller_source = pinned['source']
            raise error
        # Re-read current project state in a fresh private copy. This proves the
        # subscriber still matches; historical boundary success alone is insufficient.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='v2-subscriber-', dir=root) as temporary:
            evidence = Path(temporary) / 'target'
            RootCauseCoordinator._copy_diagnostic_tree(Path(subscriber['project']), evidence)
            from .diagnostic_replay import copy_submission_evidence
            copy_submission_evidence(Path(subscriber['project']), evidence, subscriber['payload']['repair'])
            result = _boundary_once(verifier, root, approved['v2_receipt']['snapshot'], Path(approved['runtime']),
                                    evidence, subscriber['payload']['repair'], threading.Event())
            _assert_owner(request)
            failure = classify(result, subscriber['payload']['repair']) if not result['ok'] else None
            if failure and failure['domain'] == 'candidate':
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
                    store.transition(state, status='blocked', phase='implement', failures=[{
                        'unit': 'subscriber-boundary', 'reason': json.dumps(result.get('observed', {}), ensure_ascii=False)}])
                    revoked = root / 'revocations.json'
                    values = json.loads(revoked.read_text()) if revoked.exists() else []
                    values.append(approved['v2_receipt']['reference']['digest'])
                    atomic_json(revoked, sorted(set(values)))
            store = Store(root)
            with store.locked():
                state = store.load()
                if failure:
                    block(store, state, failure, operation=action)
                else:
                    store.transition(state, status='ready', phase='activation', blocker={},
                        recovery_failure=None, recovery_operation=action,
                        recovery_owner={'job': job['id'], 'generation': job['generation'],
                                        'subscriber': subscriber['id'], 'context': recovery_context,
                                        'artifact_id': artifact['artifact_id']})
        atomic_json(root / ('subscriber-' + subscriber['id'] + '.json'), result)
        return {'ok': result['ok'], 'proof': json.dumps(result), 'failure': failure,
                'error': failure['message'] if failure else '', 'recovery_operation': action,
                'engine_full_proof': {**approved['engine_full_proof'], 'delivery_verified': bool(result['ok'])}}


def acknowledge_recovery(job, subscriber, details):
    """Only called after Supervisor authenticates the live child and its route."""
    root = Path(job['result']['v2_transaction'])
    with transaction_lock(root):
        store = Store(root)
        with store.locked():
            state = store.load()
            owner = state.get('recovery_owner') or {}
            if (owner.get('job') != job['id'] or owner.get('generation') != job['generation']
                    or owner.get('subscriber') != subscriber['id']
                    or state.get('receipt') != job['result']['v2_receipt']['reference']
                    or owner.get('artifact_id') != job['result']['runtime_artifact']['artifact_id']):
                raise RepairBlocked('stale_recovery', '接管确认不属于当前恢复操作')
            if state['status'] == 'complete':
                return state['live_recovery']
            if state.get('phase') != 'activation' or state['status'] != 'ready':
                raise RepairBlocked('invalid_recovery_phase', '当前阶段不接受接管确认')
            receipt = {'job': job['id'], 'subscriber': subscriber['id'], 'generation': job['generation'],
                       'boundary': details, 'commit': job['result']['commit'],
                       'operation': state['recovery_operation'], 'artifact_id': owner['artifact_id']}
            store.transition(state, status='complete', phase='recovered', live_recovery=receipt,
                             revalidation_signatures=[])
            atomic_json(root / 'live-recovery.json', receipt)
            marker = root / 'incident.json'
            if marker.exists():
                incident = store.read(json.loads(marker.read_text()))
                atomic_json(root / 'incident-resolution.json', store.artifact('incidents', {
                    **incident, 'status': 'resolved', 'recovery': digest(receipt)}))
            return receipt


def record_activation_failure(config, job, subscriber, receipt):
    from .recovery import block, classify
    root = Path(job['result']['v2_transaction'])
    if root.parent.resolve() != (Path(config['root']) / 'v2-transactions').resolve():
        raise RepairBlocked('invalid_acceptance', '启动失败回执不属于当前控制器')
    failure = classify({'ok': False, 'observed': receipt}, subscriber['payload']['repair'])
    if failure['domain'] == 'unknown':
        failure.update(code=receipt.get('code') or 'activation_failed', recover_at='activation',
                       message=receipt.get('error') or '原任务在确认接管前退出；已保留代码验收，停止自动实施。')
    with transaction_lock(root):
        _assert_owner({'config': config, 'job': job})
        store = Store(root)
        with store.locked():
            state = store.load()
            if state:
                block(store, state, failure, operation=state.get('recovery_operation', 'activation'))
    return failure


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
        from .recovery import completed_result
        if completed_result(current) is None:
            raise RepairBlocked('publication_state', '发布需要当前事务保存的真实接管确认')
        verify_receipt(approved, expected_root=root)
        _assert_owner(request)
        repository = Repository(config)
        remote, fresh = repository.fetch()
        if not fresh: raise RepairBlocked('upstream_unavailable', 'publication refresh failed')
        import subprocess
        contained = subprocess.run(['git', '-C', str(repository.cache), 'merge-base', '--is-ancestor',
                                    approved['commit'], remote], capture_output=True).returncode == 0
        if not contained:
            fast_forward = subprocess.run(['git', '-C', str(repository.cache), 'merge-base', '--is-ancestor',
                                           remote, approved['commit']], capture_output=True).returncode == 0
            if not fast_forward:
                raise RepairBlocked('publication_diverged', '远端已发生独立变更；原任务继续运行，发布等待单独处理。')
            publication_policy(config)
            repository.push(approved['commit'])
        from . import images
        images.release(root)
        images.maintain()
        return {'ok': True, 'engine': 'v2', 'commit': approved['commit'], 'status': 'already_published' if contained else 'published'}
