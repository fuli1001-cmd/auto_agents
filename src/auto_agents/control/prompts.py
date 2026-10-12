"""Prompts are derived from the current contract, never from a repair transcript."""

import json

WRITING = {
    "clarify",
    "prototype",
    "prototype_prepare",
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
        "prototype_prepare": 'Inspect the admitted baseline, verified approvals, active requirements, implementation, tests and historical designs. Do not regenerate the candidate. Only write an evidenced repaired baseline under repair_path. Preserve user edits and intentionally retired features; ask questions on conflicts, ambiguity, or requested scope expansion. Return {"scope":{version:1,mode:"reuse|incremental|redesign|create",reason,repairs:[{id,flow_ids,evidence,reason}],changes:[{id,kind:"add|change|remove|linkage",page:"home.html",selector:"#stable-region",files:["home.html"],requirement_ids:["REQ-001"],evidence:[source refs or "goal"],because:[change ids],reason}],flows:[{id,page,requirement_ids,evidence,checks:[check ids]}],checks:[{id,flow,page,kind:"preservation|new",steps:[{action:"click|fill|select|check|seek|press",selector,value}],asserts:[{selector,property:"text|value|checked|enabled|visible|count|attribute|currentTime",equals,name:only for attribute}]}],geometry:[{page,selector,properties:["x|y|width|height"],change_ids}],viewports:["1440x900"]}}. Selectors for changed regions must be a single #id or [data-prototype-region="id"], never root or whole-page containers for a local task. Use exact file paths. Every linkage needs a direct causal dependency and the smallest necessary edit. Default: preserve all unrelated controls, options, defaults, flows, fonts, colors and layout. Only declare geometry shifts causally required by a requested region. Removal/redesign needs explicit user goal evidence, not model preference. Existing flows need preservation checks; new functionality needs new checks. Check all preserved behavior, not only visible screenshots or success toasts. Inventory flows with implementation/history evidence even if the current prototype already omitted them. Baseline repairs must fail their frozen preservation checks on the original and pass on the repaired copy. In create mode leave repair_path empty. In reuse mode declare no changes and do not regenerate. All fields are required; empty lists allowed only when inapplicable. Use source refs from admitted_baseline.inputs/history or canonical original files as evidence. No arbitrary JS, commands or executable tests. Finish with JSON and optional questions only.',
        "prototype_scope_review": 'Independently review proposed_scope against the goal, admitted source, existing implementation/tests and historical approvals. Inspect actual baseline browser evidence and attached screenshots. Audit the completeness of preserved flows and the strength of every frozen assertion. Reject broad regions or unrelated layout/behavior changes. Distinguish evidenced baseline restoration from new features, retired behavior and user edits. Full redesign/removal requires explicit user intent. Ask concrete questions for unresolved intent conflicts. Return {"decision":"pass|revise","coverage_complete":boolean,"reason":"concrete findings","questions":[]}. Pass only when both preservation coverage and the minimal allowed scope are justified. You cannot edit or widen the proposal.',
        "prototype_review": 'Independently inspect the actual candidate package against the frozen original/repaired baseline, permitted regions and current requirements. Inspect the controller browser observations and attached screenshots; audit semantic behavior and test coverage, including functions the previous prototype omitted. Reject false-positive assertions, omissions, unrelated changes and ineffective controls. Do not expand scope or weaken oracles. Return {"decision":"pass|revise","coverage_complete":boolean,"reason":"specific comparisons"}.',
        "clarify": 'Produce requirements and measurable acceptance from the spec. Write .auto-agents/docs/project_brief.md and .auto-agents/docs/requirements.md. Return {"artifacts":[paths],"frontend":boolean,"questions":[]}. Ask only about missing user decisions.',
        "prototype": 'Produce a static frontend prototype for the approved requirements. Write standalone HTML with viewport meta tags under .auto-agents/docs/frontend_prototype/, including home.html and manifest.json. The manifest declares pages:[{id,title,route,requirement_ids,html_ref}], viewports:[WIDTHxHEIGHT], index_ref and artifacts:[every package file path, including supporting assets, evidence and verification scripts]. All refs are workspace-relative; embed assets rather than loading remote/script dependencies. Register every HTML file in pages or index_ref. If a design catalog is supplied, return selection:{candidates:[{slug,score,rationale,risks}],selected_slug} with three candidates and one highest-scoring choice; preserve exact upstream DESIGN.md bytes. Return {"artifacts":[paths],"selection":selection}. Do not implement the product yet.',
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
    if phase == "prototype_prepare":
        lines.append(
            "When previous_proposal is supplied, it contains the complete prior "
            "scope, hashes and controller-owned read-only original_path/baseline_path. "
            "Inspect those files to correct the reported failure and reuse valid "
            "restoration work under repair_path rather than asking the user to "
            "supply artifacts already available there. It is an unapproved draft, "
            "not a new source or permission to expand scope. The admitted baseline "
            "and previous_frozen_scope remain authoritative; all repairs, checks "
            "and scope still require controller verification and independent review."
        )
        lines.append(
            "Evidence refs must be exact admitted_baseline.inputs/history keys, canonical "
            "original file refs, or exact admitted file paths under the supplied read-only "
            "original_path. Snapshot refs retain the same preservation obligations as "
            "canonical refs. repair_path and arbitrary absolute paths are not admitted evidence. "
            "admitted_baseline.original_manifest_errors is the controller's original package "
            "validation: an empty list means no package-structure repair is needed. Manifest "
            "refs are workspace-relative; for the original snapshot their workspace is the "
            "parent of original_path, not original_path itself. Keep repaired manifest refs "
            "relative to the model workspace and inside repair_path."
        )
        lines.append(
            "Every browser action other than click requires an explicit frozen value. "
            "For check, use a boolean value:true to select a radio/checkbox or value:false "
            "to clear a checkbox; never omit it. fill/select take the intended value, "
            "seek targets the actual video/audio element and takes seconds; a range slider "
            "uses fill with its intended value. press takes a key string such as Enter. "
            "text assertions compare exact trimmed textContent, including source case and "
            "whitespace; they do not insert rendered line breaks or apply CSS text-transform. "
            "Do not replace behavioral assertions with existence or success-message checks."
        )
        lines.append(
            "Requirement bindings distinguish requested changes from preserved behavior. "
            "Every change and every flow with a new check needs nonempty requirement_ids "
            "from admitted_baseline.active_requirements. Every requirement ID on a change "
            "must also appear on a flow with a new behavior check. "
            "A preservation-only flow may cite admitted_baseline.historical_requirements "
            "without turning those historical IDs into current requirements; a preserved "
            "auxiliary flow with no requirement identity may use requirement_ids:[], "
            "with concrete implementation or admitted prototype/history evidence and "
            "executable preservation checks. Goal or current requirement documents alone "
            "do not establish an existing flow. Citing current requirements or backend "
            "code for a new UI flow does not make that flow preexisting. Do not invent "
            "identities or edit the requirements trace to satisfy these bindings."
        )
        lines.append(
            "Flows and checks may declare relative state URLs such as home.html?state=error "
            "or home.html#loading. Each check must stay on its flow's same HTML file; "
            "query parameters and fragments are frozen test inputs. Changes, repair regions "
            "and geometry must name actual package HTML files without URL parameters. "
            "All declared states retain the same file's scope and preservation constraints. "
            "Dynamic regions may be absent in empty/default states and appear after frozen "
            "actions; each required region must be observed in every viewport. Regions "
            "remain unique and bounded whenever present. A region introduced only in the "
            "candidate uses kind:add, including related additions with because dependencies; "
            "kind:linkage modifies a region that exists in the repaired baseline states. "
            "The controller waits up to two "
            "seconds for all frozen postconditions to hold together, without weakening them."
        )
        lines.append(
            "For removal/redesign changes, also provide permission: an exact excerpt of the current user goal or recorded user decision explicitly authorizing that deletion or full redesign. Goal evidence alone never grants permission. Geometry entries may name html/body solely for causally required coordinate/size shifts; appearance remains protected. A structural baseline repair may list flow_ids:['package-structure']; the original manifest must actually fail validation and the repaired one must pass. If a design catalog is supplied for a new frontend, return selection:{candidates:[{slug,score,rationale,risks}],selected_slug} with three candidates and one highest-scoring selection. This selection and its exact upstream bytes will be frozen before candidate generation. Existing frontends keep their established design source."
        )
        lines.append("Retain all admitted manifest viewports; the candidate must use exactly the frozen viewport list. Explicit removal of an entire page may use whole_page:true only on a remove change whose permission excerpt names that admitted page and authorizes its removal. A local removal never grants a redesign of other pages. Redesign still preserves unchanged pages and every non-retired behavior.")
        lines.append("Baseline repairs also have bounded scope. A repair may declare regions:[{page,selector,kind:'add|change|remove'}], files:[exact package paths], geometry:[{page,selector,properties}]. Without regions/files, the controller derives them narrowly from stable selectors and pages in that repair's frozen preservation checks. Declare missing wrappers and necessary coordinate/size shifts explicitly. Restoration cannot change unrelated files, colors, fonts, controls or layouts, or implement new requirements in the baseline. The controller compares the original and repaired UI outside these bounds before freezing either.")
    if phase == "prototype":
        lines.append("The frozen_prototype_scope is immutable. Start from its repaired baseline and modify only its exact allowed files and regions. Only candidate_path, .tmp/ and .tmp-tests/ are writable. The controller copies upstream DESIGN.md and LICENSE; do not write them outside the candidate. Do not change prototype_preparation, controller records, baseline, frozen checks, defaults or unrelated design. A failure must be repaired inside the same scope; any expansion requires an explicit new scope decision. Do not run production or paid providers.")
        lines.append(
            "Preserve existing product behavior unless the approved requirements explicitly change it. "
            "Inspect the existing frontend implementation and tests, historical prototype manifests "
            "and the current package before editing. A newer prototype may already have omitted "
            "features; do not use it as the sole behavioral baseline. Inventory the affected flows, "
            "restore every omission named in operator feedback, and retain unrelated interactions. "
            "Use the frozen preservation inventory and browser checks. The controller owns "
            "the preservation report and evidence outside the candidate package. Do not add "
            "undeclared reports or test files, change checks, or invent pass results. "
            "Preserve old flows as well as the new requirements; file existence and screenshots "
            "alone do not prove their behavior."
        )
        lines.append(
            "Use untracked files under this workspace's .tmp/ or .tmp-tests/ real "
            "directories for temporary browser profiles, downloads, tooling dependencies "
            "and test scripts. Temporary aliases must stay inside private runtime. "
            "Do not change tracked files under temporary directories, product source, "
            "credentials, databases or controller state. "
            "Temporary files are private tooling, not deliverables; write and declare "
            "durable prototype assets and evidence inside the allowed prototype package."
        )
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
