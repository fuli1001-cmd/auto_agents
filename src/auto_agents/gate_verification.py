"""Nested project verification launcher with inherited content/metadata limits."""
import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

from auto_agents import artifact_temp as tempfile
from auto_agents.verification_sandbox import restrict_nested_writes


@contextmanager
def confined_session_executor(executor):
    # Legacy custody may live beneath the shared project. Gate worktrees must
    # be separate so the complete shared tree can remain read-only.
    with tempfile.TemporaryDirectory(prefix='session-gates-') as temporary:
        executor.worktree_root = Path(temporary)
        with executor as active:
            yield active


def restrict_gate_metadata():
    """Close Landlock's metadata gap when nesting inside an existing sandbox.

    Landlock covers content, links, renames and deletion, but not chmod/chown
    or timestamps/xattrs. A nested verification conservatively cannot alter these
    metadata fields, even on private files; creation modes remain supported.
    The filter is inherited across exec and by every tool descendant.
    """
    import errno
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError('verification metadata confinement is unavailable')
    try:
        for name in ('chmod', 'fchmod', 'fchmodat', 'fchmodat2', 'chown', 'lchown',
                     'fchown', 'fchownat', 'utime', 'utimes', 'futimesat', 'utimensat',
                     'setxattr', 'lsetxattr', 'fsetxattr', 'removexattr', 'lremovexattr', 'fremovexattr'):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                raise RuntimeError('verification metadata syscall is unavailable: ' + name)
            if library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0):
                raise RuntimeError('could not bind verification metadata filter')
        if library.seccomp_load(context):
            raise RuntimeError('could not enforce verification metadata filter')
    finally:
        library.seccomp_release(context)


if __name__ == '__main__':
    if len(sys.argv) < 5 or sys.argv[1] != '--landlock':
        raise SystemExit('internal gate launcher requires --landlock ROOTS COMMAND')
    roots = json.loads(sys.argv[2])
    restrict_nested_writes(roots)
    restrict_gate_metadata()
    os.chdir(roots[0])
    os.execvp(sys.argv[3], sys.argv[3:])
