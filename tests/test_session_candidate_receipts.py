"""Receipt admission through real Git and public child/parent recovery."""
import base64
from copy import deepcopy
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from auto_agents.config import (load_session_state, save_session_state,
                                load_project_config, save_project_config)
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
import auto_agents.session as session_module
import auto_agents.session_candidate as candidate
from test_session_verification_ownership import project, git
from test_engine_child_recovery import parent_workflow, ObservationBoundary


BINARY = b"candidate\x00\xff\r\n"


def fixture(tmp_path, monkeypatch, *, parent=False, gitlink=False):
    root, child = project(tmp_path)
    config = load_project_config(root)
    # These scenarios require cache reuse, not probabilistic audit execution.
    # Retain real lookup, source admission and evidence from executed tests.
    config.execution.acceleration.proof_audit_sample_rate = 0.0
    save_project_config(root, config)
    (root / 'obsolete.bin').write_bytes(b'old\x00bytes')
    (root / '.gitattributes').write_text('binary.dat filter=receipt\n')
    test = root / 'tests/test_owned.py'
    test.write_text(test.read_text() +
        '    assert Path("binary.dat").read_bytes() == ' + repr(BINARY) + '\n'
        '    assert Path("value.py").stat().st_mode & 0o7777 == 0o750\n'
        '    assert Path("writer-link").is_symlink()\n'
        '    assert not Path("obsolete.bin").exists()\n')
    git(root, 'add', '-A')
    if gitlink:
        # Seed a local commit identity; no submodule initialization or network.
        vendor_oid = git(root, 'rev-parse', 'HEAD').strip()
        git(root, 'update-index', '--add', '--cacheinfo', '160000', vendor_oid, 'vendor')
        test.write_text(test.read_text() +
            '    assert Path("vendor").is_dir()\n'
            '    assert list(Path("vendor").iterdir()) == []\n')
        git(root, 'add', 'tests/test_owned.py')
    git(root, 'commit', '-m', 'retain exact candidate contract')
    child.baseline_git_ref = child.baseline_head_ref = git(root, 'rev-parse', 'HEAD').strip()
    save_session_state(root, child)
    if parent:
        parent_workflow(root, child)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text('{"task_id": "task-owned"}')
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00untracked')
    paths = ['value.py', 'foreign.py', 'foreign-note.txt', 'obsolete.bin', '.git/index',
             '.git/config', '.git/HEAD', '.auto-agents/config.json', '.auto-agents/state/task_plan.json']
    if gitlink:
        (root / 'vendor').mkdir()
        (root / 'vendor/foreign.bin').write_bytes(b'foreign vendor\x00bytes')
        (root / 'vendor/foreign.bin').chmod(0o640)
        (root / 'vendor').chmod(0o751)
        vendor_mode = (root / 'vendor').stat().st_mode
        paths.append('vendor/foreign.bin')
    shared = {p: ((root / p).read_bytes(), (root / p).stat().st_mode) for p in paths}
    refs = git(root, 'show-ref')
    events = []
    def agent(self, request):
        events.append('parent' if request.purpose.startswith('collab') else 'writer')
        if gitlink:
            assert (request.cwd / 'vendor').is_dir()
            assert list((request.cwd / 'vendor').iterdir()) == []
            assert git(request.cwd, 'ls-files', '--stage', 'vendor').strip() == f'160000 {vendor_oid} 0\tvendor'
        if request.purpose.startswith('collab'):
            assert (request.cwd / 'binary.dat').read_bytes() == BINARY
            assert (request.cwd / 'value.py').stat().st_mode & 0o7777 == 0o750
            raise ObservationBoundary()
        assert request.cwd != root and not request.cwd.is_relative_to(root)
        (request.cwd / 'value.py').write_text('VALUE = 2\n')
        git(request.cwd, 'add', 'value.py')
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        (request.cwd / 'value.py').chmod(0o750)
        (request.cwd / 'binary.dat').write_bytes(BINARY)
        (request.cwd / 'writer-link').symlink_to('value.py')
        (request.cwd / 'obsolete.bin').unlink()
        reply = 'Fixed\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', agent)
    context = Session._session_gate_executor_context
    def executor(self, *args, **kwargs):
        result = context(self, *args, **kwargs)
        state = self._current_state
        if state and state.candidate_custody.get('receipt'):
            prepare = result.prepare_retained_command
            def observed(*a, **kw):
                events.append('collection' if '--collect-only' in a[0] else 'execution')
                return prepare(*a, **kw)
            result.prepare_retained_command = observed
        return result
    monkeypatch.setattr(Session, '_session_gate_executor_context', executor)
    deliver = candidate.deliver_candidate
    def delivery(*args, **kwargs):
        events.append('delivery')
        return deliver(*args, **kwargs)
    monkeypatch.setattr(candidate, 'deliver_candidate', delivery)
    def unchanged():
        assert {p: ((root / p).read_bytes(), (root / p).stat().st_mode) for p in paths} == shared
        assert git(root, 'show-ref') == refs
        if gitlink:
            assert (root / 'vendor').stat().st_mode == vendor_mode
            assert list((root / 'vendor').iterdir()) == [root / 'vendor/foreign.bin']
    return root, child, events, unchanged


