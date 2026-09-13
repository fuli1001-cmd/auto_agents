"""Real subprocess metadata controls, including two nested production boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import time
import ctypes

import pytest

from auto_agents.verification_sandbox import verification_argv


def execute(tmp_path, program):
    root, shared = tmp_path / 'candidate', tmp_path / 'shared'
    root.mkdir(); shared.mkdir()
    (root / 'src').symlink_to(Path(__file__).resolve().parents[1] / 'src', target_is_directory=True)
    sentinel = shared / 'input'
    sentinel.write_text('shared'); sentinel.chmod(0o640)
    script = 'SHARED = ' + repr(str(sentinel)) + '\n' + program
    with verification_argv([sys.executable, '-c', script], root, shared) as argv:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    assert sentinel.read_text() == 'shared' and sentinel.stat().st_mode & 0o777 == 0o640
    return process, root


def test_path_fd_symlink_and_directory_fd_metadata(tmp_path):
    execute(tmp_path, '''
import ctypes, errno, os, subprocess
from pathlib import Path
p = Path('private'); p.write_text('own')
p.chmod(0o750)
assert p.stat().st_mode & 0o777 == 0o750
fd = os.open(p, os.O_RDONLY)
os.fchmod(fd, 0o640)
assert p.stat().st_mode & 0o777 == 0o640
subprocess.run(['chmod','750',str(p)],check=True)
assert p.stat().st_mode & 0o777 == 0o750
opath=os.open(p,os.O_PATH)
libc=ctypes.CDLL(None,use_errno=True)
assert libc.syscall(452,opath,ctypes.c_char_p(b''),0o640,0x1000)==0,ctypes.get_errno()
os.close(opath)
assert p.stat().st_mode & 0o777 == 0o640
os.utime(p, ns=(123000000000, 456000000000))
assert p.stat().st_mtime_ns == 456000000000
os.utime(fd, ns=(123000000000, 789000000000))
assert p.stat().st_mtime_ns == 789000000000
os.close(fd)
Path('private-link').symlink_to('private')
os.utime('private-link', ns=(123000000000, 456000000000), follow_symlinks=False)
assert Path('private-link').lstat().st_mtime_ns == 456000000000
Path('escape').symlink_to(SHARED)
fd = os.open(SHARED, os.O_RDONLY)
directory = os.open(str(Path(SHARED).parent), os.O_PATH)
for action in [lambda: os.chmod(SHARED, 0o777), lambda: os.fchmod(fd, 0o777),
               lambda: os.chmod('escape', 0o777), lambda: os.chmod('input', 0o777, dir_fd=directory),
               lambda: os.chmod('/proc/self/fd/' + str(fd), 0o777),
               lambda: os.utime(SHARED, ns=(1, 1)), lambda: os.utime(fd, ns=(1, 1))]:
    try: action()
    except OSError as error: assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
    else: raise AssertionError('shared metadata changed')
os.close(fd); os.close(directory)
''')


def test_two_nested_launches_narrow_one_supervisor_and_preserve_ancestor_policy(tmp_path):
    inner = '''
import ctypes, errno, json, os
from pathlib import Path
from auto_agents.verification_metadata import POLICY_REQUEST, VERSION
libc=ctypes.CDLL(None, use_errno=True)
assert libc.prctl(POLICY_REQUEST, 0, 0, 0, 0) == VERSION
# A forged request for the entire filesystem only adds another intersection.
payload=json.dumps({'roots':['/']}).encode()
assert libc.prctl(POLICY_REQUEST, ctypes.c_char_p(payload), len(payload), 0, 0) == 0
p=Path('owned'); p.write_text('private'); p.chmod(0o750)
assert p.stat().st_mode & 0o777 == 0o750
for name in [SHARED, OUTER]:
    assert Path(name).read_text()
    try: os.chmod(name,0o777)
    except OSError as error: assert error.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
    else: raise AssertionError('ancestor boundary widened')
'''
    middle = '''
import os, subprocess, sys
from pathlib import Path
from auto_agents.verification_sandbox import verification_argv
parent=Path.cwd(); Path('parent-owned').write_text('parent')
Path('child').mkdir()
program = 'OUTER = ' + repr(str(parent/'parent-owned')) + '\\n' + INNER
with verification_argv([sys.executable,'-c',program],parent/'child',Path(SHARED).parent) as argv:
    result=subprocess.run(argv,capture_output=True,text=True,timeout=15)
assert result.returncode==0, result.stdout+result.stderr
assert Path('child/owned').stat().st_mode & 0o777 == 0o750
'''
    outer = '''
import subprocess, sys
from pathlib import Path
from auto_agents.verification_sandbox import verification_argv
Path('first').mkdir()
program='SHARED = '+repr(SHARED)+'\\nINNER = '+repr('SHARED = '+repr(SHARED)+'\\n'+INNER)+'\\n'+MIDDLE
with verification_argv([sys.executable,'-c',program],Path.cwd()/'first',Path(SHARED).parent) as argv:
    result=subprocess.run(argv,capture_output=True,text=True,timeout=20)
assert result.returncode==0, result.stdout+result.stderr
'''
    # Keep package import paths available after each private cwd change.
    outer = 'INNER = ' + repr(inner) + '\nMIDDLE = ' + repr(middle) + '\n' + outer
    execute(tmp_path, outer)


def test_nested_launch_preserves_environment_with_inaccessible_path_entries(tmp_path):
    execute(tmp_path, '''
import os, subprocess, sys
from pathlib import Path
from auto_agents.verification_sandbox import verification_argv
Path('inner').mkdir()
env=dict(os.environ,PATH='/run/unavailable:'+os.environ['PATH'])
code="import os; from pathlib import Path; assert os.environ['PATH'].startswith('/run/unavailable:'); p=Path('private'); p.write_text('ok'); p.chmod(0o750)"
with verification_argv([sys.executable,'-c',code],Path.cwd()/'inner',Path(SHARED).parent,
                       execution_environment=env) as argv:
    result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=10)
assert result.returncode==0,result.stdout+result.stderr
''')


def test_tracees_cannot_detach_or_signal_the_supervisor(tmp_path):
    execute(tmp_path, '''
import ctypes, errno, os
libc=ctypes.CDLL(None,use_errno=True)
for number,arguments in [(101,(0,0,0,0)), (56,(0x00800000|17,0,0,0,0)),
                         (62,(os.getppid(),15))]:
    result=libc.syscall(number,*arguments)
    assert result==-1 and ctypes.get_errno()==errno.EPERM,(number,result,ctypes.get_errno())
''')


def test_threaded_children_cannot_race_a_symlink_outside_their_boundary(tmp_path):
    execute(tmp_path, '''
import errno, os, threading, subprocess, sys
from pathlib import Path
Path('owned').write_text('private')
Path('link').symlink_to('owned')
root_mode=Path.cwd().stat().st_mode
done=threading.Event()
def swap():
    while not done.is_set():
        for destination in [SHARED, 'owned']:
            Path('next').symlink_to(destination)
            os.replace('next','link')
thread=threading.Thread(target=swap); thread.start()
try:
    for i in range(150):
        try: os.chmod('link',0o600 if i%2 else 0o750)
        except OSError as error: assert error.errno in (errno.EPERM,errno.EACCES,errno.ENOENT)
finally:
    done.set(); thread.join()
assert Path.cwd().stat().st_mode == root_mode
result=subprocess.run([sys.executable,'-c',"from pathlib import Path; p=Path('subprocess'); p.write_text('ok'); p.chmod(0o750)"],timeout=5)
assert result.returncode==0
''')


def test_shared_hardlink_and_readonly_subroot_keep_metadata_protection(tmp_path):
    execute(tmp_path, '''
import errno, json, os, subprocess, sys
from pathlib import Path
from auto_agents.verification_sandbox import verification_argv
root=Path.cwd(); Path('outer').write_text('original'); Path('inner').mkdir()
os.link('outer','inner/alias')
Path('inner/protected').mkdir(); Path('inner/protected/file').write_text('retained')
script="""
import errno, os
from pathlib import Path
for name in ['alias','protected/file']:
    try: os.chmod(name,0o777)
    except OSError as e: assert e.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
    else: raise AssertionError('protected inode changed')
