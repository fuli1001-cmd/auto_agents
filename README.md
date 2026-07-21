# auto-agents

`auto-agents` is a quality-first local orchestrator for AI-assisted project delivery.

V1 scope:

- New and existing projects
- Provider-agnostic orchestration
- Stage-specific effort policy
- File-driven context to reduce token usage
- Strict task plan validation before execution
- Agent-generated verification strategy and test commands during planning
- Limited retries for planning, review formatting, and task rework
- Automatic git commit after each verified feature slice

## Why this shape

The system optimizes for quality over throughput:

- One orchestrator owns state and gates
- Providers are replaceable adapters
- LLM calls stay short and stage-specific
- Task review defaults to a lighter pass and escalates to deeper review only when the current diff looks risky
- Review prompts are narrowed to the current changed files and diff so the reviewer spends less time rediscovering context
- Scripts, not the model, enforce quality gates
- Retry prompts carry structured failure summaries, implicated paths, and cropped evidence excerpts instead of dumping raw logs back into the next attempt
- Per-task verification now compares against a task-local baseline so unrelated pre-existing failures are de-emphasized during the implement loop
- Invalid plans and malformed reviews are rejected and retried with focused feedback
- Local project isolation is preferred over changing shared system environments

## Core workflow

1. `clarify`: interactively refine an idea into a project brief, or extract it automatically if the spec is a detailed design
2. `design`: create a top-level architecture document
3. `plan`: generate a JSON task plan with small verifiable feature slices plus a verification strategy
4. `implement`: execute one feature slice at a time, with per-task verification and independent agent review
5. `verify`: run a final local gate pass across the full project
6. `readme`: interactively generate a project README from the finalized repository state

Review and commit happen inside the `implement` loop for each task, not as separate top-level stages.

## Environment isolation policy

The workflow now treats environment isolation as a hard rule rather than a suggestion.

- Python projects must use a project-local conda environment at `./.conda`
- Python package installation must run inside that conda environment
- Python verification uses structured `verification_steps` with the `pytest` runner; auto_agents
  derives commands such as `conda run -p ./.conda python -m pytest -q tests`
- Non-Python projects must also avoid modifying shared system state; prefer repo-local dependency
  directories, project-local toolchains, or other isolated setup that stays inside the project

This policy is enforced in two places:

- planner and implementation prompts explicitly instruct the agent not to use global installs
- local validation rejects obvious global install commands and rejects Python verification commands
  that do not run inside a project-local conda environment

## Execution details

The automation does split the project into tasks automatically, but only after `plan` runs. The
planner rewrites `.auto-agents/state/task_plan.json` with as many small feature slices as the MVP
actually needs, and that file
becomes the execution contract for the rest of the run.

There is no hard task-count cap now. Instead, validation warns when a plan looks over-fragmented so
you can inspect whether the work was sliced too finely.

Task execution is sequential by default:

- the orchestrator walks `task_plan.json` in order
- each task moves `pending -> in_progress -> done` or `blocked`
- only one task is implemented at a time
- after one task passes `implement -> verify -> review`, the orchestrator automatically starts the
  next unfinished task in the same `run`
- `--max-tasks N` stops the current invocation after `N` successful tasks, which is useful for demos
  or controlled rollout

An experimental opt-in path can parallelize independent tasks in separate git worktrees. That mode
stays conservative:

- `execution.parallel_tasks.enabled` must be true
- every non-done task must carry planner-generated `depends_on`
- malformed or missing dependency metadata falls back to sequential mode, or fails fast when
  `execution.parallel_tasks.strict=true`
- each worker still runs implement/verify/review, and the main worktree still integrates task
  results one commit at a time
- successful worker commits are retained under run-scoped Git refs until integration completes, so
  Ctrl+C does not force an expensive worker task to run again
- results that touch a path already integrated from the same batch are replayed on the latest HEAD
  in an isolated worktree and jointly verified; only a real merge conflict or combined verification
  failure falls back to a persistent sequential retry
- task path history, planned evidence selectors, and declared path hints are used to avoid scheduling
  likely-conflicting tasks in the same batch
- high-risk tasks run a read-only evidence preflight in an isolated worktree before implementation;
  it can return a proof checklist or route an infeasible slice back to plan/clarify
- batch logs include ready/deferred counts, dependency reasons for deferred tasks, and all failed
  workers in the batch instead of only the first failure

For each task, the effective loop is:

1. mark the task `in_progress`
2. run the implementation agent for the current slice
3. run task-owned local verification first, derived from the task's proof `evidence_refs` when
   executable tests are available; otherwise fall back to the configured gate commands
4. run an independent review for the current uncommitted changes
5. if verification and review both pass, mark the task `done` and optionally commit
6. continue to the next unfinished task

If verification or review fails, the orchestrator retries the same task with focused feedback. The
retry prompt includes structured verification triage and allows tightly coupled regression fixes in
explicitly implicated paths, even when those fixes sit slightly outside the original slice. If the
retry budget is exhausted, that task is marked `blocked` and the run exits with failure instead of
silently skipping ahead. When a task falls back to full gate verification and the new failures are
clearly outside the task's owned proof/test surface, auto_agents stops retrying that implementation
loop and reports the result as a contract or gate-scope mismatch.

For comparable owned proof failures, auto_agents can schedule bounded evidence-repair tasks before
it gives up. A repair task is inserted ahead of the blocked parent, owns only precise
`verification_refs`, and the parent waits on those task IDs before retrying its original proof gate.
Review rejection recovery is not limited to evidence-repair tasks: planned tasks, scope-split/replan
children, and stage-recovery tasks all enter the same bounded recovery state machine. Persisted
`task_origin`, `parent_task_id`, `recovery_epoch`, and `recovery_round` fields define lineage without
depending on task ID spelling.

