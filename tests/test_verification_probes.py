"""Actual observed filesystem reads and cross-snapshot cache invalidation."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from auto_agents.gate_result_cache import GateResultCache
from auto_agents.models import CommandResult
from auto_agents.verification_probes import PREFIX, matches


def observe(root, code):
    # A managed outer pytest already owns a profiler. Exercise the observer in
    # its own interpreter, just like the trusted verification driver does.
    import auto_agents.verification_inputs as module
    harness = '''
import json, os, sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from auto_agents.verification_inputs import InputObserver
from auto_agents.verification_probes import OPERATIONS
root, code = sys.argv[2:]
class CustomPath:
    def __fspath__(self): return str(Path(root, 'data'))
custom = CustomPath()
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
native_stat = OPERATIONS['stat']
compiled = compile(code, str(Path(root, 'check.py')), 'exec')
observer = InputObserver(root)
observer.start()
try:
    exec(compiled)
finally:
    receipt = observer.finish()
    os.close(directory)
assert sys.getprofile() is None
assert all(getattr(os, name) is original for name, original in OPERATIONS.items())
print(json.dumps(receipt))
'''
    result = subprocess.run([sys.executable, '-c', harness, str(Path(module.__file__).parent.parent),
                             str(root), code], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('change', ['unrelated', 'content', 'mode', 'missing', 'symlink'])
def test_recorded_metadata_supports_safe_reuse_and_invalidates_changed_inputs(tmp_path, change):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'data').write_text('one')
    (source / 'link').symlink_to('data')
    receipt = observe(source, '''
assert Path(root, 'data').read_text() == 'one'
assert os.stat(Path(root, 'data')).st_size == 3
assert os.access(str(Path(root, 'data')), os.R_OK)
assert os.readlink(str(Path(root, 'link'))) == 'data'
assert not Path(root, 'missing').exists()
''')
    assert receipt['complete'], receipt['reasons']
    assert any(key.startswith(PREFIX) for key in receipt['manifest'])
    cache = GateResultCache(source, cache_path=tmp_path / 'proofs.sqlite3', environment_fingerprint='same')
    cache.record('check', CommandResult('check', True, 0, observed_inputs=receipt['manifest'], input_trace_complete=True),
                 source_fingerprint='old', cache_scope='source', result_cache_scope='observed_inputs', metadata_signature='policy')
    if change == 'unrelated':
        (source / 'unrelated').write_text('new')
    elif change == 'content':
        (source / 'data').write_text('two')
    elif change == 'mode':
        (source / 'data').chmod(0o600 if (source / 'data').stat().st_mode & 0o777 != 0o600 else 0o640)
    elif change == 'missing':
        (source / 'missing').write_text('new')
    else:
        (source / 'link').unlink()
        (source / 'link').symlink_to('other')
    hit = cache.lookup('check', source_fingerprint='new', cache_scope='source',
                       result_cache_scope='observed_inputs', metadata_signature='policy')
    assert bool(hit) == (change == 'unrelated')


@pytest.mark.parametrize('kind', ['native_alias', 'subprocess', 'dir_fd', 'fspath', 'chdir', 'changed_probe'])
def test_unproved_inputs_still_decline_reuse(tmp_path, kind):
    (tmp_path / 'data').write_text('data')
    code = {
        'native_alias': "native_stat(Path(root, 'data'))",
        'subprocess': "subprocess.run([sys.executable, '-c', 'pass'], check=True)",
        'dir_fd': "os.stat('data', dir_fd=directory)",
        'fspath': "os.stat(custom)",
        'chdir': "os.chdir(root)",
        'changed_probe': "os.stat(Path(root, 'data')); Path(root, 'data').write_text('different'); os.stat(Path(root, 'data'))",
    }[kind]
    receipt = observe(tmp_path, code)
    assert not receipt['complete'] and receipt['reasons']


def test_cwd_receipt_is_bound_to_verification_workspace(tmp_path):
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        receipt = observe(tmp_path, 'os.getcwd()')
    finally:
        os.chdir(previous)
    assert receipt['complete']
    key, expected = next(iter(receipt['manifest'].items()))
    assert matches(tmp_path, key, expected)
    assert not matches(tmp_path / 'different', key, expected)


def test_malformed_probe_cannot_select_arbitrary_operations(tmp_path):
    assert not matches(tmp_path, PREFIX + '{"operation":"system","path":"command"}', 'anything')
    assert not matches(tmp_path, PREFIX + '{"operation":"stat","path":"../other"}', 'anything')


def test_trusted_pytest_script_loads_its_own_probe_implementation(tmp_path):
    from auto_agents import verification_pytest
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/verification_probes.py').write_text('raise AssertionError("candidate replaced trusted probe")\n')
    (tmp_path / 'test_check.py').write_text('def test_check(): assert 2 + 2 == 4\n')
    receipt = tmp_path / 'receipt.json'
    result = subprocess.run([sys.executable, verification_pytest.__file__, str(tmp_path), str(receipt),
                             '-q', '-p', 'no:cacheprovider', str(tmp_path / 'test_check.py')],
                            cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(receipt.read_text())['passed'] == ['test_check.py::test_check']
