# Private metadata during nested verification

Verification must allow private candidate permissions to be restored while
keeping shared files unchanged. The former nested gate filter denied every
`chmod`/`fchmod`, including private files. An additional user namespace or seccomp
notification listener is not available in all production wrappers.

`verification_metadata.metadata_exec` supplies one ptrace supervisor inside the
existing verification sandbox. A seccomp TRACE filter reports permission and
timestamp operations to that supervisor without allocating a notification
listener. Forks, threads and exec descendants retain supervision. A nested
launcher requests another policy through an intercepted `prctl`; the new policy
is intersected with every ancestor policy. An environment marker alone does not
authorize reuse. Both retained `--landlock` and `--writer-landlock` entry protocols
use this mechanism; generic verification additionally supplies its read-only
roots, including repository metadata.

Direct verification and gate launchers bind their engine imports to the source
root containing the launcher, before importing any `auto_agents` module. Managed
interpreters are cached by dependency metadata and can contain an older installed
engine, while sandbox preflight intentionally runs in a workspace without `src`.
The launcher must therefore not rely on that workspace's `PYTHONPATH` or the
cached installation. This bootstrap is local to the launcher interpreter; the
executed project command retains its original import environment.

The supervisor pins a requested object, verifies its identity and membership
under pinned directory descriptors, then performs the operation on that inode.
It substitutes the syscall result instead of resuming a pathname operation in
the tracee. Shared descriptors and symlinks cannot become private authority.
`/proc/self/fd` is interpreted using the tracee's descriptor table; indirect
magic-link resolution is rejected. Changed or ambiguous path identities are
denied. Files with multiple hard links are conservatively denied metadata writes.
Private file and directory permissions and timestamps remain supported; other
metadata syscall families retain the conservative deny rules.

The supervisor is non-dumpable. Tracees cannot ptrace it, access its memory, signal
it through the supported signal APIs, or create a replacement mount/user view.
Unsupported clone3 requests receive ENOSYS so ordinary libc process creation can
use clone. The tracer uses EXITKILL and reaps remaining descendants at command
completion. Unsupported supervision blocks before executing the command.

This implementation currently requires Linux x86_64, ptrace of child processes,
seccomp TRACE, openat2, and the existing Landlock ABI 3 boundary. It preserves
inherited kernel restrictions: an ancestor's stronger seccomp denial cannot be
overridden by the supervisor. The outer launcher can use its already-private
`/tmp`; nested policies receive only their own admitted roots and scratch.

Production capability observations run through the real outer wrapper and
nested gate. They check private chmod/fchmod/timestamps and shared path/descriptor
denial. A supported metadata mechanism is independent of nested namespace
availability and is supplied to repair planning as an alternative. These
observations are diagnostics, not repair acceptance credit. Real provider
dispatch, recovery and compatibility tests remain required.

See the kernel's [Landlock limitations](https://docs.kernel.org/userspace-api/landlock.html)
and the [ptrace syscall and seccomp event documentation](https://man7.org/linux/man-pages/man2/ptrace.2.html)
for the underlying APIs and the reason pathname validation must not be followed
by an unguarded tracee syscall.
