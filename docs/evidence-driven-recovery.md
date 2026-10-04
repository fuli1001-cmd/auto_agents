# Evidence-driven recovery (policy 2)

Policy 2 separates workflow consumption from local search decisions. A failed
candidate can make verified progress without being accepted for delivery. Native
fixes and engine repairs use the same decisions, reservations and journal.

## Authority and migration

The adopted runtime enables policy 2 at ordinary business entrypoints. The
`recovery_policy_activated` event introduces a `recovery` subtree; earlier events
retain their original reducer semantics and serialized results. Existing model,
implementation and repair consumption is preserved, including a sealed copy of
the legacy budget. Owner-specific rejection and diagnosis history seeds local
searches. Migration imports structured executor results where available;
historical prose does not become a passed check.

Before adoption, the independent verifier streams journal events from one read
transaction. It still compares every revision, checksum and projected result;
memory use no longer grows with the total size of retained history.

A scope derives from the original goal, owner kind, source scope and verification
obligations. Ephemeral operation IDs and equivalent task aliases share that
scope. Independent engine repairs do not borrow or erase product retry history.
User call limits and explicit repair limits are still checked at reservation.

## Observations and progress

Verification observations bind the exact command, source, contract, environment,
verifier and execution result. The trusted pytest recorder retains collection
and setup/call/teardown outcomes. Unsupported launchers keep command-level
evidence. Skips and unexecuted checks are never inferred to pass.

Complete observations are immutable objects. Compact check states are carried
in the journal; models receive a bounded failure summary and a readable complete
evidence file. Candidate review also uses this representation, so a large passing
matrix or a multi-megabyte baseline traceback does not inflate the provider
request. The complete reason remains in the evidence file. Failed review
requests retain a bounded, redacted provider diagnostic and consumption is not
refunded. Baseline executions and collection-only commands do not overwrite
candidate execution results.

Local progress requires a previously observed failure to pass under comparable
inputs while preserving the previously verified checks. Only retained selectors
and originally declared commands can earn credit. Candidate-added tests cannot
renew search by repeatedly creating and fixing new tests. A previously credited
check cannot earn credit a second time. A newly completed verification manifest
also records a one-time completion boundary. Candidate rejection remains a
rejection even when some checks improve.

Known failures select existing managed verification commands for early replay.
Selection uses retained collected nodes, including pytest filters and parameter
identities. A filename appearing only in `--deselect` is not a selected test.
Smaller existing commands are preferred when several commands cover a failure.
After contract validation, these probes run before broad collection. Progress
eligibility is collected only for executed commands; untouched commands cannot
contribute pass credit and do not need recollection on an early failure.
Known baseline failures, including their failed command summaries, are not new
counterexamples. A priority probe that encounters only those failures continues
to ordinary verification. New observations mark this distinction explicitly;
older observations retain their original replay semantics.
Passing those commands still leads to the ordinary affected/release checks,
independent review, custody validation and delivery.

## Decisions and permits

After two implementations without verified progress, further writes require
bounded diagnosis. At most two diagnosis calls are available in that progress
window; malformed replies also consume their recorded call. A diagnosis names
the current observation, observed failures, a distinct hypothesis, relative
source paths and an expected result. The same validator runs before result
settlement and during journal replay.

A successful diagnosis permits one correction of the exact observed source.
Validated independent review findings can also supply a bound counterexample
for diagnosis. They cannot close test obligations or grant verification credit.
Review allowances bind source and verification inputs, so changing an operation
ID cannot renew an exhausted format-correction allowance.
Native review enumerates changes from the sealed delivery commit, including
files whose working index is intentionally untracked. Finding and coverage
requirement IDs use the same controller sets in the schema and local validation.
Format correction may replace an unknown requirement ID while preserving the
decision and every substantive finding field; it cannot turn rejection into
approval or reassign an already valid requirement.
The writer remains confined by the existing project contract. Additional product
paths outside the diagnostic plan are recorded atomically with its result and
require independent review of necessity and evidence for every added path.
Protected control files cannot use this amendment route. Review approval and
delivery remain blocked until the complete verified candidate covers the original
requirements and all pending scope amendments. Rephrasing a request, changing source, or generating a new operation ID does
not reset the window. A successful verification permits review of that same
candidate, with at most one format correction. Ordinary run planning reviews
retain their stage authority and do not require a fix-candidate receipt.

Verification presentation counts logical command batches, not individual pytest
cases. Collection preflight is one check set and stops at its first failed entry.
Interactive terminals refresh one action with counts and active-batch elapsed
time. Plain/file output reports changes in completed counts and a throttled
60-second heartbeat while a batch runs. These display events never grant
verification progress or update health-control evidence.

`recovery_permit_issued` records the controller decision and exact command.
Reservation atomically consumes the permit with budget charges and the outbox
entry. Stale permits and duplicated dispatches are rejected. Interrupted calls
retain their reservations and are reconciled from durable receipts.

Independent root-cause and proof-amendment roles retain separate read-only
ownership. Policy 2 records their consumption without publishing business phase
proofs. Parallel roles use revision-checked reservations; duplicate and unknown
requests cannot silently invoke the provider again. A saved result can settle
an interrupted acknowledgement on resume.