Before starting another implementation cycle, an adaptive read-only judge returns `CONTINUE`,
`REPLAN`, or `STOP`. Deterministic no-progress checks and `execution.recovery.max_rounds` remain hard
limits even if the judge requests more work. A terminal lineage can open a new epoch only after its
contract or repository evidence fingerprint changes. Product-code and generated-test failures stay
owned by the target project; auto_agents self-repair is considered only when structured recovery
state reports an orchestrator routing invariant violation.

The top-level `verify` stage still runs the full configured verification suite before release. If
that full suite fails, auto_agents now treats it as a recovery signal instead of immediately
terminating: it rewinds to `implement`, creates a focused verification-recovery task, and asks the
agent to decide whether the failure is implementation code, stale tests, or both. If repeated
automatic recovery cannot resolve the conflict, the pipeline rewinds to `clarify` so the next run can
use the normal clarification dialog to ask for user guidance.

Manual approvals are supported at three high-value gates:

- `requirements`
- `architecture`
- `release`

`run --auto-approve` auto-passes those manual gates only. It does not disable interactive clarify or
README conversations.

## Repo map (token saver)

Implement / review / fix prompts are augmented with a token-budgeted, ranked
**repo map** — a flat outline of class/function signatures from the most relevant
files. The goal is to give the agent an upfront bird's-eye view so it spends
fewer tokens on blind `grep`/`view` exploration.

Behavior:

- Auto-skipped on non-Python projects (no signal markers, no `.py` files)
- Auto-skipped if explicitly disabled
- Cached per file under `.auto-agents/state/repomap_cache.json`, keyed by parser version and file content
- Anchor files mentioned in the task description / acceptance / retry feedback
  are forced into the map even under tight budget
- Repo map header tells the agent it's a partial view and to use `grep`/`view`
  for symbols not listed

Configuration in `.auto-agents/config.json`:

```json
{
  "repo_map": {
    "enabled": true,
    "budget_tokens": 1500,
    "review_budget_tokens": 750,
    "max_files_scanned": 2000
  }
}
```

CLI override (single run, does not persist):

```sh
auto_agents run --no-repo-map
```

Per-run metrics include `repo_map_enabled`, `repo_map_skipped_reason`,
`repo_map_files_included`, `repo_map_tokens_actual`, `repo_map_tokens_budget`,
`repo_map_cache_hit`, `repo_map_cache_hits`, and `repo_map_cache_misses` so you can quantify savings
from telemetry.

## Quick start

Create a target project skeleton:

```bash
python3 -m auto_agents init --project /tmp/demo --name demo
```

`init` also creates `.auto-agents/project-rules.md` and immediately runs
`sync-agent-instructions`, which writes provider-native instruction files:

- `AGENTS.md`
- `.github/copilot-instructions.md`
- `.github/instructions/product-contract.instructions.md`

Leave `.auto-agents/project-rules.md` empty to use only the default engineering rules. Add stable
project contracts there when a repository needs domain-specific behavior, then run:

```bash
python3 -m auto_agents sync-agent-instructions --project /tmp/demo
```

`sync-agent-instructions` uses the configured provider to normalize the human-readable source into
`.auto-agents/project-rules.normalized.json`, then renders concise agent-facing rules from that
structured file instead of copying the whole document into provider context. The normalization stage
uses `efforts.sync-agent-instructions` when configured, otherwise it falls back to `efforts.plan`.
If `.auto-agents/project-rules.md` is empty or clearly placeholder text, auto_agents skips the LLM
call and emits only the default engineering rules.

`run`, `fix`, `collab`, and `provider-resolve` automatically check the source hash and generated file
hashes before starting agent work. If `.auto-agents/project-rules.md` changes, or if a generated
instruction file drifts, auto_agents regenerates the files and continues.

Switch provider at run time and persist the new default provider:

```bash
python3 -m auto_agents run --project /tmp/demo --provider copilot-cli
```

Convenience defaults:

- `init --name` defaults to the final directory name from `--project`
- `init --doc-language` defaults to `en`
- `run --provider` is optional; if omitted, the orchestrator uses the persisted default provider

Run the orchestrator:

```bash
python3 -m auto_agents run --project /tmp/demo --spec-file /tmp/demo/spec.md
```

`run --spec-file` defaults to `<project>/spec.md`, so this also works:

```bash
python3 -m auto_agents run --project /tmp/demo
```

The input spec can be either a rough product idea or a more detailed design document. The
orchestrator classifies it automatically and adjusts the clarify/design prompts accordingly.

Stream agent stdout and stderr to the terminal while keeping the final run summary on stdout:

```bash
python3 -m auto_agents run --project /tmp/demo --print-agent-output
```

Generate persisted documents, including the final project README, in Simplified Chinese:

```bash
python3 -m auto_agents init --project /tmp/demo --doc-language zh
python3 -m auto_agents run --project /tmp/demo --doc-language zh
```

`run` performs local preflight validation before any agent call. Use `--skip-validate` only for
manual recovery or debugging.

By default, implementation refuses to start if the target repository already has local changes.
Use `--allow-dirty-tree` only when you explicitly want task work to proceed on top of an existing
dirty workspace.

During `plan`, the agent must write `test_strategy` and structured `verification_steps` into
`.auto-agents/state/task_plan.json`. By default the orchestrator derives runnable gate commands from
those steps and stores both the structured steps and derived commands in `.auto-agents/config.json`,
so new projects do not need a hand-written `gates.commands` block.

Python verification steps must use `runner: "pytest"`. The orchestrator derives the project-local
conda command automatically, for example `conda run -p ./.conda python -m pytest -q tests`.
JavaScript and TypeScript verification steps must use `runner: "vitest"`. Legacy
`verification_commands` are still accepted for compatibility, but new plans should not generate
free-form shell verification commands.

