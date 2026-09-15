"""Exercise write confinement through real public resume, including collection."""
import json
import socketserver
import sys
import threading

import pytest

from test_session_verification_ownership import project, run_session, git


def shared_environment(tmp_path):
    root, state = project(tmp_path)
    prefix = tmp_path / 'shared-prefix'
    (prefix / 'bin').mkdir(parents=True)
    (prefix / 'bin/python').symlink_to(sys.executable)
    hook = prefix / 'etc/conda/activate.d/context.sh'
    hook.parent.mkdir(parents=True)
    hook.write_text('original environment')
    hook.chmod(0o640)
    (root / '.conda').unlink()
    (root / '.conda').symlink_to(prefix, target_is_directory=True)
    test = root / 'tests/test_owned.py'
    test.write_text('''from pathlib import Path
import errno, os, socket, tempfile
def metadata_controls():
    with tempfile.NamedTemporaryFile() as private:
        p=Path(private.name); p.chmod(0o750)
        assert p.stat().st_mode & 0o777 == 0o750
        os.fchmod(private.fileno(),0o640)
        assert p.stat().st_mode & 0o777 == 0o640
    for name in ['.conda/etc/conda/activate.d/context.sh', HOOK]:
        p=Path(name); before,mode=p.read_bytes(),p.stat().st_mode
        fd=os.open(p,os.O_RDONLY)
        try:
            for action in (lambda: os.fchmod(fd,0o777),lambda: os.chmod('/proc/self/fd/'+str(fd),0o777)):
                try: action()
                except OSError as error: assert error.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
                else: raise AssertionError('shared descriptor was writable')
        finally: os.close(fd)
        assert p.read_bytes()==before and p.stat().st_mode==mode
metadata_controls()
# This executes during collection, before any test body or writer.
for p in [Path('.conda/etc/conda/activate.d/context.sh'), Path(HOOK)]:
    assert p.read_text() == 'original environment'
    for action in [lambda: p.write_text('foreign write'), lambda: p.chmod(0o777), lambda: p.unlink()]:
        try:
            action()
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
        else:
            raise AssertionError('shared environment was writable')
assert os.environ['CONFINEMENT_TEST_INPUT'] == 'retained'
with tempfile.TemporaryFile() as f:
    f.write(b'private scratch remains writable')
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
with socket.create_connection(('127.0.0.1', int(os.environ['CONFINEMENT_TEST_PORT'])), timeout=5) as sock:
    assert sock.recv(20) == b'retained service'
def test_owned(tmp_path):
    metadata_controls()
    (tmp_path / 'result').write_text('owned temporary fixture')
    assert 'VALUE = 1' in Path('value.py').read_text()
'''.replace('HOOK', repr(str(hook))))
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retained shared environment proof')
    # Update the fixture's retained baseline, before the first public resume.
    from auto_agents.config import save_session_state
    from auto_agents.git_ops import head_ref
    state.baseline_head_ref = state.baseline_git_ref = head_ref(root)
    save_session_state(root, state)
    return root, hook


@pytest.mark.parametrize('nested', [False, True])
def test_public_resume_confines_shared_inputs_during_collection_and_execution(tmp_path, monkeypatch, nested):
    root, hook = shared_environment(tmp_path)
    monkeypatch.setenv('CONFINEMENT_TEST_INPUT', 'retained')
    if nested:
        monkeypatch.setenv('AUTO_AGENTS_VERIFICATION_SANDBOX', '1')
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b'retained service')
    with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
        monkeypatch.setenv('CONFINEMENT_TEST_PORT', str(server.server_address[1]))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result, calls, _ = run_session(root, monkeypatch)
            assert result.status == 'completed', result.to_dict()
            assert calls == ['fix']
            repeated, calls, _ = run_session(root, monkeypatch)
            assert repeated.status == 'completed' and calls == []
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result.status == 'completed', result.to_dict()
    assert hook.read_text() == 'original environment' and hook.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('failure', ['unavailable', 'collection-private_chmod',
                                    'collection-private_fchmod', 'execution-private_chmod',
                                    'execution-private_fchmod'])
