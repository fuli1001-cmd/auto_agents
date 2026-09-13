"""One inherited ptrace supervisor for private chmod, without a seccomp listener.

The tracee never executes a pathname metadata syscall after authorization. The
supervisor pins the object, checks every ancestor policy, changes that descriptor's
inode, and substitutes the return value. Nested boundaries can only add policies.
Other metadata operations keep the existing fail-closed seccomp restrictions.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import re
import signal
import stat
import sys

POLICY_REQUEST = 0x41414D44
VERSION = 1
_libc = ctypes.CDLL(None, use_errno=True)
_libc.ptrace.restype = ctypes.c_long
_libc.syscall.restype = ctypes.c_long


class Registers(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        'r15 r14 r13 r12 rbp rbx r11 r10 r9 r8 rax rcx rdx rsi rdi orig_rax '
        'rip cs eflags rsp ss fs_base gs_base ds es fs gs').split()]


def _ptrace(request, pid, address=0, data=0):
    ctypes.set_errno(0)
    result = _libc.ptrace(ctypes.c_uint(request), ctypes.c_int(pid),
                          ctypes.c_void_p(address), data if not isinstance(data, int) else ctypes.c_void_p(data))
    if result == -1 and ctypes.get_errno():
        raise OSError(ctypes.get_errno(), 'metadata supervision ptrace failed')
    return result


def _read(pid, address, size=None):
    result = bytearray()
    limit = size if size is not None else 4096
    for offset in range(0, limit, 8):
        word = _ptrace(2, pid, address + offset) & ((1 << 64) - 1)  # PEEKDATA
        block = word.to_bytes(8, sys.byteorder)[:min(8, limit - offset)]
        if size is None and b'\0' in block:
            return bytes(result + block.split(b'\0', 1)[0])
        result.extend(block)
    if size is None:
        raise OSError(errno.ENAMETOOLONG, 'metadata path too long')
    return bytes(result)


class Root:
    def __init__(self, path, *, directory=True):
        self.path = str(Path(path).resolve(strict=True))
        self.fd = os.open(self.path, os.O_PATH | os.O_CLOEXEC | (os.O_DIRECTORY if directory else 0))

    def __del__(self):
        if hasattr(self, 'fd'):
            os.close(self.fd)

    def contains(self, path, identity):
        try:
            relative = str(Path(path).relative_to(os.readlink(f'/proc/self/fd/{self.fd}')))
        except ValueError:
            return False
        # openat2 pins a lookup beneath the original directory, including after
        # a rename or symlink race. Compare the inode with the already pinned object.
        class How(ctypes.Structure):
            _fields_ = [('flags', ctypes.c_uint64), ('mode', ctypes.c_uint64), ('resolve', ctypes.c_uint64)]
        how = How(os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW, 0, 0x08 | 0x02)  # BENEATH | NO_MAGICLINKS
        fd = _libc.syscall(437, self.fd, ctypes.c_char_p(os.fsencode(relative)), ctypes.byref(how), ctypes.sizeof(how))
        if fd < 0:
            return False
        try:
            value = os.fstat(fd)
            return (value.st_dev, value.st_ino) == identity
        finally:
            os.close(fd)


class Policy:
    def __init__(self, roots, readonly):
        if not roots or len(roots) + len(readonly) > 128:
            raise ValueError('invalid metadata boundary roots')
        if any(not isinstance(p, str) or not Path(p).is_absolute() for p in [*roots, *readonly]):
            raise ValueError('metadata roots must be absolute')
        self.roots = [Root(p) for p in roots]
        self.readonly = [Root(p) for p in readonly if Path(p).is_dir()]
        self.readonly_files = [(str(Path(p).resolve()), Root(p, directory=False) if Path(p).exists() else None)
                               for p in readonly if not Path(p).is_dir()]

    def allows(self, path, value):
        identity = (value.st_dev, value.st_ino)
        files = [(name, os.fstat(pin.fd) if pin else None) for name, pin in self.readonly_files]
        return (not any(path == name or value is not None and (value.st_dev, value.st_ino) == identity
                        for name, value in files)
                and not any(Path(path).is_relative_to(root.path) or root.contains(path, identity)
                            for root in self.readonly)
                and any(root.contains(path, identity) for root in self.roots))


def _filter():
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    names = {}
    context = library.seccomp_init(0x7fff0000)
    if not context:
        raise RuntimeError('metadata syscall filter unavailable')
    try:
        actions = {
            0x7ff00000: ('chmod', 'fchmod', 'fchmodat', 'fchmodat2', 'utime', 'utimes',
                         'utimensat', 'futimesat', 'prctl', 'kill', 'tkill', 'tgkill'),
            0x00050000 | errno.EPERM: (
                'chown', 'lchown', 'fchown', 'fchownat',
                'setxattr', 'lsetxattr', 'fsetxattr', 'removexattr', 'lremovexattr', 'fremovexattr',
                'ptrace', 'process_vm_readv', 'process_vm_writev', 'pidfd_getfd', 'pidfd_send_signal',
                'rt_sigqueueinfo', 'rt_tgsigqueueinfo',
                'unshare', 'setns', 'chroot', 'pivot_root', 'mount', 'umount2', 'move_mount', 'open_tree', 'fsopen', 'fsmount',
                'fsconfig', 'mount_setattr', 'io_uring_setup', 'io_uring_enter', 'io_uring_register')}
        for action, calls in actions.items():
            for name in calls:
                number = library.seccomp_syscall_resolve_name(name.encode())
                if number < 0 or library.seccomp_rule_add(context, action, number, 0):
                    raise RuntimeError('metadata syscall is unavailable: ' + name)
                names[number] = name
        # A tracee must not create a different mount/user view via clone either.
        class Compare(ctypes.Structure):
            _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                       ('mask', ctypes.c_uint64), ('value', ctypes.c_uint64)]
        number = library.seccomp_syscall_resolve_name(b'clone')
        for flag in (0x00020000, 0x10000000, 0x00800000):  # NEWNS, NEWUSER, UNTRACED
            condition = Compare(0, 7, flag, flag)  # MASKED_EQ
            if library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 1, condition):
                raise RuntimeError('metadata namespace filter unavailable')
        # ENOSYS retains libc's ordinary clone fallback without interpreting a mutable clone3 struct.
        if library.seccomp_rule_add(context, 0x00050000 | errno.ENOSYS,
                                   library.seccomp_syscall_resolve_name(b'clone3'), 0):
            raise RuntimeError('metadata clone3 filter unavailable')
        if _libc.prctl(38, 1, 0, 0, 0) or library.seccomp_load(context):
            raise RuntimeError('metadata syscall filter could not be installed')
        return names
    finally:
        library.seccomp_release(context)


def _change(pid, registers, name, policies):
    signed = lambda value: ctypes.c_int(value & 0xffffffff).value
    timestamps = name in {'utime', 'utimes', 'utimensat', 'futimesat'}
    nofollow = False
    anchored = False
    if name == 'fchmod':
        descriptor, mode = signed(registers.rdi), registers.rsi
        info = Path(f'/proc/{pid}/fdinfo/{descriptor}').read_text()
        flags = next(int(line.split()[1], 8) for line in info.splitlines() if line.startswith('flags:'))
        if flags & os.O_PATH:
            return -errno.EBADF
        path = f'/proc/{pid}/fd/{descriptor}'
    else:
        direct = name in {'chmod', 'utime', 'utimes'}
        pointer = registers.rdi if direct else registers.rsi
        raw = os.fsdecode(_read(pid, pointer)) if pointer else ''
        mode = registers.rsi if direct else registers.rdx
        flags = registers.r10 if name in {'fchmodat2', 'utimensat'} else 0
        nofollow = bool(flags & 0x100)
        if flags & ~(0x100 | (0x1000 if name == 'fchmodat2' else 0)):
            return -errno.EOPNOTSUPP
        if not raw and (name == 'utimensat' and not pointer or name == 'fchmodat2' and flags & 0x1000):
            path = f'/proc/{pid}/fd/{signed(registers.rdi)}'
        elif not raw:
            return -errno.ENOENT
        elif raw.startswith('/'):
            match = re.fullmatch(r'/(?:proc/(self|thread-self)/fd|dev/fd)/(\d+)', raw)
            if match:
                task = pid
                if match[1] != 'thread-self':
                    task = int(next(line.split()[1] for line in Path(f'/proc/{pid}/status').read_text().splitlines()
                                    if line.startswith('Tgid:')))
                path = f'/proc/{task}/fd/{match[2]}'
            else:
                anchor, path, anchored = f'/proc/{pid}/root', raw.lstrip('/'), True
        else:
            directory = -100 if direct else signed(registers.rdi)
            anchor = f'/proc/{pid}/cwd' if directory == -100 else f'/proc/{pid}/fd/{directory}'
            path, anchored = raw, True
    flags = os.O_PATH | os.O_CLOEXEC | (os.O_NOFOLLOW if nofollow else 0)
    if anchored:
        # Resolve proc magic links in the caller's context, never the supervisor's.
        # Explicit self-fd aliases above use the tracee's descriptor table. Reject
        # indirect magic-link aliases rather than accidentally resolving /proc/self
        # as the supervisor.
        class How(ctypes.Structure):
            _fields_ = [('flags', ctypes.c_uint64), ('mode', ctypes.c_uint64), ('resolve', ctypes.c_uint64)]
        parent = os.open(anchor, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            how = How(flags, 0, 0x02)
            fd = _libc.syscall(437, parent, ctypes.c_char_p(os.fsencode(path)), ctypes.byref(how), ctypes.sizeof(how))
            if fd < 0:
                raise OSError(errno.EPERM if ctypes.get_errno() == errno.ELOOP else ctypes.get_errno(),
                              'metadata path cannot be resolved safely')
            try:
                observed = os.stat(path, dir_fd=parent, follow_symlinks=not nofollow)
                pinned = os.fstat(fd)
                if (observed.st_dev, observed.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise OSError(errno.EPERM, 'metadata path changed while being pinned')
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(parent)
    else:
        fd = os.open(path, flags)
    try:
        value = os.fstat(fd)
        actual = os.readlink(f'/proc/self/fd/{fd}')
        if nofollow and stat.S_ISLNK(value.st_mode) and not timestamps:
            return -errno.EOPNOTSUPP
        if (not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode) or nofollow and stat.S_ISLNK(value.st_mode))
                or not stat.S_ISDIR(value.st_mode) and value.st_nlink != 1
                or not all(policy.allows(actual, value) for policy in policies)):
            return -errno.EPERM
        if timestamps:
            numbers = [0, (1 << 30) - 1, 0, (1 << 30) - 1]
            if mode:
                size = 16 if name == 'utime' else 32
                data = _read(pid, mode, size)
                values = [int.from_bytes(data[i:i+8], sys.byteorder, signed=True) for i in range(0, size, 8)]
                numbers = ([values[0], 0, values[1], 0] if name == 'utime' else
                           [values[0], values[1] * 1000, values[2], values[3] * 1000]
                           if name in {'utimes', 'futimesat'} else values)
            array = (ctypes.c_long * 4)(*numbers)
            # AT_EMPTY_PATH targets the pinned inode, including symlinks; no
            # path re-resolution or tracee memory is used when applying the change.
            if _libc.syscall(280, fd, ctypes.c_char_p(b''), array, 0x1000 | (0x100 if nofollow else 0)):
                raise OSError(ctypes.get_errno(), 'private timestamp operation failed')
        else:
            os.chmod(f'/proc/self/fd/{fd}', mode & 0o7777)
        return 0
    finally:
        os.close(fd)


def _event(pid, policies, names, tasks, traces=None):
    registers = Registers()
    _ptrace(12, pid, data=ctypes.byref(registers))  # GETREGS
    name = names.get(registers.orig_rax)
    result = -errno.EPERM if name is None else None
    try:
        if name == 'prctl' and registers.rdi == 0x41415452 and traces is not None:
            result = traces.request(pid, registers, policies)
        elif name == 'prctl' and registers.rdi == POLICY_REQUEST:
            if registers.rdx == 0:
                registers.orig_rax = (1 << 64) - 1
                registers.rax = VERSION
                _ptrace(13, pid, data=ctypes.byref(registers))
                return
            if registers.rdx > 65536:
                raise ValueError('metadata policy too large')
            value = json.loads(_read(pid, registers.rsi, registers.rdx))
            if len(policies) >= 64:
                raise ValueError('metadata nesting limit reached')
            tasks[pid] = (*policies, Policy(value['roots'], value.get('readonly', [])))
            result = 0
        elif name in {'chmod', 'fchmod', 'fchmodat', 'fchmodat2', 'utime', 'utimes', 'utimensat', 'futimesat'}:
            result = _change(pid, registers, name, policies)
        elif name in {'kill', 'tkill', 'tgkill'}:
            number = registers.rdx if name == 'tgkill' else registers.rsi
            if number == 0:
                # Native signal-zero checks have no side effects. In particular,
                # ESRCH must remain distinguishable from EPERM after a group exits;
                # cleanup and proof admission rely on that distinction.
                return
            target = ctypes.c_int(registers.rsi if name == 'tgkill' else registers.rdi).value
            group = -target if name == 'kill' and target < -1 else None
            if group and group != os.getpgrp():
                members = []
                for tid in tasks:
                    try:
                        if os.getpgid(tid) == group:
                            members.append(tid)
                    except ProcessLookupError:
                        continue
                permitted = members and all(tasks[tid][:len(policies)] == policies for tid in members)
            else:
                permitted = target > 0 and target in tasks and tasks[target][:len(policies)] == policies
            if not permitted:
                result = -errno.EPERM
        elif name == 'prctl' and registers.rdi in {4, 0x59616d61}:  # dumpability / PR_SET_PTRACER
            result = -errno.EPERM
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        result = -getattr(error, 'errno', None) if getattr(error, 'errno', None) else -errno.EPERM
    if result is not None:
        registers.orig_rax = (1 << 64) - 1  # Never continue the original pathname operation.
        registers.rax = result & ((1 << 64) - 1)
        _ptrace(13, pid, data=ctypes.byref(registers))


def _trace_loop(leader, tasks, names, pending):
    """Do not resume an auto-attached child before its parent's creation event.

    waitpid can report the child's initial SIGSTOP before the parent's FORK,
    VFORK or CLONE event. Only the latter establishes the inherited policy.
    """
    from auto_agents.verification_input_trace import TraceSessions
    traces = TraceSessions()
    def resume(pid, delivered=0):
        try:
            _ptrace(24 if pid in traces.sessions else 7, pid, data=delivered)
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise
    retired = set()
    while tasks or pending:
        pid, status = os.waitpid(-1, 0x40000000)  # __WALL includes traced threads.
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            traces.retire(pid, terminated=os.WIFSIGNALED(status))
            known = pid in tasks or pid in retired
            tasks.pop(pid, None)
            retired.discard(pid)
            pending.pop(pid, None)
            if pid == leader:
                return os.waitstatus_to_exitcode(status)
            if not known:
                raise RuntimeError(f'metadata child {pid} exited before its parent policy was registered')
            continue
        event = status >> 16
        delivered = os.WSTOPSIG(status)
        try:
            if event == 4:  # exec can replace the thread-group leader.
                former = ctypes.c_ulonglong()
                _ptrace(0x4201, pid, data=ctypes.byref(former))
                if former.value not in tasks:
                    pending[pid] = status
                    raise RuntimeError(f'metadata exec task {pid} has no registered origin policy for {former.value}')
                if former.value != pid:
                    tasks[pid] = tasks.pop(former.value)
                    retired.add(former.value)
                traces.exec(former.value, pid)
                pending.pop(pid, None)
            if pid not in tasks:
                pending[pid] = status  # Also retain it for fail-closed cleanup.
                if event or delivered != signal.SIGSTOP:
                    raise RuntimeError(f'metadata event {event} for task {pid} arrived without a registered parent policy')
                continue  # Never PTRACE_CONT with an absent or guessed policy.
            if event in (1, 2, 3):  # fork, vfork, clone
                child = ctypes.c_ulonglong()
                _ptrace(0x4201, pid, data=ctypes.byref(child))
                if child.value in tasks:
                    raise RuntimeError('metadata child already has an active policy')
                retired.discard(child.value)
                tasks[child.value] = tasks[pid]
                traces.inherit(pid, child.value)
                if child.value in pending:
                    pending.pop(child.value)
                    resume(child.value)
            elif event == 7:
                _event(pid, tasks[pid], names, tasks, traces)
            elif delivered == signal.SIGTRAP | 0x80:
                # GET_SYSCALL_INFO distinguishes entry/exit even after exec,
                # seccomp stops, signal delivery and child registration.
                info = ctypes.create_string_buffer(88)
                _ptrace(0x420e, pid, 88, ctypes.byref(info))
                registers = Registers()
                _ptrace(12, pid, data=ctypes.byref(registers))
                if info.raw[0] == 1:
                    traces.enter(pid, registers)
                elif info.raw[0] == 2:
                    traces.exit(pid, ctypes.c_longlong(registers.rax).value)
            resume(pid, 0 if event or delivered in (signal.SIGSTOP, signal.SIGTRAP | 0x80) else delivered)
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise
            # SIGKILL can win a race with ptrace. Keep the known identity until
            # waitpid reports its terminal status; never infer another policy.
    return 125


def metadata_exec(command, roots, readonly=()):
    """Execute through a new supervisor, or narrow the existing ancestor's policy."""
    if platform.machine() != 'x86_64' or not sys.platform.startswith('linux'):
        raise RuntimeError('private metadata supervision requires Linux x86_64')
    encoded = json.dumps({'roots': list(roots), 'readonly': list(readonly)}).encode()
    result = _libc.prctl(POLICY_REQUEST, ctypes.c_char_p(encoded), len(encoded), 0, 0)
    if result == 0:
        from auto_agents.verification_sandbox import restrict_nested_writes
        restrict_nested_writes(roots)
        os.chdir(roots[0])
        os.execvp(command[0], command)
    if ctypes.get_errno() != errno.EINVAL:
        raise RuntimeError('inherited metadata supervisor refused the boundary')
    policy = Policy(roots, readonly)
    # The tracee cannot inspect or alter the supervisor through /proc or ptrace.
    if _libc.prctl(4, 0, 0, 0, 0):
        raise RuntimeError('metadata supervisor protection unavailable')
    leader = os.fork()
    if not leader:
        try:
            _libc.prctl(4, 1, 0, 0, 0)
            _ptrace(0, 0)  # TRACEME; no additional notification listener.
            os.kill(os.getpid(), signal.SIGSTOP)
            _filter()
            from auto_agents.verification_sandbox import restrict_nested_writes
            restrict_nested_writes(roots)
            os.chdir(roots[0])
            os.execvp(command[0], command)
        except BaseException as error:
            print('private metadata launcher unavailable: ' + str(error), file=sys.stderr, flush=True)
            os._exit(125)
    tasks = {leader: (policy,)}
    pending = {}
    exitcode = 125
    try:
        _, status = os.waitpid(leader, 0)
        if not os.WIFSTOPPED(status):
            return os.waitstatus_to_exitcode(status)
        # All descendants remain traced, and are killed if this supervisor dies.
        _ptrace(0x4200, leader, data=0x00100000 | 0x01 | 0x80 | 0x02 | 0x04 | 0x08 | 0x10)
        library = ctypes.CDLL('libseccomp.so.2')
        names = {library.seccomp_syscall_resolve_name(name.encode()): name for name in
                 ('chmod', 'fchmod', 'fchmodat', 'fchmodat2', 'utime', 'utimes', 'utimensat', 'futimesat',
                  'prctl', 'kill', 'tkill', 'tgkill')}
        _ptrace(7, leader)
        exitcode = _trace_loop(leader, tasks, names, pending)
    finally:
        remaining = set(tasks) | set(pending)
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while remaining:
            try:
                pid, status = os.waitpid(-1, 0x40000000)
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    remaining.discard(pid)
                else:
                    remaining.add(pid)
                    _ptrace(7, pid, data=signal.SIGKILL)
            except (ChildProcessError, ProcessLookupError):
                break
    return exitcode if exitcode >= 0 else 128 - exitcode