Inspect persisted progress:

```bash
python3 -m auto_agents status --project /tmp/demo
```

The `runtime` section reports whether a validated run owner is active, how many supervised process
groups are running, the last process-control heartbeat, and whether cleanup is incomplete. To stop
an active run safely, including its provider and gate subprocess groups, use:

```bash
python3 -m auto_agents stop --project /tmp/demo
```

`stop` verifies the project, run token, PID start time, user, and process-group identity before
signalling anything. It sends `SIGTERM`, waits 10 seconds by default, and then escalates remaining
validated processes to `SIGKILL`. The grace period can be changed with `--grace-seconds`.

Approve a paused gate:

```bash
python3 -m auto_agents approve --project /tmp/demo --gate requirements
```

Reject a paused gate and provide explicit unstructured feedback for the agent:

```bash
python3 -m auto_agents reject --project /tmp/demo --gate requirements --reason "Add a PostgreSQL database."
```

If the run is currently paused on a manual gate, `approve` and `reject` can infer the gate from the persisted run
state, so this is usually enough:

```bash
python3 -m auto_agents approve --project /tmp/demo
python3 -m auto_agents reject --project /tmp/demo --reason "Add a PostgreSQL database."
```

Run tests for this repository:

```bash
python3 -m pytest -q tests
```

Validate a target project without spending tokens:

```bash
python3 -m auto_agents validate --project /tmp/demo
```

Recover a run that is blocked in `provider_research` and continue it after the references are fixed:

```bash
python3 -m auto_agents provider-resolve --project /tmp/demo
```

`python3 -m auto_agents run ...` now auto-enters a fresh provider-recovery session for the
current blocker when it fails with `provider research is blocked`, and continues the saved run
automatically after the provider references are resolved. The explicit `provider-resolve` command
remains available for manual recovery or resuming an interrupted recovery session.

If validation reports that Python commands are running outside `./.conda`, fix the commands before
rerunning the workflow.

Run the real Codex provider demo:

```bash
./examples/run_codex_demo.sh
```

## Provider model

The orchestrator uses its own effort labels:

- `balanced`
- `deep`
- `max`

Adapters map those labels to provider-specific controls through the `profile_map` in project config.

### Codex

For Codex this maps to native config profiles: `balanced`, `deep`, and `max`.
All model configuration lives in Codex's own config files, and the project config only carries the profile name mapping.

### Copilot CLI

Copilot CLI uses the same minimal-config pattern. Each profile name in `profile_map` corresponds to
a native config directory at `~/.copilot/profiles/<name>/`. The adapter passes
`--config-dir <path>` so all model, tool-permission, and effort settings are managed in the
provider's own config files, not in the project config.

Because of a current Copilot CLI issue, model declared in `<config-dir>/config.json` may be ignored
when `--config-dir` is provided. The adapter works around this by reading `model` from that
`config.json` and forwarding it as `--model <value>` unless you already set `--model` in
`extra_args`.

By default, the adapter adds `--allow-all` for headless automation. To override this, pass
explicit tool-permission flags in `extra_args`.

Example project config (`providers` and `active_provider` only):

```json
{
  "providers": {
    "codex": {
      "kind": "codex",
      "binary": "codex",
      "profile_map": {
        "balanced": "balanced",
        "deep": "deep",
        "max": "max"
      },
      "extra_args": [],
      "cwd_flag": "-C",
      "prompt_via_stdin": true,
      "output_flag": "-o",
      "timeout_seconds": 1800,
      "idle_timeout_seconds": 3600
    },
    "copilot-cli": {
      "kind": "copilot-cli",
      "binary": "copilot",
      "profile_map": {
        "balanced": "balanced",
        "deep": "deep",
        "max": "max"
      },
      "extra_args": [],
      "cwd_flag": "",
      "prompt_via_stdin": true,
      "output_flag": "",
      "timeout_seconds": 3600,
      "idle_timeout_seconds": 3600
    }
  },
  "active_provider": "codex"
}
```

Legacy auto-generated `copilot-cli.timeout_seconds = 1800` configs are treated as the old default
and normalize to `3600` on load.

New project configs write `idle_timeout_seconds: 3600` for the bundled providers, and omitted
provider entries now also default to `3600` on load.

To use an absolute path instead of the conventional `~/.copilot/profiles/` location, set the
`providers.copilot-cli.profile_map` value to the full path:

```json
{
  "profile_map": {
    "deep": "/home/user/my-copilot-configs/deep"
  }
}
```

### Antigravity CLI

Antigravity CLI 1.1 and later require the non-interactive prompt as the value of `--print`.
The bundled Antigravity providers therefore set `prompt_via_stdin` to `false`; the adapter places
all CLI options before `--print <prompt>` and closes stdin. Prompts too large for a safe command-line
argument are stored under `.auto-agents/runs/provider-prompts/`, and the CLI receives a short
instruction to read that file instead.

### Generic shell adapter

If another provider does not support reasoning strength directly, the adapter can ignore the hint
and still satisfy the interface.

With smart timeout enabled, a generic shell provider must set
`progress_protocol: "auto-agents-jsonl-v1"`. The wrapper receives
`AUTO_AGENTS_PROGRESS_PATH`, `AUTO_AGENTS_ATTEMPT_ID`, and
`AUTO_AGENTS_RESUME_SESSION_ID`; it must append one JSON object per line to the progress path. The
supported event types are `session.started`, `activity`, `tool.started`, `tool.progress`,
`tool.completed`, `milestone`, `session.completed`, and `error`. Every event must include the
protocol field, for example:

```json
{"protocol":"auto-agents-jsonl-v1","type":"tool.completed","tool_id":"test-1","fingerprint":"sha256-of-command-and-result","detail":"pytest tests/test_api.py"}
```

`session.started` should include `session_id` when the provider supports exact conversation resume.
`tool.completed` and `milestone` should include a stable `fingerprint`; repeated equal fingerprints
without a workspace change are used for loop detection.

Each stage in the `efforts` config block can be set to any of these labels. The default
configuration balances quality and token usage:

| Stage | Default | Effective | Rationale |
|-------|---------|-----------|-----------|
| clarify | `deep` | dynamic | Downgraded to `balanced` when spec is already a design doc |
| design | `deep` | dynamic | Downgraded to `balanced` when spec is already a design doc |
| plan | `deep` | `deep` | Task decomposition affects the whole run |
| provider_research | `deep` | `deep` | Resolves provider-specific requirement references before implementation |
| implement | `deep` | `deep` | Stronger reasoning reduces review rejections |
| review | `balanced` | auto-escalated | Automatically escalated to `deep` for risky diffs |
| verify | `balanced` | `balanced` | Runs local commands, no LLM reasoning needed |
| self_repair | `max` | `max` | Repairs auto_agents itself after eligible orchestrator-owned failures |
| evidence_preflight | `balanced` | conditional | Checks high-risk proof feasibility before code changes |
| readme | `balanced` | `balanced` | Interactive README generation from finalized repo |

Review auto-escalation triggers (when configured as `balanced`):

- High-risk evidence contracts or newly changing retry blockers → `deep`
- Code changes without corresponding test changes → `deep`
- More than 3 non-test files changed → `deep`
- High-risk files changed (pyproject.toml, Dockerfile, CI configs) → `deep`
- Large diffs (>240 lines of non-test code) → `deep`
- Only test files changed in a small diff → stays `balanced`

Setting review to `deep` or `max` overrides auto-escalation and uses that effort for every review.

Implementation-stage verification baselines are cached per command in
`.auto-agents/state/gate_baseline_cache.sqlite3`, keyed by the effective baseline ref, normalized
command, execution mode, and collect-all behavior. Adding one command therefore runs only the new
command. The legacy JSON cache is ignored; the first run after upgrading captures one cold baseline.
Cache corruption disables reuse and falls back to running the real commands.

Gate commands remain sequential by default. A structured `verification_steps` entry is concurrent
only when it explicitly sets `parallel_safe=true`; safe entries are grouped by runner and capped by
`gates.max_auto_workers` (default 2). The marker is appropriate only for checks isolated from shared
databases, ports, mutable fixtures, snapshots, and build output. Existing `gates.parallel_groups`
remain supported as an explicit opt-in.

Gate commands use an activity lease plus an absolute wall-clock ceiling. The defaults for new
projects are 900 seconds without observable output/CPU activity and a 7200-second ceiling:

```json
{
  "gates": {
    "adaptive_timeout_enabled": true,
    "command_idle_timeout_seconds": 900,
    "command_timeout_seconds": 7200
  }
}
```

On a stall or ceiling timeout, auto_agents terminates the entire command process group with bounded
`SIGTERM`/`SIGKILL` cleanup and persists a structured execution incident. Deterministic rules route
high-confidence incidents to bounded retry, stage rewind, or a pre-baseline target-project repair
task. The same incident gets at most `execution.recovery.max_rounds` recovery rounds (two by
default), and a run gets at most `max_incidents_per_run` distinct incidents. Cleanup uncertainty,
low-confidence diagnosis, or exhausted budgets pause safely instead of mutating blindly.

In an interactive terminal, a paused incident enters a recovery-agent dialogue. Background runs
persist the incident under `.auto-agents/runs/<run-id>/recovery_incidents/` and exit paused; resume it
later with `python3 -m auto_agents recover --project /tmp/demo`. Recovery does not automatically
weaken checks, change credentials/global environment, or raise the absolute safety ceiling.

Forbidden-pattern requirements use timeout-capable regex matching with per-pattern/file and total
audit limits. Broad DOTALL wildcards and nested unbounded quantifiers fail closed with a diagnostic;
use bounded spans such as `[\s\S]{0,500}?`. File match results are cached incrementally in
`.auto-agents/state/requirements_audit_cache.sqlite3` by pattern set and file-content hash.

Experimental parallel task execution uses planner-generated dependencies plus isolated git
worktrees. Example:

```json
{
  "execution": {
    "parallel_tasks": {
      "enabled": true,
      "workers": "auto",
      "max_auto_workers": 4,
      "adaptive": true,
      "strict": false,
      "worktree_root": "",
      "pressure_cooldown_seconds": 3600,
      "soft_pressure_threshold": 2
    },
    "requirements_audit": {
      "pattern_timeout_ms": 250,
      "total_timeout_seconds": 300,
      "cache_enabled": true
    },
    "evidence_preflight": {
      "mode": "high_risk"
    },
    "smart_timeout": {
      "enabled": true,
      "provider_idle_seconds": 1800,
      "tool_idle_seconds": 900,
      "semantic_stall_seconds": 3600,
      "safety_ceiling_seconds": 43200,
      "loop_repeat_limit": 3,
      "same_provider_resume_limit": 1
    },
    "recovery": {
      "enabled": true,
      "max_rounds": 2,
      "max_repair_tasks_per_round": 6,
      "max_refs_per_repair_task": 8,
      "max_incidents_per_run": 6,
      "diagnostic_probe_timeout_seconds": 300
    }
  }
}
```

When that mode is enabled, the planner should emit `depends_on` arrays in
`.auto-agents/state/task_plan.json`, for example `[]` for an independent task or
`["task-001"]` for a dependent slice.

