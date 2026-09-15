import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from auto_agents.verification_sandbox import verification_argv


def test_missing_sandbox_blocks_instead_of_running_unrestricted(tmp_path):
    with patch("auto_agents.verification_sandbox.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="needs a local Codex sandbox"):
            with verification_argv(["true"], tmp_path / "candidate", tmp_path / "project"):
                raise AssertionError("unrestricted fallback")


def test_verification_cannot_grant_write_access_to_live_project(tmp_path):
    with pytest.raises(RuntimeError, match="overlaps"):
        with verification_argv(["true"], tmp_path, tmp_path / "project"):
            pass


def test_runtime_reservation_keeps_identity_across_namespaces_and_rejects_forgery(tmp_path):
    from test_verification_metadata import execute

    inner = '''
import json, os, uuid
from pathlib import Path
from auto_agents.verification_input_trace import file_identity
from auto_agents.verification_sandbox import runtime_reservation, RUNTIME_ID_ENV, ConfinementPreflightError
from auto_agents.gate_execution import short_job_runtime_root
pool = runtime_reservation()
token = os.environ[RUNTIME_ID_ENV]
identity = json.loads(token)
assert identity == file_identity(pool.lstat())
assert identity[2] == os.getuid()
for field in range(4):
    forged = list(identity)
    forged[field] += 1
    os.environ[RUNTIME_ID_ENV] = json.dumps(forged)
    try:
        runtime_reservation()
    except ConfinementPreflightError as error:
        assert error.diagnostic['phase'] == 'runtime_allocation'
        assert error.diagnostic['attempted_location'] == str(pool)
        assert error.diagnostic['errno'] == 1
    else:
        raise AssertionError('forged reservation identity was accepted')
    finally:
        os.environ[RUNTIME_ID_ENV] = token
assert runtime_reservation() == pool
leaf = short_job_runtime_root(uuid.uuid4().hex)
assert leaf.parent == pool and leaf.stat().st_uid == os.getuid()
assert leaf.stat().st_mode & 0o777 == 0o700
(leaf / '.auto-agents-runtime.json').unlink()
leaf.rmdir()
print(json.dumps({'pool': str(pool), 'identity': identity}))
'''
    execute(tmp_path, f'''
import json, os, subprocess, sys
from pathlib import Path
from auto_agents.verification_input_trace import file_identity
from auto_agents.verification_sandbox import verification_argv, runtime_reservation, RUNTIME_ID_ENV
pool = runtime_reservation()
identity = json.loads(os.environ[RUNTIME_ID_ENV])
assert identity == file_identity(pool.lstat())
assert identity[2] == os.getuid()
root = Path.cwd() / 'nested'
root.mkdir()
with verification_argv([sys.executable, '-c', {inner!r}], root, Path(SHARED).parent) as argv:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
assert result.returncode == 0, result.stdout + result.stderr
assert json.loads(result.stdout) == {{'pool': str(pool), 'identity': identity}}
assert runtime_reservation() == pool
''')


@pytest.mark.skipif(shutil.which("codex") is None, reason="local Codex sandbox not installed")
def test_actual_sandbox_protects_live_inputs_and_keeps_candidate_writable(tmp_path):
    project, candidate = tmp_path / "project", tmp_path / "candidate"
    project.mkdir()
    candidate.mkdir()
    live = project / "input.txt"
    live.write_text("original")
    mode = live.stat().st_mode
    metadata = """
import errno, os, tempfile
with tempfile.NamedTemporaryFile(dir='.') as private:
    p=Path(private.name); p.chmod(0o750)
    assert p.stat().st_mode & 0o777 == 0o750
    os.fchmod(private.fileno(),0o640)
    assert p.stat().st_mode & 0o777 == 0o640
p=Path(LIVE)
fd=os.open(p,os.O_RDONLY)
try:
    for action in (lambda: p.chmod(0o777),lambda: os.fchmod(fd,0o777),lambda: os.chmod('/proc/self/fd/'+str(fd),0o777)):
        try: action()
        except OSError as error: assert error.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
        else: raise AssertionError('shared metadata changed')
finally: os.close(fd)
""".replace('LIVE', repr(str(live)))
    code = (
        "from pathlib import Path; Path('result.txt').write_text('allowed'); "
        f"p=Path({str(live)!r}); "
        "\ntry: p.write_text('bad'); blocked=False\n"
        "except OSError: blocked=True\n"
        "assert blocked\n" + metadata
    )
    with verification_argv([sys.executable, "-c", code], candidate, project) as command:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert live.read_text() == "original" and live.stat().st_mode == mode
    assert (candidate / "result.txt").read_text() == "allowed"


def test_private_shared_memory_supports_multiprocessing_across_nested_boundaries(tmp_path):
    import os
    from uuid import uuid4
    from auto_agents.verification_input_trace import owner_identity
    from auto_agents.verification_sandbox import SHM_ENV
    inherited = owner_identity()['metadata'] and os.environ.get(SHM_ENV) == '1'
    marker = Path('/dev/shm') / ('auto-agents-test-' + uuid4().hex)
    candidate, shared = tmp_path / 'candidate', tmp_path / 'shared'
    candidate.mkdir(); shared.mkdir()
    inner = "import multiprocessing; sem=multiprocessing.Semaphore(1); assert sem.acquire(timeout=1); sem.release()"
    program = f'''
import multiprocessing,subprocess,sys
from pathlib import Path
from auto_agents.verification_sandbox import verification_argv
Path({str(marker)!r}).write_text('private')
sem=multiprocessing.Semaphore(1); assert sem.acquire(timeout=1); sem.release()
Path('inner').mkdir()
with verification_argv([sys.executable,'-c',{inner!r}],Path.cwd()/'inner',Path({str(shared)!r})) as argv:
    result=subprocess.run(argv,capture_output=True,text=True,timeout=10)
assert result.returncode==0,result.stderr
'''
    try:
        with verification_argv([sys.executable, '-c', program], candidate, shared) as argv:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.exists() is bool(inherited)
    finally:
        if inherited:
            marker.unlink(missing_ok=True)
