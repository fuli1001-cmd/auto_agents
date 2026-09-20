"""Refresh retained code before any attempt to implement its historical plan."""
from pathlib import Path
from contextlib import nullcontext
import re
import subprocess

from .store import digest
from .types import RepairBlocked
from .workspace import git, inventory, source_identity


def prepare(controller, repository, revision, *, force_check=False, locked=False):
    with nullcontext() if locked else controller.store.locked():
        state = controller.store.load()
        if not state or state['status'] in ('waiting_user', 'skipped', 'complete'):
            return False
        if state['request_digest'] != digest(controller.request.to_dict()):
            raise RepairBlocked('request_changed', 'source refresh differs from the frozen repair contract')
        controller.state = state
        root = controller.workspace.prepare()
        pending = state.get('source_refresh_pending')
        if pending and revision != pending['parent']:
            controller.checkpoint(source_refresh_next={'repository': str(repository), 'revision': revision})
        conflicts = git(root, 'diff', '--name-only', '--diff-filter=U').splitlines()
        if conflicts:
            # Keep failed external-correction/import merges owned by their
            # existing recovery protocol; never commit conflict markers here.
            if not pending:
                raise RepairBlocked('source_merge_conflict', '保留候选存在未解决的合并冲突：' + ', '.join(conflicts))
            return False
        if not pending:
            git(root, 'fetch', '--quiet', str(repository), revision)
            parent = git(root, 'rev-parse', 'FETCH_HEAD^{commit}')
            contained = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', parent, 'HEAD'],
                                       capture_output=True).returncode == 0
            dirty = bool(git(root, 'status', '--porcelain'))
            active_call = state.get('active_call') or {}
            unchecked = (state.get('phase') == 'implement' and active_call.get('role') == 'implement'
                         and active_call.get('source') != source_identity(root))
            if contained and not dirty and not force_check and not unchecked:
                return False
            if state.get('blocker', {}).get('code') == 'no_progress' and not state.get('external_correction'):
                return False
            before = source_identity(root)
            controller.workspace.checkpoint()
            pending = {'parent': parent, 'source_before': before,
                       'candidate_before': git(root, 'rev-parse', 'HEAD'),
                       'previous_phase': state.get('phase'), 'previous_failures': state.get('failures', [])}
            controller.checkpoint(source_refresh_pending=pending)
        try:
            git(root, 'merge', '--no-edit', pending['parent'])
        except subprocess.CalledProcessError:
            conflicts = git(root, 'diff', '--name-only', '--diff-filter=U').splitlines()
            if not conflicts:
                raise
        parents = list(dict.fromkeys([*state.get('integration_parents', []), pending['parent']]))
        if conflicts:
            controller.checkpoint(status='active', phase='source_conflicts', blocker={},
                                  source_conflicts=conflicts, integration_parents=parents,
                                  test_preservation_findings=[])
            controller.store.event('source_refresh_conflicts', parent=pending['parent'], paths=conflicts)
        else:
            finish(controller, root, pending, parents)
            continue_queued(controller)
        return True


def finish(controller, root, pending, parents):
    refreshed = {**pending, 'source_after': source_identity(root), 'resume_token': controller.resume_token}
    controller.checkpoint(status='active', phase='audit', blocker={}, source_conflicts=[],
                          source_refresh_pending=None, source_refreshed=refreshed,
                          source_refresh_check_pending=True, integration_parents=parents,
                          test_preservation_findings=[])
    controller.store.event('source_refreshed', **refreshed)


def resolve_conflicts(controller, root):
    conflicts = controller.state.get('source_conflicts', [])
    if not conflicts:
        return
    if not controller.allow_implementation or controller.state.get('external_correction'):
        raise RepairBlocked('source_merge_conflict', '当前策略不允许调用模型解决保留候选的合并冲突。')
    controller.phase('source_conflicts')
    before = inventory(root)
    prompt = ('Resolve ONLY the current Git merge conflicts in the listed files. Preserve the current engine '
              'changes and the retained candidate changes; inspect both merge parents. Do not implement the '
              'historical repair plan, add features, edit other files, or write Git metadata. The controller '
              'will recheck the original recovery boundary immediately after this merge.\n'
              + controller.context() + '\nConflicting paths:\n' + '\n'.join(conflicts))
    controller.agent('implement', prompt, root)
    after = inventory(root)
    touched = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    if touched - set(conflicts):
        raise RepairBlocked('source_merge_scope', '合并修改超出了冲突文件，候选保留但不交付。')
    for name in conflicts:
        path = Path(root) / name
        if path.is_file() and re.search(rb'^(<<<<<<< |>>>>>>> )', path.read_bytes(), re.MULTILINE):
            raise RepairBlocked('source_merge_conflict', '合并冲突尚未解决：' + name)
    controller.workspace.checkpoint()
    pending = controller.state['source_refresh_pending']
    git(root, 'merge-base', '--is-ancestor', pending['parent'], 'HEAD')
    controller.checkpoint(attempts=controller.state['attempts'] + 1)
    finish(controller, root, pending, controller.state['integration_parents'])
    continue_queued(controller)


def continue_queued(controller):
    following = controller.state.get('source_refresh_next')
    if following:
        controller.checkpoint(source_refresh_next=None)
        prepare(controller, following['repository'], following['revision'], locked=True)


def probe(controller):
    if not controller.state.get('source_refresh_check_pending'):
        return
    controller.phase('source_recheck')
    identity, snapshot = controller.workspace.freeze()
    controller.checkpoint(snapshot=identity, snapshot_path=str(snapshot))
    if controller.boundary is None:
        controller.checkpoint(source_refresh_check_pending=False, phase='audit')
        return
    observed = controller.boundary(identity, snapshot, controller.cancel)
    if controller.cancel.is_set():
        raise KeyboardInterrupt()
    if observed.get('snapshot') != identity or source_identity(snapshot) != identity:
        raise RepairBlocked('snapshot_changed', 'refreshed recovery proof belongs to another source')
    reference = controller.store.artifact('source-recheck', observed)
    controller.checkpoint(source_recheck=reference)
    if observed.get('infrastructure'):
        raise RepairBlocked('verification_infrastructure', observed.get('reason') or '原任务恢复验证环境不可用。')
    controller.checkpoint(source_refresh_check_pending=False, failures=[] if observed.get('ok') else [{
        'unit': 'original-boundary', 'reason': 'current engine still fails the original recovery boundary',
        'observed': observed.get('observed', observed)}], phase='audit' if observed.get('ok') else 'implement')
    if observed.get('ok'):
        controller._refreshed_boundary = observed
        controller._refreshed_boundary_runtime = getattr(controller.verifier, 'runtime', '')
        if not controller.state.get('plan'):
            controller.checkpoint(plan=controller.store.artifact('plan', {
                'text': 'The refreshed source restores the retained boundary. Verify existing requirements without new implementation.',
                'request': controller.state['request_digest']}))
    controller.store.event('source_rechecked', snapshot=identity, ok=bool(observed.get('ok')))