`workers` may be an integer for fixed concurrency or `"auto"` for local adaptive scheduling.
In auto mode, auto_agents starts from the active provider's local subscription-tier limit,
caps concurrency at `max_auto_workers`, and lowers concurrency immediately for hard pressure
(rate limits, quota, throttling) or after repeated soft pressure (timeouts, stalls, availability).
A persisted one-worker setting stays in the parallel scheduler during the cooldown and automatically
tries a two-worker canary when `pressure_cooldown_seconds` expires. Successful batches can gradually
raise the next batch's worker count within the cap. A batch only counts as successful for adaptive
scaling when every launched worker result is usefully integrated; deferred/replayed results lower
the next batch's concurrency instead of incorrectly scaling it up. Integration metrics are persisted
in the run state's `resume_context.parallel_integration_metrics` object.
For `copilot-cli`, `subscription_tier` can also be set to `pro+`.

Run logs are written both to stderr and to `.auto-agents/runs/<run_id>/run.log`. CLI command results
remain on stdout for scripts.

## Enterprise WeChat notifications

`run`, `fix`, `collab`, and `provider-resolve` can send an Enterprise WeChat group-robot
notification when the flow completes or fails. The CLI automatically loads `.env` from the current
working directory, without overriding environment variables that are already set. It does not load
the target project's `--project/.env`.

Create `.env` from `.env.example` and fill in the webhook:

```bash
WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
```

You can also set the variable in the shell:

```bash
export WECHAT_WEBHOOK_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'
```

If `WECHAT_WEBHOOK_URL` is unset, no notification is sent. Notification delivery failures are
ignored and do not change the original command output or exit code.

Keep the webhook out of git and out of `.auto-agents/config.json`; it is a secret.

### Provider auto-failover

When multiple providers are configured, the orchestrator automatically switches to the
next available provider if the current one returns a **qualifying error** — rate-limit
(429), quota exhaustion, timeout/stall, service unavailable, or binary not found.

Smart timeout is enabled by default. It replaces a single hard provider deadline with independent
progress leases:

- `provider_idle_seconds`: no protocol, output, child-process CPU/I/O, or workspace activity
- `tool_idle_seconds`: a declared tool remains active without tool/process progress
- `semantic_stall_seconds`: no new tool result, milestone, output artifact, or workspace fingerprint
- `loop_repeat_limit`: the same completed-tool fingerprint repeats without a workspace change
- `safety_ceiling_seconds`: final emergency ceiling even when lower-level activity continues

Provider output heartbeats refresh only the provider lease; they do not count as semantic progress.
Codex and Copilot use native JSONL events, while Antigravity combines its native log with its local
conversation SQLite state. Checkpoints are written every 30 seconds under the run's
`provider-attempts/` directory and include the provider session ID and bounded diagnostics.

`provider_idle`, explicit provider errors, and protocol errors switch provider immediately. Tool
stalls, semantic stalls, loops, and the safety ceiling first resume the same provider once, using
its exact session when one was captured; a second failure switches provider. Set
`execution.smart_timeout.enabled` to `false` to restore the legacy `timeout_seconds` and
`idle_timeout_seconds` hard-deadline behavior.

**How it works**

1. Each agent call tries providers in a prioritized order.
2. On the first call, `active_provider` goes first, followed by the others.
3. When a provider succeeds via failover its identity is remembered **for the
   duration of the run** (in-memory, never persisted).  Subsequent calls start
   with **the last successful provider**, then untried providers, then
   previously-failed providers (lowest priority, but still attempted in case
   the limit resets).
4. `active_provider` in `config.json` is **never modified** by failover. A restart normally begins
   with the user's preference, except when a matching `running` checkpoint identifies an interrupted
   fallback-provider session; that exact provider is resumed first.
5. Only qualifying infrastructure errors trigger a switch; ordinary failures
   (bad code, validation issues) are handled by the normal retry logic.
6. If **all** providers return qualifying errors for a single agent call, the
   stage fails immediately and the run aborts.

**Qualifying error patterns** (matched case-insensitively against stderr):

`rate limit`, `429`, `quota`, `too many requests`, `capacity`, `timed out`,
`stalled`, `unavailable`, `service unavailable`, `not found`, `No such file`, `ENOENT`

**Log output**

```
[failover] provider=codex quota/rate error (429 Too Many Requests), trying next...
[failover] using provider=copilot-cli
[failover] provider=copilot-cli timeout/stall (timed out after 3600s), trying next...
```

## Task plan contract

`state/task_plan.json` is treated as an execution contract, not a loose note. Each task must contain:

- `task_id`
- `title`
- `description`
- `acceptance`
- `status`
- `commit_message`

The orchestrator validates task IDs, duplicate entries, acceptance lists, and allowed statuses before
the implementation loop starts.

The plan root can also define:

- `test_strategy`
- `verification_steps`
- `oracle_proof_schema_version`

Those fields are required for completed plan output and are preserved when task status is updated
during implementation.

Across iterations, `state/task_plan.json` is the active plan for the current run, not a permanent
history table. When a completed project starts a new iteration, auto_agents archives the previous
plan to `.auto-agents/history/task_plans/<run_id>.json`, archives the final run state beside it,
and resets the active plan to an empty `{ "tasks": [] }` placeholder until the new plan stage
generates current-iteration tasks.

Archived done tasks from `.auto-agents/history/task_plans/*.json` are still used as historical
requirement coverage. If an archived task has verified requirement proofs that still satisfy the
current active requirement oracles, the next iteration does not need to create a regression-lock task
solely to re-prove that same requirement. The current `task_plan.json` should contain only new or
changed scope and any requirements whose historical proof no longer satisfies the current trace.
Run artifacts under `.auto-agents/runs/` remain local-only and can stay ignored; the durable proof
ledger now lives under `.auto-agents/history/task_plans/`.

