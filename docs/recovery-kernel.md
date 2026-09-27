# Unified recovery kernel

A new installation starts in staged mode. No SDGP session or installed runtime is switched
by importing the package, running tests, or inspecting migration inputs.
`repair upgrade --runtime PATH` is the explicit release boundary. It requires
a clean committed runtime, independent release evidence, compatible control
protocols, quiescent operations, and all project migrations before adoption.

## State and execution

`src/auto_agents/recovery/model.py` defines immutable task contracts, typed
outcomes, commands, and phase evidence. `reducer.decide` is a pure transition
function. `KernelStore.apply` commits the event, replayable state, usage, and
outbox in one SQLite transaction, with aggregate revision checks. Protocol 2
also requires the current runtime epoch for mutations.

Tasks, candidates, incidents, continuations, and runtime adoption have separate
identities. A version change cannot reopen a resolved incident. Native writer,
reviewer, and verifier effects retain their original business contract while
recording separate execution contracts and input identities. A model response
proves only that its phase returned; it does not prove business acceptance.

Dispatch is durable before the executor runs. A crash after dispatch leaves an
unconfirmed effect. The executor first reconciles its durable receipt; an
unconfirmed external operation is never automatically sent again. Completed
responses are reusable without another model reservation. Cancel retires only
undispatched outbox entries and never refunds usage.

Two unsuccessful candidate checks require one bounded diagnosis. Two further
unsuccessful checks stop model work. Restart, a new operation ID, or a version
change does not reset these counters. Only a new verified obligation grants
progress; repeated evidence for the same obligation cannot do so.

## Native state and migration

For admitted projects, session/run/workflow/handoff JSON files become small
display projections. The kernel database supplies reads even if a projection
file is lost. Workflow events and their snapshot advance in one kernel transaction;
the old event index is a projection. Loaded model objects carry their own expected blob identity, so
reading another model cannot authorize a stale object's write. Diagnostic
snapshots export hydrated data without a connection to the live kernel.

Large preimages and proof manifests are content-addressed objects. File sealing
uses streaming SHA-256, atomic publication and fsync. Event replay checks both
the journal chain and the resulting projection.

`repair migrate --check --project PATH` inspects legacy inputs without changing
them. The importer seals original records, preserves attempt consumption,
validates retained candidate receipts and workflow journal chains, and combines repeated jobs for the same
route into one incident. An immutable, matching live-recovery ACK takes
precedence over a later mutable blocked transaction. Invalid candidate evidence
is imported as blocked, including when an old status flag says completed.
Explicit repair-chain limits and proof-review consumption are retained separately
from ordinary business calls. A newly created child is bound to its parent's
workflow before its first state publication.

The SDGP audit imported its state into a temporary database. It retained 31
historical calls and combined the six jobs for session `091707202dc3` into one
resolved incident. The before/after input manifest was identical. This proves
historical import and terminal-state retention, not current project acceptance.

## Review protocol

One `ReviewManifest` supplies the prompt, JSON schema, permitted change IDs,
coverage checks and correction diagnostics. For an empty change set,
`change_coverage` has `maxItems: 0`. A reviewer cannot substitute filenames or
line numbers for controller IDs. A format correction preserves the substantive
verdict and findings and does not grant verification credit.

## Upgrade and maintenance

The bootstrap validates the adopted immutable runtime before starting a fresh
interpreter. Release checks come from the trusted verifier's corpus, execute
without networking in a disposable container, and bind their receipts to the
exact runtime source. Candidates cannot select a smaller gate inventory.
The trusted controller prepares Python and Vitest from its declared dependency
recipes. Missing verification software is an environment outcome.

Current journals are also replayed by the proposed runtime before adoption;
an old receipt cannot authorize rollback after business history has advanced.

The release path fences new legacy dispatch, takes project locks in a stable
order, rechecks migration inputs, and atomically advances the runtime epoch and
pointer. Pending, running, or unconfirmed effects prevent cutover. Compatible
rollback selects a previously verified runtime without rolling back business
events, consumption, or project membership.

Maintenance commands are `repair status --json`, `repair migrate --check`,
`repair upgrade --runtime PATH`, and retained `resume`, `cancel`, and
`retry-publish` operations selected with `--job` or `--project` as appropriate.
Publication reconciles the accepted revision against the remote; it never
opens another implementation search to resolve remote divergence.

## Engine execution and rollout boundary

New engine incidents use `engine.EngineRunner`: plan, implementation, current
regression/recovery verification, and independent review are kernel commands.
The executor reuses the confined provider, candidate Git custody, mandatory
test collection, test-preservation audit, and original-scene replay primitives.
Review protocol failure gets one correction and cannot restart implementation.
Candidate rejection returns to the same task; uncertain effects remain blocked.
The accepted engine candidate goes through the separate upgrade transaction
before the original business continuation runs in a fresh interpreter.

Business acceptance retains the existing acceptance coordinator and adds a
kernel completion observation after it validates the goal, authorization,
environment, review and evidence. A provider response alone cannot complete the
business task.

The staged adapters are not evidence that the proposed replacement has been
deployed. Production cutover requires the complete gate receipt and migration
transaction on the final committed source. Architecture checks and deployment
receipts do not claim that the SDGP business goal has been accepted.
