"""Host runtime submounts must not break or widen the verification boundary."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('retained_environment', [False, True])
def test_host_run_submount_is_hidden_before_sandbox_and_host_is_unchanged(tmp_path, retained_environment):
    root, shared = tmp_path / 'candidate', tmp_path / 'shared'
    root.mkdir(); shared.mkdir()
    (shared / 'input').write_text('retained')
    source = Path(__file__).resolve().parents[1] / 'src'
    program = '''
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, SOURCE)
from auto_agents.verification_sandbox import verification_argv
subprocess.run(['mount', '-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', '/run'], check=True)
Path('/run/WSL').mkdir()
subprocess.run(['mount', '-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', '/run/WSL'], check=True)
sentinel = Path('/run/WSL/private-host-input')
sentinel.write_text('host-private')
identity = (sentinel.stat().st_dev, sentinel.stat().st_ino)
code = ''' + repr('''
from pathlib import Path
for path in [Path('/run/WSL/private-host-input'), Path('/run/new-output')]:
    try:
        path.read_text() if path.name == 'private-host-input' else path.write_text('escape')
    except OSError:
        pass
    else:
        raise AssertionError('host runtime access escaped the boundary')
p = Path('private'); p.write_text('allowed'); p.chmod(0o750)
assert p.stat().st_mode & 0o777 == 0o750
p = Path(SHARED) / 'input'
assert p.read_text() == 'retained'
for action in [lambda: p.write_text('bad'), lambda: p.chmod(0o777)]:
    try: action()
    except OSError: pass
    else: raise AssertionError('shared input changed')
''') + '''
env = {'PATH': os.environ['PATH'], 'HOME': ROOT, 'LANG': 'C.UTF-8'} if RETAINED else None
with verification_argv([sys.executable, '-c', 'SHARED=' + repr(SHARED) + '\\n' + code],
                       Path(ROOT), Path(SHARED), execution_environment=env) as argv:
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
assert result.returncode == 0, result.stdout + result.stderr
assert sentinel.read_text() == 'host-private'
assert (sentinel.stat().st_dev, sentinel.stat().st_ino) == identity
'''
    inputs = dict(ROOT=str(root), SHARED=str(shared), SOURCE=str(source), RETAINED=retained_environment)
    script = '\n'.join(key + '=' + repr(value) for key, value in inputs.items()) + '\n' + program
    result = subprocess.run(['unshare', '--user', '--map-root-user', '--mount',
                             sys.executable, '-c', script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (shared / 'input').read_text() == 'retained'
