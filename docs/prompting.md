# Prompt policies

auto_agents uses a shared stage contract and a small, versioned model-specific
supplement. New calls, including calls in resumed workflows, use the current
policy. Existing task progress and completed work are retained.

Policy v3 keeps stable instructions before dynamic path/state fields, deduplicates
identical rule text, and records rule aliases in prompt metadata. It does not
summarize acceptance contracts or discard rules whose applicability is unknown.

```json
{
  "prompting": {
    "model_adaptation": "auto"
  }
}
```

`auto` is the default for both new and existing project configurations. Set
`model_adaptation` to `generic` to disable model supplements while retaining the
current stage boundaries, output contracts, context layout and test ownership.
This setting does not select a model or change native reasoning effort or tool
permissions. Continue to configure those in the provider's native configuration.

## Stage contracts

The internal `PromptSpec.purpose` identifies the actual role independently of
the effort-routing `AgentRequest.stage`. In particular, fix classification and
collab diagnosis remain read-only even when they use implementation effort.
Custom adapters can continue accepting `AgentRequest.prompt` as a string.
Requests without a `prompt_spec` or explicit `purpose` are passed through intact.

Owned acceptance criteria and required evidence remain mandatory. Existing
behavioral test coverage can be reused when sufficient; explicit requests for
new tests are still binding. auto_agents executes managed `verification_refs`
and broad verification. Implementation agents prepare the candidate and proof
declarations, and must not claim unexecuted checks passed.

The final output contract follows the task context, retry evidence and RepoMap.
Short summaries do not replace required `ORACLE_PROOF_UPDATES`, coverage analysis,
review decisions, routing markers or commit-message lines. Their existing wire
formats and validators continue to apply.

## Model identification

Resolution uses CLI arguments and native configuration, without model calls or
network catalog requests. Help/version probes are bounded and cached against
the binary identity; configuration is read afresh. Config parsing diagnostics
never include raw config values. Python 3.9 and 3.10 use the conditional `tomli`
dependency for TOML parsing; Python 3.11+ uses `tomllib`.

* Codex: model flags and `--config` overrides, trusted project layers, native
  profile files (or legacy profile tables), user and system configuration.
* Claude Code: explicit model selection, configured aliases, settings and alias
  environment overrides. Unresolved aliases, hybrid modes, third-party gateway
  mappings and native fallback models use the generic policy.
* Copilot CLI: explicit model selection and the selected native profile's
  settings. Modern `settings.json` overrides legacy `config.json` values.
* Antigravity: explicit/native model selection. When the installed CLI supports
  `--model`, profile selection uses that flag without changing global settings.
  Legacy settings-based selection does not enable a guessed model policy.

Unknown aliases or unreadable/unsupported configuration use `generic`; they do
not block a task or cause provider failover. Profiles are reviewed code shipped
with auto_agents, not prompts downloaded at runtime. Runtime-reported model IDs
are recorded separately from the pre-call policy decision when the CLI exposes
them. They are not used to guess what a later invocation's mutable alias means.

## Native project instructions

The human source remains `.auto-agents/project-rules.md`. Normalized rules retain
the existing four categories. The generated complete contract is
`.auto-agents/project-rules.agent.md`; unlike the short entry files, it does not
truncate rules to character or rule-count limits.

Codex, Claude Code and Copilot entry files link to this complete contract.
Copilot's general product contract applies repository-wide. Antigravity gets an
always-on `.agents/rules/auto-agents.md` entry. Explicitly hand-authored path
constraints are preserved.

The instruction lock records generator version and actual generated-file hashes.
Unmodified legacy generated files can be replaced on upgrade. Other existing
content is preserved; the generator only replaces its marked managed block or
appends a new one. Malformed/ambiguous managed block boundaries produce an error
instead of silently overwriting the author's file.

## Continuations and diagnostics

Failover renders each provider attempt from the unrendered specification. A
native session is reused only when existing workspace/session checks and the
prompt compatibility identity match. Missing legacy metadata or changed stage,
model policy, effort, native settings, workspace, CLI version or project instructions causes a fresh native session
with the saved task and progress. A completed task is not restarted.

Session checkpoint v4 records sent input identities and immutable history-prefix
hashes. Compatible calls send new messages and changed context values; the native
model's own preceding answer is not appended again as a new input. Failed calls
invalidate the checkpoint. Older checkpoints load normally but require a full
new native session. Review protocol correction can reuse a compatible session
only for a missing/invalid `DECISION` marker; semantic failures and rollback do
not use this optimization.

The saved `PromptSpec` always retains the complete task. Switching providers
clears all native continuation state, re-renders for the destination provider,
and includes observable progress plus references to preceding outputs. Prior
model claims are not verification evidence. Returning to a previous provider
also starts from the latest complete task, rather than its stale native history.
Incomplete process cleanup blocks both failover and automatic continuation.

Model review caches include prompt-policy identity. Deterministic test
certificates keep their existing code/dependency/contract validity checks.

Provider attempts with structured prompts write adjacent `.prompt.txt` and
`.prompt.json` artifacts next to their progress reports. Metadata includes
purpose, policy version/hash, rule IDs, model resolution source, native
instruction fingerprint, bytes, prompt hash, elapsed time and available usage.
Session performance spans also include prompt metadata. These are diagnostics,
not proof that task acceptance passed. Progress narration does not replace the
supervisor's semantic-progress checks.

Metadata also records full versus sent prompt bytes, full/delta/handoff mode,
fallback reasons, and observed delta size in observation mode. Physical call
records include failures, internal continuations and recovery probes. Their
usage is deduplicated independently of logical workflow spans. Missing usage is
unknown, not a zero-cost call. See [token optimization](token-optimization.md)
for report fields and rollout controls.

## Explicit evaluation

Normal tests are offline and never invoke configured models. The captured
pre-change baseline is `tests/fixtures/prompt_baseline.json`. To capture another
baseline, run the evaluator script with `PYTHONPATH` pointing at that checkout's
`src` directory:

```bash
PYTHONPATH=/path/to/baseline/src python /path/to/current/src/auto_agents/prompting/evaluate.py capture --output /tmp/baseline.json
```

To explicitly run real providers with three repetitions per variant:

```bash
python -m auto_agents prompt-eval run \
  --project /path/to/configured-project \
  --baseline tests/fixtures/prompt_baseline.json \
  --providers codex claude-code antigravity-gemini \
  --effort deep --repetitions 3 --output /tmp/prompt-evaluation-001
```

The source project is used only to read configuration. Each case and variant
runs in a new temporary fixture; results and exact prompts are retained under
the requested new report directory. Cases exercise small edits, multiple
behaviors, read-only review, retry feedback, missing frontend evidence,
destructive persistence behavior, user corrections and retained work.

Use explicitly pinned model IDs and the same CLI versions/effort for comparable
runs. Check task acceptance, protocol and scope results first, then compare
tokens and latency. These smoke cases complement the workflow failover and
recovery tests; they do not demonstrate production statistical significance or
measure every kind of frontend or persistence correctness.
