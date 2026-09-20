"""Explicit Docker acceptance; run separately from tests inside the verifier.

AUTO_AGENTS_TEST_VERIFIER_IMAGE pins an already provisioned tool image.
AUTO_AGENTS_TEST_REPAIR_TRANSACTION and AUTO_AGENTS_TEST_PROJECT select read-only
historical/live inputs. All execution uses disposable copies; no provider runs.
"""
import json
import os
from pathlib import Path
import threading

import pytest

from auto_agents.repair_v2.budget_recovery import anchors
from auto_agents.repair_v2.docker import DockerVerifier
from auto_agents.repair_v2.runtime_artifact import build
from auto_agents.repair_v2.types import ValidationUnit
from auto_agents.repair_v2.workspace import Workspace, git, inventory, overlay
from auto_agents.root_cause import RootCauseCoordinator


@pytest.fixture(scope='module')
def runtime(tmp_path_factory):
    temporary = tmp_path_factory.mktemp('portable-runtime-acceptance')
    repository = Path(__file__).resolve().parents[1]
    workspace = Workspace(temporary / 'workspace', repository, git(repository, 'rev-parse', 'HEAD'))
    candidate = workspace.prepare()
    overlay(repository, candidate, inventory(repository))
    workspace.checkpoint()
    identity, snapshot = workspace.freeze()
    verifier = DockerVerifier(temporary / 'verification')
    verifier.image = os.environ['AUTO_AGENTS_TEST_VERIFIER_IMAGE']
    verifier.prepare()
    artifact = build(temporary, snapshot, identity, {'verifier': verifier.runtime})
    return temporary, artifact, verifier


def test_final_artifact_runs_control_contracts_with_host_git_hidden(runtime):
    _, artifact, verifier = runtime
    assert verifier.artifact_preflight(artifact, threading.Event())['ok']
    nodes = ['tests/test_repair_recovery_contract.py', 'tests/test_repair_runtime.py',
             'tests/test_repair_chain.py::test_receipt_consumption_cannot_complete_or_publish_before_real_child_entry',
             'tests/test_repair_chain.py::test_registered_process_ack_uses_real_socket_and_transferred_lock']
    result = verifier.validate(artifact['source'], Path(artifact['path']), [ValidationUnit(
        'required:portable-recovery-contract', 'python -m pytest -q ' + ' '.join(nodes),
        tuple(nodes), fresh=True, profile='sandbox')], threading.Event())
    assert result.ok, result