def test_public_resume_blocks_validation_if_confinement_unavailable(tmp_path, monkeypatch, failure):
    if failure != 'unavailable':
        # Keep the retained public parameter IDs and the original direct-node
        # case, then exercise the additional command-contract boundary.
        for variant in ('direct', 'command' if failure.startswith('collection-') else 'partial'):
            directory = tmp_path / variant
            directory.mkdir()
            with monkeypatch.context() as isolated:
                _public_metadata_preflight_failure(directory, isolated, failure, variant=variant)
        return
    root, hook = shared_environment(tmp_path)
    monkeypatch.setenv('CONFINEMENT_TEST_INPUT', 'retained')
    from auto_agents import verification_sandbox
    original = verification_sandbox.shutil.which
    monkeypatch.setattr(verification_sandbox.shutil, 'which',
                        lambda name, *a, **kw: None if name == 'codex' else original(name, *a, **kw))
    from auto_agents import gate_execution
    launched = []
    def forbidden(*args, **kwargs):
        launched.append(True)
        raise AssertionError('collection or execution started without confinement')
    monkeypatch.setattr(gate_execution, 'run_supervised_shell_command', forbidden)
    result, calls, _ = run_session(root, monkeypatch)
    assert result.status != 'completed' and calls in ([], ['fix'])
    assert hook.read_text() == 'original environment'
    diagnostic = json.dumps(result.to_dict()).lower()
    assert 'sandbox' in diagnostic or 'confinement' in diagnostic
    assert not launched
    custody, attempt = result.candidate_custody, result.current_attempt
    for _ in range(2):
        repeated, calls, _ = run_session(root, monkeypatch)
        assert repeated.status != 'completed' and calls == []
        assert repeated.candidate_custody == custody and repeated.current_attempt == attempt
        assert not repeated.candidate_custody.get('delivered_revision')
        assert not any(e.get('verification', {}).get('ok') for e in repeated.execution_log
                       if e.get('action') == 'receipt_verification')
    assert not launched
    assert hook.read_text() == 'original environment' and hook.stat().st_mode & 0o777 == 0o640


