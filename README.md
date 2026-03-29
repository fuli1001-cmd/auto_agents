# auto-agents

`auto-agents` is a quality-first local orchestrator for AI-assisted new project delivery.

V1 scope:

- New projects only
- Provider-agnostic orchestration
- Stage-specific effort policy
- File-driven context to reduce token usage
- Strict task plan validation before execution
- Limited retries for planning, review formatting, and task rework
- Automatic git commit after each verified feature slice

## Why this shape

The system optimizes for quality over throughput:

- One orchestrator owns state and gates
- Providers are replaceable adapters
- LLM calls stay short and stage-specific
- Scripts, not the model, enforce quality gates
- Invalid plans and malformed reviews are rejected and retried with focused feedback

## Core workflow

1. `clarify`: turn an idea into a compact project brief
2. `design`: create a top-level architecture document
3. `plan`: generate a JSON task plan with small verifiable feature slices
4. `implement`: execute one feature slice at a time
5. `review`: run an independent agent review for the current task
6. `verify`: run local gates
7. `commit`: auto-commit only when the task passes gates

Manual approvals are supported at three high-value gates:

- `requirements`
- `architecture`
- `release`

## Quick start

Create a target project skeleton:

```bash
python3 -m auto_agents init --project /tmp/demo --name demo --provider codex
```

Run the orchestrator:

```bash
python3 -m auto_agents run --project /tmp/demo --idea-file /tmp/demo/idea.md
```

Approve a paused gate:

```bash
python3 -m auto_agents approve --project /tmp/demo --gate requirements
```

Run tests for this repository:

```bash
python3 -m unittest discover -s tests
```

## Provider model

The orchestrator uses its own effort labels:

- `balanced`
- `deep`

Adapters map those labels to provider-specific controls. For Codex this can map to local config
profiles such as `m` and `h`. If another provider does not support reasoning strength directly, the
adapter can ignore the hint and still satisfy the interface.

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