`state/requirements_trace.json` is also a contract, not scratch metadata. Each active requirement is
expected to carry:

- `acceptance_oracles`
- `oracle_type`
- `oracle_strength`
- `evidence_boundary`
- `forbidden_proxy_oracles`
- an engine-stamped `contract_sha256`

This lets downstream planning and review distinguish proxy checks from behavioral/semantic oracles
and distinguish internal-state evidence from system-boundary or external-side-effect proof.
The hash covers the normative requirement contract, but not lifecycle status or free-form notes.
Once a done task has claimed or proved a requirement ID, clarify cannot rewrite that contract in
place. A changed contract must use a new requirement ID and reciprocal `supersedes` /
`superseded_by` links; the old record remains in the append-only trace with status `superseded`.

When the input spec references frontend pages together with prototype artifacts such as HTML
mockups, screenshots, Figma files, or design images, the clarify stage records an optional
`frontend_surfaces` array in `state/requirements_trace.json`. Each surface names the page/screen,
the source prototype refs, known viewports, and the expected visual fidelity level. Projects without
frontend surfaces or without prototype artifacts can omit the field or leave it empty.

For those frontend surface requirements, task planning must create page-level work and proof entries
that use rendered-surface evidence. Deterministic DOM/CSS checks and browser screenshot evidence
such as Playwright visual tests are the baseline. A vision judge can be added when available, but it
is supplemental; route-existence checks, payload-only tests, or internal-state assertions are not
sufficient proof that a generated frontend matches a prototype.

`visual_judge` is an optional completion gate for those frontend surface proofs. In the default
`auto` mode, auto_agents runs it only when a task has frontend prototype proof evidence, screenshot
pairs are available through `visual_evidence`, and at least one configured provider is available,
not marked `vision: "disabled"`, and able to pass native image attachments to its CLI. The
`vision` setting is a policy switch, not an assertion that the adapter can transport screenshots;
setting it to `enabled` cannot force an attachment-incapable provider into the judge. The judge
compares prototype screenshots with actual browser-rendered
screenshots using a fixed visual fidelity rubric and writes a JSON report under
`.auto-agents/runs/<run_id>/visual_judge/<task_id>/`. If the judge runs and returns a low score or a
blocker finding, the task is retried before it can be marked done. If the provider or screenshot
artifacts are unavailable in `auto` mode, auto_agents records a skipped report and still relies on
the deterministic screenshot/DOM/CSS proof requirements.

| Provider adapter | Native visual-judge attachments | Default `vision` |
| --- | --- | --- |
| Codex | Yes, repeated `--image` arguments | `auto` |
| GitHub Copilot CLI | Yes when the installed CLI exposes `--attachment` | `auto` |
| Antigravity | No native attachment transport | `disabled` |
| Generic shell / mock | No unless a future adapter implements the capability | provider-specific |

Copilot attachment requests always use its required non-interactive `-p/--prompt` mode and repeat
`--attachment <path>` in screenshot order. Text-only Copilot requests continue to honor
`prompt_via_stdin`. Existing project configs retain their explicit `vision` values; the defaults
above apply to newly initialized projects.

Visual pairs are explicit-only: auto_agents never guesses a prototype/actual pair from ordinary
`evidence_refs`. Each comparable `visual_evidence` entry must point to two distinct PNG, JPEG, or
WebP screenshots for the same surface, viewport, and UI state; HTML belongs in
`prototype_source_ref`. Duplicate declarations are judged once and retain all owning proof records.
Codex and Copilot receive the ordered screenshots through their native attachment arguments. A
failed batch judgment is rechecked one pair at a time, and only an isolated below-threshold score or
blocker finding can block the task. Business-flow success/failure screenshots remain deterministic
interaction evidence unless they are explicitly paired with a same-state prototype screenshot.

New task plans set `oracle_proof_schema_version: 2` and use `requirement_proofs` on every task
that declares `requirement_ids`. A proof entry maps one requirement oracle to concrete evidence:

```json
{
  "requirement_id": "REQ-057",
  "requirement_contract_sha256": "sha256:<engine-stamped requirement contract hash>",
  "oracle_index": 1,
  "proof_type": "integration_test",
  "oracle_strength": "behavioral",
  "evidence_boundary": "system_boundary",
  "evidence_refs": ["tests/test_retry_budget.py::test_retry_budget_is_per_asset"],
  "forbidden_proxy_oracles": ["ledger output without retry decision coverage"],
  "proxy_oracles": [],
  "status": "verified"
}
```

In strict oracle-proof mode, the final requirements audit does not treat `requirement_ids` alone as
coverage. Every active mandatory acceptance oracle needs a verified done-task proof with concrete
`evidence_refs`, sufficient oracle strength, and the required evidence boundary. For example, a
per-asset retry-budget requirement is not proven merely by outputting a retry ledger; the cited proof
must exercise the retry decision path at the required boundary and exclude ledger-only proxy evidence
when the requirement forbids that proxy.
Legacy plans without proof records remain readable, but v2 proofs are accepted only for the exact
requirement contract hash they were created against. Provider-reference lock entries are bound to
the aggregate hash of their active consumer requirements, so a contract change automatically makes
an otherwise verified reference `needs_refresh` until provider research verifies it again.
On the first contract-identity upgrade, resolved legacy lock entries are backfilled without network
research only when their pre- and post-clarify consumer contracts are identical. Provider research
then scopes its checks to requirement IDs in the current task plan (plus explicit review-forced
references), so unrelated historical provider debt cannot block an iteration that did not change it.

Interrupted implementation work is resumable:

- `in_progress` tasks can continue from review and verification
- `blocked` tasks can be retried without forcing a clean tree first
- new projects start with a minimal `.gitignore` to avoid common Python artifact noise

