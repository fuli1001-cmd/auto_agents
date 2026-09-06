# Collab recovery and repair evidence

An explicit `collab --session ID` resumes that collab workflow. The presence of
an unrelated saved run never changes the command into `run` or authorizes
clearing that run's blocker.

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

Three consecutive non-improving candidates or rejected designs trigger strategy
correction. Verified components survive compatible redesign; merely rewording a
summary or discovering another defect is not repair progress. There is no total
candidate or wall-clock ceiling. Infrastructure interruptions and operator stops
retain resumable evidence instead of manufacturing successful proof.

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
