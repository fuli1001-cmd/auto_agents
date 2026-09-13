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

Input tracing uses that same owner. A gate selects its tracing backend inside
the final boundary, using a separate `prctl` protocol to query the live owner's
implementation identity and register a private output descriptor. Registration
checks the pinned inode against every ancestor policy. The existing tracer then
uses syscall entry/exit stops for that command and its descendants; `ptrace`
remains forbidden to tracees. No second tracer, listener or namespace is needed.
The original metadata policy version remains compatible with retained callers.

The owner resolves cwd and directory-fd inputs per stopped task, records negative
lookups and network use, and closes the trace only after all registered
descendants exit. Unsupported calls, ambiguous inputs, concurrent shared input
state, signal termination and missing footers prevent complete-input credit.
Anonymous controller output-spool `fstat` calls are runtime plumbing; reading
those unnameable files is still incomplete. Repeated observed paths are hashed
once during manifest construction.

With an old metadata owner, the gate runs the original command once without
claiming complete tracing. An unsupervised command can still use `strace`.
Certificates bind the selected helper sources and the negotiated live owner,
so importing candidate code cannot relabel an old owner as a new implementation.

Worker startup checks real input tracing before repair planning, using the
selected runtime's standalone helper even with a reused installed interpreter.
Both automatic quick and expanded checks use this worker's outer launcher;
nested gates negotiate with the owner it has already started. Production
capability evidence includes the exercised tracing protocol, owner identity and
activation route. This correction does not require candidate code to activate
itself, or permit an unverified candidate to replace the trusted outer boundary.

An auto-attached child's initial stop may precede the parent's fork/vfork/clone
event. The supervisor holds that child until the kernel identifies its parent
and the exact inherited policy is registered. It never resumes an unknown PID
with a default policy. Thread-to-leader exec retains the originating thread's
policy, and shutdown includes children still awaiting registration.

Signal-zero existence checks execute with native kernel semantics; they neither
deliver a signal nor grant permission to signal an unrelated process. Preserving
ESRCH for exited process groups prevents false incomplete-cleanup reports.
Incomplete cleanup still prevents caching a success, and cached proofs bind both
the metadata supervisor and gate launcher implementation fingerprints.

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