def resume(root, parent=False):
    return Session(Orchestrator(root), mode='collab' if parent else 'fix', auto_approve=True).resume(
        'parent' if parent else 'owned-child')


def blocked(state, path):
    diagnostics = [e['diagnostic'] for e in state.execution_log if 'diagnostic' in e]
    diagnostics += [e['verification']['diagnostic'] for e in state.execution_log
                    if e.get('action') == 'receipt_verification' and 'diagnostic' in e['verification']]
    assert state.status != 'completed', state.to_dict()
    assert diagnostics, state.to_dict()
    diagnostic = diagnostics[-1]
    assert diagnostic['retry_fix'] is False
    assert diagnostic['session_id'] == 'owned-child'
    assert diagnostic['handoff_id'] == state.parent_handoff_id
    assert diagnostic['contract_fingerprint']
    assert diagnostic['task_scope']['task_ids'] == ['task-owned']
    assert path in diagnostic['conflicting_paths']
    assert not state.candidate_custody.get('delivered_revision')


def corrupt(root, receipt, kind):
    if kind == 'wrong-base':
        receipt['base_revision'] = receipt['source_revision']
    else:
        # An independent Git index makes a valid alternative source commit;
        # the writer's worktree and index remain exactly as frozen.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index')}
            def run(*args, data=None):
                p = subprocess.run(['git', *args], cwd=root, env=env, input=data, capture_output=True)
                assert p.returncode == 0, p.stderr
                return p.stdout.decode().strip()
            run('read-tree', receipt['source_revision'])
            path = {'extra-path-initial': 'extra.bin', 'base-path-tamper-cached-resume': 'foreign.py',
                    'missing-deletion': 'obsolete.bin', 'executable-mode': 'value.py',
                    'changed-oid': 'vendor', 'missing-link': 'vendor', 'blob-replacement': 'vendor'}[kind]
            data = b'VALUE = 1\n' if kind == 'executable-mode' else b'unrelated\x00bytes'
            oid = run('hash-object', '-w', '--stdin', data=data)
            if kind == 'missing-link':
                run('update-index', '--force-remove', path)
            elif kind == 'changed-oid':
                run('update-index', '--add', '--cacheinfo', '160000', receipt['base_revision'], path)
            else:
                run('update-index', '--add', '--cacheinfo', '100644', oid, path)
            tree = run('write-tree')
            receipt['source_revision'] = run('commit-tree', tree, '-p', receipt['base_revision'], '-m', 'alternative source')
    receipt['fingerprint'] = candidate.fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'})


def pause_after_verification(root, monkeypatch):
    with monkeypatch.context() as patch:
        def stop(*args):
            raise KeyboardInterrupt()
        patch.setattr(Session, '_run_session_persistence_action', stop)
        state = resume(root)
    assert state.status == 'paused', state.to_dict()
    records = [e for e in state.execution_log if e.get('action') == 'receipt_verification']
    assert len(records) == 1 and records[0]['verification']['ok']
    return state


@pytest.mark.parametrize('case', ['extra-path-initial', 'base-path-tamper-cached-resume',
                                 'missing-deletion', 'executable-mode', 'wrong-base'])
