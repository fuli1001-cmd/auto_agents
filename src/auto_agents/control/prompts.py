"""Prompts are derived from the current contract, never from a repair transcript."""

import json

WRITING = {
    "clarify",
    "prototype",
    "design",
    "plan",
    "provider_research",
    "implement",
    "readme",
    "research",
    "acceptance",
    "finalize",
}


def build(context, phase, *, inputs=None, feedback=None):
    c = context.contract.to_dict()
    inputs = inputs or {}
    lines = [
        f"Workspace: {context.workspace_root}",
        f'Business mode: {c["mode"]}; current phase: {phase}',
        "The controller owns the goal, source, scope, authorization, verification and budget.",
        "Never edit .auto-agents/state or operator configuration. Never commit, reset or push Git.",
        "Use only this workspace. Do not modify the original project, accounts or production data.",
        "Application runtime configuration is inherited through the process environment and operator bindings. "
        "Private workspaces intentionally exclude credential files and original runtime databases. "
        "An absent workspace .env or .data directory alone does not prove configuration is missing. "
        "Check required variable presence without printing values, and use the inherited configuration. "
        "Do not request credentials that are already available, copy secrets into artifacts, or switch real backends to fake/mock.",
        "Required goal: " + c["goal"],
        "Authorization and human decisions: "
        + json.dumps(c["authorization"], ensure_ascii=False),
        "Owned inputs:\n"
        + json.dumps({**c["inputs"], **inputs}, ensure_ascii=False, indent=2),
        "Allowed source paths: " + json.dumps(c["scope"]),
        "Required checks:\n" + json.dumps(c["checks"], ensure_ascii=False, indent=2),
    ]
    if c["checks"]:
        lines.append(
            "Verification is already fixed. Omit checks/verification_command from your result or copy the exact contract. Do not wrap interpreters, replace arguments, add targets or adopt historical tasks."
        )
    if phase in WRITING:
        lines.append(
            "Make the bounded workspace changes required for this phase. Finish with one JSON object only."
        )
    else:
        lines.append(
            "Read-only phase: inspect and report, without changing any workspace files."
        )
    instructions = {
        "clarify": 'Produce requirements and measurable acceptance from the spec. Write .auto-agents/docs/project_brief.md and .auto-agents/docs/requirements.md. Return {"artifacts":[paths],"frontend":boolean,"questions":[]}. Ask only about missing user decisions.',
        "prototype": 'Produce a static frontend prototype for the approved requirements. Write standalone HTML with viewport meta tags under .auto-agents/docs/frontend_prototype/, including home.html and manifest.json. The manifest declares pages:[{id,title,route,requirement_ids,html_ref}], viewports:[WIDTHxHEIGHT] and index_ref. All refs are workspace-relative; embed assets rather than loading remote/script dependencies. Declare every package artifact. If a design catalog is supplied, return selection:{candidates:[{slug,score,rationale,risks}],selected_slug} with three candidates and one highest-scoring choice; preserve exact upstream DESIGN.md bytes. Return {"artifacts":[paths],"selection":selection}. Do not implement the product yet.',
        "design": 'Write .auto-agents/docs/architecture.md with concrete interfaces and constraints. Preserve an existing approved or upstream DESIGN.md exactly. If no DESIGN.md exists, you may create it. Return {"artifacts":[paths]}.',
        "plan": 'Write .auto-agents/docs/task_plan.json. Return {"tasks":[{"task_id":"T1","goal":"bounded verifiable slice","paths":[paths],"depends_on":[],"checks":[{"command":"project test command","purpose":"behavior"}]}],"checks":[release checks]}. Include every required behavior. Do not introduce unsupported selectors or modify operator config.',
        "provider_research": 'Review required external provider references using primary sources, preserve account configuration and write .auto-agents/docs/provider_references.md. Return {"artifacts":[paths],"references":[{"url":"primary source URL","claim":"supported fact"}]}. If no external provider is required, return {"not_required":true,"reason":"specific justification"}.',
        "research": 'Repair only the blocked provider reference artifacts. Return {"artifacts":[paths],"references":[{"url":"primary source URL","claim":"supported fact"}]}. No product changes.',
        "classify": 'Classify the owned issue. Return {"decision":"fix|run_iteration|need_user|not_bug","summary":"facts","question":"only if needed","checks":[only when the controller has not fixed checks],"paths":[bounded affected paths]}. No implementation. If persistence needs changes, report the required explicit user decision before editing.',
        "implement": 'Implement only the owned behavior and required tests. Preserve approved artifacts. Return {"summary":"what changed and why"}. Do not run real/paid media providers or modify production databases.',
        "review": 'Independently review the actual diff and controller verification evidence against the goal. Examine test edits for weakening or false positives. Return {"decision":"pass|revise|finalize","reason":"specific findings","test_changes_valid":boolean,"evidence_paths":[only declared output reports requiring post-verification annotations]}. Use finalize only when verified source is correct and human/visual annotations must be recorded after a test regenerates the declared evidence. Do not request code edits or a fresh behavior run to record an already observed image. A test exit code alone does not prove user-facing acceptance.',
        "finalize": 'Complete only the declared JSON/Markdown evidence reports listed in Owned inputs, using the current verification and independent review observations. Inspect supplied images where necessary, bind annotations to their exact current hashes and report facts honestly. Do not change source, tests, images, commands, goal, authorization or dependencies, and do not rerun tests that regenerate evidence. Return {"summary":"recorded observations","evidence_refs":[updated reports]}. The controller will check source immutability and request independent review again.',
        "verify": "Verification is performed by the controller; report no invented outcomes.",
        "visual_judge": 'Inspect the declared prototype and actual runtime evidence, including supplied images. Return {"decision":"pass|revise","reason":"specific visual comparisons","evidence_refs":[actual evidence paths]}. Mere file existence or layout stability is not prototype fidelity.',
        "readme": 'Write README.md covering actual verified behavior, setup, interfaces and limitations. Return {"artifacts":["README.md"]}.',
        "diagnose": 'Diagnose the current goal using owned evidence. Return {"action":"fix|run|resume|acceptance|need_user","reason":"specific evidence","issue":{"summary":"bounded issue"},"spec":"only for run","child_id":"only owned child for resume","question":"only if needed"}. Never route to another repository or claim permission to repair the engine.',
        "acceptance": 'Execute the original user acceptance in this workspace. Preserve all product and test source. Record new evidence only under .auto-agents/docs/acceptance/. Use browser frontend interaction where required, capture concrete evidence and provider provenance. Never fake results or repeat unknown paid requests. Return {"accepted":boolean,"evidence_refs":[paths],"checks":[behavioral acceptance commands],"reason":"observed results","external_ids":{}}. If an operation may exceed authorized costs, ask instead of running it.',
        "acceptance_review": 'Independently inspect the recorded user-facing acceptance evidence and provenance against each original criterion. Return {"decision":"pass|revise","reason":"semantic findings","evidence_refs":[verified evidence paths]}. Reject mock/placeholder output for a real goal.',
    }
    lines.append(instructions[phase])
    if c["mode"] == "collab" and phase in {"diagnose", "acceptance"}:
        from ..collab_goal_scope import acceptance_only, INSTRUCTION

        if acceptance_only(c["goal"]):
            lines.append(INSTRUCTION)
    if phase == "acceptance":
        lines.append(
            "Acceptance may create untracked runtime data and temporary browser/test scripts only "
            "under this workspace's .data/, .tmp/ or .tmp-tests/ real directories. "
            "Temporary aliases may point only inside private runtime; Chromium's inert "
            "SingletonLock/SingletonCookie metadata is allowed. Ignored, untracked Next.js "
            "next-env.d.ts declarations generated by its existing package are runtime output. "
            "These files are private runtime, not product deliverables. Do not edit tracked files, "
            "credentials, operator configuration or original project data. Use existing initialization "
            "commands only for a missing, private acceptance database; never reset or migrate an "
            "existing database without its required decision. Record durable evidence under "
            ".auto-agents/docs/acceptance/. If retained acceptance work is supplied, inspect its "
            "runtime and operation receipts before any new external request. Preserve and reuse "
            "known results; never repeat a paid request whose result is unknown. Retained workspace "
            "paths outside this workspace are read-only; use a supported backup into private runtime "
            "when reuse is necessary. The controller runs behavioral checks in this same owned "
            "workspace. Checks must verify the retained browser/runtime observations for the same "
            "test project, without creating another project or resubmitting generation requests."
        )
        if inputs.get("previous_acceptance"):
            lines.append(
                "Continue the exact project and external request identities from previous_acceptance. "
                "Do not rerun project-creation checks. If its blocker persists, report the existing "
                "observations and evidence instead of creating a replacement project or repeating paid work."
            )
    if phase == "diagnose" and inputs.get("acceptance_blocker"):
        lines.append(
            "The preceding acceptance already executed against an existing test project. "
            "Inspect acceptance_blocker and its evidence, preserve all project/request identities, "
            "and diagnose only the observed blocker. A bounded fix must use its reproduction "
            "and targeted regression checks, then return to acceptance of the same project. "
            "Do not repeat project creation or request results already recorded."
        )
    if phase == "diagnose" and inputs.get("workflow_policy", {}).get("kind") == "existing_acceptance":
        lines.append(
            'For action=fix, issue must include summary, reproduction:[observed failures], paths:[bounded editable paths], '
            'checks:[{command:"pytest/unittest/vitest with exact regression file targets",purpose:"behavior"}], '
            'and evidence_refs:[existing .auto-agents/docs/acceptance/ files]. Historical requirements or prototypes '
            'alone are not reproduction evidence. Never include task_id/task_ids or adopt saved development ownership. '
            'workflow_policy is controller-owned; source edits forbidden there cannot be granted by a model reply.'
        )
    if phase == "classify" and c["inputs"].get("acceptance_only"):
        lines.append(
            "This issue is a focused blocker in an existing-behavior acceptance goal. "
            "Use observed reproduction evidence, bounded source paths and targeted regression checks. "
            "Do not choose run_iteration or adopt historical development tasks."
        )
    if phase == "prototype" and context.contract.inputs.get("variant_only"):
        lines.append(
            "This is an additional candidate. Write the entire static package only under .auto-agents/docs/frontend_prototype_variants/"
            + context.contract.inputs["variant_only"]
            + "/ including home.html and manifest.json. Manifest refs must use this package prefix. Preserve every other candidate and the canonical prototype. Return the actual variant artifact paths."
        )
    if phase == "clarify" and context.contract.inputs.get("control_version") == 2:
        lines.append(
            'Also write and declare .auto-agents/docs/requirements_trace.json using the existing requirements schema: {"version":1,"requirements":[{"id":"REQ-001","text":"specific original behavior","source":"spec","status":"active","priority":"mandatory","acceptance_oracles":["actual observable assertion"],"oracle_type":"mixed","oracle_strength":"behavioral","evidence_boundary":"system_boundary","forbidden_proxy_oracles":[],"forbidden_patterns":[],"notes":""}]}. Cover every original behavior and negative constraint. Preserve prior requirement identities and proof contracts; never turn unmet requirements into optional or superseded entries.'
        )
    if phase == "plan":
        lines.append(
            "Every task must declare requirement_ids covering the active mandatory requirements in requirements_trace.json. Verification checks must declare outputs:[exact relative paths] for generated reports/screenshots. Source and test files cannot be outputs; tests may not modify undeclared inputs. Include persistence_change with storage_transition, compatibility_policy, decision_id, target_ids and append-only migration_artifacts when schemas change. Never infer permission to reset a database. A task is pending; you cannot declare tasks already completed."
        )
    if feedback:
        lines.append(
            "Rejected outcome to correct without changing the contract:\n"
            + json.dumps(feedback, ensure_ascii=False)
        )
    lines.append(
        "Verification purpose is an enum: behavior, environment or artifact. Explanations belong in reason fields. Behavior commands must use pytest, unittest, or vitest; wrap custom browser scripts in a unittest/pytest test that asserts their observations. Release checks must include the actual behavior checks, not only artifact integrity. Output one complete JSON object; do not return controller identifiers, status, authority or budget fields."
    )
    return "\n\n".join(lines)
