# Collab stopped-search diagnosis, 2026-09-29

Session `091707202dc3` in `/home/fuli/projects/sdgp` stopped at 03:18:47
Asia/Shanghai with `Verified progress is required before more model work`.

The retained journal establishes this sequence:

- Child `1ff433939181` reverified candidate
  `81d8dfb771ef0031dabcd3b51677fa328f0740c7`, receipt
  `50929cf7b041c2aec72ac4652499df9f83d55722280c3cfdab20ddb67b0b3ff1`.
- Its release verification executed 185 commands in 10,823.69 seconds and
  rejected two new failures at 03:08:49. The independent proof amendment
  approval remained valid, but did not establish that the candidate passed
  release verification.
- The workflow budget retained `stagnant=3`, `rediagnoses=1`, and
  `diagnosis_due=false`. The parent consumed the blocked child return.
- Terminal model diagnosis then approved another engine repair. Its first
  planning reservation encountered the same exhausted workflow budget, after
  preparing the isolated environment. No new engine planning command was
  admitted. The displayed diagnostic was empty.

## Actual candidate failures

Both failures were reproduced in the retained candidate checkout using the
SDGP virtual environment, with zero real provider calls:

```text
tests/test_storyboard_retry_contract_regressions.py::test_req_232_schema_and_apply_share_authorization_source
tests/test_storyboard_retry_contract_regressions.py::test_truncation_then_contract_repair_succeeds_with_independent_budgets
```

The new provider-facing diagnostics include `adjacent_shot_facts`. The server
authorization diagnostics and retry-trace diagnostic do not include that field.
The existing equality assertions consequently fail. These are different from
the earlier simulated-call-budget failure corrected in the boundary test.

The candidate needs a contract-consistent correction that retains the neighbor
context and the authorization/trace guarantees, followed by managed verification
and delivery. Clearing the budget or retrying the unchanged command does not
repair these failures.

## Engine correction

- Model admission retains the same stagnation policy and now includes the
  workflow counters and latest rejected command, task, source, result reference,
  and verification reason in its diagnostic.
- A terminal `kernel_no_progress` or legacy `agent_errors_exhausted` result with
  an authoritative exhausted search records a deterministic `kernel_budget`
  decision and displays the verification failure. It does not launch another
  terminal model diagnosis or engine implementation.
- Engine submission checks stagnation before preparing an environment for new
  model work. Existing verification, reconciliation, and completed delivery
  paths retain their existing eligibility.
- The existing explicit-resume path can still verify a changed retained
  candidate/runtime. Successful verification can establish real progress;
  failed verification grants no new writer credit.

The initial incident correction was made in the development checkout without
altering the installed runtime, SDGP candidate, receipts or workflow budget.
The subsequent [evidence-driven recovery implementation](evidence-driven-recovery.md)
extends that correction into an automatic continuation policy. Neither this
incident record nor the offline checks claim successful video acceptance.

## Validation

93 distinct regression cases passed across `test_recovery_kernel.py`,
`test_recovery_native.py`, `test_recovery_engine.py`,
`test_retained_candidate_resume.py`, and `test_controlled_workflow_failure.py`.
The first combined run passed 89 cases. Two new output assertions were corrected
to accept the reporter's stderr output and passed on rerun; the two synthetic
root-cause cases passed with `WSL_DISTRO_NAME` unset because the sandbox cannot
invoke Windows PowerShell for backing-storage discovery. This does not validate
live WSL storage admission. Tests disabled external notification hooks.

The new retained-child regressions exercise both current and legacy parent
stops, reject engine submission before environment preparation, preserve the
entire replayed state and budgets, and assert the actionable terminal record.
Existing retained-candidate tests cover successful reverification and delivery
through session/workflow entrypoints and direct/engine-return handoffs.

`git diff --check` passed. The separate SDGP reproduction returned two failures
and reported `real_provider_calls=0`.