def _public_metadata_preflight_failure(tmp_path, monkeypatch, failure, *, variant='direct'):
    from copy import deepcopy
    from auto_agents import gate_execution, verification_sandbox
    from auto_agents.config import load_session_state, save_session_state
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from execution_marker import ExecutionMarker
    from test_engine_child_recovery import configure_local_writer, REAL_PROVIDER_CALL

    root, hook = shared_environment(tmp_path)
    child = load_session_state(root, 'owned-child')
    body = ExecutionMarker(tmp_path / 'candidate-body')
    test = root / 'tests/test_owned.py'
    test.write_text(test.read_text() + '    ' + body.source("'executed'") + '\n')
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')")
    # Exercise the targeted-command catch after the real required collection.
    child.fix_verify_command = 'python -m pytest -q tests/test_owned.py::test_owned'
    second_command = 'python -m pytest -q tests/test_owned.py::test_second'
    if variant != 'direct':
        from auto_agents.config import load_project_config
        from test_session_verification_ownership import _retain_contract
        config = load_project_config(root)
        config.gates.steps = []
        config.gates.parallel_groups = []
        config.gates.commands = [child.fix_verify_command]
        if variant == 'partial':
            test.write_text(test.read_text() + '\ndef test_second():\n    raise AssertionError("refused body executed")\n')
            config.gates.commands.append(second_command)
            # Both executions must be in the same gate call. An earlier
            # targeted call would conceal lost partial-plan accounting.
            child.fix_verify_command = ''
        plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Owned command preflight',
                          'requirement_ids': ['REQ-owned'],
                          'verification_refs': ['cmd:' + command for command in config.gates.commands]}],
                'verification_steps': []}
        _retain_contract(root, child, config, plan)
    save_session_state(root, child)
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
    child.workflow_id = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id)).workflow_id
    save_session_state(root, child)
    paths = [root / p for p in ('value.py', 'foreign.py', '.git/index', '.git/config',
                               '.git/HEAD', '.auto-agents/state/task_plan.json')]
    paths.append(hook)
    def image():
        return [(p.read_bytes(), p.stat().st_mode) for p in paths], git(root, 'show-ref')
    before = image()
    writers, launched, injected = [], [], []
    def provider(self, request):
        writers.append(request.purpose)
        assert request.writer_boundary is not None
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    target_phase, operation = failure.split('-private_')
    active_phase = None
    active_command = None
    run = gate_execution.LocalGatePlanExecutor._run_command
    def observed_run(self, command, **kwargs):
        nonlocal active_phase, active_command
        active_phase = 'collection' if '--collect-only' in command else 'execution'
        active_command = command
        try:
            return run(self, command, **kwargs)
        finally:
            active_phase = None
            active_command = None
    monkeypatch.setattr(gate_execution.LocalGatePlanExecutor, '_run_command', observed_run)
    probe = verification_sandbox.metadata_probe_command
    def denied_probe(*args, **kwargs):
        argv = probe(*args, **kwargs)
        if active_phase == target_phase and (variant != 'partial' or active_command == second_command):
            injected.append(active_phase)
            i = argv.index('-c') + 1
            argv[i] = ("import os,errno\n"
                       "def denied(*a,**k): raise PermissionError(errno.EPERM,'private metadata denied')\n"
                       "os." + operation + "=denied\n" + argv[i])
        return argv
    monkeypatch.setattr(verification_sandbox, 'metadata_probe_command', denied_probe)
    dispatch = gate_execution.run_supervised_shell_command
    def observed_dispatch(*args, **kwargs):
        launched.append(active_phase)
        return dispatch(*args, **kwargs)
    monkeypatch.setattr(gate_execution, 'run_supervised_shell_command', observed_dispatch)
    monkeypatch.setenv('CONFINEMENT_TEST_INPUT', 'retained')
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b'retained service')
    with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
        monkeypatch.setenv('CONFINEMENT_TEST_PORT', str(server.server_address[1]))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def resume():
                return Session(Orchestrator(root), mode='fix', auto_approve=True).resume('owned-child')
            result = resume()
            assert result.status in {'failed', 'blocked'}, result.to_dict()
            record = next(e['verification'] for e in result.execution_log
                          if e.get('action') == 'receipt_verification')
            diagnostic = record['diagnostic']
            assert record['retry_fix'] is False and diagnostic['retry_fix'] is False
            assert record['failure_kind'] == 'verification_confinement'
            assert diagnostic['phase'] == 'verification_preflight'
            assert diagnostic['launcher_protocol'] == '--metadata'
            assert diagnostic['supervisor_version'] == 1 and diagnostic['errno'] == 1
            assert diagnostic['session_id'] == child.session_id
            assert diagnostic['workflow_id'] == child.workflow_id
            assert diagnostic['contract_fingerprint'] == result.verification_binding['contract_fingerprint']
            assert diagnostic['owners'] and 'task-owned' in json.dumps(diagnostic['owners'])
            assert diagnostic['task_ids'] == ['task-owned']
            assert diagnostic['requirement_ids'] == ['REQ-owned']
            assert 'REQ-owned' in json.dumps(diagnostic['owners'])
            assert 'tests/test_owned.py' in diagnostic['command']
            assert ('--collect-only' in diagnostic['command']) == (target_phase == 'collection')
            if variant != 'direct':
                expected_command = second_command if variant == 'partial' else child.fix_verify_command
                assert diagnostic['original_command'] == expected_command
                assert result.verification_binding['proof_graph']['commands'][expected_command]
                assert 'cmd:' + expected_command in diagnostic['owners'][0]['verification_refs']
            expected_launches = list(launched)
            if variant == 'partial':
                assert launched == ['collection', 'collection', 'execution']
                assert record['logical_commands'] == record['executed_commands'] == 3
                assert body.read_text() == 'executed'
            else:
                assert all(phase == 'collection' for phase in launched)
            assert bool(launched) == (target_phase == 'execution')
            assert record['executed_commands'] == len(expected_launches)
            assert record['logical_commands'] == len(expected_launches)
            assert record['certificate_hits'] == 0
            assert injected == [target_phase] and writers == ['fix']
            assert body.exists() == (variant == 'partial') and image() == before
            custody, attempt = deepcopy(result.candidate_custody), result.current_attempt
            assert custody['receipt'] and result.verification_diagnostics
            for _ in range(2):
                repeated = resume()
                assert repeated.status == 'blocked'
                assert repeated.candidate_custody == custody and repeated.current_attempt == attempt
                assert not repeated.candidate_custody.get('delivered_revision')
                assert any(e.get('diagnostic') == diagnostic for e in repeated.execution_log)
                assert not any(e.get('verification', {}).get('ok') for e in repeated.execution_log
                               if e.get('action') == 'receipt_verification')
                assert injected == [target_phase] and writers == ['fix']
                assert launched == expected_launches
                assert body.exists() == (variant == 'partial') and image() == before
        finally:
            server.shutdown()
            worker.join(timeout=5)
