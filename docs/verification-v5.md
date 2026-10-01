# Verification policy v5

Policy v5 separates candidate feedback from final coverage. A fix first runs
affected checks and independent review. Delivery additionally requires a sealed
`final`/`release` receipt for the same candidate, environment and complete proof
inventory. An affected pass cannot satisfy that boundary. Explicit full
verification still forces current candidate execution. Known baseline failures
remain comparison evidence, never hard-requirement passes.

## Configuration and migration

Versions 1–4 keep their selection rules. Upgrade a project's current config with:

```sh
python scripts/migrate_verification_v5.py --project PROJECT
python scripts/migrate_verification_v5.py --project PROJECT --manifest REVIEWED.json --apply
```

Preview is read-only. A reviewed manifest contains `config_sha256` and `updates`
indexed by existing proof ID. The original config digest must still match when
applying. Exact targets, runner arguments, proof IDs and final coverage cannot
be removed by this migration. Existing frozen sessions keep their original
bindings; a subsequent newly bound task uses the new config. No running command,
candidate receipt, budget or diagnostic is reset.

New optional step fields:

| Field | Meaning | Default |
| --- | --- | --- |
| `impact_symbols` | Reviewed `file.py::qualified_name` associations | empty |
| `release_trigger` | Require immediate release coverage | false |
| `coalesce_safe` | Independently established compatibility in one pytest process | false |
| `node_replay_safe` | Independently established failed-node baseline replay | false |

`Class.*` and `*` cover reviewed components. `<module>` explicitly declares
initialization-only dependency. Body-only changes can use these associations;
namespace/signature/decorator/import/global changes retain file dependency
coverage. Unknown business changes escalate to release. New or changed test
files execute directly using the project's trusted runner, without inheriting
another test's concurrency/replay capabilities. Test/fixture edits disable
execution optimizations until reassessed.

`risk=critical` no longer implies every release check on every iteration.
Release-blocking paths and explicit triggers still do. Final coverage includes
both affected and release proofs, independently of ordering edges. True
artifact-producing dependencies remain. Generated task plans preserve reviewed
v5 metadata when proof identities and exact execution definitions still match.

## Evidence and scheduling

Raw test certificates bind execution policy, source, environment and relevant
metadata. Overall receipts separately bind selection, comparison and delivery
rules, so changing a decision rule can reassess existing raw proofs without
blindly accepting an old verdict or rerunning every unchanged test. Cache miss
diagnostics contain component digests and variable names, not secret values.
Unavailable HMAC keys disable persistent reuse.

Collection certificates are separate from body execution and do not create
progress credit. Trusted body receipts include collected nodes and phase
outcomes; failed subtests cannot be overwritten by a passed parent report.

Baseline certificates use a separate table and require complete, unchanged,
network-free input evidence. Infrastructure errors, interrupted execution,
mutations and incomplete traces are ineligible. Their failure nodes, phases and
error features are compared with the candidate. A changed error in the same
node is a regression, not a baseline exemption.

Compatible pytest checks can share a physical command while retaining their
logical proof IDs. Existing constituent certificates are checked before running
uncertified members. Audits and explicit fresh execution remain effective.
Coalescing respects dependency frontiers and the existing 300-second batch
target. Reports distinguish selected proofs, physical batches and reused
evidence. Interactive output refreshes the current action in place; plain output
keeps one action heading, with counts and heartbeats retained in diagnostic events.

Start audited parallel work with two workers and real CPU/memory leases.
Independent commands receive private worktrees, database/temp state and process
state. Actual shared host resources retain named locks. Artifact chains retain
their admitted shared lane. Tests that change their own isolation assumptions
are not automatically trusted with old capabilities.

## Measurements

`scripts/benchmark_verification_v5.py` measures cold validation, a local edit,
unchanged recovery and final release in a disposable offline reference project.
It never calls a provider. The reference run measured 1.380 s, 1.026 s,
0.051 s and 1.051 s respectively; unchanged recovery executed zero test
commands, while final release covered both proofs and executed only its missing
check. These are mechanism measurements, not SDGP end-to-end speed claims.

The SDGP migration retains all 82 existing proofs and real artifact edges.
For the retained four-file candidate, affected selection is 50 steps rather
than the old 82-step release expansion. The additional explicit fix check
remains mandatory. Two offline text protocol nodes passed together and in
independent concurrent processes. Broad API/media release checks remain pending
the normal project workflow; no complete SDGP release pass is claimed here.

## Validation limits

Focused v5, recovery, cache, selection, validation and managed-execution checks
passed. The full engine suite was not completed. An existing observed-input
executor test fails to resolve its input manifest in this environment; the same
failure was reproduced from the unmodified pre-change HEAD. A broader CLI group
also exits during a test that clears the process environment and reaches the
installed-runtime handoff. The final executor/v5 check passed 44 tests, excluding
that input-manifest case and an existing fixed-job-name test whose temporary
runtime directory was already occupied. Neither exclusion establishes a
full-suite pass.

## Runtime adoption and large journals

Passing the 11 static upgrade gates is followed by replay of the installation's
actual business history. A large journal could exceed the independent verifier's
unchanged 120-second replay limit, leaving the previous runtime active despite
all static checks passing. Replay now mutates one privately owned state through
the same transition rules and compares every intermediate canonical projection
inside SQLite. Normal decisions remain pure; event checksums, projection checks
and final-state equality remain mandatory. No history or budget is reset.

For the observed 519-event workflow, the largest stored snapshot was about
67 MB and the installation database about 4 GB. Read-only host replay fell from
68.65 seconds to 27.85 seconds with the same history frontier. This host timing
does not replace the installation's independent container gate. Upgrade logs
now announce real-history replay and retain its phase, exit code and sealed log
reference when it fails.
