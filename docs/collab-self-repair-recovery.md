# Collab recovery and repair evidence

An explicit `collab --session ID` resumes that collab workflow. The presence of
an unrelated saved run never changes the command into `run` or authorizes
clearing that run's blocker.

After self-repair stops, repeat the original `collab --project PROJECT --session
ID` command with the same provider and authorization options. The foreground
workflow runs first; an old repair failure cannot by itself stop that new
invocation. A newly observed failure automatically retries an equivalent blocked
repair or starts a new request after input changes, retaining compatible candidate
work for revalidation. Manual cancellation is not required. This does not reset
historical search progress or authorize automatic retries within the same run.

If the requested session's control files are missing, recovery inspects hashed
checkpoint blobs and committed history. It selects the newest complete journal
boundary, validates its hash chain and session/handoff identities, and restores
only missing workflow control records. Existing different records cause a
conflict before application. The source manifest and application receipt live
under `.auto-agents/state/session-restorations/ID/`. Repeating an interrupted
application is idempotent. Product code, the business database and an unrelated
run state are not restored from these historical snapshots.

Repair triage binds command, session, workflow and a proven child run. Session
repair records use a session-scoped artifact subject, and restart retains the
original command, session, provider and approval options. A missing session with
no valid recovery evidence fails before invoking a diagnostic model.

## Candidate proof and continuation

Experiment schema v4 separates completed components from proof that the original
failure is resolved. Findings are tracked on each candidate's lineage. Resolving
one counterexample does not close other counterexamples under the same causal
obligation. Known passing component checks become sticky regressions for later
candidates. New failures are compared with the parent before being classified as
candidate regressions.

The experiment freezes one target checkpoint for its candidate family. Diagnostic
copies retain private checkpoint refs and the staged index, as well as the dirty
worktree. Generating a prompt is read-only. A comparison applies the same
candidate regression tests to base and candidate implementations; collection or
environment failure is not behavioral proof. Final approval requires both
boundary replay and a valid differential, then code review, full-suite proof and
sealing. Component-only candidates defer whole-repair proof to integration.

Scope guards compare complete per-path content and mode plus semantic index and
HEAD observations. Existing dirty paths are not evidence of a new mutation.
Protected configuration and contracts remain included; run output is excluded
only when explicitly requested. Read-only reviewers are checked for mutation.

Completed generation and normal interruption save a binary-capable patch and
its digest before temporary worktree cleanup. Failed checkpoint persistence
retains the worktree. A compatible interrupted patch can seed the next candidate.
Legacy v3 records and patches remain available; their unbound proof is rechecked,
and the original experiment is backed up before writing v4.

Three consecutive non-improving candidates, rejected component reviews, or rejected
designs trigger strategy correction. The retained workspace survives this transition.
Verified components survive compatible redesign, and repeated resolution of the same
historical finding does not earn new progress. Another exhausted attempt window with
no accepted component or root-proof gain returns `search_stalled`, preserving code and
diagnostics. Infrastructure interruptions and operator stops also retain resumable
evidence instead of manufacturing successful proof.

After an operator fixes a stopped deep-repair checkout, restart preserves its
committed and unfinished corrections when Git proves that checkout descends from
the latest recorded candidate. An older continuous checkout still yields to the
newer deep candidate, and an interrupted deep checkpoint keeps precedence. The
import receipt records the actual source commit. This carries code only: prior
approval receipts remain invalid, and the stalled state, counters, and progress
credits are not reset by the import.

## Isolated operational check

`scripts/verify_collab_recovery.py --project PATH --session ID` compares the
committed engine with the working implementation on two private project copies.
It records the reconstructed graph and checks that the live project did not
change. The probe stops before making a model call; reaching that boundary proves
session resumption, not completion of the user's business goal. Its JSON report
and subprocess output are retained in a uniquely named directory under `/tmp`.

## Deterministic verification and repository-binding failures

An explicit `target_repository` in a route, issue seed, or iteration seed is
checked before ambient run recovery, baseline creation, or child execution.
The in-process coordinator owns one repository: its provider write scope,
verification environment, checkpoint and rollback scope cannot be switched by
an issue description or a shell `cd`. A foreign target without an explicitly
bound execution channel now returns `execution_binding_mismatch` and blocks the
parent. Historical/resumed handoffs receive the same check. This is a bounded
stop, not a claim that cross-repository execution has succeeded; engine repair
must use the engine-owned repair path or an authorized engine workspace.

Fix verification preflight rejects missing Conda prefixes/interpreters and
foreign execution directories/test sources before another fix model call.
These failures propagate as `verification_execution_binding`, so a parent does
not repeatedly create replacement fix children for the same invalid command.
No environment is fabricated and no unrelated pending run is reset or resumed.

Vitest cache isolation identifies executable tokens in individual shell
commands, not the substring `vitest`. For example, a pytest file named
`test_workbench_vitest_launcher.py` does not receive `--no-cache`; in a mixed
`vitest ... && pytest ...` command the flag is attached only to Vitest. Quoting,
separators, comments, and existing cache flags are preserved. Nested shell
programs that cannot be safely rewritten are left unchanged, not reparsed as
top-level commands; use an explicit verification script for those cases.

## Terminal repair delivery to older runtimes

A resumed candidate can contain an older repair client that restores its
registration before inspecting a terminal result. After a failed repair, the
controller normally closes its copy of the project lock. If that older client
re-registers, the controller retains the transferred descriptor until the owner
reads its status, then closes it on the following tick. Diagnostic status reads
from other processes do not consume this delivery. Owner death or cancellation
still releases the registration without waiting for an acknowledgement.

