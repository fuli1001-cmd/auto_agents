# V1 Design

## Scope

V1 supports only greenfield projects. Legacy repository takeover is intentionally out of scope.

## Principles

- Quality is enforced by explicit gates and small feature slices.
- Token usage is controlled by file-driven context, not by long conversational memory.
- The orchestrator owns state. Providers are stateless workers.
- The same model can be reused across stages while effort varies by stage.

## Internal layout

- `.auto-agents/config.json`: project-local orchestration config
- `.auto-agents/docs/*.md`: compact human-readable documents
- `.auto-agents/state/*.json`: machine-readable state
- `.auto-agents/runs/<run_id>/`: prompts, responses, logs, summaries

## Gate strategy

- Pause after `clarify` for `requirements` approval
- Pause after `design` for `architecture` approval
- Pause after `verify` for `release` approval

## Git strategy

Each task in `task_plan.json` is a minimal verifiable feature slice. A commit happens only after:

1. implementation completed
2. independent review passed
3. verification commands passed

