# Collab preflight recovery, 2026-09-27

The restored SDGP command stopped in automation repair job `c289608bbcd140ca9d9b1678`,
before session `091707202dc3` resumed. Its preserved original-boundary observation
already proved that fix child `891c808047b7` could re-enter implementation, but broad
validation stopped with an incomplete baseline classification.

## Corrections

- Provider selection is controller bookkeeping. Successful selection records are
  now persisted after stage mutation checks and rollback finish. Agent edits to
  the same protected file still fail ownership checks. Direct provider calls and
  restoration of the last successful provider retain their previous behavior.
- A focused fix may name an exact regression test that it will add. Retained
  collection now isolates only known missing exact targets, preserves all filters,
  and still checks existing targets. Candidate verification requires collection
  from the authenticated candidate receipt. A missing or excluded candidate test
  cannot pass; task-owned fixes receive no provisional exception.
- An explicitly bound engine repair of a preflight incident proves engine behavior
  and actual re-entry of the retained child. Product repair and browser/video
  acceptance remain with the original workflow. The review includes this
  controller-owned boundary and accepted receipts explicitly retain
  `original_goal_completed: false`; no original goal or requirement is deleted.

The retained candidate's selection changes and regression tests were inspected
and integrated from immutable runtime commit `a6fcbbe08b580b008426c0001c51441cab0a2ab8`
(source identity `e27da096c080a587cd97f9952fd81ac82db33c7d55bf89800fd78e489c5a329f`).
Its old acceptance receipt is not reused as proof for these new source changes.

## Validation

Focused collections, stage migration/ownership and failover checks passed.
The broader clarification and retry checks passed; controller checks that need
WSL host disk observations passed when rerun outside the restricted shell.
Recovery evidence, strict no-new-failures comparison, project instruction and
frontend checks passed. Original-child recovery and session acceptance checks
passed with the full sandbox boundary using a temporary private `/run` mount.
No live session state, product code, media or paid provider operation was modified
by these checks. The retained command still needs to be run to continue product
repair and its real acceptance.
