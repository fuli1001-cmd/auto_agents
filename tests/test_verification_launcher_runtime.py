"""A reused interpreter must not choose its installed engine for a new launcher."""
import json
from pathlib import Path
import shutil
import subprocess
import venv

import pytest


@pytest.fixture(params=['missing_metadata', 'old_metadata'])
def runtime(tmp_path, request):
    selected = tmp_path / 'selected'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'src', selected / 'src',
                    ignore=shutil.ignore_patterns('__pycache__'))
    # Keep the interpreter under a preserved runtime root when the production
    # launcher mounts its private /tmp. It still has an independent site-packages.
    environment = selected / 'cached-environment'
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    python = environment / 'bin/python'
    site = Path(subprocess.check_output([str(python), '-I', '-c',
        'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    package = site / 'auto_agents'
    package.mkdir()
    (package / '__init__.py').write_text('ORIGIN = "cached distribution"\n')
    (package / 'artifact_temp.py').write_text('from tempfile import *\n')
    if request.param == 'old_metadata':
        (package / 'verification_metadata.py').write_text('raise RuntimeError("stale installed metadata code")\n')
    return selected, python, package


def test_real_preflight_uses_selected_runtime_with_reused_interpreter(runtime, tmp_path):
    selected, python, package = runtime
    target = tmp_path / 'target'; target.mkdir()
    original = {path.name: path.read_bytes() for path in package.glob('*.py')}
    driver = ('import sys; from pathlib import Path; sys.path.insert(0,' + repr(str(selected / 'src')) + '); '
              'from auto_agents.verification_sandbox import check_verification_sandbox; '
              'check_verification_sandbox(Path(' + repr(str(selected)) + '),' + repr(str(python)) + ', '
              'Path(' + repr(str(target)) + ')); '
              'from auto_agents.verification_input_trace import check_input_tracing; '
              'check_input_tracing(Path(' + repr(str(selected)) + '),' + repr(str(python)) + ', '
              'Path(' + repr(str(target)) + ')); print("selected-runtime preflight passed")')
    result = subprocess.run([str(python), '-I', '-c', driver], cwd=tmp_path,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'selected-runtime preflight passed' in result.stdout
    assert not list(target.iterdir())
    assert original == {path.name: path.read_bytes() for path in package.glob('*.py')}


@pytest.mark.parametrize('launcher,protocol', [
    ('verification_sandbox.py', '--landlock'),
    ('verification_sandbox.py', '--writer-landlock'),
    ('gate_verification.py', '--landlock'),
])
def test_direct_launchers_bind_helpers_without_changing_child_imports(runtime, tmp_path, launcher, protocol):
    selected, python, package = runtime
    private = tmp_path / 'private'; private.mkdir()
    shared = tmp_path / 'shared'; shared.write_text('retained'); shared.chmod(0o640)
    program = ('import os,auto_agents; from pathlib import Path; '
        'assert auto_agents.ORIGIN == "cached distribution"; '
        'p=Path("private"); p.write_text("own"); p.chmod(0o750); '
        'fd=os.open(p,os.O_RDONLY); os.fchmod(fd,0o640); os.close(fd); '
        'assert p.stat().st_mode & 0o777 == 0o640\n'
        'try: os.chmod(' + repr(str(shared)) + ',0o777)\n'
        'except OSError: pass\n'
        'else: raise AssertionError("shared metadata changed")\n')
    result = subprocess.run([str(python), '-I', str(selected / 'src/auto_agents' / launcher),
        protocol, json.dumps([str(private)]), str(python), '-I', '-c', program], cwd=tmp_path,
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert shared.read_text() == 'retained' and shared.stat().st_mode & 0o777 == 0o640
