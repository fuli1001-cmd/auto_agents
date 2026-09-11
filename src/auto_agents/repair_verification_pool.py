"""Stable, job-local verification paths; no proof crosses an input snapshot."""
from contextlib import contextmanager
import fcntl
import math
import os
import json
from pathlib import Path

from .git_ops import add_worktree
from .repair_control import atomic_json, digest
from .verification_ledger import source_identity, repository_identity


@contextmanager
def locked(path):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def prepare_pool(runner, workspace, commands, timings, environment):
    continuous = getattr(runner, '_continuous_workspace', None)
    if not continuous:
        return None
    binding = getattr(runner, '_repair_control_binding', None) or {}
    source = source_identity(workspace)
    identity = {'version': 1, 'source': source, 'environment': environment,
                'repository': repository_identity(workspace), 'job': binding.get('job'),
                'generation': binding.get('generation')}
    root = Path(continuous) / 'verification-pool' / digest(identity)
    if any(p.is_symlink() for p in [root, root.parent]):
        raise RuntimeError('verification pool must not be a symlink')
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / 'assignments.json'
    with locked(root / 'pool.lock'):
        if receipt.is_symlink():
            raise RuntimeError('verification assignments must not be a symlink')
        assignments = json.loads(receipt.read_text()) if receipt.exists() else {}
        loads = [0.0, 0.0]
        def cost(command):
            value = timings.get(command, {}).get('seconds', 60)
            return value if isinstance(value, (float, int)) and math.isfinite(value) and value >= 0 else 60
        for command in commands:
            if command in assignments:
                slot = assignments[command]
                if type(slot) is not int or slot not in (0, 1):
                    raise RuntimeError('invalid verification pool assignment')
                loads[slot] += cost(command)
        for command in sorted(commands, key=cost, reverse=True):
            if command not in assignments:
                slot = min(range(2), key=lambda index: loads[index])
                assignments[command] = slot
                loads[slot] += cost(command)
        atomic_json(receipt, assignments)
    return {'root': root, 'assignments': assignments, 'source': source, 'repository': identity['repository']}


def run_in_pool(runner, workspace, shard, pool):
    slot = pool['assignments'][shard.command]
    root = pool['root'] / ('slot-' + str(slot))
    with locked(pool['root'] / ('slot-' + str(slot) + '.lock')):
        if root.is_symlink():
            raise RuntimeError('verification worktree must not be a symlink')
        if not root.exists():
            from .git_ops import head_ref
            with runner._shard_worktree_lock:
                add_worktree(workspace, root, ref=head_ref(workspace))
        if repository_identity(root) != pool['repository'] or source_identity(root) != pool['source']:
            # Never reset a retained tree, which could still hold failure evidence.
            # A changed or interrupted workspace cannot supply a cached proof.
            return runner._execute_full_suite_shard(workspace, shard)
        return runner._execute_full_suite_shard_command(root, shard)
