# Self-repair convergence and private source handoffs

Self-repair retains one serial implementation workspace and chooses its next
step from verification evidence. There is no root-level or candidate wall-clock
budget. A step without meaningful progress can still be diagnosed and stopped.

## Incremental planning and independent review

Experiment schema 7 retains immutable plan revisions, stable step/scenario IDs,
parent revisions, independent review results, bounded repair episodes and
verification schedules. The latest draft survives rejection, malformed replies,
interruption and restart. Complete historical inputs remain in protected artifacts;
the working input supplies the active obligations, previous plan/review, unresolved
feedback and relevant changes. It does not require rereading all history each turn.
A full response or a parent-bound amendment can produce the next complete plan.
Stale amendments and removal of existing acceptance scenarios are rejected.

The controller, rather than a prompt-only convention, distinguishes these actions:

* Quantity overruns are scheduling decisions. Commands remain atomic and all
  acceptance is retained; nine commands do not force another global design.
* Protocol errors identify the field, actual value, constraint and evidence.
  At most two local format corrections are allowed per semantic round, including
  interrupted calls. They cannot change mechanisms or remove acceptance. Historical
  finding and planning-review labels are references, not new blocking finding IDs.
* Independent plan review has at most three semantic attempts for the same
  component/contract/evidence/environment episode. Commit and display-label changes
  do not replenish it. The previous draft remains usable when review artifacts
  require another independent review.
* A code review can identify an implementation error already covered by the approved
  mechanisms and scenarios. Verified scenario/obligation/path mappings allow direct
  code correction without replanning. A new mechanism or scenario needs a local
  amendment and independent review of its effects.
* Count exhaustion alone cannot discard the whole design. One bounded local
  diagnosis must identify a disproved assumption or dependency conflict before
  component/global redesign. Unsupported or exhausted recovery returns a structured
  blocker without manufacturing empty code candidates.

Planning requests have separate identities. A candidate is created only when
writing or `verify_existing` validation is admitted. Planning is not a successful
repair, and format correction, scope reclassification and introduced-regression
repair do not mint achievement credits. Existing cumulative counts remain intact.

Scope decisions remain independent of code review. `required`, `follow_up`,
`not_applicable` and `unknown` preserve their previous meaning. A safety violation
or introduced regression requires explicit disproof to become nonblocking.
Current facts retain their original request/result artifacts and a controller-built
file/import/configuration dependency manifest. Relevant changes invalidate them;
unrelated files need not. Dynamic calls, external imports or incomplete dependency
knowledge conservatively require a fact-level review. Symbol locations help target
inspection, but never authorize reuse of a safety decision in a changed file.
Independent code reviewers receive the previous verdict, unresolved findings and
changes; final integration still reviews the complete repair and interactions.

Small probes still execute in disposable checkouts with a 60-second limit and do
not issue candidate proof. Full probe results remain referenced when the working
input uses an explicit excerpt. Models and provider settings remain unchanged.

Quick selection chooses a negative oracle and a compatible positive control for
each current finding, plus explicitly required safety oracles. Its defaults are
three commands, twelve collected cases and 180 estimated seconds. These are
selection/batching targets, not runtime deadlines or permission to drop necessary
checks. A scenario may name an exact parameter case in `quick_check`; its original
`check` remains expanded acceptance. Unknown collection sizes and mandatory atomic
commands exceeding targets are reported. Commands with different target cohorts,
flags or shell effects are never treated as interchangeable proof.

Schema 6 migration backs up the old state and preserves candidates, histories,
proof schema 4 and old counters. Matching original request/input/result artifacts
can recover a legacy draft and an interrupted review slot; an old approval flag
never manufactures a new approval. New memory artifacts use the existing
transactional restart-copy mechanism. Future-schema state cannot be overwritten.
The existing trusted verification ledger continues to govern all proof reuse;
cross-environment or unproved cross-snapshot reuse is not enabled by this work.

### Offline acceptance and performance

`python scripts/benchmark_repair_incremental.py --experiment FILE --output FILE`
replays archived planning admission and scheduling without model calls, test
execution or job mutations. It reports comparable command counts separately from
plans still needing reference/format correction. It does not approve those plans
or predict end-to-end model speedups. `benchmark_verification.py` independently
measures fresh and warm trusted checks in a fixed isolated snapshot.

Planning request metrics record complete/working input size, prompt size and call
time. History reports include semantic/format counts, revision/fact counts and
blocked episodes. Quick schedules record mandatory exceptions, deferred acceptance,
actual collected-case counts, queue/execution time and certificate hits. Verification
batches are serial; adding their durations to enclosing phase durations would
count the same wall time twice.

The implementation and offline validation do not resume the stopped collab job.
A subsequent operator-requested restart still imports the retained private source
and validates actual engine/project recovery. Faster scheduling or an earlier
bounded stop alone is not evidence of faster successful end-to-end repair.

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

Experiment schema 7 separates historical achievement from currently reusable
proof. Existing proof schema 4 is not invalidated just by adding progress fields.
Repeated check identities, regrouping, candidate labels and restarting cannot
credit the same achievement again. Review-only resolutions require completed
review and actual passing check identities. Fixing an introduced regression does
not buy another search window. Verified environment preparation is credited once.

Three unsuccessful attempts allow one evidence-driven local strategy adjustment.
Global redesign requires a concrete invalidated assumption or dependency conflict.
A second exhausted window at the same achievement state reports `search_stalled`
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
