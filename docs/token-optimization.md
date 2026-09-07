# Token optimization and accounting

The optimizations retain configured models and efforts, acceptance contracts,
proof requirements, verification gates and independent implementation/review
roles. They reduce repeated inputs and unnecessary context reconstruction.
They do not guarantee a particular reduction in real token usage or latency.

## Full tasks and incremental inputs

Every structured request retains a complete canonical `PromptSpec`. Native
continuations change only the rendered input. A compatible session receives new
conversation messages, new execution evidence and updated context values, with
the output protocol retained. Updates identify the context value they replace.
User corrections retain their order; the preceding native answer is not sent
again as a new conversation message.

Session checkpoint v4 records input positions and hashes. Incremental sending
requires matching provider/session, workspace, task contract, effort, native
configuration, instructions and CLI identity. Rewritten history, removed context,
missing checkpoints or unreadable settings require full reconstruction. Old
checkpoints are readable but cannot authorize incremental input. Completed tasks
and patches are retained.

A provider switch starts a new native conversation with the complete current
task and observable progress. This includes changed-file references, prior
output/report locations and explicitly unverified prior claims. The destination
must inspect evidence before treating a claim as verified. A switch back to an
earlier provider also reconstructs the latest task. Native histories and prompt
caches are not shared between providers.

Incomplete process cleanup stops automatic takeover. Unknown external-operation
outcomes remain unverified and must follow the existing recovery route, rather
than being blindly repeated. A recognized native session-not-found error permits
one full retry only when no product changes occurred; transport diagnostics are
excluded by exact path from this check.

Review output missing a valid `DECISION` marker can be corrected in the same
compatible session. The canonical fallback includes the invalid output and
validation error. Semantic rejection, ownership violations and rollback retain
the existing full retry path.

## Prompt layout and Review evidence

Policy v3 places stable role/stage/model rules before dynamic paths and state.
Exact duplicate rules are rendered once, with aliases recorded for inspection.
Explicitly inapplicable domain rules can be omitted; unknown applicability keeps
the rule. No semantic summarizer or tighter acceptance-context limit is introduced.
Unstructured custom adapter prompts remain full inputs.

Review evidence compares HEAD with the final worktree, including staged and
unstaged edits, and includes untracked additions. Unborn repositories show current
files as additions. Existing excerpt budgets remain unchanged. Missing, binary
and truncated excerpts are labeled with instructions to inspect their full source;
omitted content is never reported as an empty change.

Stable prefixes can improve provider-side cache reuse, but cache boundaries,
native client behavior and model configuration also matter. The project does not
set new native cache options or alter user configuration. Prompt shortening,
cache reuse and real token savings are separate measurements.

## Controls

Use the existing `execution.acceleration` settings:

| Setting | Behavior |
| --- | --- |
| `mode: "on"` | Enables eligible incremental sending and protocol continuation |
| `mode: "observe"` | Sends full prompts and records candidate delta size where a compatible checkpoint exists |
| `mode: "off"` | Disables these optional continuation optimizations |
| `delta_context_enabled: false` | Disables incremental context sending |
| `session_continuation_enabled: false` | Disables optional session and protocol continuation |

Full reconstruction on failover, cleanup protection and accurate Review evidence
remain active in every mode. Observation mode does not make a second model call.
Changing the prompt policy invalidates model review caches; deterministic proof
certificates retain their existing validity checks.

## Usage reports

```bash
python -m auto_agents performance --project /path/to/project
python -m auto_agents performance --project /path/to/project --session SESSION_ID
```

Trace schema v2 records each physical adapter call with a unique `call_id` and
logical parent identity. Failed attempts, smart recovery, provider failover and
health probes are included, even when all providers fail. Duplicate physical IDs
and enclosing logical usage totals are not counted twice. Transport failures
before a provider reports usage remain unknown.

Reports expose:

- `provider_usage`: usage grouped by provider, model, effort and stage, including
  call counts, failed calls, stage-retry calls and full/sent prompt bytes.
- `metrics.provider_calls`, `provider_session_resumes`, `stage_retry_calls`,
  `prompt_modes` and `fallback_reasons`.
- `input_tokens`, `cached_input_tokens`, `output_tokens` and `usage_complete`.
  Totals are `null` when an included call lacks usage; `known_*` fields retain
  the known subtotal and `unknown_usage_calls` states the gap.
- `usage_accounting`: `physical`, `legacy`, `mixed` or `none`. Old logical records
  remain readable, but their historical omissions cannot be reconstructed.

Physical durations are available separately in `provider_duration_seconds`;
adding them to enclosing workflow durations would double-count elapsed time.
Prompt bytes describe authored input, not all provider-native instructions,
tool results, conversation history or model reasoning tokens. Diagnostics write
failures do not cause a completed operation to be retried.

## Validation and rollout

Automated tests use fake providers and temporary Git repositories. They cover
task reconstruction, partial-work takeover, A-to-B-to-A switching, unknown usage,
history rewrite, checkpoint migration, protocol correction, staged Review evidence
and unchanged contracts. No real-provider comparison is automatically launched.

Evaluate actual runs with fixed models and efforts. Compare acceptance outcomes,
rework, input/cache/output usage and end-to-end duration together. Byte savings in
offline fixtures establish reduced repeated input, not production quality or
token-cost significance. If continuation-related quality regresses, disable the
optional optimizations while retaining the correctness fixes.
