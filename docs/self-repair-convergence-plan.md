# Self-repair convergence and private source handoffs

Self-repair retains one serial implementation workspace and chooses its next
step from verification evidence. There is no root-level or candidate wall-clock
budget. A step without meaningful progress can still be diagnosed and stopped.

## Failure evidence and verification

Command output is retained in redacted diagnostic artifacts. Failed checks also
produce structured records containing the command, stage, environment, test
identity, assertion and termination reason. Managed verification returns this
information to its caller, including during candidate generation. Large records
have a complete artifact reference and an explicit count of details omitted from
the inline view; they are not silently reduced to output head/tail fragments.

Focused verification starts with the previous failing nodes, followed by the
active component and retained regressions. Checks belonging to future components
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

Experiment schema 5 separates historical achievement from currently reusable
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
