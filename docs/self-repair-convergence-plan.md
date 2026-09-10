# Self-repair convergence and private source handoffs

Self-repair retains one serial implementation workspace and chooses its next
step from verification evidence. There is no root-level or candidate wall-clock
budget. A step without meaningful progress can still be diagnosed and stopped.

## Independent planning and necessity review

The overall design is a proposed decomposition, not permission to write code.
Before a diagnosed automatic repair edits its next component, the controller
uses separate provider requests for component planning and independent review.
Neither request resumes the writer's or planner's provider conversation. The
inputs identify the current retained source, environment, original request,
frozen requirements, component and complete historical evidence. Full inputs,
outputs and request identities are retained under the experiment's `planning/`
directory. A large diff has a complete artifact in addition to its inline excerpt.

Plans specify mechanisms, safe relative paths, explicit quick-test nodes, and
negative, positive, recovery and interaction scenarios with expected behavior and
requirement/finding mappings. Inapplicable recovery or interaction cases need an
explanation. A controller-owned disposable checkout runs at most three diagnostic
probes, each bounded to 60 seconds. Probes can use existing targeted tests or a
Python memory fixture; they cannot install dependencies or modify the retained
candidate. Missing tests, import errors, timeouts and modified probe source are
inconclusive. Probe results do not enter candidate proof or progress credits.

An independent reviewer must inspect every scenario and the actual probe results.
Approval requires no remaining design issues and matching probe expectations.
There are at most two targeted revisions (three reviews) for unchanged planning
inputs; exhausting them returns a planning rejection through the existing bounded
recovery policy. Receipt reuse requires the same contract, component strategy,
material findings and environment. Changes outside planned paths invalidate it;
implementing the same plan inside those paths does not itself require replanning.
Existing code can be independently approved for `verify_existing`, which runs the
normal acceptance stages without invoking a writer or inventing a patch.

Schema 6 preserves historical component completion separately from reusable
current proof. Migration backs up the actual previous schema version atomically;
legacy plan stamps do not count as independent reviews. Existing no-progress
counters and achievement identities survive migration. Identical supported
commands are coalesced with an original-request mapping; different target cohorts,
flags, or shell effects are not merged, since fixture semantics may differ. The
existing trusted verification ledger still governs source/environment/input-bound
certificate reuse; this change does not relax its cache policy.

`scripts/audit_repair_history.py --experiment FILE --checkout DIR --output FILE`
inspects a stopped checkpoint without restarting it. The report distinguishes
historically resolved observations from outstanding findings awaiting independent
scope review and records which source the restart selector would preserve. It
does not manufacture model decisions or acceptance receipts. Compare actual
phase timings, model requests and executed/cached commands; the absence of an
expanded-suite call after semantic rejection is a structural saving, not a claim
about end-to-end speed before a subsequent real run.

Scope classification is a separate request from code review. A new finding needs
a concrete supported trigger, original requirement, consequence and evidence to
become required. Outcomes are `required`, `follow_up`, `not_applicable`, or
`unknown`. Unknown observations can request bounded diagnostic probes; they never
authorize code changes or successful acceptance. An introduced regression or
safety violation cannot be deferred merely because it is unlikely: rejecting its
applicability requires explicit disproof. Reclassification is recorded separately
without deleting historical findings or treating them as repaired. New evidence,
source or environment changes require refreshing nonblocking decisions.

Legacy direct API calls without a diagnosis keep their previous execution mode;
their records cannot supply the independent planning receipt required by a
diagnosed automatic repair. No CLI or model-selection change is required.

## Failure evidence and verification

Command output is retained in redacted diagnostic artifacts. Failed checks also
produce structured records containing the command, stage, environment, test
identity, assertion and termination reason. Managed verification returns this
information to its caller, including during candidate generation. Large records
have a complete artifact reference and an explicit count of details omitted from
the inline view; they are not silently reduced to output head/tail fragments.

With an independent component receipt, quick counterexample checks run before
semantic code review. A quick failure prevents review; a semantic rejection
prevents expanded regression and boundary work. After approval, the full active
component and retained regressions still have to pass before component completion.
The original integration, boundary, differential, full-suite and sealing gates
remain required. Pending-candidate recovery uses the same early check/review gate.
Checks belonging to future components
are deferred without deleting their final obligations. Normal and pending
candidate recovery share the same replay/differential prerequisite checks.
Failed prerequisites stop later acceptance stages. Baseline attribution remains
a separate diagnostic and cannot issue candidate progress credit.

