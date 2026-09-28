# Collab terminal diagnosis, 2026-09-28

Session `091707202dc3` in `/home/fuli/projects/sdgp` reported an incomplete
self-repair investigation at 22:41:04 Asia/Shanghai. Its `terminal-triage.json`
retains `root_cause_diagnosis_unavailable` and the underlying exception:
`Concurrent transition won; reload state`.

The retained workflow journal shows child `1ff433939181` stopped with
`kernel_no_progress` after candidate verification failures. Its shared budget
has `stagnant=2`, `rediagnoses=1`, and `diagnosis_due=false`. This is an exhausted
search boundary; restarting must not manufacture another implementation credit.
The parent nevertheless treated that boundary as a transient agent error,
repeated it five times, and stopped with `agent_errors_exhausted` at 22:36:06.

Root-cause investigator/reviewer requests have no business usage context. The
native provider adapter inferred the ambient run and classified these diagnostic
requests as business planning calls. Parallel diagnostic roles could then collide
on that unrelated stream, and successful diagnostic responses could become plan
proofs for the wrong task. The terminal renderer hid the diagnostic exception
behind a generic “investigation ended” line.

## Changes

- Collab and provider-resolution loops retain kernel blocks directly, including
  their diagnostic details, without transient-error retries or rollback of
  uncertain effects.
- The bounded, read-only root-cause consensus roles execute through their
  coordinator's provider path without registering ambient business operations.
  Normal business calls and the kernel's bounded rediagnosis remain governed by
  the existing reservations and stagnation budget.
- Terminal evidence retains the explanatory failure before a generic stop
  summary. Incomplete diagnosis is labeled accordingly, and the CLI displays the
  sanitized exception and the terminal diagnosis record path.

## Validation boundary

Regression coverage exercises parallel root-cause roles with an active kernel,
unchanged ambient run state/budgets, preserved collab/provider blocks, normal
native reservations/replay, and terminal output with secret redaction.

Use the `autoagents` Python 3.11 environment for the reporting suite. The default
Python 3.9 interpreter exposes an existing `_ParentFileHandler._closed`
compatibility failure, outside this change. Synthetic root-cause tests also need
host WSL storage discovery isolated when the sandbox cannot invoke PowerShell;
these checks do not validate live Windows backing-storage admission.

An initial CLI test run inherited the checkout's notification configuration and
attempted Enterprise WeChat notifications. The controlled-failure test module now
disables that hook explicitly, and subsequent commands used an empty
`WECHAT_WEBHOOK_URL`.

Final targeted results: 250 distinct cases passed across native recovery,
controlled terminal failures, reporting, root-cause consensus, collab/provider
sessions, and the retained stagnation-budget regression. This includes the final
35-case controlled-failure rerun after correcting the diagnostic-output test.

## Retained candidate recovery

The final candidate's full stdout identified the failing obligation:
`tests/test_text_protocol_boundary_api.py::TextProtocolBoundaryTests::test_req_234_patch_request_is_positive_first_typed_and_deduplicated`.
Both the retained baseline (`452ad3ce`) and candidate (`3d81017c`) fail this test
with `storyboard_semantic_conflict`; the new neighbor-context test already passes.
The old boundary test expects an initial candidate plus a semantic repair, but
omits the current `total_model_calls` setting. Explicitly setting its simulated
budget to 2 makes both required tests pass without changing any assertion or
production policy. The isolated runs reported zero real provider calls.

The authorized local correction was sealed through the existing candidate-writer
receipt machinery, retaining the original receipt and blocked state. The new
candidate commit is `81d8dfb771ef0031dabcd3b51677fa328f0740c7`, with receipt
`50929cf7b041c2aec72ac4652499df9f83d55722280c3cfdab20ddb67b0b3ff1`.
Business model-call and implementation budgets were unchanged.

The engine now resolves a consumed stopped-child return at explicit resume,
including the legacy parent `agent_errors_exhausted` state and engine-return
wrappers. It prepares an idempotent continuation for the original child and exact
candidate/runtime. Ownership and receipt validation precede publication. It does
not seed a new writer or grant retry credit. Successful verified delivery clears
the parent's obsolete consecutive-agent-error count; failed verification keeps
the stop in place. Retained verification identity now includes the adopted
verifier runtime, so a fixed verifier cannot reuse an old failure as fresh proof.

Targeted verification feedback preserves pytest failure IDs and assertion
excerpts from stdout even when conda emits only a launcher wrapper on stderr.
Structured diagnostics retain bounded stdout/stderr and artifact references.

Independent proof amendment review uses its own durable review reservation,
without creating a business review for the parent. The actual review exposed a
second issue: duplicated complete sources produced 1,827,779 input characters,
exceeding the provider limit of 1,048,576. Large review inputs now use bounded
diff navigation and a content-addressed complete evidence file. Approval still
requires coverage of the entire original delta and exact input identity.

After explicit user authorization to send the source evidence to
`codex-fuli0110`, the corrected bounded review completed with `approved`. Its
sealed review identity is
`9ecaf2e3951c4bb8d141694fc1d878824bea3f90bd8acdd7a401eee8c8edfcdd`.
The approval belongs to the exact new candidate receipt; business budgets remain
unchanged. The candidate awaits ordinary managed verification and delivery on
resume. No real-video acceptance was executed or claimed.

The final isolated commit candidate passed 124 regression cases. The extended
child/candidate/parent-budget/kernel/proof suites passed 90 cases (including three
overlapping prompt cases). All four session/workflow and direct/engine-return
combinations were rerun successfully after adding an explicit assertion that
parent and child attempt epochs survive recovery unchanged.
