"""Nested project verification through inherited metadata supervision."""
import ctypes
from contextlib import contextmanager
import json
from pathlib import Path
import sys

# Direct gate launchers use their selected runtime, not the managed environment's
# possibly older installed package. Ordinary library imports do not change sys.path.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_agents import artifact_temp as tempfile


@contextmanager
def confined_session_executor(executor):
    # Legacy custody may live beneath the shared project. Gate worktrees must
    # be separate so the complete shared tree can remain read-only.
    with tempfile.TemporaryDirectory(prefix='session-gates-') as temporary:
        executor.worktree_root = Path(temporary)
        with executor as active:
            yield active


def restrict_gate_metadata():
    """Legacy fail-closed filter for callers without supervised dispatch.

    New launchers use metadata_exec instead. This compatibility helper cannot
    authorize private metadata writes or undo an ancestor's syscall denials.
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
    from auto_agents.verification_metadata import metadata_exec
    raise SystemExit(metadata_exec(sys.argv[3:], roots))