Diagnostic logs use their own artifact namespace and do not masquerade as gate
output artifacts. Source mutation during verification invalidates the proof.
Successful certificates retain the existing source, environment and observed-input
checks. New engine revisions reuse the original acceptance mapping when the
request and retained inputs still match; they do not regenerate test names merely
because the revision changed.

## Progress, diagnosis and restart

Experiment schema 6 separates historical achievement from currently reusable
proof. Existing proof schema 4 is not invalidated just by adding progress fields.
Repeated check identities, regrouping, candidate labels and restarting cannot
credit the same achievement again. Review-only resolutions require completed
review and actual passing check identities. Fixing an introduced regression does
not buy another search window. Verified environment preparation is credited once.

Three unsuccessful attempts allow one evidence-driven strategy adjustment. A
second exhausted window at the same achievement state reports `search_stalled`
and preserves the code and evidence. Stopped and failed jobs can import their
latest checkpoint transactionally; exhausted state and historical credits survive
proof invalidation. The last completed review has a separate receipt so later
pre-review failures cannot overwrite it.

Introduced regressions remain blocking across component transitions. A completed
review can identify another approved component as their repair owner; the serial
scheduler prioritizes that owner when its prerequisites are complete. If the owner
depends on the blocked component, the minimal regression correction remains in
the active scope to avoid a dependency cycle. Routing never approves the rejected
candidate, expands the frozen contract, or earns progress credit. A known owner
can consume the one strategy-adjustment window without regenerating the design.
Its required reproduction is included in focused verification, and the regression
must be explicitly resolved by a completed review.

Executable references extracted from review prose exclude sentence punctuation.
Quoted IDs and parameter contents remain literal, including spaces and periods;
incomplete parameter selections cannot broaden into whole-function checks.
Scheduling and progress identities share the same extraction rules.

Previously generated commands retained in the regression list are migrated only
when the retained review and a native collection failure establish their origin.
The migration records the original command, corrected command, review digest and
failure evidence IDs. Loading, selector preflight, execution and retry guidance
use that correction; historical failure records and progress credits stay intact.

An unchanged candidate can undergo a different component's verification; it does
not need a fabricated code edit. Duplicate attempts in the same component are
still rejected, and equivalent checks cannot earn credit again after regrouping.

Unknown execution failures receive bounded read-only diagnosis of the latest
retained candidate, not the original engine base. Diagnosis reuse is bound to the
source, environment and evidence artifacts. Provider errors are not cached as
permanent source defects. Missing software continues through the trusted recipe
mechanism; an undeclared or unavailable prerequisite reports an environment block.

The command supervisor distinguishes activity from verified stage observations.
Repeated output, CPU activity or rewriting files cannot renew a self-repair
provider's semantic progress lease. Registered verification reports are written
outside candidate storage, and only the selected check identities qualify.
Ordinary workflow timeout policy remains compatible.

Native file-update events can omit patch contents. Their actual workspace state
distinguishes different edits for loop detection, while neither those edits nor
ordinary output renews the trusted semantic-progress lease.

## Candidate ownership and source delivery

Candidate capture and permission restoration open paths relative to an anchored
private directory without following links. Directory-to-file and directory-to-link
replacements record displaced descendants as deletions, preserving index and
worktree distinctions. Restored permissions apply only to private materialized
inodes; shared hardlinks and replaced paths cannot receive those writes.

Session completion records and verified code revisions are separate. Candidate
commits remain in private Git storage and are exposed by `candidate_custody`;
shared HEAD and index are not the delivery transport. A coordinator-generated
`source_descriptor` binds a session handoff to its repository, owner, exact code
revision, contract revision and registered provenance. Subsequent fix children
materialize that source before executing, including during clarification. They
retain independent objects for restart even after the earlier checkout is retired.
Missing, altered or conflicting provenance blocks execution instead of falling
back to ambient HEAD. A verification command alone cannot grant task ownership.

## Compatibility and validation

Explicit interpreter commands are not automatically wrapped in another Conda
launcher. Declared pytest ini overrides are made explicit while retaining
CLI-over-environment-over-config precedence, so configuration discovery and
execution agree even with parsers that read config addopts after ini overrides.

Regression coverage includes middle-of-output failures, large evidence packets,
read-only diagnosis of retained revisions, interrupted and failed imports, review
receipt preservation, progress deduplication, sustained progress versus repeated
output, and proof invalidation on source changes. Public session tests cover
successive private child deliveries, parent restart, retired sources, tampering,
directory replacement races and preservation of foreign content, permissions,
HEAD, refs and index. Performance comparisons must distinguish faster successful
repair from merely stopping unproductive work earlier.
