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
- Python verification commands must run through that environment, for example:
  `conda run -p ./.conda python -m unittest discover -s tests`
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

Task execution is sequential, not parallel:

- the orchestrator walks `task_plan.json` in order
- each task moves `pending -> in_progress -> done` or `blocked`
- only one task is implemented at a time
- after one task passes `implement -> verify -> review`, the orchestrator automatically starts the
  next unfinished task in the same `run`
- `--max-tasks N` stops the current invocation after `N` successful tasks, which is useful for demos
  or controlled rollout

For each task, the effective loop is:

1. mark the task `in_progress`
2. run the implementation agent for the current slice
3. run local verification commands
4. run an independent review for the current uncommitted changes
5. if verification and review both pass, mark the task `done` and optionally commit
6. continue to the next unfinished task

If verification or review fails, the orchestrator retries the same task with focused feedback. The
retry prompt includes structured verification triage and allows tightly coupled regression fixes in
explicitly implicated paths, even when those fixes sit slightly outside the original slice. If the
retry budget is exhausted, that task is marked `blocked` and the run exits with failure instead of
silently skipping ahead.

Manual approvals are supported at three high-value gates:

- `requirements`
- `architecture`
- `release`

## Quick start

Create a target project skeleton:

```bash
python3 -m auto_agents init --project /tmp/demo --name demo
```

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

Stream agent stdout and stderr to the terminal while keeping the final JSON response on stdout:

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

During `plan`, the agent must write `test_strategy` and `verification_commands` into
`.auto-agents/state/task_plan.json`. By default the orchestrator copies those verification commands
into `.auto-agents/config.json`, so new projects do not need a hand-written `gates.commands` block.

For Python projects, those generated verification commands must use the project-local conda env at
`./.conda`. Every Python-oriented command in `verification_commands` must itself be prefixed with
`conda run -p ./.conda ...`; bare `python`, `pytest`, or `coverage` commands are rejected.

Inspect persisted progress:

```bash
python3 -m auto_agents status --project /tmp/demo
```

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
python3 -m unittest discover -s tests
```

Validate a target project without spending tokens:

```bash
python3 -m auto_agents validate --project /tmp/demo
```

Recover a run that is blocked in `provider_research` and continue it after the references are fixed:

```bash
python3 -m auto_agents provider-resolve --project /tmp/demo
```

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

For Codex this maps to native config profiles: `balanced` → `m` (medium), `deep` → `h` (high),
`max` → `xh` (extra-high). All model configuration lives in Codex's own config files — the project
config only carries the profile name mapping.

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
        "balanced": "m",
        "deep": "h",
        "max": "xh"
      },
      "extra_args": [],
      "cwd_flag": "-C",
      "prompt_via_stdin": true,
      "output_flag": "-o",
      "timeout_seconds": 1800
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
      "timeout_seconds": 3600
    }
  },
  "active_provider": "codex"
}
```

To use an absolute path instead of the conventional `~/.copilot/profiles/` location, set the
`providers.copilot-cli.profile_map` value to the full path:

```json
{
  "profile_map": {
    "deep": "/home/user/my-copilot-configs/deep"
  }
}
```

### Generic shell adapter

If another provider does not support reasoning strength directly, the adapter can ignore the hint
and still satisfy the interface.

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
| readme | `balanced` | `balanced` | Interactive README generation from finalized repo |

Review auto-escalation triggers (when configured as `balanced`):

- Prior review failure on the same task → `deep`
- Code changes without corresponding test changes → `deep`
- More than 3 non-test files changed → `deep`
- High-risk files changed (pyproject.toml, Dockerfile, CI configs) → `deep`
- Large diffs (>240 lines of non-test code) → `deep`
- Only test files changed in a small diff → stays `balanced`

Setting review to `deep` or `max` overrides auto-escalation and uses that effort for every review.

### Provider auto-failover

When multiple providers are configured, the orchestrator automatically switches to the
next available provider if the current one returns a **qualifying error** — rate-limit
(429), quota exhaustion, timeout/stall, service unavailable, or binary not found.

**How it works**

1. Each agent call tries providers in a prioritized order.
2. On the first call, `active_provider` goes first, followed by the others.
3. When a provider succeeds via failover its identity is remembered **for the
   duration of the run** (in-memory, never persisted).  Subsequent calls start
   with **the last successful provider**, then untried providers, then
   previously-failed providers (lowest priority, but still attempted in case
   the limit resets).
4. `active_provider` in `config.json` is **never modified** by failover — a
   restart always begins with the user's original preference.
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
- `verification_commands`

Those fields are required for completed plan output and are preserved when task status is updated
during implementation.

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

Implementation resume is task-aware rather than fully transactional:

- if a task is already marked `in_progress`, the next run first tries to continue from
  verification/review on the existing workspace state
- if that partial work is not good enough, later retry attempts re-run implementation for the same
  task
- if verification passes and the workspace diff is unchanged, a previously passing review result can
  be reused without spending another review call
- if a task is marked `blocked`, it can be retried even when the git tree is still dirty

Current limitation: there is no fine-grained checkpoint inside a single agent call. So the system
can resume from persisted stage/task boundaries, but it cannot guarantee recovery of edits that were
still in-flight when a process was forcibly interrupted.

In practice, a forced interruption can leave partial files in the workspace:

- if the interruption happened during `clarify`, `design`, or `plan`, the next `run` re-executes the
  same unfinished stage, using whatever files were already left on disk
- if it happened during `implement`, the current task may still be `in_progress`; the next `run`
  first tries review/verification against the existing workspace, then falls back to re-running
  implementation for that same task if needed
- this means rerun is usually recoverable, but the current task may consume one extra retry cycle,
  and partial edits may influence the next attempt until they are overwritten or fixed

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
2. **Iterate** — the agent edits only provider-research artifacts (`provider_references/*.md`,
   `provider_references.lock.json`, and tightly coupled trace metadata when needed), then the tool
   validates the updated reference state locally
3. **Resume** — once the provider references are locally valid, the command reruns the original
   `run` flow from the failed `provider_research` point

```bash
python3 -m auto_agents provider-resolve --project /tmp/demo
```

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
