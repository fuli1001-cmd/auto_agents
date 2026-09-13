"""Input observation by the existing metadata owner, never a second ptracer.

The supervisor owns the stream and resolves paths in each stopped task's cwd/fd
namespace. A footer is emitted only after every registered descendant exits.
Unknown calls, unresolved paths and truncated streams cannot certify reuse.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

TRACE_REQUEST = 0x41415452
TRACE_PROTOCOL = 1


def owner_identity():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    version = libc.prctl(0x41414D44, 0, 0, 0, 0)
    protocol = libc.prctl(TRACE_REQUEST, 0, 0, 0, 0) if version >= 1 else 0
    parts = [libc.prctl(TRACE_REQUEST, index, 0, 0, 0) for index in range(1, 9)] if protocol == TRACE_PROTOCOL else []
    identity = ''.join(format(value, '08x') for value in parts) if parts and min(parts) >= 0 else ''
    return {'metadata': max(0, version), 'trace': TRACE_PROTOCOL if identity else 0, 'owner': identity}


# x86_64 syscall names are resolved by libseccomp, not copied syscall numbers.
SINGLE = set("""
open stat lstat access readlink execve mkdir unlink rmdir chmod chown lchown utime utimes truncate
statfs mknod chdir
""".split())
AT = set("""
openat openat2 newfstatat statx faccessat faccessat2 readlinkat mkdirat unlinkat mknodat fchmodat
fchmodat2 fchownat utimensat futimesat execveat name_to_handle_at
""".split())
FD = set("""
read pread64 readv preadv preadv2 fstat fstatfs getdents getdents64 fchdir mmap readahead fchmod
fchown ftruncate
""".split())
NETWORK = set("""
socket socketpair connect sendto sendmsg sendmmsg recvfrom recvmsg recvmmsg accept accept4 bind
listen getsockname getpeername setsockopt getsockopt shutdown
""".split())
NEUTRAL = set("""
write writev pwrite64 pwritev pwritev2 close close_range dup dup2 dup3 fcntl lseek brk mprotect
munmap mremap madvise mincore msync rt_sigaction rt_sigprocmask rt_sigreturn rt_sigsuspend
rt_sigpending rt_sigtimedwait sigaltstack restart_syscall ioctl poll ppoll select pselect6 pipe
pipe2 wait4 waitid exit exit_group clone clone3 fork vfork getpid getppid gettid getpgrp getpgid
setpgid setsid getsid getuid geteuid getgid getegid getresuid getresgid getgroups uname sysinfo
times getrusage getrlimit setrlimit prlimit64 arch_prctl set_tid_address set_robust_list
get_robust_list futex futex_waitv rseq prctl getrandom clock_gettime clock_getres gettimeofday time
nanosleep clock_nanosleep clock_nanosleep_time64 sched_yield sched_getaffinity sched_setaffinity
sched_getparam sched_getscheduler sched_get_priority_min sched_get_priority_max getcpu eventfd
eventfd2 epoll_create epoll_create1 epoll_ctl epoll_wait epoll_pwait epoll_pwait2 timerfd_create
timerfd_settime timerfd_gettime timer_create timer_settime timer_gettime timer_delete alarm
setitimer getitimer kill tkill tgkill umask capget capset fsync fdatasync sync syncfs fadvise64
membarrier personality
""".split())


class Observation:
    def __init__(self, fd, identity, pid):
        self.file = os.fdopen(fd, 'w', encoding='utf-8', buffering=1)
        self.complete = True
        self.reasons = set()
        # The controller's anonymous output spools have no reusable pathname.
        # Their fstat is runtime plumbing; reads from them remain unresolved.
        self.outputs = set()
        for number in (1, 2):
            try:
                value = os.stat(f'/proc/{pid}/fd/{number}')
                if stat.S_ISREG(value.st_mode) and not value.st_nlink:
                    self.outputs.add((value.st_dev, value.st_ino))
            except OSError:
                pass
        self.emit({'format': 'auto-agents-input-trace', 'version': TRACE_PROTOCOL, 'owner': identity})

    def emit(self, row):
        self.file.write(json.dumps(row, ensure_ascii=True) + '\n')

    def incomplete(self, reason):
        self.complete = False
        self.reasons.add(reason)

    def finish(self):
        self.emit({'complete': self.complete, 'reasons': sorted(self.reasons)})
        self.file.close()


class TraceSessions:
    def __init__(self):
        self.sessions = {}
        self.pending = {}
        library = ctypes.CDLL('libseccomp.so.2')
        names = SINGLE | AT | FD | NETWORK | NEUTRAL | {
            'getcwd', 'rename', 'link', 'renameat', 'renameat2', 'linkat',
            'symlink', 'symlinkat', 'sendfile', 'copy_file_range', 'splice'}
        self.names = {library.seccomp_syscall_resolve_name(name.encode()): name for name in names}
        source = hashlib.sha256(Path(__file__).read_bytes() +
            Path(__file__).with_name('verification_metadata.py').read_bytes()).digest()
        self.parts = [int.from_bytes(source[index:index+4], 'big') & 0x7fffffff for index in range(0, 32, 4)]
        self.identity = ''.join(format(value, '08x') for value in self.parts)

    def request(self, pid, registers, policies):
        if registers.rdx == 0:
            return TRACE_PROTOCOL if registers.rsi == 0 else self.parts[registers.rsi-1] if 1 <= registers.rsi <= 8 else -errno.EINVAL
        if registers.rdx != TRACE_PROTOCOL or len(self.sessions.get(pid, ())) >= 8:
            return -errno.EINVAL
        source = f'/proc/{pid}/fd/{ctypes.c_int(registers.rsi).value}'
        fd = os.open(source, os.O_WRONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            value = os.fstat(fd)
            path = os.readlink(f'/proc/self/fd/{fd}')
            if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_size
                    or not all(policy.allows(path, value) for policy in policies)):
                return -errno.EPERM
            self.sessions[pid] = (*self.sessions.get(pid, ()), Observation(fd, self.identity, pid))
            fd = -1
            return 0
        finally:
            if fd >= 0:
                os.close(fd)

    def inherit(self, parent, child):
        if parent in self.sessions:
            self.sessions[child] = self.sessions[parent]

    def retire(self, pid, *, terminated=False):
        observations = self.sessions.pop(pid, ())
        self.pending.pop(pid, None)
        for observation in observations:
            if terminated:
                observation.incomplete('traced descendant terminated by signal')
            if not any(observation in rows for rows in self.sessions.values()):
                observation.finish()

    def exec(self, old, new):
        if old != new:
            self.retire(new)
            if old in self.sessions:
                self.sessions[new] = self.sessions.pop(old)
            if old in self.pending:
                self.pending[new] = self.pending.pop(old)
        self.exit(new, 0)

    def enter(self, pid, registers):
        from auto_agents.verification_metadata import _read
        rows = self.sessions[pid]
        name = self.names.get(registers.orig_rax, 'syscall-' + str(registers.orig_rax))
        args = [registers.rdi, registers.rsi, registers.rdx, registers.r10, registers.r8, registers.r9]
        paths = []
        def descriptor(value):
            raw = os.readlink(f'/proc/{pid}/fd/{ctypes.c_int(value).value}')
            if raw.startswith(('pipe:', 'anon_inode:')) or raw == '/dev/null':
                return None
            if raw.startswith('socket:'):
                for row in rows: row.emit({'network': True})
                return None
            if name == 'fstat' and raw.endswith(' (deleted)'):
                value = os.stat(f'/proc/{pid}/fd/{ctypes.c_int(value).value}')
                if all((value.st_dev, value.st_ino) in row.outputs for row in rows):
                    return None
            if not raw.startswith('/') or raw.endswith(' (deleted)'):
                raise ValueError('unresolved descriptor: ' + raw)
            return raw
        def path(pointer, directory=-100):
            raw = os.fsdecode(_read(pid, pointer))
            if not raw:
                return descriptor(directory)
            if raw.startswith('/'):
                # A proc self alias must use the tracee identity, never the owner.
                match = re.fullmatch(r'/(?:proc/(?:self|thread-self)/fd|dev/fd)/(\d+)(/.*)?', raw)
                if match:
                    base = descriptor(int(match[1]))
                    return base + (match[2] or '') if base else None
                for prefix in ('/proc/self/', '/proc/thread-self/'):
                    if raw.startswith(prefix):
                        actual = f'/proc/{pid}/' + raw[len(prefix):]
                        return os.path.realpath(actual)
                return raw
            base = os.readlink(f'/proc/{pid}/cwd') if ctypes.c_int(directory).value == -100 else descriptor(directory)
            if base is None or base.endswith(' (deleted)'):
                raise ValueError('unresolved cwd')
            return str(Path(base) / raw)
        try:
            if name == 'clone' and (args[0] & 0x200 or args[0] & 0x100 and not args[0] & 0x4000):
                for row in rows: row.incomplete('concurrent shared memory or cwd')
            if name == 'getcwd':
                pass  # Runtime cwd identity, not a directory listing.
            elif name in SINGLE:
                paths = [path(args[0])]
            elif name in AT:
                paths = [path(args[1], args[0]) if args[1] else descriptor(args[0])]
            elif name in FD:
                paths = [descriptor(args[4] if name == 'mmap' else args[0])] if name != 'mmap' or not args[3] & 0x20 else []
            elif name in {'rename', 'link'}:
                paths = [path(args[0]), path(args[1])]
            elif name in {'renameat', 'renameat2', 'linkat'}:
                paths = [path(args[1], args[0]), path(args[3], args[2])]
            elif name == 'symlink':
                paths = [path(args[1])]
            elif name == 'symlinkat':
                paths = [path(args[2], args[1])]
            elif name in {'sendfile', 'copy_file_range', 'splice'}:
                paths = [descriptor(args[1] if name == 'sendfile' else args[0])]
            elif name in NETWORK:
                for row in rows: row.emit({'network': True})
            elif name not in NEUTRAL:
                for row in rows: row.incomplete('unsupported syscall: ' + name)
        except (OSError, ValueError, UnicodeError) as error:
            for row in rows: row.incomplete('unresolved input: ' + name + ': ' + str(error))
        self.pending[pid] = [p for p in paths if p is not None]

    def exit(self, pid, result):
        paths = self.pending.pop(pid, [])
        for row in self.sessions.get(pid, ()):
            for path in paths:
                row.emit({'path': path, 'result': result})


def resolved_trace(text):
    """Adapt a complete owner stream to the existing conservative manifest parser."""
    try:
        rows = [json.loads(line) for line in text.splitlines()]
        if (len(rows) < 2 or rows[0].get('format') != 'auto-agents-input-trace'
                or rows[0].get('version') != TRACE_PROTOCOL or not rows[0].get('owner')
                or rows[-1].get('complete') is not True):
            return None
        result = []
        for row in rows[1:-1]:
            if row.get('network'):
                result.append('0 connect() = 0')
            elif 'path' in row:
                code = row['result']
                returned = f'-1 {errno.errorcode.get(-code, "UNKNOWN")}' if code < 0 else str(code)
                result.append('0 stat(' + json.dumps(row['path']) + ') = ' + returned)
            else:
                return None
        return '\n'.join(result)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def check_input_tracing(root, python, real_project):
    """Prove the selected worker's outer owner is active before repair planning."""
    import subprocess
    from auto_agents import artifact_temp as tempfile
    from auto_agents.verification_sandbox import verification_argv
    with tempfile.TemporaryDirectory(prefix='trace-probe-', dir=root) as temporary:
        workspace = Path(temporary)
        (workspace / 'input').write_text('retained')
        argv = [python, str(Path(__file__).resolve()), 'input-trace.json', 'sh', '-c', 'cat input']
        with verification_argv(argv, workspace, Path(real_project)) as command:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        try:
            stream = (workspace / 'input-trace.json').read_text()
            rows = [json.loads(line) for line in stream.splitlines()]
            valid = (not result.returncode and result.stdout == 'retained' and resolved_trace(stream) is not None
                     and any(row.get('path') == str(workspace / 'input') for row in rows))
        except (OSError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise RuntimeError('selected verification runtime cannot provide complete input tracing; '
                               'the outer owner must be upgraded before candidate acceptance: ' + result.stderr[-1000:])
        return {'owner': rows[0]['owner'], 'protocol': rows[0]['version']}


def main():
    destination, command = Path(sys.argv[1]), sys.argv[2:]
    destination.parent.mkdir(parents=True, exist_ok=True)
    owner = owner_identity()
    if owner['trace']:
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(TRACE_REQUEST, fd, TRACE_PROTOCOL, 0, 0):
                raise OSError(ctypes.get_errno(), 'input trace registration failed')
        finally:
            os.close(fd)
    elif not owner['metadata'] and shutil.which('strace'):
        os.execvp('strace', ['strace', '-f', '-qq', '-y', '-e',
            'trace=%file,%network,%process,fchdir,getdents64,unshare,setns,mount,umount2',
            '-o', str(destination), *command])
    else:
        # Legacy metadata owners cannot be replaced from inside their boundary.
        # Execute once; lack of tracing never fabricates observed-input evidence.
        destination.write_text(json.dumps({'complete': False, 'reason': 'live owner has no input tracing', 'owner': owner}) + '\n')
    os.execvp(command[0], command)


if __name__ == '__main__':
    # Preserve the executed command's imports while using the selected helper.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
