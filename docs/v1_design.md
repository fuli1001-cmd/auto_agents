# V1 Design

## Scope

V1 supports only greenfield projects. Legacy repository takeover is intentionally out of scope.

## Principles

- Quality is enforced by explicit gates and small feature slices.
- Token usage is controlled by file-driven context, not by long conversational memory.
- The orchestrator owns state. Providers are stateless workers.
- The same model can be reused across stages while effort varies by stage.
- Invalid structured outputs are rejected quickly and retried with terse corrective feedback.
- The plan stage defines how the project will be verified; users should not need to hand-pick test commands for common greenfield cases.

## Internal layout

- `.auto-agents/config.json`: project-local orchestration config
- `.auto-agents/docs/*.md`: compact human-readable documents
- `.auto-agents/state/*.json`: machine-readable state
- `.auto-agents/runs/<run_id>/`: prompts, responses, logs, summaries
- `schemas/*.schema.json`: explicit JSON contracts for config and plan files

## Gate strategy

- Pause after `clarify` for `requirements` approval
- Pause after `design` for `architecture` approval
- Pause after `verify` for `release` approval

## Retry strategy

- `plan` retries if `task_plan.json` fails structural validation or omits `test_strategy` and `verification_commands`.
- `review` retries if the decision header is malformed.
- `implement` retries if verification or review rejects the current task.
- Retries are finite and configurable per stage.

## Review effort strategy

- The configured review effort is the floor, not always the final setting.
- Small test-only resumptions can stay on `balanced`.
- The orchestrator automatically escalates review to `deep` for higher-risk diffs such as non-test code changes, large edits, prior review failures, or dependency/config churn.
- Review prompts should prefer explicit changed-file context, diff stats, and truncated diffs over broad repo rediscovery.

## Verification strategy

- `plan` must output root-level `test_strategy` and `verification_commands` in `task_plan.json`.
- The orchestrator copies generated verification commands into `.auto-agents/config.json` when `gates.allow_agent_updates` is enabled.
- `implement` is responsible for creating or updating the tests needed to satisfy those commands.
- The orchestrator executes the configured commands before task review so obvious local failures do not spend review tokens.
- `review` checks the surviving changes for correctness, regressions, and missing tests after local verification passes.

## Resume strategy

- An interrupted task can persist as `in_progress` and resume from review or verification.
- A previously passing review can be reused when the task worktree fingerprint is unchanged.
- A previously `blocked` task can be retried without first forcing a clean tree.
- Planning baseline commits are limited to planning files so partial feature work is not committed by accident.

## Offline validation

The `validate` command performs a token-free local check of:

- `.auto-agents/config.json`
- `.auto-agents/state/task_plan.json`
- required document presence and headings

The `run` command invokes this preflight check before any agent stage unless explicitly bypassed with
`--skip-validate`.

## Git strategy

Each task in `task_plan.json` is a minimal verifiable feature slice. A commit happens only after:

1. implementation completed
2. independent review passed
3. verification commands passed
