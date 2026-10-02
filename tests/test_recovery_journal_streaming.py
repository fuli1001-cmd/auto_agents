"""Full journal integrity checks must fit the independent verifier's memory limit."""
import os
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys

import pytest

from auto_agents.recovery import Event, KernelStore
from auto_agents.recovery.model import KernelError


@pytest.mark.parametrize('incremental_reader', [True, False])
def test_replay_checks_multibyte_projection_beyond_first_chunk(tmp_path, monkeypatch, incremental_reader):
    store = KernelStore(tmp_path / 'control')
    store.set_meta('kernel_storage_format', 1)
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path / 'project')}))
    blob = store.put({'receipt': 'original'})
    store.apply('workflow', 1, Event('large', 'projection_saved', {
        'name': 'evidence', 'blob': blob, 'description': '回放证据' * 100_000 + '尾'}))
    final = store.apply('workflow', 2, Event('small', 'projection_saved', {
        'name': 'evidence', 'blob': blob, 'previous': blob}))
    if not incremental_reader:
        connect = store.connect
        class LegacyConnection:
            def __init__(self, db): self.execute = db.execute
        @contextmanager
        def legacy_connect():
            with connect() as db: yield LegacyConnection(db)
        monkeypatch.setattr(store, 'connect', legacy_connect)
    assert store.replay('workflow') == final
    with store.connect() as db:
        retained = db.execute("SELECT result FROM kernel_events WHERE stream='workflow' AND revision=2").fetchone()[0]
        assert len(retained.encode()) > 1024 * 1024
        changed = retained.replace('尾', '错')
        assert len(changed.encode()) == len(retained.encode())
        json.loads(changed)
        db.execute("UPDATE kernel_events SET result=? WHERE stream='workflow' AND revision=2", (changed,))
    # The final snapshot is still valid; every intermediate byte must be read.
    assert store.load('workflow') == final
    with pytest.raises(KernelError, match='Event projection does not match replay'):
        store.replay('workflow')


def test_replay_of_unknown_stream_returns_initial_state(tmp_path):
    store = KernelStore(tmp_path / 'control')
    assert store.replay('missing') == store.load('missing')


def test_large_history_replays_with_bounded_memory(tmp_path):
    store = KernelStore(tmp_path/'control')
    store.set_meta('kernel_storage_format', 1)
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path/'project')}))
    blob = store.put({'receipt': 'original'})
    payload = 'retained executor evidence ' * 80_000
    for index in range(55):
        state = store.load('workflow')
        store.apply('workflow', state['revision'], Event('projection:' + str(index), 'projection_saved', {
            'name': 'owned-evidence', 'blob': blob, 'previous': blob if index else None,
            'description': payload}))
    assert store.path.stat().st_size > 100_000_000
    script = ('import resource; from auto_agents.recovery import KernelStore; '
              'resource.setrlimit(resource.RLIMIT_AS, (160 << 20, 160 << 20)); '
              f"state=KernelStore({str(store.root)!r},readonly=True).replay('workflow'); "
              'assert state["revision"] == 56')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-P', '-c', script], cwd=root,
        env={**os.environ, 'PYTHONPATH': str(root/'src')}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-1200:]
