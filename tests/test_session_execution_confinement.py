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


def test_public_resume_blocks_validation_if_confinement_unavailable(tmp_path, monkeypatch):
    root, hook = shared_environment(tmp_path)
    monkeypatch.setenv('CONFINEMENT_TEST_INPUT', 'retained')
    from auto_agents import verification_sandbox
    original = verification_sandbox.shutil.which
    monkeypatch.setattr(verification_sandbox.shutil, 'which',
                        lambda name, *a, **kw: None if name == 'codex' else original(name, *a, **kw))
    result, calls, _ = run_session(root, monkeypatch)
    assert result.status != 'completed' and calls in ([], ['fix'])
    assert hook.read_text() == 'original environment'
    diagnostic = json.dumps(result.to_dict()).lower()
    assert 'sandbox' in diagnostic or 'confinement' in diagnostic
