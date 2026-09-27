# Collab candidate selection failure, 2026-09-27

Job `3a630f320584434aaac901d6` loaded `9045056`, restored the original child,
received its live recovery acknowledgement, and published the accepted engine.
The later failure was in candidate validation, after the first product writer.
The launcher eventually reported `scope_evidence` because diagnostic admission
failed while handling that new exception.

## Findings

- Child `891c808047b7` retained a classified storyboard issue, but its `fix-1`
  prompt contained only the broad parent goal and generic provider guidance.
  It omitted the classified issue and its explicit verification command. The
  resulting candidate changed APIMart refusal classification, not the requested
  adjacent-shot context, and did not add the promised storyboard regression.
- Candidate pytest selection raised `RunnerContextError` during verification
  identity calculation, outside the verification result handler. It interrupted
  the workflow instead of giving the same candidate actionable feedback.
- The diagnosis cited the entire child state, which embeds candidate preimages
  and was 33,462,967 bytes. Scope admission rejected it at the 4 MiB read limit,
  obscuring the preceding failure.

## Corrections

Implementation prompts include the classified issue from the controller-owned
session directory and the required verification command. This also works when
the writer uses a historical private checkout without those state files. Explicit
foreign issue/handoff identities are rejected; older task-only issue records
remain compatible. The original user goal remains present.

Binding admission checks retained selection authority. Actual candidate selection
still runs before executable verification and completion. A trusted collection
report with exit code 4, only known exact-node missing errors and no collection
errors can produce repair feedback. Other collection failures remain controlled
blocks with stdout/stderr diagnostics. Fresh and interrupted candidates use the
same result path, without dropping required nodes or resetting attempts.

Whole-file scope witnesses now hash incrementally and retain the full content
digest. Large embedded candidate data is neither loaded as one byte buffer nor
inserted into a model prompt. JSON-pointer evidence keeps its existing size cap.
Changed content still invalidates the witness and any reused scope receipt.

## Verification boundary

Tests cover a missing-node candidate followed by a successful correction, restart
with that same receipt, collection faults, controller-owned prompt selection,
legacy issue records, and streamed evidence revalidation. The retained real
candidate was also collected in a scratch checkout: the missing storyboard node
was confirmed, the corrected prompt included the classified issue and mandatory
test, and the actual diagnosis passed scope admission. Live session bytes remained
unchanged during these checks.

The additional offline replay copies both the latest workflow and its retained
candidate into a network-disabled container. It stops at the next writer request
after candidate failure feedback, before any model call. It does not claim that
the storyboard repair or final real-video acceptance has completed.

Validation finished with 198 distinct regression cases passing. This includes
all 20 receipt-integrity cases after the legacy issue-record compatibility fix,
and an additional interrupted collection-fault case. The sealed real-scene replay
preserved the existing candidate fingerprint, kept the parent budget unchanged,
and reserved child attempt 2 after retained attempt 1 without changing either
limit. Its only intercepted request was the correctly scoped fix request; zero
model calls and zero test bodies executed before that boundary. The observations
are retained in
[`collab-candidate-selection-validation.json`](collab-candidate-selection-validation.json).