def test_public_receipt_rejects_unrelated_source_tree_before_verification_or_cache(tmp_path, monkeypatch, case):
    root, child, events, unchanged = fixture(tmp_path, monkeypatch)
    expected_path = {'extra-path-initial': 'extra.bin', 'base-path-tamper-cached-resume': 'foreign.py',
                     'missing-deletion': 'obsolete.bin', 'executable-mode': 'value.py', 'wrong-base': 'value.py'}[case]
    if case == 'base-path-tamper-cached-resume':
        state = pause_after_verification(root, monkeypatch)
        receipt = state.candidate_custody['receipt']
        from auto_agents.execution_binding import SessionExecutionBinding
        private_session = Session(Orchestrator(root), mode='fix', auto_approve=True)
        private = Path(state.candidate_custody['checkout'])
        private_session._execution_binding = SessionExecutionBinding.for_checkout(private_session, state, private)
        private_session.project_root = private
        private_session.orch = Orchestrator(private)
        private_session._custody_control_root = root
        private_session._current_state = state
        receipt = state.candidate_custody['receipt']
        corrupt(private, receipt, case)
        for record in state.execution_log:
            if record.get('action') == 'receipt_verification':
                record['receipt_fingerprint'] = receipt['fingerprint']
                with monkeypatch.context() as sealing:
                    # Model internally consistent old admission; remove only
                    # the new tree guard while calculating its evidence key.
                    sealing.setattr(candidate, 'validate_source', lambda *a: None, raising=False)
                    record['identity'] = candidate.verification_identity(private_session, state)
        save_session_state(root, state)
        events.clear()
    else:
        record = session_module.record_candidate
        def alter(session, state, before):
            corrupt(session.project_root, session._candidate_receipt, case)
            return record(session, state, before)
        monkeypatch.setattr(session_module, 'record_candidate', alter)
    state = resume(root)
    blocked(state, expected_path)
    assert events == ([] if case == 'base-path-tamper-cached-resume' else ['writer'])
    events.clear()
    state = resume(root)
    blocked(state, expected_path)
    assert events == []
    unchanged()


def install_filter(monkeypatch, operation):
    command = shlex.join([sys.executable, '-c',
        'import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data + b"transformed")'])
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'filter.receipt.' + operation)
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', command)


@pytest.mark.parametrize('case', ['clean-blob', 'smudge-cache', 'smudge-parent'])
def test_clean_filter_mismatch_blocks_with_owned_diagnostic(tmp_path, monkeypatch, case):
    root, child, events, unchanged = fixture(tmp_path, monkeypatch, parent=case == 'smudge-parent')
    if case == 'clean-blob':
        install_filter(monkeypatch, 'clean')
        result = resume(root)
        blocked(result, 'binary.dat')
        assert events == ['writer']
    elif case == 'smudge-cache':
        # Populate actual candidate cache and successful receipt evidence first.
        state = pause_after_verification(root, monkeypatch)
        assert 'execution' in events
        events.clear()
        install_filter(monkeypatch, 'smudge')
        result = resume(root)
        blocked(result, 'binary.dat')
        assert events == []
        # Also reach the actual gate-cache shortcut with the real earlier
        # result still eligible. Only the higher-level receipt result is absent.
        monkeypatch.delenv('GIT_CONFIG_COUNT')
        state.execution_log = [e for e in state.execution_log if e.get('action') != 'receipt_verification']
        save_session_state(root, state)
        cached_result = LocalGatePlanExecutor.cached_result
        hits, lookups = [], []
        def smudge_before_lookup(executor, command):
            if getattr(executor, 'validate_source_materialization', None) and executor.use_result_cache:
                prior = cached_result(executor, command)
                assert prior is not None and prior.ok, 'the original gate evidence must be eligible'
                hits.append(command)
                # This valid lane was admitted above. A new execution checkout
                # must be admitted too; force fresh materialization with Git's
                # real smudge filter before the next cache lookup.
                executor.close()
                install_filter(monkeypatch, 'smudge')
                lookup = executor.result_cache.lookup_with_reason
                def observed(*a, **kw):
                    lookups.append(command)
                    return lookup(*a, **kw)
                monkeypatch.setattr(executor.result_cache, 'lookup_with_reason', observed)
            return cached_result(executor, command)
        monkeypatch.setattr(LocalGatePlanExecutor, 'cached_result', smudge_before_lookup)
        result = resume(root)
        blocked(result, 'binary.dat')
        assert len(hits) == 1 and lookups == []
        assert 'writer' not in events and 'execution' not in events and 'delivery' not in events
    else:
        consume = candidate.consume_delivery
        def smudged(*args, **kwargs):
            install_filter(monkeypatch, 'smudge')
            return consume(*args, **kwargs)
        monkeypatch.setattr(candidate, 'consume_delivery', smudged)
        result = resume(root, parent=True)
        assert result.status == 'blocked', result.to_dict()
        child = load_session_state(root, 'owned-child')
        assert child.status == 'completed'
        assert 'parent' not in events
        diagnostic = result.execution_log[-1]['diagnostic']
        assert diagnostic['retry_fix'] is False
        assert diagnostic['session_id'] == child.session_id
        assert 'binary.dat' in diagnostic['conflicting_paths']
        assert not result.candidate_custody.get('consumed_delivery')
    receipt = load_session_state(root, 'owned-child').candidate_custody['receipt']
    assert base64.b64decode(receipt['manifest']['binary.dat']['postimage']['worktree']['bytes']) == BINARY
    unchanged()


