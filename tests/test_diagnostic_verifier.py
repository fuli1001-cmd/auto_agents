"""The controller must observe recovery, not accept a clearance-only report."""
from pathlib import Path
import threading

import pytest

from auto_agents.repair_v2 import docker, integration
from auto_agents.repair_v2.recovery import classify
from auto_agents.repair_v2.store import atomic_json
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.repair_v2.workspace import git, source_identity
from test_diagnostic_recovery_boundary import retained_submission_scene
from test_repair_v2_controller import job, controller


def test_controller_rejects_clearance_only_proof_without_candidate_retry(tmp_path, monkeypatch):
    with retained_submission_scene() as (target, _, original, _, payload, _, _, _):
        source = tmp_path / 'candidate'
        source.mkdir()
        git(source, 'init', '-q')
        (source / 'boundary_driver.py').write_text('raise AssertionError("candidate harness")\n')
        git(source, 'add', '.')
        git(source, 'commit', '-qm', 'candidate')
        calls = []

        def run(args, **kwargs):
            if args[:2] == ['docker', 'run']:
                calls.append(args)
                assert args[-2:] == ['python', '/opt/repair/boundary_driver.py']
                for name in ('boundary_driver.py', 'diagnostic_replay.py'):
                    trusted = Path(docker.__file__).with_name(name)
                    assert f'type=bind,src={trusted},dst=/opt/repair/{name},readonly' in args
                output = Path(next(arg for arg in args if arg.endswith(',dst=/result')).split(',')[1][4:])
                atomic_json(output / 'boundary.json', {
                    'ok': True, 'run_id': original.run_id, 'status': 'pending',
                    'remaining_blocked': [], 'same_blocker': False,
                })
            return 0, ''

        monkeypatch.setattr(docker, 'run', run)
        verifier = docker.DockerVerifier(tmp_path / 'verification', image='pinned')
        before = (target / '.auto-agents/state/run_state.json').read_bytes()
        result = verifier.boundary(source_identity(source), source, target, payload, threading.Event())
        assert len(calls) == 1
        assert not result['ok'] and result['proof_incomplete']
        failure = classify(result, payload)
        assert (failure['domain'], failure['owner'], failure['code']) == (
            'controller', 'controller', 'recovery_proof_incomplete')
        assert (target / '.auto-agents/state/run_state.json').read_bytes() == before
        monkeypatch.setattr(integration, '_boundary_once', lambda *args: result)
        with pytest.raises(RepairBlocked) as rejected:
            integration._boundaries(verifier, tmp_path, source_identity(source), source, payload, threading.Event())
        assert rejected.value.code == 'recovery_proof_incomplete'
        assert rejected.value.failure['recover_at'] == 'boundary_preflight'


def test_incomplete_proof_waits_for_new_controller_without_reimplementation(job):
    runner = controller(job)
    runner.preflight_boundary = True
    runner.verifier.runtime = 'old-controller'
    runner.resume_token = 'first'

    def boundary(identity, *args):
        if runner.verifier.runtime == 'old-controller':
            raise RepairBlocked('recovery_proof_incomplete', 'missing continuation proof')
        return {'ok': True, 'snapshot': identity, 'runtime': runner.verifier.runtime}

    runner.boundary = boundary
    assert runner.run()['blocker']['code'] == 'recovery_proof_incomplete'
    before = list(runner.driver.calls)
    attempts = runner.state['attempts']
    runner.resume_token = 'explicit-resume-same-controller'
    assert runner.run()['status'] == 'blocked'
    assert runner.driver.calls == before
    runner.verifier.runtime = 'corrected-controller'
    runner.resume_token = 'explicit-resume-new-controller'
    assert runner.run()['status'] == 'ready'
    assert runner.state['attempts'] == attempts
    assert runner.driver.calls[len(before):] == [('review', '')]


def test_fresh_replay_binds_submission_events_before_workflow_resume(tmp_path):
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.repair_v2.diagnostic_replay import observe_submission

    with retained_submission_scene() as (root, _, original, _, request, runtime, _, diagnosis):
        fresh = Orchestrator(root)
        try:
            submission = observe_submission(fresh, original, diagnosis, request, runtime, tmp_path / 'proof')
            assert submission['event']['subject_id'] == original.run_id
            assert submission['event']['type'] == 'repair.submitted'
            assert (root / submission['event_ref']).is_file()
        finally:
            fresh.reporter.close()
