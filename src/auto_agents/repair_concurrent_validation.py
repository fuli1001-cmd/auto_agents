"""Review always finishes; expanded checks stop when that review rejects.

Only the foreground reviewer writes experiment state. The verification worker
owns a snapshot of mutable runner feedback and merges it after all children exit.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from copy import copy, deepcopy
import threading
import shutil
from pathlib import Path


verification_cancel = ContextVar('repair_verification_cancel', default=None)


def cancelled():
    event = verification_cancel.get()
    return event is not None and event.is_set()


def cancellation_result(command=''):
    from .self_repair import _VerificationResult
    return _VerificationResult(False, 'expanded verification cancelled after review rejection',
        commands=(command,) if command else (), returncodes=(130,) if command else (),
        termination_reasons=('cancelled',) if command else (),
        payload={'cancelled': True, 'source_commands': [command] if command else [],
                 'failure_evidence': [], 'command_timings': []})


def review_and_verify(runner, workspace, review_call):
    from .verification_ledger import source_identity
    from .repair_memory import remember_check_timings
    worker = copy(runner)
    for name, value in runner.__dict__.items():
        if name.startswith(('_candidate_', '_pending_', '_verification_dependency_')) and isinstance(value, (dict, list, set)):
            setattr(worker, name, deepcopy(value))
    worker._experiment = deepcopy(runner._experiment)
    worker._feedback_lock = threading.Lock()
    worker._verification_cleanup_incomplete = False
    original_failures = len(getattr(worker, '_candidate_failure_evidence', []))
    before = source_identity(workspace)
    environment = runner._full_suite_environment_fingerprint()
    event = threading.Event()

    def verify():
        from . import artifact_temp as tempfile
        from .git_ops import add_worktree, remove_worktree, head_ref
        token = verification_cancel.set(event)
        try:
            # Even a serial fallback or a shell preparation command must not
            # mutate the reviewer's checkout or its repository guard.
            temporary = tempfile.mkdtemp(prefix='repair-expanded-')
            try:
                isolated = Path(temporary) / 'verification'
                with runner._shard_worktree_lock:
                    add_worktree(workspace, isolated, ref=head_ref(workspace))
                try:
                    result = worker._run_active_group_verification(isolated, record=False)
                    worker._verification_cleanup_incomplete |= bool(result.payload.get('cleanup_incomplete'))
                    return result
                finally:
                    if not worker._verification_cleanup_incomplete:
                        with runner._shard_worktree_lock:
                            remove_worktree(workspace, isolated, force=True)
            finally:
                if not worker._verification_cleanup_incomplete:
                    shutil.rmtree(temporary, ignore_errors=True)
        except (OSError, RuntimeError):
            if event.is_set():
                return cancellation_result()
            raise
        finally:
            verification_cancel.reset(token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(copy_context().run, verify)
        try:
            review = review_call()
        except BaseException:
            event.set()
            # The executor joins before the caller can modify/remove a worktree.
            raise
        if not review.ok:
            event.set()
        expanded = future.result()

    for name in ('_candidate_verified_check_ids', '_candidate_prepared_dependencies',
                 '_pending_prepared_dependencies', '_verification_dependency_attempts'):
        setattr(runner, name, set(getattr(runner, name, set())) | set(getattr(worker, name, set())))
    runner._candidate_failure_evidence = [*getattr(runner, '_candidate_failure_evidence', []),
        *getattr(worker, '_candidate_failure_evidence', [])[original_failures:]]
    plan = getattr(worker, '_candidate_verification_plan', {'commands': [], 'requests': []})
    runner._candidate_verification_plan = plan
    if source_identity(workspace) != before or runner._full_suite_environment_fingerprint() != environment:
        expanded.ok = False
        expanded.summary += '\nsource or environment changed during concurrent validation; revalidation required'
        expanded.payload['outcome'] = 'invalid'
        runner._candidate_verified_check_ids = set()
    expanded.payload['concurrent_review'] = True
    expanded.payload['review_rejected'] = not review.ok
    if worker._verification_cleanup_incomplete:
        expanded.ok = False
        expanded.payload['cleanup_incomplete'] = True
        expanded.summary += '\nverification process cleanup incomplete; stop before further candidate changes'
    remember_check_timings(runner, workspace, runner._candidate_group, plan, expanded, phase='expanded')
    return expanded, review