@pytest.mark.parametrize('case', ['exact-tree', 'exact-tree-without-gitlink', 'interrupted-before-record', 'interrupted-receipt'])
def test_matching_receipt_tree_materializes_exact_candidate(tmp_path, monkeypatch, case):
    root, child, events, unchanged = fixture(tmp_path, monkeypatch,
        parent=case in {'exact-tree', 'exact-tree-without-gitlink'}, gitlink=case != 'exact-tree-without-gitlink')
    if case == 'interrupted-before-record':
        frozen = []
        with monkeypatch.context() as patch:
            def stop(session, state, before):
                frozen.append(deepcopy(session._candidate_receipt))
                raise KeyboardInterrupt()
            patch.setattr(session_module, 'record_candidate', stop)
            state = resume(root)
        assert state.status == 'paused' and frozen
        events.clear()
        result = resume(root)
        blocked(result, 'value.py')
        assert events == []
        assert not result.candidate_custody.get('receipt')
        assert (Path(result.candidate_custody['checkout']) / 'binary.dat').read_bytes() == BINARY
    else:
        if case == 'interrupted-receipt':
            paused = pause_after_verification(root, monkeypatch)
            receipt_id = paused.candidate_custody['receipt']['attempt_id']
            events.clear()
        if case in {'exact-tree', 'exact-tree-without-gitlink'}:
            with pytest.raises(ObservationBoundary):
                resume(root, parent=True)
        else:
            resume(root)
        result = load_session_state(root, child.session_id)
        assert result.status == 'completed', result.to_dict()
        receipt = result.candidate_custody['receipt']
        private = Path(result.candidate_custody['checkout'])
        assert git(private, 'show', ':value.py') == 'VALUE = 2\n'
        assert git(private, 'show', receipt['source_revision'] + ':value.py') == 'VALUE = 1\n'
        assert receipt['manifest']['value.py']['postimage']['worktree']['mode'] == 0o750
        assert receipt['manifest']['obsolete.bin']['postimage']['worktree']['kind'] == 'absent'
        if case == 'interrupted-receipt':
            assert receipt['attempt_id'] == receipt_id
            assert 'writer' not in events and 'execution' not in events and 'collection' not in events
        else:
            assert events.count('writer') == 1 and 'execution' in events and 'collection' in events
        if case != 'exact-tree-without-gitlink':
            identity = git(root, 'ls-tree', child.baseline_git_ref, '--', 'vendor')
            for revision in (receipt['base_revision'], receipt['source_revision'], result.candidate_custody['delivered_revision']):
                assert git(private, 'ls-tree', revision, '--', 'vendor') == identity
            assert not any(p == 'vendor' or p.startswith('vendor/') for p in receipt['manifest'])
        before = result.candidate_custody['delivered_revision']
        events.clear()
        if case in {'exact-tree', 'exact-tree-without-gitlink'}:
            with pytest.raises(ObservationBoundary):
                resume(root, parent=True)
            assert events == ['parent']
        else:
            assert resume(root).status == 'completed'
            assert events == []
        assert load_session_state(root, child.session_id).candidate_custody['delivered_revision'] == before
    unchanged()


