# Iteration performance review

Reviewed on 2026-09-05. The comparison starts at engine revision `2226640`.

## Evidence and bottlenecks

A local iteration recorded a self-repair experiment spanning about 15 hours 21 minutes on
September 3–4. Its 29 candidates included 13 semantic-review rejections, 7 verification failures,
3 deterministic rejections, 4 completed components, 1 full-suite failure, and 1 approval.
The 29 provider-attempt records contain 9.04 hours of candidate execution, with a median of
15.52 minutes and a maximum of 66.89 minutes. This includes the provider's reasoning, tools,
and tests; it is not pure model inference time. Historical records predate these optimizations
and can include interruptions and earlier engine implementations.

The same run's performance trace contains 4 planning spans totaling 28.6 minutes and
13 implementation spans totaling 3.92 hours. These are partial stage records, and nested gate
durations must not be added to parent implementation durations. Self-repair phases were missing
from that trace, so precise historical review-versus-validation time cannot be reconstructed
from it.

| Stage | Finding | Action |
| --- | --- | --- |
| Clarification and prototype approval | The sample does not quantify these stages; required human decisions are workflow boundaries. | Preserve approvals and collect stage evidence before tuning them. |
| Design and planning | Recorded planning work is substantial, but the engine already reuses approved repair designs and supports bounded plan patches. | Preserve that reuse; expose repair-design time explicitly. |
| Candidate generation | Retry context carried candidate prose and command names, but omitted actual verification failures. stderr could also hide pytest stdout. | Include bounded, redacted failure evidence in subsequent candidate context and retain both output tails. |
| Candidate review | The final component underwent two semantic reviews of unchanged code with the same replay evidence. | Review the integrated repair first, then reuse approval only for the same clean commit and unchanged contract, components, and blocking findings. |
| Full-suite verification | The baseline thread was awaited before candidate execution. Resource waiters could occupy all executor threads. | Execute base/candidate proof concurrently through one bounded resource pool; reserve resources before dispatch and backfill with ready shards. |
| Gate scheduling | Priority and dispatch estimation each reopened the timing database for every command. | Batch one plan's timing reads and reuse estimates; refresh a command after execution. |
| Diagnosis, external services, and model/tool latency | The available trace does not isolate their end-to-end cost. | Preserve proof requirements and model effort; use the new phase records to guide subsequent tuning. |

## Controlled measurements

These measurements isolate the changed mechanisms. They are not forecasts for a complete
iteration involving an LLM, external services, or human interaction.

| Scenario | Before | After | Interpretation |
| --- | ---: | ---: | --- |
| Base and candidate proof, each with 150 ms of independent work | 0.3011 s | 0.1514 s | The baseline wait no longer precedes all candidate work. Both proof results are still required. |
| Three shared-resource shards plus three independent shards, 150 ms each, two execution slots | 0.6038 s | 0.4570 s | About 24% less wall time from dispatching ready work past resource waiters. |
| Priority and dispatch estimates for 500 commands with populated timing history | 0.2847 s; 1000 connections | 0.0058 s; 1 connection | About 49x faster metadata reads. This is scheduler overhead, not test execution time. |
| Successful single-component candidate, deterministic provider fixture | 2 semantic review calls | 1 semantic review call | A fresh review remains mandatory if code, contract, component, or findings change. |

## Verification and operational behavior

Regression tests exercise ready-work dispatch, cross-suite exclusivity, unchanged-review reuse,
source/contract invalidation, acceleration opt-out, timing persistence on exceptions, and failure
feedback. The baseline and candidate suites retain their timeouts, resource constraints, full
coverage, and differential comparison. Final approval still requires deterministic proof sealing.
The timing cache is advisory and remains scoped by environment and resource signature.

The new overlap and review reuse follow `execution.acceleration.mode`. The default `on` enables
them; `observe` and `off` retain sequential comparison and fresh integration reviews. Timing
remains available in all modes. No model or effort defaults were reduced.

Inspect the next run with:

```sh
python -m auto_agents performance --project /path/to/project
```

The `totals` map now contains `self_repair:repair_design`, `self_repair:candidate_generation`,
`self_repair:candidate_correction`, `self_repair:contract_reanalysis`,
`self_repair:review_*`, `self_repair:focused_verification`, `self_repair:focused_baseline`,
`self_repair:integration_verification`, `self_repair:boundary_replay`,
`self_repair:diagnosis_differential`, `self_repair:full_suite`, and `self_repair:proof_seal`.
Entries identify their candidate and root in the underlying JSONL metadata. Full-suite timing
records the enclosing wall time once rather than summing concurrent base/candidate durations.

The effect on the number of candidates and total iteration time requires a subsequent real run.
The historical experiment was analyzed without rerunning its provider calls.