Structured output requests use the supported wire subset described in the
[OpenAI Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
Nonempty values and uniqueness are checked locally. A sealed HTTP 400
`invalid_json_schema` response with no model output, tool activity, source change
or incomplete cleanup can settle an older unknown call as a protocol failure.
Timeouts and ambiguous transport failures remain unknown. Consumption is never
refunded; one request-format correction is tracked separately from diagnostic
hypotheses. A second request-format rejection stops the window.

After a verified runtime correction, an unchanged failed candidate can resume
that rejected diagnosis from its retained evidence. It does not repeat broad
verification merely to correct an outbound schema, and still requires normal
verification and review of any subsequent source correction.

For a stopped engine repair, `repair reverify --job <engine-task-or-incident>
--runtime <committed-correction-checkout>` retains an explicit local correction
and selects verification as the next phase. The checkout must descend from the
retained candidate, preserve the original tests and protected control files, and
have no uncommitted changes. Unresolved operations and unretained producer work
block this operation. Original attempts, consumption, diagnoses and stagnation
remain charged; no model result or passing proof is created. Every changed path
requires independent necessity review after verification. Running the original
business command then continues from this candidate's verification.

Native engine verification seals a passing complete suite before attempting the
original-task recovery boundary. Its checkpoint binds the immutable candidate,
contract, acceptance commands, baseline and suite execution environment. Retrying
the recovery boundary reuses that stage only under identical inputs. Changes to
the candidate, obligations or suite executor require another suite execution;
boundary-driver corrections do not. Recovery errors retain the complete suite
receipt and original boundary diagnostics while remaining unsuccessful overall.

Diagnostic copies preserve tracked working-tree deletions on repeated Git clones,
including deliberately removed host-core bindings. Offline recovery can reconstruct
a consumed private delivery from a registered, immutable Git snapshot: source
identity, registration, revisions and tree must agree with the frozen handoff.
The live private checkout is never mounted. A retained verified engine reply
resumes its original child before another parent-model call, preserving parent
attempts and the original child contract.

A completed legacy writer rejected solely for exceeding its diagnostic path
plan can resume without another model call. Recovery validates its sealed success
result, original custody, reconstructed input fingerprint, unchanged output and
absence of unsettled operations before issuing a replacement candidate receipt.
The original receipt is archived, usage remains charged, and added product paths
still require independent review. Uncertain writes and ownership conflicts outside
this precise case remain blocked. Diagnostic snapshots exclude transient atomic
state-write files while retaining final records and product files.

Public bootstrap also reconciles interrupted native fix verification before
source adoption. Recovery requires a dead process identity, unchanged valid
candidate custody, and no final result; a saved executor result takes precedence.
New dispatches retain PID, start ticks and boot identity. Legacy dispatches need
the matching verification health record. The existing fenced receipt protocol
records an inconclusive interruption with no pass evidence or refunded usage.
The next verification receives a new durable operation identity, including when
the runtime has not changed. Models, writers, review and delivery cannot use this
recovery route. Existing cache input validation continues to govern reuse.

## Session continuation and operations

Session stop decisions consult the kernel for admitted policy-2 workflows.
Provider interruption protections remain active. The retained-child continuation
includes the evidence decision, so a newly justified correction can continue
without changing the original receipt or manually resetting counters. The normal
writer then creates its replacement receipt, and the original parent receives
the verified delivery.
Retained verification/environment and review-protocol blockers can likewise
resume through the original parent after a runtime correction. This reuses the
candidate and verification path rather than starting another writer.

`repair status --json` exposes the persisted scopes, observations, hypotheses,
permits and auxiliary reservations in each workflow's `recovery` state. Terminal
stops retain the latest verification reason and diagnostic record. Source
changes are automatically validated and adopted when the original business
command starts; a separate `repair upgrade` is not a routine prerequisite.

An older runtime that cannot replay policy-2 events cannot be adopted against
the newer journal. Rollback never rewrites business history. Verification gates
and the independently installed verifier remain authoritative.

## Validation

The convergence tests exercise partial progress across more than four writers,
bounded diagnosis, regressions and skips, recurring failures, task aliases,
single-use permits, frozen input checks, correction scope, independent engine
work and exact legacy replay. Auxiliary tests cover concurrent roles, duplicate
dispatch and uncertain outcomes.

The stopped-child integration test uses actual Git custody and pytest execution:
a retained failing candidate is diagnosed and corrected, a new receipt is
registered, verification and review complete, and the original parent resumes.
No manual receipt edit or budget reset is used to obtain that completion.

Pre-adoption validation on 2026-09-29: 210 selected regressions passed, followed
by 22 policy/continuation/auxiliary cases on the updated implementation (these
sets overlap). The selected regressions cover kernel replay, native and engine
execution, restart, upgrade, migration, bootstrap, receipts, terminal diagnosis,
proof amendments and verification metadata. Synthetic tests isolated WSL host
storage discovery; they do not attest live Windows storage admission.

Read-only replay of the installed control database passed for both workflows
and all 372 historical events. A separate copied-database migration/replay also
preserved every historical command, budget and domain projection. Live business
acceptance remains a separate execution, with its evidence retained by the
original session.