This compatibility path lets older resumed processes observe `blocked` or
`finished`, exit, and release their own lock descriptors. It preserves the
original repair result and resumable failure instead of converting a provider
timeout into cancellation. `tests/test_repair_terminal_handoff.py` exercises the
legacy ordering in real subprocesses and checks subsequent project lock
acquisition. The current client continues to inspect terminal results before
attempting re-registration.

## Runtime compatibility before engine replacement

The controller admits replaceable engines using versioned runtime capabilities
and its own offline behavioral probes. Admission covers initial environment
setup, worker replacement for repair/validation/publication, and final workflow
launch. A remote or cached revision, a capability declaration, and a previous
full-suite receipt do not individually prove compatibility. The probes run from
the calling controller's checkout in an isolated interpreter, with temporary
state and no provider credentials or model calls. They exercise mandatory
progress supervision, acceptance planning, and terminal repair delivery using
the selected engine's implementation, without depending on its test suite.

Incompatible engines return `runtime_incompatible` before model execution, with
the selected runtime, required/available capabilities and failed checks retained
in diagnostics. The workflow stays resumable and reports that engine versions
must be synchronized. This does not merge divergent histories, force-push, or
silently replace upstream work with the local checkout. Integrate and verify the
required changes before publishing a compatible upstream revision and retrying.

Acceptance-planning capability v2 retains explicit planned pytest nodes from
coverage explanations as required checks, including when the structured list
is empty or contains only partial existing coverage. Normalization only adds
fully qualified, repository-local identifiers; it never invents test names or
discards existing checks. The raw provider output remains unchanged, and the
normalized unverified contract is cached. Missing planned tests still prevent
upstream reuse and must be implemented and proved by the candidate. The
controller's planning probe checks this behavior before runtime replacement.

## Checks that require an independent metadata owner

Engine acceptance and managed engine verification request fixed supervisor
checks from the selected verification launcher. Inside the existing OS sandbox,
before starting its metadata owner, the launcher observes a frozen legacy owner,
denied ptrace startup, and supervisor death. A negative control removes EXITKILL
and must observe that the sleeping tracee still holds its pipes. Each check uses
private temporary files, bounded subprocesses, and explicit process cleanup.
No candidate command runs before the metadata boundary.

Nested tests consume these observations through
`verification_supervisor_checks.observation`. They require the live tracer PID
and hashes matching the runtime identified by that tracer's `/proc/<pid>/cmdline`.
The production launcher uses an absolute `verification_sandbox.py --metadata`
path. Report-supplied paths and candidate import paths do not select the source
used for validation. Absent observations, unreadable launcher identity, or
mismatched producing source fail closed. This also consumes existing version 1
reports when the selected launcher predates the candidate. The packaged legacy
fixture is byte-for-byte revision `8ea6662`,
including its original ptrace restriction, so installed or shallow runtimes do
not require Git history. The death check first proves that the tracee's
`TracerPid` is the separate supervisor that it kills, then requires pipe EOF.

The planning capability observation describes this activation route. It does
not require a nested user/mount namespace or grant repair acceptance by itself.
Returned observations expose `source_root`, `sources`, and `launcher_pid` so their
provenance remains explicit. The runtime capability route tells planning that
these are selected-runtime diagnostics, not proof of candidate supervisor changes.
Quick and expanded acceptance still execute their tests and assertions. Ordinary
nested gates retain their inherited owner and narrowing policy. If a candidate
changes the supervisor implementation, observations from another selected
runtime cannot certify that changed implementation.

## Offline recovery sandbox startup

The pinned controller selects the session replay container profile. It permits
the nested verification sandbox's namespace and metadata supervisor syscalls
with `seccomp=unconfined`, while retaining the non-root user, dropped capabilities,
`no-new-privileges`, no network, read-only container root and disposable project
copy. No host administration capabilities are added. Other boundary replays and
ordinary test profiles keep their existing restrictions. The replay profile is
part of the verification runtime digest, so changing it invalidates old proofs.

A structured `verification_confinement` cause remains an infrastructure failure
even when the session wraps it as `verification_ownership`. Verification stays
failed and the controller stops before another implementation or replan. On an
explicit retry it checks the retained candidate again. Genuine ownership and
test-discovery failures retain their original verdict; error text alone does
not grant acceptance or mark an assertion failure as an environment problem.

The controller also snapshots installed project `node_modules` needed by the
retained Vitest discovery declarations. It mounts private, content-checked copies
at their original project paths, read-only, alongside captured Conda inputs.
Selection uses the frozen configuration and lockfile locations; it does not
resume any task that owns those declarations. Missing dependencies stop replay
as infrastructure failure, without installing substitutes. External dependency
links and changes to captured packages invalidate the inputs. Npm configuration
and `.env` credential files are excluded, and no package scripts run during
snapshot admission.

Vitest discovery retains bounded, redacted exit evidence. The known missing
Workbench Vitest launcher failure is classified as infrastructure only with a
failed discovery exit and its specific diagnostic; generic missing product
imports and test assertions retain their original failures. The dependency
snapshot implementation belongs to the pinned controller. Candidate changes to
that implementation alone cannot supply inputs to the running verifier.
Timeouts and missing or malformed replay reports also stop implementation as
verification infrastructure failures; a timeout cannot accept a partial success
report. Explicit cancellation remains cancellation, and retained candidates can
be rechecked once the verification environment is available.
