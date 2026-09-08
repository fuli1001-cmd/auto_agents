from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from auto_agents.repair_control import Store, Supervisor
from auto_agents.run_lock import ProjectRunLock
from test_repair_control import configuration, failure, registration


def blocked_registration(tmp_path):
    supervisor = Supervisor(configuration(tmp_path))
    lock = ProjectRunLock(tmp_path / "project", environ={}).acquire()
    payload = registration(lock.project_root, lock.run_token)
    request = {"payload": payload, "_peer_pid": os.getpid()}
    subscriber = supervisor.register(request, [os.dup(lock.fileno)])["subscriber"]
    job = supervisor.store.submit(subscriber, failure(lock.project_root))
    supervisor.store.transition(job, "blocked", {"ok": False, "error": "provider timeout"})
    with supervisor.store.connect() as db:
        db.execute("UPDATE subscribers SET state='blocked' WHERE id=?", (subscriber,))
    supervisor.tick()
    assert subscriber not in supervisor.registrations
    supervisor.register(request, [os.dup(lock.fileno)])
    return supervisor, lock, subscriber, job


def close_registration(supervisor, lock):
    for registered in supervisor.registrations.values():
        for fd in registered["fds"]:
            os.close(fd)
    supervisor.registrations.clear()
    lock.release()


def test_only_owner_status_consumes_terminal_registration(tmp_path):
    supervisor, lock, subscriber, job = blocked_registration(tmp_path)
    try:
        supervisor.tick()
        observer = {"version": 1, "op": "status", "subscriber": subscriber, "_peer_pid": -1}
        supervisor.dispatch(observer, [])
        supervisor.tick()
        assert subscriber in supervisor.registrations
        response = supervisor.dispatch({**observer, "_peer_pid": os.getpid()}, [])
        assert subscriber in response["registered"]
        assert response["job"]["state"] == "blocked"
        supervisor.tick()
        assert subscriber not in supervisor.registrations
        assert supervisor.store.job(job)["result"]["error"] == "provider timeout"
        assert supervisor.store.subscriptions(job)[0]["state"] == "blocked"
    finally:
        close_registration(supervisor, lock)


@pytest.mark.parametrize("cancelled", [False, True])
def test_terminal_registration_does_not_outlive_owner_or_cancellation(tmp_path, monkeypatch, cancelled):
    supervisor, lock, subscriber, job = blocked_registration(tmp_path)
    try:
        if cancelled:
            supervisor.store.cancel(job=job)
        else:
            monkeypatch.setattr("auto_agents.repair_control.alive", lambda pid, ticks: False)
        supervisor.tick()
        assert subscriber not in supervisor.registrations
    finally:
        close_registration(supervisor, lock)


@pytest.mark.parametrize("job_state,subscriber_state,exit_code", [
    ("blocked", "blocked", 3),
    ("completed", "blocked", 3),
    ("completed", "finished", 0),
])
def test_legacy_runtime_exits_and_releases_real_project_lock(
    tmp_path, job_state, subscriber_state, exit_code,
):
    # Reproduce the old runtime's registration-first loop in another process.
    # Every RPC is followed by the real controller tick, including the cleanup
    # between re-registration and the next status request that caused the hang.
    script = textwrap.dedent("""
        import os
        from pathlib import Path
        import sys
        from auto_agents.repair_control import Supervisor
        from auto_agents.run_lock import ProjectRunLock
        from test_repair_control import configuration, failure, registration

        root = Path(sys.argv[1])
        job_state, subscriber_state = sys.argv[2:4]
        supervisor = Supervisor(configuration(root))
        code = 11
        with ProjectRunLock(root / 'project', environ={}) as lock:
            payload = registration(lock.project_root, lock.run_token)
            request = {'payload': payload, '_peer_pid': os.getpid()}
            subscriber = supervisor.register(request, [os.dup(lock.fileno)])['subscriber']
            job = supervisor.store.submit(subscriber, failure(lock.project_root))
            supervisor.store.transition(job, job_state, {'error': 'original provider timeout'})
            with supervisor.store.connect() as db:
                db.execute('UPDATE subscribers SET state=? WHERE id=?', (subscriber_state, subscriber))
            supervisor.tick()
            for attempt in range(3):
                response = supervisor.dispatch({'version': 1, 'op': 'status',
                    'subscriber': subscriber, '_peer_pid': os.getpid()}, [])
                supervisor.tick()
                if subscriber not in response.get('registered', []):
                    supervisor.register(request, [os.dup(lock.fileno)])
                    supervisor.tick()
                    continue
                if response['subscribers'][0]['state'] == 'finished':
                    code = 0
                    break
                if (response['job']['state'] in {'blocked', 'cancelled'}
                        or response['subscribers'][0]['state'] in {'blocked', 'cancelled'}):
                    code = 3
                    break
            assert not supervisor.registrations
        raise SystemExit(code)
    """)
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), job_state, subscriber_state],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(repository / "src"), str(repository / "tests")])},
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    store = Store(tmp_path / "control")
    subscriber = store.subscriptions()[0]
    assert subscriber["state"] == subscriber_state
    assert store.job(subscriber["job"])["result"]["error"] == "original provider timeout"
    with ProjectRunLock(tmp_path / "project", environ={}):
        pass