## Failure and resume behavior

Progress is persisted in two files:

- `.auto-agents/state/run_state.json`
- `.auto-agents/state/task_plan.json`

If a run fails because of a bug, provider error, network issue, or token exhaustion, the usual
recovery path is simply to fix the underlying issue and rerun the same command:

```bash
python3 -m auto_agents run --project /tmp/demo --spec-file /tmp/demo/spec.md
```

What resumes depends on the stage:

- `clarify`, `design`, `plan`, `verify`: completed stages are tracked in `run_state.json`, so a new
  `run` continues from the first unfinished stage
- `implement`: task status is read from `task_plan.json`, so finished tasks are skipped and the next
  unfinished task is resumed
- `implement` with a blocked proof failure: the next run can insert focused repair tasks and retry
  the parent task after those repairs pass
- approval pauses: `approve` clears the pending gate, and the next `run` continues from there

When `provider_research` fails because the remaining provider references still require a user decision
(`ambiguous`, `blocked`, `needs_user_input`, or similar), you can enter a dedicated recovery dialog:

```bash
python3 -m auto_agents provider-resolve --project /tmp/demo
```

That command starts a resumable conversation focused on the provider reference files, applies the
agreed edits, validates the updated reference state locally, and then reruns the original pipeline
from the failed `provider_research` point using the stored run context.

Requirements audit failures now participate in the retry pipeline instead of always terminating the
run immediately:

- implementation-actionable audit failures (for example forbidden-pattern hits in runtime files)
  automatically rewind to `implement`, attach feedback that points at
  `.auto-agents/docs/requirements_audit.md`, and retry within the same run
- missing mandatory requirement coverage rewinds to `plan`, so the planner can append or correct
  tasks before implementation resumes
- missing provider references rewind to `provider_research`
- audit blockers that already require external resolution (for example provider references marked
  `blocked` or `needs_user_input`) still fail clearly instead of looping unsafely

When an audit failure rewinds the pipeline, the next `run` also resumes from that rewound stage
rather than pretending `verify` already completed.
Each generated audit records an `Input context` hash over the trace, task ledger, assumed task
status, current spec, provider lock, architecture/brief, and referenced provider documents. Audit-
dependent verification materializes that exact context before taking its baseline, preventing a
fresh audit failure from being hidden as a pre-existing baseline failure.

Plain full-suite verification failures use a similar recovery path: the first failures rewind to
`implement` with structured triage, implicated paths, and evidence excerpts. The recovery task is
allowed to fix product code, migrate stale tests, or do both, but must stop and surface a
clarification blocker if active requirements and repository tests disagree in a way the existing
oracles cannot resolve. After the configured recovery budget is exhausted, auto_agents rewinds to
`clarify` instead of looping indefinitely.

When a `run` fails with a conservative auto_agents-owned signal, such as a gate-scope
classification error produced by the orchestrator itself, the CLI can start an automatic
auto_agents self-repair pass. That pass edits and verifies the auto_agents repository only, commits
the generic repair, sends an Enterprise WeChat summary when notifications are configured, and then
restarts the original `run` command in a fresh process. It does not edit the target project and is
allowed to continue across different auto_agents-owned failures. Recovery-loop identity is based on
the owner stage, requirement IDs, stable failure IDs, and owner-artifact fingerprints rather than
task IDs or changing review prose. A repeated failure with unchanged owner artifacts stops as a
target-project no-progress condition. Automatic self-repair is attempted only when diagnostics also
show an auto_agents routing invariant mismatch, and that invariant repair is capped at one attempt.
Before a destructive review/scope rewind, the engine preserves the task, failure IDs, changed paths,
owner route, and worktree fingerprint under the run's `recovery_incidents/` directory.

Implementation resume is task-aware rather than fully transactional:

- if a task is already marked `in_progress`, the next run first tries to continue from
  verification/review on the existing workspace state
- if that partial work is not good enough, later retry attempts re-run implementation for the same
  task
- if verification passes and the workspace diff is unchanged, a previously passing review result can
  be reused without spending another review call
- if a task is marked `blocked`, it can be retried even when the git tree is still dirty

Single agent calls now write provider-attempt checkpoints. If the host is interrupted while a
checkpoint is still marked `running`, rerunning the same command resumes the newest matching native
provider session when the provider exposed a session ID and the workspace fingerprint still matches.
If the old process is still alive, auto_agents refuses to start a duplicate attempt.

In practice, a forced interruption can leave partial files in the workspace:

- if the interruption happened during `clarify`, `design`, or `plan`, the next `run` re-executes the
  same unfinished stage, using whatever files were already left on disk
- if it happened during `implement`, the current task may still be `in_progress`; the next `run`
  resumes the exact provider conversation when possible; otherwise it first tries
  review/verification against the existing workspace, then falls back to re-running implementation
- an in-flight local tool process itself is not resurrected; its completed file changes remain in the
  workspace, and the resumed provider is instructed to inspect them before choosing a bounded next step
- if no session ID was captured, or the workspace changed after the checkpoint, recovery falls back to
  the persisted stage/task boundary rather than risking an incorrect conversation resume

When a forced interruption leaves suspicious partial edits behind, inspect `git status` and the
persisted state before rerunning:

```bash
python3 -m auto_agents status --project /tmp/demo
git -C /tmp/demo status --short
```

## Schemas

Explicit schema files live in:

- `schemas/project_config.schema.json`
- `schemas/task_plan.schema.json`

The built-in `validate` command checks project files against these contracts plus required document
headings.

## Lightweight session workflows

For completed projects, `auto-agents` provides two conversational session modes that bypass the full
seven-stage pipeline. These are designed for quick, iterative work where the full orchestration flow
would be too heavyweight.