@pytest.mark.parametrize('scene', ['original', 'current'])
def test_final_artifact_reenters_original_child_with_real_budget_logic(runtime, scene):
    temporary, artifact, verifier = runtime
    transaction = Path(os.environ['AUTO_AGENTS_TEST_REPAIR_TRANSACTION'])
    payload = json.loads((transaction / 'original-payload.json').read_text())
    original = transaction / 'target-evidence'
    origin = original if scene == 'original' else Path(os.environ['AUTO_AGENTS_TEST_PROJECT'])
    protected = {p: p.read_bytes() for p in origin.glob('.auto-agents/state/sessions/*/session_state.json')}
    target = temporary / ('scene-' + scene)
    RootCauseCoordinator._copy_diagnostic_tree(origin, target)
    payload['_budget_anchors'] = anchors(original, payload['invocation'])
    result = verifier.boundary(artifact['source'], Path(artifact['path']), target, payload, threading.Event())
    (temporary / (scene + '-result.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert result['ok'], result
    observed = result['observed']; child = observed['recovery_observation']
    assert observed['engine_runtime']['ok']
    assert child['parent_session_id'] == payload['invocation']['session_id']
    assert child['boundary_session_id'] == child['child_session_id']
    assert child['boundary_kind'] == 'implementation' and child['preflight_rechecked']
    assert child['full_dispatch'] and child['provider_boundary_calls'] == child['budget_reserved'] == 1
    assert child['diagnostic_provider_calls'] == 0
    assert child['parent_budget']['before'] == child['parent_budget']['after']
    assert {p: p.read_bytes() for p in protected} == protected


@pytest.mark.parametrize('legacy_budget_reset,child_status', [(False, 'blocked'), (True, 'blocked'), (False, 'failed')])
def test_real_launcher_registers_and_reaches_one_native_provider_boundary(runtime, tmp_path, legacy_budget_reset, child_status):
    """Only the review/model endpoints are fixtures; launch/ACK are real."""
    import signal
    import subprocess
    import sys
    import time
    from auto_agents.authorization import authorization_policy_for_state
    from auto_agents.config import load_session_state, save_session_state, load_project_config, save_project_config
    from auto_agents.repair_control import Supervisor, atomic_json, digest as route_digest
    from auto_agents.repair_v2 import integration
    from auto_agents.repair_v2.chain import RepairChain
    from auto_agents.repair_v2.controller import Controller
    from auto_agents.repair_v2.recovery import context
    from auto_agents.repair_v2.store import Store, digest
    from auto_agents.repair_v2.transaction import frozen_request, transaction_root
    from auto_agents.repair_v2.types import Acceptance, AgentReply, RepairRequest
    from auto_agents.run_lock import ProjectRunLock
    from execution_marker import ExecutionMarker
    from test_engine_child_recovery import configure_local_writer, parent_workflow
    from test_repair_control import configuration, registration
    from test_session_verification_ownership import project

    _, selected, _ = runtime
    product, child = project(tmp_path / 'product')
    configured = load_project_config(product)
    configured.execution.autonomy.mode = 'guarded'
    save_project_config(product, configured)
    git(product, 'rm', '--cached', '--ignore-unmatch', '.conda')
    with (product / '.gitignore').open('a') as handle: handle.write('\n.conda\n')
    git(product, 'add', '.gitignore'); git(product, 'commit', '-qm', 'Keep dependencies outside source snapshots')
    marker = ExecutionMarker(tmp_path / 'provider-entered')
    configure_local_writer(product, child, marker.source(repr('entered')) + '\nimport time\ntime.sleep(30)')
    child.fix_verify_command = 'conda run -p ./.conda python -m pytest -q tests/test_owned.py::test_owned'
    child.authorization_policy = authorization_policy_for_state(auto_approve=True).to_dict()
    child.status, child.resolution, child.hard_ceiling = child_status, 'verification_ownership', 15
    child.resume_phase = 'executing' if child_status == 'failed' else ''
    child.execution_log.append({'action': 'execution_preflight_blocked', 'failure_kind': 'verification_ownership',
                               'retry_fix': False, 'result': 'retained engine preflight failure'})
    workflows, workflow, handoff = parent_workflow(product, child, engine=True)
    handoff.payload['issue_seed']['target_repository'] = selected['path']
    workflows.save_handoff(handoff)
    parent = load_session_state(product, 'parent')
    parent.current_attempt, parent.attempt_epoch, parent.attempts_since_progress = 2, 10, 1
    save_session_state(product, parent)
    child = load_session_state(product, child.session_id)
    config = configuration(tmp_path)
    config.update(source_root=selected['path'], implementation_root=str(Path(__file__).resolve().parents[1]),
                  repair_engine='v2', publish=False)
    subprocess.run(['git', 'clone', '--bare', '--quiet', selected['path'], config['remote']], check=True)
    git(config['remote'], 'update-ref', 'refs/heads/master', selected['commit'])
    atomic_json(Path(config['root']) / 'operator.json', config)
    verifier = DockerVerifier(Path(config['root']) / 'v2-verification', python=sys.executable)
    verifier.prepare()
    payload = {'project': str(product), 'base': selected['commit'], 'environment': verifier.runtime,
        'fingerprint': 'retained-child', 'contract': {}, 'autonomy': 'guarded',
        'invocation': {'command': 'collab', 'session_id': 'parent', 'engine_route': handoff.payload},
        'boundary': {'kind': 'engine_route', 'route_digest': route_digest(handoff.payload)},
        'resume_argv': [sys.executable, 'unused-entry', 'collab', '--project', str(product),
                        '--provider', 'claude-code', '--auto-approve', '--session', 'parent']}
    root = transaction_root(config, payload)
    node = 'tests/test_repair_runtime.py::test_current_runtime_passes_controller_owned_behavior_checks'
    request = frozen_request(root, payload, lambda: RepairRequest(root.name, selected['commit'],
        'Resume the original child with the selected engine', (Acceptance('runtime', 'Verified runtime'),),
        'fixture', invocation=payload['invocation']))
    store = Store(root)
    RootCauseCoordinator._copy_diagnostic_tree(product, root / 'target-evidence')
    captured = anchors(product, payload['invocation'])
    anchor_ref = store.artifact('budget-anchors', captured)
    atomic_json(root / 'budget-anchors.json', anchor_ref)
    chain = RepairChain(config, payload, root); chain.admit()
    class ReviewEndpoint:
        def run(self, role, *args, **kwargs):
            assert role == 'review'
            return AgentReply(True, json.dumps({'decision': 'APPROVE', 'findings': [],
                'coverage': [{'requirement': 'runtime', 'nodes': [node]}]}))
    workspace = Workspace(root / 'workspace', Path(selected['path']), selected['commit'])
    controller = Controller(request, store, workspace, ReviewEndpoint(), verifier, chain=chain,
        allow_implementation=False, preflight_boundary=True,
        units=lambda source: [ValidationUnit('required:runtime', 'python -m pytest -q ' + node, (node,))],
        regression=lambda identity, source, coverage, cancel: verifier.regression(identity, source,
            Path(selected['path']), selected['commit'], coverage, cancel),
        boundary=lambda identity, source, cancel: verifier.boundary(identity, source,
            root / 'target-evidence', {**payload, '_budget_anchors': captured}, cancel))
    controller.runtime_environments = {'python': sys.executable, 'environment': verifier.runtime}
    controller.budget_anchors = anchor_ref
    controller.recovery_context = store.artifact('recovery-context', context(payload, digest(request.to_dict()), chain.store.root))
    state = controller.run()
    assert state['status'] == 'ready', state
    assert chain.context()['used']['model_calls'] == 1 and chain.context()['used']['implementations'] == 0
    if legacy_budget_reset:
        reset = load_session_state(product, 'parent')
        reset.current_attempt, reset.attempt_epoch, reset.attempts_since_progress = 0, 16, 0
        reset.execution_log.append({'action': 'attempt_epoch_started', 'result': 'failed session resumed'})
        save_session_state(product, reset)
    supervisor = Supervisor(config)
    with ProjectRunLock(product, environ={}) as lock:
        subscriber = supervisor.register({'payload': registration(product, lock.run_token), 'environment': {
            'AUTO_AGENTS_REPAIR_CONTROL_DISABLED': '0', 'AUTO_AGENTS_REPAIR_ROUTE_PROBE': ''}},
            [os.dup(lock.fileno)])['subscriber']
        job_id = supervisor.store.submit(subscriber, payload)
        job = supervisor.store.job(job_id)
        approved = integration._approved({'config': config, 'job': job}, root, state, store,
                                         sys.executable, verifier.runtime)
        approved['source_delivery_needed'] = False
        supervisor.store.transition(job_id, 'ready', approved)
        thread = threading.Thread(target=supervisor.serve, daemon=True); thread.start()
        try:
            deadline = time.monotonic() + 180
            while not marker.exists():
                assert time.monotonic() < deadline, supervisor.store.job(job_id)
                job = supervisor.store.job(job_id)
                assert job['state'] != 'blocked', job
                time.sleep(.1)
            assert supervisor.store.job(job_id)['state'] == 'completed'
            assert store.load()['status'] == 'complete'
            restored = load_session_state(product, child.session_id)
            assert restored.current_attempt == child.current_attempt + 1
            restored_parent = load_session_state(product, 'parent')
            assert (restored_parent.current_attempt, restored_parent.attempt_epoch,
                    restored_parent.attempts_since_progress) == (2, 16 if legacy_budget_reset else 10, 1)
            assert marker.read_text() == 'entered'
        finally:
            supervisor.halt = True; thread.join(timeout=5)
            for process in list(supervisor.resumes.values()):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
            for item in supervisor.registrations.values():
                for descriptor in item['fds']: os.close(descriptor)
        assert not thread.is_alive()
