from pathlib import Path
import os
import subprocess
import sys

import pytest

from auto_agents.recovery import Event, KernelError, KernelStore
from auto_agents.recovery import release
from auto_agents.recovery.upgrade import MANDATORY_CHECKS, independent_verify
from test_recovery_upgrade import artifact


@pytest.mark.parametrize(('size', 'expected'), [(0, 120), (1, 121), (16 << 20, 121),
                                               (9 << 30, 696), (100 << 30, 1800)])
def test_journal_timeout_scales_with_history_and_has_a_ceiling(size, expected):
    assert release.journal_replay_timeout(size) == expected


@pytest.fixture
def journal(tmp_path):
    store = KernelStore(tmp_path / 'control')
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path / 'project')}))
    runtime = artifact(tmp_path, store, 'candidate')
    store.set_meta('trusted_verifier', 'd' * 64)
    receipt = independent_verify(store, runtime, {
        name: lambda _: {'ok': True, 'image': 'sha256:' + 'a' * 64} for name in MANDATORY_CHECKS}, 'd' * 64)
    return store, runtime, receipt


def replay_log(command):
    mount = next(arg for arg in command if arg.startswith('type=bind,src=') and 'dst=/state,' in arg)
    return Path(mount.split('src=', 1)[1].split(',dst=', 1)[0]) / 'replay.log'


def test_journal_script_replays_snapshot_and_seals_progress(journal, monkeypatch):
    store, runtime, receipt = journal
    def run(command, *, timeout, output, observation):
        assert timeout >= 120
        assert command[command.index('--memory') + 1] == '1g'
        script = command[-1].replace("KernelStore('/state'", f'KernelStore({str(replay_log(command).parent)!r}')
        result = subprocess.run([sys.executable, '-P', '-c', script], capture_output=True, text=True, timeout=10,
                                env={**os.environ, 'PYTHONPATH': str(Path(release.__file__).resolve().parents[2])})
        output.write_text(result.stdout + result.stderr)
        observation.update(termination='', exit_code=result.returncode)
        return result.returncode, result.stdout + result.stderr
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    updated = release.verify_current_journal(store, runtime, receipt)
    proof = store.read(updated['checks']['journal_replay'])
    assert updated['state_frontier'] == store.frontier()
    assert proof['execution']['database_bytes'] > 0
    assert proof['execution']['termination'] == ''
    log = store.read_bytes(proof['log']).decode()
    assert 'Journal replay started' in log and 'Replaying stream: workflow' in log
    assert log.splitlines()[-1] == store.frontier()


@pytest.mark.parametrize(('termination', 'code', 'text', 'diagnostic'), [
    ('timeout', 130, '', 'replay timed out'),
    ('', 130, '', 'cannot replay'),
    ('', 1, 'journal integrity failure', 'cannot replay'),
    ('', 0, 'wrong frontier', 'cannot replay'),
])
def test_journal_failure_records_cause_without_issuing_proof(journal, monkeypatch, termination, code, text, diagnostic):
    store, runtime, receipt = journal
    before = store.meta('verified_upgrades')
    def run(command, *, timeout, output, observation):
        observation.update(termination=termination, exit_code=code)
        output.write_text(text)
        return code, text
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    with pytest.raises(KernelError, match=diagnostic) as failure:
        release.verify_current_journal(store, runtime, receipt)
    details = failure.value.details
    assert details['returncode'] == code
    assert details['termination'] == termination
    assert details['timeout_seconds'] >= 120
    assert store.meta('verified_upgrades') == before
    if termination:
        assert 'Journal replay stopped:' in store.read_bytes(details['log_ref']).decode()


def test_history_advance_during_replay_blocks_cutover(journal, monkeypatch):
    store, runtime, receipt = journal
    before = store.meta('verified_upgrades')
    def run(command, *, timeout, output, observation):
        frontier = store.frontier()
        store.apply('workflow', 1, Event('pause', 'workflow_stopped', {'status': 'paused'}))
        output.write_text(frontier)
        return 0, frontier
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    with pytest.raises(KernelError, match='Business history changed'):
        release.verify_current_journal(store, runtime, receipt)
    assert store.meta('verified_upgrades') == before
