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

Verification and replay subprocesses use the installed Codex CLI's local sandbox
command (no model/API call). The permission profile makes the live target and
other filesystem paths read-only, allowing writes only to the verification
workspace and a private temporary directory. A private Linux network namespace
contains loopback test services; verification receives a credential-free
environment, private HOME and private `/tmp`. Nested sandbox checks retain the
outer isolation and use Landlock to narrow their own write roots. Linux/WSL
hosts need `unshare`, `mount`, `ip`, and Landlock ABI 3 or newer in addition to
the local Codex sandbox command. The sandbox is probed before generation; an unavailable or
incompatible host fails closed. This follows the official
[Codex permissions](https://learn.chatgpt.com/docs/permissions) model; it is
independent of which provider generates the repair.

## Operator configuration and commands

The first registration pins the engine installation's explicitly tracked Git
remote and branch. Operator configuration and jobs live under
`$XDG_STATE_HOME/auto-agents/repair-control` (default
`~/.local/state/auto-agents/repair-control`). `AUTO_AGENTS_REPAIR_CONTROL_ROOT`
can select another private directory. Each engine identity has `operator.json`
with `remote`, `ref`, `python`, and `publish`. These are operator settings, not
project/model-controlled route fields. Embedded HTTP Git credentials are refused;
use the configured credential helper or SSH agent.

This directory is persistent state, not a temporary workspace. Dependency venvs
under `environments/`, Git worktrees under `runtimes/`, the repository cache and
job evidence survive process exits and restarts. New producer invocations register
these resources with the storage lifecycle service. Unused registered environments
can expire, while active runtimes and recovery/publication evidence remain protected;
unregistered legacy paths are retained. Each venv is created with the configured Python
and keyed by `pyproject.toml` contents and that interpreter's path. A successful
`ready.json` receipt allows reuse. Repair dependencies are installed into this
venv rather than the launching Conda environment so preparing a candidate does
not replace the running engine or change its dependencies; the venv does not
inherit the launching environment's site-packages.

On startup, the foreground compares the running controller's committed revision
with the installation HEAD, even when their IPC protocol is identical. An idle
older controller is replaced without resetting jobs or recovery evidence. Active
work defers the update with an explicit error. Repair progress and stop reasons
use the normal user-event renderer and are saved in the invocation's user log.

The repair worker separately selects its engine from the configured remote branch.
A local commit and an updated controller therefore do not establish that the worker
will load that commit. Before environment setup or worker replacement, the controller
imports the requested installation commit and requires it to be an ancestor of the
selected remote revision. A behind or divergent remote, including a prepared cached
runtime, stops with `runtime_revision_mismatch`. The result names both revisions and
asks the operator to publish/integrate the installation into the configured branch.
It does not silently select unpublished local code or discard retained repair work.
Successful selection records `runtime_selected` with both revisions. The existing
runtime compatibility, behavioral proof and publication checks still apply.

The foreground shows an eight-character job ID and explains the problem once,
using the current workflow's engine request or approved diagnosis. Without an
approved diagnosis it labels the error as a symptom under investigation. Later
lines show only progress in plain Chinese; repeated polls do not repeat messages.
The full job log directory appears on first observation and again if repair stops.
Validation and resume failures retain a sanitized, generation-specific reason
with the affected subscriber, including the exit code when a resumed process
provides no explanation. A passed recovery check means the original workflow is
continuing, not that the entire workflow has finished.

Package installation honors pip configuration and the invocation environment.
If a configured mirror is unavailable, an operator can select an index for one
invocation with `PIP_INDEX_URL=https://pypi.org/simple auto-agents ...`; this does
not modify the global pip configuration or silently introduce an index fallback.

Commands and current retention behavior are documented in
[Storage maintenance](storage-maintenance.md), with the cross-stage design in
[Artifact cleanup design](artifact-cleanup-design.md).

Environment preparation saves each command's sanitized `stdout.txt`, `stderr.txt`
and `command.json` under `jobs/JOB/environment-setup/ATTEMPT/STEP/`. Direct setup
calls without a job use `environment-setup/ATTEMPT/STEP/` under the controller root.
This covers venv creation, pip installation, dependency listing and import checks,
including captured partial output on timeout. A failed worker result includes
`environment_diagnostics` with these paths, and its error message links to the
command metadata. If persistence fails, `environment_diagnostics_error` explains
the logging failure while retaining the original setup failure.

Engine proof prerequisites use a shared software-failure classifier, covering
executables, Python imports, Node packages and shared libraries. Trusted pytest
receipts distinguish subprocess launch failures from missing input files, while
collection and non-pytest errors use the same classification boundary. Missing
repository modules/scripts and ordinary assertions remain candidate failures.

Preparation comes from the bundled recipe catalog or operator-owned dependency
declarations, rather than software-name branches in the repair loop. Supported
recipes install hash-locked Python wheels, install locked npm toolchains without
lifecycle scripts, or snapshot a supplied executable/library with a declared
SHA-256. Undeclared or unsuccessful prerequisites return a structured environment
blocker and preserve the candidate. A trusted verification worker also signals
the matching owner/generation so a running code-generation call stops instead of
continuing to rewrite code around an unavailable tool.

After preparation, the same candidate and required checks run again. Tooling is
read-only inside the private HOME/network/filesystem sandbox and participates in
proof identity. Concurrent failures from an older environment retry the newer
environment without duplicating setup; failed setup cannot repeat in the same
repair generation. See [Verification dependencies](verification-dependencies.md)
for declarations and recovery behavior.

The foreground relays the current candidate, phase and elapsed minutes while
the top-level job remains `repairing`, including dependency preparation and
contract/design work. It also retains the previous candidate's failure reason.
A new generation clears the displayed phase history. Native candidate
continuations separate stable scope/authorization/design from changing failure
evidence. Compatible retries bound historical summaries but carry complete current
review findings, including reasons, counterexamples, required tests and evidence.
Candidate regressions remain actionable even though they do not become new root
contract obligations. Both full prompts and native continuations receive this feedback.
A changed contract, component, settings or workspace still requires a fresh full prompt.

Candidate lineage follows the actual retained commit, which can differ from the
highest-scoring search candidate. Feedback is checked against the experiment,
candidate and commit before use. If cancellation interrupted result registration,
the next attempt can recover the previous review through Git ancestry or a scoped
candidate ref and matching checkpoint diff. That historical review does not grant
verification proof to the unreviewed commit. Resolved findings remain visible as
constraints to preserve, and unrelated review observations remain outside repair scope.

Cancelling a repair still cancels its workflow registration. Running the original
command again creates a newly authorized job. If every request input except the
engine base revision matches, that job can import a quiescent cancelled job's
retained code and review history. It preserves committed, staged, unstaged and
untracked candidate changes, then integrates the current trusted engine revision.
The cancelled job and its subscribers remain cancelled; the new job must earn
fresh verification and recovery approval. Native provider sessions and full-suite
checkpoints are not imported. Active or mismatched jobs and a new job that already
started an attempt are never replaced. `prior-repair-import.json` records the source,
and foreground progress explicitly confirms that the previous candidate was retained.

Import has a durable preparation marker and a separate completion receipt. An
interruption before completion rolls back only the new job's partial import and
allows retry; an interruption after completion keeps the imported code. Cleanup
can resume after a process dies before rollback. The worker waits briefly for a
cancelled source's processes to exit and reports a stopping blocker if they remain
alive, instead of silently generating from an older candidate or the base.

Upstream integration checks actual Git ancestry, completes any pending merge and
commits the merged tree before recording its revision. A stale `base.json` cannot
make an unfinished integration count as complete. Failure before semantic review
retains the last actual review, including candidate regressions; a completed review
can explicitly replace it. Validation milestones follow the current execution order:
replay failure alone grants neither focused-test nor semantic-review proof, and a
failed differential does not invalidate a separately successful boundary replay.

Bounded failure excerpts preserve both the beginning and end of diagnostics, so a
primary replay failure cannot disappear behind later passing-test output. Under
acceleration, failed boundary replay returns the candidate immediately; the expensive
diagnosis differential waits until the replay passes. Invalid replay evidence also
stops later proof. No final validation requirement is removed.

Output is redacted before writing: configured secret values and their URL-encoded
forms, URL userinfo/query/fragment, secret assignments and authorization headers.
Environment variables are not dumped. Each stream retains up to approximately
2 MiB of its head/tail with explicit truncation metadata; files are private to the
user. Dependency receipts also redact credential-bearing URLs, while retaining
the dependency fingerprint. These logs live outside the disposable venv so that
failed-environment cleanup does not remove the diagnostic evidence.

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
   edits and tests. Three non-improving attempts or rejected reviews of the same
   component enter deeper design review while continuing the same workspace.
   Repeated resolution of old findings cannot reset the progress counter. A second
   exhausted attempt window without accepted component/root progress returns a
   `search_stalled` blocker with the candidates and evidence intact. Approved proofs,
   reviews, full-suite checks and resumable validation retain their existing gates.
5. Independently validate each subscribing project, then launch its original
   invocation against the approved runtime. Native gate/stage/engine-route
   receipts can confirm recovery before the whole business goal finishes.
   Unknown boundaries conservatively require successful workflow completion.
6. Publish only after recovery confirmation. Source developer files, index and
   unrelated local commits are not promoted or pushed by this path.

When recovering cancelled jobs from older engines that abandoned their continuous
workspace during deep search, import the latest candidate or its verified interrupted
patch instead of the stale continuous HEAD. Resolve recorded parent refs to immutable
commits before applying patches. A compatible approved design is retained as a plan,
with all components pending fresh validation against the selected engine revision.

Explicit engine routes use a separate admission path: `EngineRepairRequired`
is an internal work request, not an exception that a model must first prove is
an engine defect. The CLI checks the registered repository and invocation/saved
workflow authorization, then submits directly, including when resuming a saved
collab handoff or using `resume --workflow`. Ordinary exceptions retain their
root-cause adjudication.

After fetching upstream, the worker makes one read-only acceptance-planning
request (180-second timeout) and caches its result by route and upstream SHA.
Every requested behavior must map to named engine tests; missing coverage must
be supplied by a candidate, not treated as success. This unverified contract is
not a diagnosis or an approval. Already-satisfied work requires successful tests
without skipped checks and an isolated replay that consumes the exact original
engine route before the next execution boundary. Candidate changes still require
the existing differential, review and full-suite proofs. The same frozen contract
is carried into subscriber validation and publication integration. Older cached
workers reject the request marker rather than invoking the legacy no-diagnosis
repair path.

The replay receipt lives only in the copied target and is explicitly passed
through the verification sandbox's credential-free environment. Reaching an
unrelated provider boundary without consuming the original route is not recovery.

Failures with the same fingerprint, contract, base and environment share one
local job, including its blocked state. Changing a child ID does not reset the
job. Each subscribing project still needs its own boundary check. There is one
code-repair/validation/integration worker at a time per supervisor; a separate
lightweight Git publication lane prevents an unrelated long repair from delaying
an already-verified fast-forward push. Healthy business processes continue
independently on their own versions.

The supervisor records worker and resumed-process identities and adopts live
processes after restart. A foreground relay can transfer its still-held lock
again after a control restart. A late result from an obsolete generation is not
accepted. A user stop writes cancellation before signaling processes and prevents
automatic restart. An unexplained business crash without a durable repair request
is not treated as permission to repeat external side effects.

Terminal subscribers release their controller registration. The foreground
reads their durable result before attempting to register again: a finished
subscriber exits with code 0, and a blocked or cancelled repair/subscriber exits
with code 3. Only ongoing work needs registration recovery. Environment setup
failure blocks the job; it does not automatically start another repair generation.
After resolving the blocker, `repair resume --job JOB` can queue another attempt.

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