Path('private').write_text('own'); Path('private').chmod(0o750)
"""
with verification_argv([sys.executable,'-c',script],root/'inner',Path(SHARED).parent,
                       read_roots=[root/'inner/protected']) as argv:
    result=subprocess.run(argv,capture_output=True,text=True,timeout=10)
assert result.returncode==0,result.stdout+result.stderr
assert Path('outer').stat().st_mode & 0o777 != 0o777
assert Path('inner/protected/file').stat().st_mode & 0o777 != 0o777
''')


@pytest.mark.parametrize('protocol', ['--landlock', '--writer-landlock'])
def test_retained_launcher_protocol_keeps_private_modes_and_shared_fd_denial(tmp_path, protocol):
    program = '''
import os, subprocess, sys
from pathlib import Path
import auto_agents.verification_sandbox as sandbox
Path('private-writer').mkdir()
script = """
import errno, os
from pathlib import Path
Path('.git').mkdir(); Path('.git/index').write_text('private index')
Path('.git/index').chmod(0o640)
p=Path('script'); p.write_text('owned'); p.chmod(0o750)
fd=os.open(p,os.O_RDONLY); os.fchmod(fd,0o640); os.close(fd)
assert p.stat().st_mode & 0o777 == 0o640
fd=os.open(SHARED,os.O_RDONLY)
try: os.fchmod(fd,0o777)
except OSError as e: assert e.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
else: raise AssertionError('shared fd changed')
os.close(fd)
"""
result=subprocess.run([sys.executable,sandbox.__file__,PROTOCOL,
    json.dumps([str(Path.cwd()/'private-writer')]),sys.executable,'-c','SHARED='+repr(SHARED)+'\\n'+script],
    capture_output=True,text=True,timeout=10)
assert result.returncode==0,result.stdout+result.stderr
'''
    execute(tmp_path, 'import json\nPROTOCOL=' + repr(protocol) + '\n' + program)


def test_unsupported_supervision_never_executes_the_command(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    marker = root / 'must-not-run'
    source = str(Path(__file__).resolve().parents[1] / 'src')
    script = '''
import sys
sys.path.insert(0,SOURCE)
from auto_agents import verification_metadata as metadata
def denied(*args, **kwargs): raise PermissionError('ptrace unavailable')
metadata._ptrace=denied
raise SystemExit(metadata.metadata_exec([sys.executable,'-c',COMMAND],[ROOT]))
'''
    prefix = '\n'.join(name + '=' + repr(value) for name, value in {
        'SOURCE': source, 'ROOT': str(root),
        'COMMAND': 'from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("unexpected")'}.items())
    result = subprocess.run([sys.executable, '-I', '-c', prefix + '\n' + script],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 125 and 'ptrace unavailable' in result.stderr
    assert not marker.exists()


def test_supervisor_death_kills_its_tracees(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    ready = root / 'ready'
    source = str(Path(__file__).resolve().parents[1] / 'src')
    command = 'from pathlib import Path; import time; Path(' + repr(str(ready)) + ').write_text("ready"); time.sleep(60)'
    script = ('import sys; sys.path.insert(0,' + repr(source) + '); '
              'from auto_agents.verification_metadata import metadata_exec; '
              'raise SystemExit(metadata_exec(' + repr([sys.executable, '-c', command]) + ',' + repr([str(root)]) + '))')
    process = subprocess.Popen([sys.executable, '-I', '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), process.poll()
        os.kill(process.pid, signal.SIGKILL)
        # Both pipes stay open in the sleeping tracee. communicate can finish
        # only when EXITKILL also terminates that descendant.
        _, errors = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL, errors
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_concurrent_short_lived_children_keep_metadata_supervision(tmp_path):
    worker = '''
import ctypes,errno,os
from pathlib import Path
assert ctypes.CDLL(None).prctl(0x41414D44,0,0,0,0)==1
p=Path('child-'+str(os.getpid())); p.write_text('private'); p.chmod(0o750)
assert p.stat().st_mode & 0o777 == 0o750
try: os.chmod(SHARED,0o777)
except OSError as error: assert error.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
else: raise AssertionError('child lost its inherited boundary')
'''
    program = '''
from concurrent.futures import ThreadPoolExecutor
import subprocess,sys
code='SHARED='+repr(SHARED)+'\\n'+WORKER
def run(index):
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
with ThreadPoolExecutor(max_workers=8) as pool:
    assert len(list(pool.map(run,range(80))))==80
'''
    execute(tmp_path, 'WORKER=' + repr(worker) + '\n' + program)


def test_completed_process_groups_do_not_report_incomplete_cleanup(tmp_path):
    execute(tmp_path, '''
import os,shlex,sys
from pathlib import Path
from auto_agents.process_supervision import run_supervised_shell_command
from auto_agents.gate_result_cache import GateResultCache
from auto_agents.models import CommandResult
command=shlex.join([sys.executable,'-c',"from pathlib import Path; p=Path('owned'); p.write_text('private'); p.chmod(0o750)"])
result=run_supervised_shell_command(command,cwd=Path.cwd(),env=dict(os.environ),timeout_seconds=10)
assert result.returncode==0,result.stderr
assert not result.cleanup_incomplete,result.process_snapshot
cache=GateResultCache(Path.cwd(),cache_path=Path.cwd()/'proofs.sqlite3')
identity=dict(source_fingerprint='retained',cache_scope='source',result_cache_scope='candidate',metadata_signature='owned')
cache.record(command,CommandResult(command,True,result.returncode,cleanup_incomplete=result.cleanup_incomplete),**identity)
proof=cache.lookup(command,**identity)
assert proof is not None and proof.ok
''')


def test_nonleader_exec_preserves_its_narrowed_boundary(tmp_path):
    child = '''
import errno,os
from pathlib import Path
p=Path('own'); p.write_text('private'); p.chmod(0o750)
for name in [SHARED,OUTER]:
    try: os.chmod(name,0o777)
    except OSError as error: assert error.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
    else: raise AssertionError('exec lost the originating thread policy')
'''
    program = '''
import sys,threading
from pathlib import Path
from auto_agents.verification_metadata import metadata_exec
Path('outer').write_text('retained outer object'); Path('inner').mkdir()
code='SHARED='+repr(SHARED)+'\\nOUTER='+repr(str(Path.cwd()/'outer'))+'\\n'+CHILD
def replace():
    metadata_exec([sys.executable,'-c',code],[str(Path.cwd()/'inner')])
thread=threading.Thread(target=replace); thread.start(); thread.join()
raise AssertionError('exec returned')
'''
    execute(tmp_path, 'CHILD=' + repr(child) + '\n' + program)


def stopped(event=0, sig=signal.SIGTRAP):
    return event << 16 | sig << 8 | 0x7f


@pytest.mark.parametrize('creation', [1, 2, 3])
@pytest.mark.parametrize('child_first', [True, False])
def test_auto_attached_child_waits_for_its_actual_parent_policy(monkeypatch, creation, child_first):
    from auto_agents import verification_metadata as metadata
    parent, child = 10, 11
    policy = (object(), object())  # Already narrowed parent, not the root policy.
    tasks, pending, calls = {parent: policy}, {}, []
    initial = (child, stopped(sig=signal.SIGSTOP))
    birth = (parent, stopped(creation))
    events = iter([*( [initial, birth] if child_first else [birth, initial]),
                   (child, stopped(7)), (child, 0), (parent, 0)])
    def wait(*args):
        event = next(events)
        if child_first and event == birth:
            assert child not in [pid for op, pid in calls if op == 7]
            assert child in pending and child not in tasks
        return event
    def ptrace(operation, pid, address=0, data=0):
        calls.append((operation, pid))
        if operation == 0x4201:
            ctypes.cast(data, ctypes.POINTER(ctypes.c_ulonglong))[0] = child
        if operation == 7 and pid == child:
            assert tasks[child] is policy
    checked = []
    monkeypatch.setattr(metadata.os, 'waitpid', wait)
    monkeypatch.setattr(metadata, '_ptrace', ptrace)
    monkeypatch.setattr(metadata, '_event', lambda pid, inherited, *_: checked.append((pid, inherited)))
    assert metadata._trace_loop(parent, tasks, {}, pending) == 0
    assert checked == [(child, policy)] and not pending


def test_unregistered_syscall_never_gets_a_guessed_policy(monkeypatch):
    from auto_agents import verification_metadata as metadata
    tasks, pending, calls = {10: (object(),)}, {}, []
    monkeypatch.setattr(metadata.os, 'waitpid', lambda *_: (11, stopped(7)))
    monkeypatch.setattr(metadata, '_ptrace', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match='without a registered parent policy'):
        metadata._trace_loop(10, tasks, {}, pending)
    assert 11 in pending and 11 not in tasks and not calls


def test_exec_keeps_the_origin_threads_narrower_policy(monkeypatch):
    from auto_agents import verification_metadata as metadata
    outer, narrowed = (object(),), (object(), object())
    tasks, pending, observed = {10: outer, 11: outer, 12: narrowed}, {}, []
    events = iter([(11, stopped(4)), (11, stopped(7)), (12, 0), (11, 0), (10, 0)])
    def ptrace(operation, pid, address=0, data=0):
        if operation == 0x4201:
            ctypes.cast(data, ctypes.POINTER(ctypes.c_ulonglong))[0] = 12
    monkeypatch.setattr(metadata.os, 'waitpid', lambda *_: next(events))
    monkeypatch.setattr(metadata, '_ptrace', ptrace)
    monkeypatch.setattr(metadata, '_event', lambda pid, inherited, *_: observed.append((pid, inherited)))
    assert metadata._trace_loop(10, tasks, {}, pending) == 0
    assert observed == [(11, narrowed)]


def test_child_killed_during_registration_does_not_leave_parent_stopped(monkeypatch):
    from auto_agents import verification_metadata as metadata
    tasks, pending, continued = {10: (object(),)}, {}, []
    events = iter([(11, stopped(sig=signal.SIGSTOP)), (10, stopped(1)), (11, signal.SIGKILL), (10, 0)])
    def ptrace(operation, pid, address=0, data=0):
        if operation == 0x4201:
            ctypes.cast(data, ctypes.POINTER(ctypes.c_ulonglong))[0] = 11
        elif operation == 7:
            continued.append(pid)
            if pid == 11:
                raise ProcessLookupError(3, 'killed while stopped')
    monkeypatch.setattr(metadata.os, 'waitpid', lambda *_: next(events))
    monkeypatch.setattr(metadata, '_ptrace', ptrace)
    assert metadata._trace_loop(10, tasks, {}, pending) == 0
    assert continued == [11, 10] and not pending