### Bug fix (`fix`)

Interactive bug-fix loop:

1. **Converse** — describe the bug; the agent analyzes the codebase and asks clarifying
   questions until the problem is clear. If the agent determines the reported issue is
   **not actually a bug** (e.g., expected behavior, configuration issue), it will explain
   its reasoning and ask for your confirmation before closing the session
2. **Execute** — the agent applies a targeted fix with convergence-based retry (see below)
3. **Verify** — configured gate commands are run to confirm the fix
4. **Commit** — changes are committed on success

```bash
python3 -m auto_agents fix --project /tmp/demo
```

### Collaborative debugging (`collab`)

Interactive debug loop for goals that need user–agent collaboration (e.g. "test the video player in
the browser"):

1. **Converse** — describe the goal; the agent clarifies
2. **Iterate** — the agent works toward the goal autonomously; when it needs user action (e.g. "open
   the browser and check the result"), it pauses with `NEED_USER_ASSIST`; verified bug fixes are
   committed as they happen; when it believes the goal is achieved it asks for your confirmation
3. **Complete** — you confirm success and any remaining changes are committed

```bash
python3 -m auto_agents collab --project /tmp/demo
```

### Provider research recovery (`provider-resolve`)

Interactive recovery loop for runs blocked in `provider_research` because provider references still
need explicit user decisions:

1. **Converse** — the agent summarizes the unresolved provider references and asks only the questions
   needed to decide whether to verify, defer, or assumption-approve them
2. **Iterate** — the agent edits provider references and their lock; requirements trace edits are
   limited to non-normative `notes` and an explicitly user-approved `active` → `deferred` status
   change, then the tool validates both the trace contract and reference state locally
3. **Resume** — once the provider references are locally valid, the command reruns the original
   `run` flow from the failed `provider_research` point

```bash
python3 -m auto_agents provider-resolve --project /tmp/demo
```

If `python3 -m auto_agents run ...` encounters this blocker, it now starts a **fresh**
provider-recovery session for the current blocked run automatically instead of asking whether to
resume unrelated historical provider-recovery sessions first. Manual `provider-resolve` invocations
keep the existing resumable-session chooser behavior.

Provider recovery never rewrites proof-bearing requirement fields such as `source`, requirement
text, acceptance oracles, forbidden patterns, or provider-reference bindings, and it never stamps
`contract_sha256` itself. Approval context belongs in provider references, the lock, and `notes`.
When research reveals that the requirement contract itself must change, provider recovery restores
the attempt and rewinds the saved run to `clarify`, where normal supersession and proof-preservation
rules apply. Lock consumer hashes are rebound only after the requirements trace and full project
preflight pass.

### Convergence-based stopping

`fix`, `collab`, and `provider-resolve` use **convergence detection** instead of a fixed attempt
limit. The loop
continues as long as the agent is making progress, and stops automatically when it stalls:

- **Progress signal** — after each attempt, the tool computes a hash of `git diff` and of the
  normalized verification error output (zero token cost, purely local). If either hash changed
  compared to the previous attempt, the agent is making progress and the stall counter resets.
- **Stall threshold** — if neither hash changes for **3 consecutive attempts**, the session stops
  with a "no progress" message (`SESSION_STALL_THRESHOLD`).
- **Agent error threshold** — transient agent errors (network timeouts, API failures) are tracked
  independently. **5 consecutive agent errors** trigger a stop (`SESSION_AGENT_ERROR_THRESHOLD`).
- **Hard ceiling** — a safety net prevents runaway loops: **15 attempts** for fix, **25** for collab,
  and **15** for `provider-resolve` (`SESSION_HARD_CEILING`). These are deliberately generous;
  convergence detection is the primary stop mechanism.

### Resuming sessions

Resume an interrupted **or failed** session (works for `fix`, `collab`, and `provider-resolve`):

```bash
python3 -m auto_agents collab --project /tmp/demo --session <session_id>
python3 -m auto_agents provider-resolve --project /tmp/demo --session <session_id>
```

If `--session` is omitted, the CLI automatically detects resumable sessions (including failed ones)
of the same mode, shows all unfinished sessions in newest-first order, and lets you choose a session
number or ID to resume. Press Enter to accept the newest recommended session, or enter `n` to start
a new one.

When a **failed** session is resumed, the stall counter and agent-error counter are reset to zero,
but the conversation history and execution log are preserved so the agent retains full context.

`fix` and `collab` call the agent with the `implement`-stage effort from
`config.efforts["implement"]` (default: `deep`). `provider-resolve` uses
`config.efforts["provider_research"]`.

### Listing sessions

```bash
# Active sessions only (default)
python3 -m auto_agents sessions --project /tmp/demo

# Filter by mode
python3 -m auto_agents sessions --project /tmp/demo --mode fix

# Include completed and failed sessions
python3 -m auto_agents sessions --project /tmp/demo --all

# Delete one saved session record (state only; does not revert code changes)
python3 -m auto_agents sessions-delete --project /tmp/demo --session <session_id>

# Delete all saved session records (state only; does not revert code changes)
python3 -m auto_agents sessions-clear --project /tmp/demo
```

The sessions list shows a compact summary per session (`session_id`, `mode`, `status`, `goal`,
`resolution`, `created_at`). Verbose fields like conversation history and execution logs are omitted
for readability. Results are sorted newest first. `sessions-delete` and `sessions-clear` remove only
persisted session state under `.auto-agents/state/sessions/` and never roll back worktree changes.

Both `fix` and `collab` accept `--provider` and `--print-agent-output`. Session state is persisted
independently at `.auto-agents/state/sessions/<session_id>/` and does not interfere with the main
`run_state.json`.
