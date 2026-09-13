"""Controller-owned observations through the production verification wrapper."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess


def _probe_environment():
    # The outer wrapper imports the controller before its clean child env is
    # installed. Support an interpreter without an editable engine installation.
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), env.get('PYTHONPATH')]))
    return env


def _cancel_event():
    from .repair_concurrent_validation import verification_cancel
    return verification_cancel.get()


def metadata_observation(runner, workspace):
    from . import artifact_temp as tempfile
    from .verification_ledger import source_identity
    try:
        if _cancel_event() is not None and _cancel_event().is_set():
            raise InterruptedError('capability observation cancelled')
        # Candidate launchers are untrusted. Their real dispatch must never
        # run against the retained implementation workspace during planning.
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=workspace,
                               capture_output=True, timeout=15, check=True)
        if dirty.stdout:
            raise RuntimeError('metadata capability needs a committed source snapshot')
        before = source_identity(workspace)
        with tempfile.TemporaryDirectory(prefix='repair-boundary-probe-') as temporary:
            snapshot = Path(temporary) / 'source'
            subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks', str(workspace), str(snapshot)],
                           capture_output=True, timeout=30, check=True)
            result = _metadata_observation_in_snapshot(runner, snapshot)
            if source_identity(snapshot) != before or source_identity(workspace) != before:
                raise RuntimeError('source changed during metadata capability observation')
            return result
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return {'capability': 'nested_gate_private_metadata', 'acceptance_proof': False,
                'supported': None, 'status': 'inconclusive', 'reason': str(error)}


def _metadata_observation_in_snapshot(runner, workspace):
    """Exercise the real nested gate, not an in-process replacement for it."""
    from . import artifact_temp as tempfile
    from .process_supervision import run_supervised_shell_command
    root = Path(workspace)
    runtime = root / '.auto-agents-gate-runtime'
    result = {'capability': 'nested_gate_private_metadata', 'acceptance_proof': False,
              'scope': 'production wrapper and nested gate subprocess', 'supported': None}
    try:
        if runtime.is_symlink():
            raise RuntimeError('probe runtime is a symbolic link')
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime, prefix='metadata-probe-') as temporary:
            base = Path(temporary)
            private = base / 'private'
            private.mkdir()
            shared = base / 'shared'
            shared.write_bytes(b'protected')
            shared.chmod(0o640)
            program = ('import ctypes,json,os\nfrom pathlib import Path\n'
                'p=Path("private"); p.write_bytes(b"private")\n'
                'result={"shared_read":Path(' + repr(str(shared)) + ').read_bytes()==b"protected"}\n'
                'result["metadata_supervisor_version"]=ctypes.CDLL(None).prctl(0x41414D44,0,0,0,0)\n'
                'shared_fd=os.open(' + repr(str(shared)) + ',os.O_RDONLY)\n'
                'for name,action in [("private_chmod",lambda:p.chmod(0o750)), '
                '("private_fchmod",lambda:os.fchmod(os.open(p,os.O_RDONLY),0o640)), '
                '("private_utime",lambda:os.utime(p,ns=(1000000000,2000000000))), '
                '("shared_fchmod",lambda:os.fchmod(shared_fd,0o777)), '
                '("shared_fd_alias",lambda:os.chmod("/proc/self/fd/"+str(shared_fd),0o777)), '
                '("shared_chmod",lambda:os.chmod(' + repr(str(shared)) + ',0o777))]:\n'
                ' try: action(); result[name]=True\n'
                ' except OSError as error: result[name]=False; result[name+"_errno"]=error.errno\n'
                'print(json.dumps(result))\n')
            launcher = root / 'src/auto_agents/gate_verification.py'
            if not launcher.is_file():
                launcher = Path(__file__).with_name('gate_verification.py')
            bootstrap = ('import runpy,sys; sys.path.insert(0,sys.argv[1]); '
                         'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")')
            argv = [runner._verification_python(), '-I', '-c', bootstrap, str(launcher.parent.parent),
                    str(launcher), '--landlock', json.dumps([str(private)]),
                    runner._verification_python(), '-I', '-c', program]
            with runner._verification_argv(argv, root) as arguments:
                process = run_supervised_shell_command('exec ' + shlex.join(arguments), cwd=root,
                    env=_probe_environment(), timeout_seconds=15, kind='repair-metadata-capability', cancel_event=_cancel_event())
            if process.termination_reason or process.cleanup_incomplete or process.returncode:
                return {**result, 'status': 'inconclusive', 'reason': process.termination_reason or process.stderr[-1600:]}
            observed = json.loads(process.stdout)
            if not isinstance(observed, dict):
                raise ValueError('invalid metadata capability response')
            unchanged = shared.read_bytes() == b'protected' and shared.stat().st_mode & 0o777 == 0o640
            valid = (observed.get('private_chmod') is True and observed.get('private_fchmod') is True
                     and observed.get('private_utime') is True
                     and observed.get('metadata_supervisor_version') == 1
                     and observed.get('shared_fchmod') is False and observed.get('shared_fd_alias') is False)
            valid = (valid and observed.get('shared_read') is True and observed.get('shared_chmod') is False
                     and observed.get('shared_chmod_errno') in {1, 13, 30} and unchanged)
            return {**result, 'status': 'observed', 'supported': valid, 'checks': observed,
                    'shared_unchanged': unchanged,
                    'mechanism': 'inherited_ptrace_metadata_supervisor' if valid else ''}
    except (OSError, RuntimeError, ValueError) as error:
        return {**result, 'status': 'inconclusive', 'reason': str(error)}


def namespace_observation(runner, workspace):
    from .process_supervision import run_supervised_shell_command
    result = {'capability': 'nested_user_mount_namespace', 'acceptance_proof': False,
              'scope': 'production verification wrapper', 'supported': None}
    executable = shutil.which('unshare')
    if not executable:
        return {**result, 'status': 'unavailable', 'supported': False, 'reason': 'unshare executable missing'}
    # No mounts or project writes: only the disposable child's namespaces and
    # UID mapping are affected. A success proves creation, not a secure launcher.
    program = 'import json,os; print(json.dumps({"namespace_created":True,"uid":os.getuid()}))'
    argv = [executable, '--user', '--map-root-user', '--mount', '--fork',
            runner._verification_python(), '-I', '-c', program]
    try:
        with runner._verification_argv(argv, Path(workspace)) as arguments:
            process = run_supervised_shell_command('exec ' + shlex.join(arguments), cwd=Path(workspace),
                env=_probe_environment(), timeout_seconds=15, kind='repair-capability', cancel_event=_cancel_event())
        if process.termination_reason or process.cleanup_incomplete:
            return {**result, 'status': 'inconclusive', 'reason': process.termination_reason or 'cleanup incomplete'}
        if process.returncode == 0:
            payload = json.loads(process.stdout)
            if isinstance(payload, dict) and payload.get('namespace_created') is True and payload.get('uid') == 0:
                return {**result, 'status': 'observed', 'supported': True}
            raise ValueError('invalid namespace capability response')
        return {**result, 'status': 'unavailable', 'supported': False,
                'returncode': process.returncode, 'reason': process.stderr[-1600:] or 'namespace creation failed'}
    except (OSError, RuntimeError, ValueError) as error:
        return {**result, 'status': 'inconclusive', 'reason': str(error)}


def production_capabilities(runner, workspace):
    from .verification_ledger import source_identity
    key = (source_identity(workspace), runner._full_suite_environment_fingerprint())
    cached = getattr(runner, '_production_capability_cache', None)
    if cached and cached[0] == key:
        return cached[1]
    result = namespace_observation(runner, workspace)
    result['nested_gate_metadata'] = metadata_observation(runner, workspace)
    if _cancel_event() is None or not _cancel_event().is_set():
        runner._production_capability_cache = (key, result)
    return result
