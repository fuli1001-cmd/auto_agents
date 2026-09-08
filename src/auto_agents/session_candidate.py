"""Attribute fix edits to one isolated provider workspace before publishing."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from auto_agents import artifact_temp as tempfile

from .gate_execution import discover_dependency_links, install_dependency_links
from .git_ops import head_ref
from .root_cause import RootCauseCoordinator
from .session_verification import SessionOwnershipError, candidate_snapshot, product_path


@contextmanager
def candidate_request(session, state, request):
    if request.purpose != 'fix' or not state.verification_binding:
        yield request
        return
    from .workflow_runtime import _copy_path, _remove_path

    root = session.project_root
    before = candidate_snapshot(session.orch)
    protected = set(state.protected_preexisting_paths) | (set(before) - set(state.candidate_paths))
    session._candidate_attempt_paths = []
    with tempfile.TemporaryDirectory(prefix='auto-agents-fix-candidate-') as temporary:
        candidate = Path(temporary) / 'project'
        RootCauseCoordinator._copy_diagnostic_tree(root, candidate, include_private=True)
        install_dependency_links(candidate, discover_dependency_links(root))
        observer = SimpleNamespace(project_root=candidate)
        observer._worktree_change_snapshot = lambda: type(session.orch)._worktree_change_snapshot(observer)
        candidate_before = observer._worktree_change_snapshot()
        source_head = head_ref(candidate)
        # Continuations refer to a different physical workspace. Resume via
        # the durable transcript, never via a provider's old writable checkout.
        yield replace(request, cwd=candidate, resume_session_id='', resume_provider='',
                      prompt_is_continuation=False, prompt_continuation='')
        if head_ref(candidate) != source_head:
            raise SessionOwnershipError('isolated candidate changed its Git checkpoint')
        delta = [path for path in session.orch._snapshot_delta_paths(
            candidate_before, observer._worktree_change_snapshot()) if product_path(path)]
        current = candidate_snapshot(session.orch)
        conflicts = [path for path in delta
                     if path in protected or before.get(path) != current.get(path)]
        if conflicts:
            raise SessionOwnershipError('candidate ownership is ambiguous: ' + ', '.join(sorted(conflicts)))
        for path in delta:
            _remove_path(root / path)
            _copy_path(candidate / path, root / path)
        session._candidate_attempt_paths = delta
