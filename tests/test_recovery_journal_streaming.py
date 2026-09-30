"""Full journal integrity checks must fit the independent verifier's memory limit."""
import os
from pathlib import Path
import subprocess
import sys

from auto_agents.recovery import Event, KernelStore


def test_large_history_replays_with_bounded_memory(tmp_path):
    store = KernelStore(tmp_path/'control')
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
