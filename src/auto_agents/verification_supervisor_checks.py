"""Fixed checks requiring a fresh owner, run before the verification boundary.

The launcher owns these programs; callers cannot supply code, paths or commands.
Their observations are diagnostics consumed by ordinary acceptance tests, never
standalone acceptance receipts. Nested execution cannot start another ptracer.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPORT_ENV = 'AUTO_AGENTS_SUPERVISOR_CHECKS'
CASES = ('legacy_owner', 'startup_denied', 'owner_death', 'missing_exitkill')
# Exact copy of 8ea6662:src/auto_agents/verification_metadata.py. Keep its
# original filter (including ptrace denial), not an emulated protocol response.
LEGACY_SHA256 = '571b87579a36bb884a21e5dae723f67af0f3cc20d88b5d0a362aed8cc880f02f'


def source_identity():
    root = Path(__file__).parent
    names = ('verification_metadata.py', 'verification_input_trace.py',
             'verification_sandbox.py', 'verification_supervisor_checks.py',
             'verification_fixtures/metadata_v1.py')
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def _owner():
    from auto_agents.verification_input_trace import owner_identity
    return owner_identity()


def _tracer_pid():
    return int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                    if line.startswith('TracerPid:')))


def observation(case):
    """Read a source-bound launch observation, or run locally without an owner."""
    if case not in CASES:
        raise ValueError('unknown standalone supervisor check')
    if not _owner()['metadata']:
        return _run_case(case)
    try:
        report = json.loads(os.environ[REPORT_ENV])
        if report['version'] != 1 or report['sources'] != source_identity():
            raise ValueError('standalone supervisor check source mismatch')
        if report['launcher_pid'] != _tracer_pid():
            raise ValueError('standalone supervisor check owner mismatch')
        result = report['checks'][case]
        if result['case'] != case or result['inherited_owner']['metadata'] != 0:
            raise ValueError('standalone supervisor check inherited an owner')
        return result
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError('fresh supervisor checks require the selected engine verification '
                           'launcher before entering its metadata owner: ' + str(error)) from error


def prepare():
    """Called only by the trusted launcher, before executing the test command."""
    if _owner()['metadata']:
        # Never fabricate fresh-owner evidence from an inherited process.
        for case in CASES:
            observation(case)
        return
    sources = source_identity()
    checks = {case: _run_case(case) for case in CASES}
    if source_identity() != sources:
        raise RuntimeError('supervisor implementation changed during standalone checks')
    os.environ[REPORT_ENV] = json.dumps({'version': 1, 'sources': sources,
                                       'launcher_pid': os.getpid(), 'checks': checks})


def _run_case(case):
    inherited = _owner()
    if inherited['metadata']:
        raise RuntimeError('standalone supervisor check cannot run beneath an owner')
    with tempfile.TemporaryDirectory(prefix='supervisor-check-') as temporary:
        root = Path(temporary)
        private = root / 'private'
        private.mkdir()
        shared = root / 'shared'
        shared.write_text('retained')
        shared.chmod(0o640)
        command = [sys.executable, '-I', str(Path(__file__).resolve()),
                   '--child', case, str(root), str(os.getpid())]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        result = {'case': case, 'inherited_owner': inherited, 'supervisor_pid': process.pid}
        try:
            if case in {'owner_death', 'missing_exitkill'}:
                ready = private / 'ready'
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not ready.exists():
                    raise RuntimeError('standalone supervisor did not start its tracee')
                state = json.loads(ready.read_text())
                if state['tracer_pid'] != process.pid or state['pid'] == process.pid:
                    raise RuntimeError('death check did not establish a separate ptrace owner')
                result.update(state)
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
                # The sleeping tracee holds both pipes. EOF before its sleep
                # ends proves EXITKILL, not merely termination of the launcher.
                try:
                    stdout, stderr = process.communicate(timeout=0.2 if case == 'missing_exitkill' else 3)
                    result['tracee_pipes_closed'] = True
                except subprocess.TimeoutExpired:
                    if case != 'missing_exitkill':
                        raise
                    # Negative control: without EXITKILL the sleeping tracee
                    # must retain the pipes until our explicit final cleanup.
                    stdout, stderr = '', ''
                    result['tracee_pipes_closed'] = False
            else:
                stdout, stderr = process.communicate(timeout=10)
            result.update(returncode=process.returncode, stdout=stdout, stderr=stderr)
            if case == 'legacy_owner':
                result['count'] = (private / 'count').read_text()
                result['trace'] = json.loads((private / 'trace').read_text())
                result['legacy_sha256'] = hashlib.sha256(
                    Path(__file__).with_name('verification_fixtures').joinpath('metadata_v1.py').read_bytes()).hexdigest()
            if case == 'startup_denied':
                result['command_executed'] = (private / 'must-not-run').exists()
            result['shared_unchanged'] = shared.read_text() == 'retained' and shared.stat().st_mode & 0o777 == 0o640
            return result
        finally:
            # Also reap the intentionally killed owner's descendants if a
            # regression disabled EXITKILL. The process group is probe-private.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)


def _child(case, root, parent):
    if case not in CASES or _owner()['metadata']:
        raise RuntimeError('standalone supervisor child requires a fresh owner')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
        raise RuntimeError('standalone supervisor parent is unavailable')
    private = root / 'private'
    if case == 'legacy_owner':
        from auto_agents.verification_fixtures import metadata_v1 as metadata
        if hashlib.sha256(Path(metadata.__file__).read_bytes()).hexdigest() != LEGACY_SHA256:
            raise RuntimeError('frozen legacy supervisor changed')
        helper = str(Path(__file__).with_name('verification_input_trace.py'))
        program = ('from pathlib import Path; import os; '
                   'p=Path("count"); p.open("a").write("executed\\n"); p.chmod(0o640)\n'
                   'try: os.chmod(' + repr(str(root / 'shared')) + ',0o777)\n'
                   'except PermissionError: pass\n'
                   'else: raise AssertionError("legacy owner changed shared metadata")\n')
        command = [sys.executable, helper, str(private / 'trace'), sys.executable, '-c', program]
    else:
        from auto_agents import verification_metadata as metadata
        if case == 'missing_exitkill':
            ptrace = metadata._ptrace
            def without_exitkill(request, pid, address=0, data=0):
                if request == 0x4200:
                    data &= ~0x00100000
                return ptrace(request, pid, address, data)
            metadata._ptrace = without_exitkill
        if case == 'startup_denied':
            def denied(*args, **kwargs):
                raise PermissionError('ptrace unavailable')
            metadata._ptrace = denied
            program = 'from pathlib import Path; Path("must-not-run").write_text("unexpected")'
        else:
            program = ('import json,os,time\nfrom pathlib import Path\n'
                       'tracer=int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines() '
                       'if line.startswith("TracerPid:")))\n'
                       'Path("ready.tmp").write_text(json.dumps({"pid":os.getpid(),"tracer_pid":tracer}))\n'
                       'Path("ready.tmp").rename("ready")\ntime.sleep(15)\n')
        command = [sys.executable, '-c', program]
    return metadata.metadata_exec(command, [str(private)], [str(root / 'shared')])


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--child':
        raise SystemExit('internal standalone supervisor check requires a fixed case')
    raise SystemExit(_child(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])))
