"""Small native capability observations for read-only repair planning.

The seccomp experiment runs only in a disposable child. Its allow filter does
not relax inherited restrictions and never intercepts or services a syscall.
These observations constrain a design; they are not acceptance evidence.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def observe():
    result = {'version': 1, 'python_version': sys.version.split()[0],
              'python_apis': {name: hasattr(os, name) for name in ('memfd_create', 'pidfd_open')},
              'kernel': list(os.uname())[2:5] if hasattr(os, 'uname') else [sys.platform],
              'scope': 'verification interpreter with inherited worker restrictions',
              'acceptance_proof': False}
    if not sys.platform.startswith('linux'):
        result['seccomp_notification'] = {'status': 'not_applicable'}
        return result
    descriptors = []
    try:
        seccomp = ctypes.CDLL('libseccomp.so.2', use_errno=True)
        seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        number = seccomp.seccomp_syscall_resolve_name(b'seccomp')
        if number < 0:
            raise OSError('seccomp syscall is unavailable')
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.syscall.restype = ctypes.c_long
        if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS, this child only.
            raise OSError(ctypes.get_errno(), 'no_new_privs unavailable')
        class Instruction(ctypes.Structure):
            _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte), ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]
        class Program(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ushort), ('instructions', ctypes.POINTER(Instruction))]
        instructions = (Instruction * 1)(Instruction(0x06, 0, 0, 0x7fff0000))  # BPF_RET ALLOW.
        program = Program(1, instructions)
        def install():
            fd = libc.syscall(ctypes.c_long(number), ctypes.c_uint(1), ctypes.c_uint(8), ctypes.byref(program))
            if fd >= 0:
                descriptors.append(fd)
            return fd, ctypes.get_errno() if fd < 0 else 0
        first, first_error = install()
        if first < 0:
            result['seccomp_notification'] = {'status': 'initial_listener_unavailable', 'errno': first_error,
                'additional_listener_supported': False if first_error == errno.EBUSY else None}
        else:
            second, second_error = install()
            result['seccomp_notification'] = {'status': 'observed', 'first_listener_opened': True,
                'second_listener_errno': second_error,
                'additional_listener_supported': True if second >= 0 else False if second_error == errno.EBUSY else None}
    except (OSError, AttributeError) as error:
        result['seccomp_notification'] = {'status': 'unavailable', 'reason': str(error),
                                           'additional_listener_supported': None}
    finally:
        for fd in descriptors:
            os.close(fd)
    return result


def planning_capabilities(runner):
    python = runner._verification_python()
    script = Path(__file__).resolve()
    try:
        stat = Path(python).stat()
        key = (python, stat.st_mtime_ns, stat.st_size, hashlib.sha256(script.read_bytes()).hexdigest())
    except OSError:
        key = (python, 'unavailable')
    cached = getattr(runner, '_planning_capability_cache', None)
    if cached and cached[0] == key:
        return cached[1]
    try:
        process = subprocess.run([python, str(script)], capture_output=True, text=True, timeout=10)
        if process.returncode:
            raise ValueError('capability child exited ' + str(process.returncode))
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or result.get('version') != 1 or result.get('acceptance_proof') is not False:
            raise ValueError('invalid capability observation')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result = {'version': 1, 'status': 'inconclusive', 'reason': str(error), 'acceptance_proof': False}
    runner._planning_capability_cache = (key, result)
    return result


if __name__ == '__main__':
    print(json.dumps(observe(), sort_keys=True))
