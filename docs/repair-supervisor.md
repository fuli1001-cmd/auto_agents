# Independent repair control

Eligible engine failures in `run`, `fix`, `collab`, and workflow resume are now
handed to a user-local supervisor. The foreground becomes a waiting relay;
it no longer generates candidates or owns the recovered business execution.
The existing sidecar remains an observer and does not acquire publishing rights.

The supervisor uses an immutable bootstrap and a committed implementation
worktree, Unix-domain IPC with descriptor transfer, and a private SQLite task
store. The project lock remains held across quiescence and process replacement.
The resumed CLI loads a verified SHA in a fresh interpreter, retaining the
original command, workflow/session, provider and approval arguments.

## Operator configuration and commands

The first registration pins the engine installation's explicitly tracked Git
remote and branch. Operator configuration and jobs live under
`$XDG_STATE_HOME/auto-agents/repair-control` (default
`~/.local/state/auto-agents/repair-control`). `AUTO_AGENTS_REPAIR_CONTROL_ROOT`
can select another private directory. Each engine identity has `operator.json`
with `remote`, `ref`, `python`, and `publish`. These are operator settings, not
project/model-controlled route fields. Embedded HTTP Git credentials are refused;
use the configured credential helper or SSH agent.

```
auto-agents repair status
auto-agents repair status --job JOB
auto-agents repair resume --job JOB
auto-agents repair cancel --job JOB
auto-agents repair retry-publish --job JOB
auto-agents stop --project PROJECT
```

The default is automatic publication to the explicitly tracked upstream after
proof and recovery. Set `publish` to false to disable writes to that remote.
`--autonomy off` disables enrollment; guarded mode may reuse proven remote fixes
but cannot generate code. `--no-health-watch` affects observation, not terminal
error recovery. `AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1` retains the legacy
in-process path for compatibility and isolated tests.

## Update, repair and recovery

1. Quiesce managed project children and transfer the held project lock. Freeze
   project evidence; no engine patch is applied to the target project.
2. Fetch the trusted branch in a private bare repository, never pull into the
   developer checkout. Three bounded attempts may fall back to a cached SHA,
   explicitly marked stale; an empty cache blocks recovery.
3. Prepare an engine-specific dependency environment. A behavior differential
   and an original-boundary replay must both pass to reuse a remote fix.
   Collection/import/setup failure is not proof that the engine is repaired.
4. Otherwise retain one repair workspace and provider continuation for focused
   edits and tests. Three non-improving attempts enter the existing deeper
   search without erasing the retained workspace or experiment. Approved proofs,
   reviews, full-suite checks and resumable validation retain their existing gates.
5. Independently validate each subscribing project, then launch its original
   invocation against the approved runtime. Native gate/stage/engine-route
   receipts can confirm recovery before the whole business goal finishes.
   Unknown boundaries conservatively require successful workflow completion.
6. Publish only after recovery confirmation. Source developer files, index and
   unrelated local commits are not promoted or pushed by this path.

Failures with the same fingerprint, contract, base and environment share one
local job, including its blocked state. Changing a child ID does not reset the
job. Each subscribing project still needs its own boundary check. There is one
repair/validation/publication worker at a time per supervisor; healthy business
processes continue independently on their own versions.

The supervisor records worker and resumed-process identities and adopts live
processes after restart. A foreground relay can transfer its still-held lock
again after a control restart. A late result from an obsolete generation is not
accepted. A user stop writes cancellation before signaling processes and prevents
automatic restart. An unexplained business crash without a durable repair request
is not treated as permission to repeat external side effects.

## Publication and evidence

Remote updates during repair are integrated only in a private publication
worktree. Equivalent upstream fixes are rechecked. Integration conflicts use
the scoped conflict resolver; the result must pass behavior, boundary and full
suite checks. Cached integration receipts avoid repeating successful validation
after a network-only push failure. All pushes are ordinary, non-forced pushes.

Publication retries are durable with delays of 1, 5, 15, then 60 minutes. A
permission failure becomes `authorization_required`; fix the credentials or
operator policy and use `retry-publish`. Publication failure does not regenerate
the repair and does not stop a recovered project. Local-runtime and published
commit identities remain distinct.

`repair status` exposes task state and publication state. Per-job logs, phase
events, candidate/proof artifacts, frozen evidence and recovery receipts live
under the private job directory. Environment credentials are transported in
memory, not serialized into job records. Nothing from project diagnostics or
operator configuration is added to the engine Git commit.

The v1 controller is for Linux/WSL and one user on one host. Cross-host duplicate
generation is possible; remote advancement is handled safely through fetch,
integration, revalidation and normal push rejection rather than a distributed
lease service. Installed controller code upgrades at a new controller generation,
not through hot reload. Installing this feature does not resume stopped projects.