@pytest.mark.parametrize('case', [
    'changed-oid', 'missing-link', 'blob-replacement', 'materialized-missing',
    'materialized-file', 'materialized-symlink', 'materialized-populated-directory',
    'materialized-index-oid',
])
def test_retained_gitlink_rejects_changed_identity_or_materialization(tmp_path, monkeypatch, case):
    import os

    root, child, events, unchanged = fixture(tmp_path, monkeypatch, gitlink=True)
    if not case.startswith('materialized-'):
        record = session_module.record_candidate
        def alter(session, state, before):
            corrupt(session.project_root, session._candidate_receipt, case)
            return record(session, state, before)
        monkeypatch.setattr(session_module, 'record_candidate', alter)
        result = resume(root)
        blocked(result, 'vendor')
        assert events == ['writer']
    else:
        retained = pause_after_verification(root, monkeypatch)
        assert 'execution' in events
        retained.execution_log = [e for e in retained.execution_log if e.get('action') != 'receipt_verification']
        save_session_state(root, retained)
        events.clear()
        cached_result = LocalGatePlanExecutor.cached_result
        hits, lookups = [], []
        foreign = {(p.stat().st_dev, p.stat().st_ino) for p in
                   (root / 'vendor', root / 'vendor/foreign.bin')}
        def mutate_before_lookup(executor, command):
            if getattr(executor, 'validate_source_materialization', None) and executor.use_result_cache:
                assert executor.proof_audit_sample_rate == 0.0
                if case == 'materialized-missing':
                    # Demonstrate why the former uncontrolled precondition
                    # could miss even though genuine cached evidence exists.
                    with monkeypatch.context() as audit:
                        audit.setattr(executor, 'proof_audit_sample_rate', 1.0)
                        assert cached_result(executor, command) is None
                        assert executor._cache_miss_reasons[command] == 'proof_audit_sample'
                prior = cached_result(executor, command)
                assert prior is not None and prior.ok, (
                    'real retained cache evidence must be eligible',
                    executor._cache_miss_reasons.get(command))
                hits.append(command)
                lane = executor._shared_sandboxes['receipt-admission']
                vendor = lane / 'vendor'
                assert vendor.is_dir() and list(vendor.iterdir()) == []
                if case == 'materialized-index-oid':
                    git(lane, 'update-index', '--cacheinfo', '160000', retained.candidate_custody['base_revision'], 'vendor')
                elif case == 'materialized-populated-directory':
                    (vendor / 'unexpected.bin').write_bytes(b'not the retained placeholder')
                else:
                    vendor.rmdir()
                    if case == 'materialized-file':
                        vendor.write_bytes(b'not a gitlink')
                    elif case == 'materialized-symlink':
                        vendor.symlink_to(root / 'vendor', target_is_directory=True)
                lookup = executor.result_cache.lookup_with_reason
                def observed(*args, **kwargs):
                    lookups.append(command)
                    return lookup(*args, **kwargs)
                monkeypatch.setattr(executor.result_cache, 'lookup_with_reason', observed)
                events.clear()
                # Guard the production revalidation itself. Descriptor-based
                # access must not open or chmod the foreign symlink target.
                open_fd, chmod, fchmod = os.open, os.chmod, os.fchmod
                def guarded_open(*args, **kwargs):
                    fd = open_fd(*args, **kwargs)
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) in foreign:
                        os.close(fd)
                        pytest.fail('gitlink admission opened foreign content')
                    return fd
                def guarded_fchmod(fd, mode):
                    info = os.fstat(fd)
                    assert (info.st_dev, info.st_ino) not in foreign
                    return fchmod(fd, mode)
                def guarded_chmod(path, mode, **kwargs):
                    info = os.stat(path, **{k: v for k, v in kwargs.items()
                                           if k in {'dir_fd', 'follow_symlinks'}})
                    assert (info.st_dev, info.st_ino) not in foreign
                    return chmod(path, mode, **kwargs)
                with monkeypatch.context() as guard:
                    guard.setattr(os, 'open', guarded_open)
                    guard.setattr(os, 'chmod', guarded_chmod)
                    guard.setattr(os, 'fchmod', guarded_fchmod)
                    return cached_result(executor, command)
            return cached_result(executor, command)
        monkeypatch.setattr(LocalGatePlanExecutor, 'cached_result', mutate_before_lookup)
        result = resume(root)
        blocked(result, 'vendor')
        assert len(hits) == 1 and lookups == []
        assert events == []
    events.clear()
    result = resume(root)
    blocked(result, 'vendor')
    assert events == []
    unchanged()
