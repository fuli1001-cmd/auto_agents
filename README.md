# auto-agents

`auto-agents` is a quality-first local orchestrator for AI-assisted new project delivery.

V1 scope:

- New projects only
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
- Retry prompts carry structured failure summaries, and cheap local pre-checks can stop obviously invalid verification paths before another review call
- Invalid plans and malformed reviews are rejected and retried with focused feedback
- Local project isolation is preferred over changing shared system environments

## Core workflow

1. `clarify`: turn an idea into a compact project brief
2. `design`: create a top-level architecture document
3. `plan`: generate a JSON task plan with small verifiable feature slices plus a verification strategy
4. `implement`: execute one feature slice at a time
5. `review`: run an independent agent review for the current task
6. `verify`: run local gates
7. `readme`: generate a project README from the finalized repository state
8. `commit`: auto-commit only when the task passes gates

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
- after one task passes `implement -> review -> verify`, the orchestrator automatically starts the
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

If verification or review fails, the orchestrator retries the same task with focused feedback. If
the retry budget is exhausted, that task is marked `blocked` and the run exits with failure instead
of silently skipping ahead.

Manual approvals are supported at three high-value gates:

- `requirements`
- `architecture`
- `release`

## Quick start

Create a target project skeleton:

```bash
python3 -m auto_agents init --project /tmp/demo --name demo --provider codex
```

Convenience defaults:

- `init --name` defaults to the final directory name from `--project`
- `init --provider` defaults to `codex`
- `init --doc-language` defaults to `en`

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

If the run is currently paused on a manual gate, `approve` can infer the gate from the persisted run
state, so this is usually enough:

```bash
python3 -m auto_agents approve --project /tmp/demo
```

Run tests for this repository:

```bash
python3 -m unittest discover -s tests
```

Validate a target project without spending tokens:

```bash
python3 -m auto_agents validate --project /tmp/demo
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

Adapters map those labels to provider-specific controls. For Codex this maps to local config
profiles: `balanced` → `m` (medium), `deep` → `h` (high), `max` → `xh` (extra-high). If another
provider does not support reasoning strength directly, the adapter can ignore the hint and still
satisfy the interface.

Each stage in the `efforts` config block can be set to any of these labels. The default
configuration balances quality and token usage:

| Stage | Default | Effective | Rationale |
|-------|---------|-----------|-----------|
| clarify | `deep` | dynamic | Downgraded to `balanced` when spec is already a design doc |
| design | `deep` | dynamic | Downgraded to `balanced` when spec is already a design doc |
| plan | `deep` | `deep` | Task decomposition affects the whole run |
| implement | `deep` | `deep` | Stronger reasoning reduces review rejections |
| review | `balanced` | auto-escalated | Automatically escalated to `deep` for risky diffs |
| verify | `balanced` | `balanced` | Runs local commands, no LLM reasoning needed |

Review auto-escalation triggers (when configured as `balanced`):

- Prior review failure on the same task → `deep`
- Code changes without corresponding test changes → `deep`
- More than 3 non-test files changed → `deep`
- High-risk files changed (pyproject.toml, Dockerfile, CI configs) → `deep`
- Large diffs (>240 lines of non-test code) → `deep`
- Only test files changed in a small diff → stays `balanced`

Setting review to `deep` or `max` overrides auto-escalation and uses that effort for every review.

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
