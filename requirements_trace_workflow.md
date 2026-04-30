# Requirements Trace Workflow

## Problem

`auto_agents` currently relies on the clarified brief, architecture document, and generated task
acceptance criteria as the execution contract. If a user's original requirement is narrowed during
planning, later implement and review stages can pass while the original intent remains only partially
implemented.

The workflow must support both detailed requirement documents and requirements that are expressed
only through clarify conversation. In both cases, clarify must produce a durable requirements trace
that downstream stages can validate against.

## Goals

- Generate and maintain `.auto-agents/state/requirements_trace.json` during clarify.
- Give every active requirement a stable ID, source, mandatory/deferred status, acceptance oracles,
  an explicit oracle contract (`oracle_type`, `oracle_strength`, `evidence_boundary`,
  `forbidden_proxy_oracles`), optional forbidden patterns, and optional provider-documentation
  requirements.
- Require task plans to map tasks to requirement IDs.
- Validate that every mandatory active requirement is covered by at least one task, unless it is
  explicitly deferred or superseded.
- Inject the bound requirements and oracles into implement and review prompts.
- Treat requirement oracles as review-failing criteria, not advisory context.
- Run a release-time requirements audit before release approval.
- Support a centralized provider research stage that gathers official protocol references once and
  lets later implementation/review tasks reuse local reference files.

## Non-Goals

- Do not require users to provide formal requirements documents.
- Do not require every project to use external provider research.
- Do not automatically browse for provider docs during every implementation task.
- Do not block release on deferred requirements that the user explicitly marked out of scope.

## Data Files

### `.auto-agents/state/requirements_trace.json`

The trace is the machine-readable source of truth after clarify. Required shape:

```json
{
  "version": 1,
  "requirements": [
    {
      "id": "REQ-001",
      "text": "Implement the external provider backend using the official protocol.",
      "source": "clarify conversation",
      "status": "active",
      "priority": "mandatory",
      "acceptance_oracles": [
        "Outbound requests match the official provider request schema.",
        "Legacy private gateway payload fields are not used."
      ],
      "oracle_type": "integration_test",
      "oracle_strength": "behavioral",
      "evidence_boundary": "system_boundary",
      "forbidden_proxy_oracles": [
        "configuration-only checks",
        "log-only evidence"
      ],
      "forbidden_patterns": [
        "task_type.*tts_synthesize"
      ],
      "external_docs_required": true,
      "provider_reference": ".auto-agents/docs/provider_references/doubao_tts.md",
      "notes": ""
    }
  ]
}
```

Legacy traces that predate the oracle-contract fields are normalized in memory to
`mixed` / `behavioral` / `system_boundary` / `[]` so existing projects still validate, but new
clarify output should write the full shape explicitly.

Allowed requirement statuses:

- `active`: in scope for the current run.
- `deferred`: explicitly postponed.
- `superseded`: replaced by another requirement.

Allowed priorities:

- `mandatory`: must be satisfied or explicitly deferred before release.
- `optional`: tracked but not release-blocking.

### `.auto-agents/docs/provider_references/*.md`

Provider research writes concise local references. These files summarize only implementation-relevant
protocol facts and cite official source URLs.

### `.auto-agents/state/provider_references.lock.json`

The lock records provider references that were researched, their status, source URLs, retrieval time,
and local file path. Later tasks use this lock instead of searching independently.

## Workflow

1. Clarify updates `project_brief.md` and `requirements_trace.json`.
2. Plan reads the trace and creates tasks with `requirement_ids`.
3. Plan validation fails if mandatory active requirements are not covered.
4. Provider research runs before implementation when active requirements require external docs.
5. Implement prompts include each task's bound requirement text and oracles.
6. Review prompts include the same requirement bundle and can fail on unsatisfied oracles.
7. Verify still runs local commands.
8. Requirements audit runs before release approval and blocks unresolved mandatory gaps.

## Provider Research Rules

- Research is centralized and provider/protocol based.
- The provider research agent should use available browsing or network tools to find official
  documentation when local user-provided notes are insufficient.
- Only official provider documentation or user-provided protocol notes may be used.
- If docs are missing or ambiguous, mark the provider reference as `blocked` or `needs_user_input`.
- Implementation tasks must not invent protocol fields from memory when the reference is missing.
- Provider references are reused by later implement/review tasks.

## Audit Rules

The release audit checks:

- Every mandatory active requirement is covered by at least one done task.
- Bound provider references are present and verified when `external_docs_required` is true.
- Forbidden patterns do not appear in source/test/doc files, unless explicitly documented as legacy
  non-runtime material.
- The final audit writes `.auto-agents/docs/requirements_audit.md`.

When the audit result is `fail`, the orchestrator routes recovery by owner:

- implementation-actionable findings rewind to `implement`
- missing requirement coverage rewinds to `plan`
- missing provider references rewind to `provider_research`
- blockers that still require external/user resolution remain hard failures

Release approval is blocked until the rerun audit result is `pass`.
