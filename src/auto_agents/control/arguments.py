"""Public command syntax; execution has exactly one controller."""

import argparse
from ..worker_cluster import WORKER_API_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quality-first orchestration for AI-assisted project delivery."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prompt_eval_parser = subparsers.add_parser(
        "prompt-eval",
        help="Capture prompt baselines or explicitly evaluate configured providers",
    )
    prompt_eval_parser.add_argument(
        "eval_args",
        nargs=argparse.REMAINDER,
        help="capture or run; use 'prompt-eval run --help' for options",
    )

    init_parser = subparsers.add_parser("init", help="Bootstrap a new target project.")
    init_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    init_parser.add_argument(
        "--name",
        help="Project name. Defaults to the final directory name from --project.",
    )
    init_parser.add_argument(
        "--doc-language",
        choices=("en", "zh"),
        default="en",
        help="Language for generated documents. Defaults to en.",
    )

    run_parser = subparsers.add_parser("run", help="Run the orchestration pipeline.")
    run_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    run_parser.add_argument(
        "--spec-file",
        help="Path to the input specification markdown. Defaults to <project>/spec.md.",
    )
    run_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically pass manual gates except the mandatory frontend prototype review.",
    )
    run_parser.add_argument(
        "--allow-dirty-tree",
        action="store_true",
        help="Allow implementation to start even when the project git tree already has local changes.",
    )
    run_parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help="Run directly without the optional maintenance supervisor.",
    )
    run_parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Optional task execution cap for the current run.",
    )
    run_parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip local preflight validation before agent execution.",
    )
    run_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Print each agent stage output to stderr as it completes.",
    )
    run_parser.add_argument(
        "--provider",
        help="Use a configured provider entry by name and persist it as the new default.",
    )
    run_parser.add_argument(
        "--doc-language",
        choices=("en", "zh"),
        help="Override and persist the language for generated documents.",
    )
    run_parser.add_argument(
        "--no-repo-map",
        action="store_true",
        help="Disable Aider-style repo map injection for this run.",
    )
    run_parser.add_argument(
        "--restart-blocked",
        action="store_true",
        help="Archive a blocked run and start a fresh run; refuses dirty project code.",
    )
    run_parser.add_argument(
        "--full-verify",
        action="store_true",
        help="Bypass incremental gate certificates and execute every final shard.",
    )
    run_parser.add_argument(
        "--autonomy",
        choices=("off", "guarded", "max"),
        help=(
            "Override the autonomous repair mode for this workflow. "
            "Defaults to execution.autonomy.mode."
        ),
    )
    run_parser.add_argument(
        "--interaction-mode",
        choices=("auto", "tty", "pause", "fail"),
        help="How the run handles required operator input.",
    )
    run_parser.add_argument(
        "--secret-echo",
        choices=("auto", "visible", "hidden"),
        help="Whether interactive secret input is echoed.",
    )

    answer_parser = subparsers.add_parser(
        "answer", help="Answer the active structured operator-input request."
    )
    answer_parser.add_argument("--project", required=True)
    answer_parser.add_argument("--request-id", default="")
    answer_values = answer_parser.add_mutually_exclusive_group()
    answer_values.add_argument("--yes", action="store_true")
    answer_values.add_argument("--no", action="store_true")
    answer_values.add_argument("--value")
    answer_values.add_argument("--from-env")
    answer_values.add_argument("--from-file")
    answer_parser.add_argument(
        "--echo", choices=("auto", "visible", "hidden"), default="auto"
    )
    answer_parser.add_argument("--no-resume", action="store_true")
    answer_parser.add_argument(
        "--autonomy",
        choices=("off", "guarded", "max"),
        help="Override autonomous repair mode for the resumed workflow.",
    )

    inputs_parser = subparsers.add_parser(
        "inputs", help="Inspect or manage persisted project operator inputs."
    )
    inputs_subparsers = inputs_parser.add_subparsers(
        dest="inputs_command", required=True
    )
    inputs_list = inputs_subparsers.add_parser("list")
    inputs_list.add_argument("--project", required=True)
    inputs_remove = inputs_subparsers.add_parser("remove")
    inputs_remove.add_argument("--project", required=True)
    inputs_remove.add_argument("--key", required=True)
    inputs_set = inputs_subparsers.add_parser("set")
    inputs_set.add_argument("--project", required=True)
    inputs_set.add_argument("--key", required=True)
    inputs_set.add_argument("--value")
    inputs_set.add_argument("--secret", action="store_true")
    inputs_set.add_argument(
        "--echo", choices=("auto", "visible", "hidden"), default="auto"
    )

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop the active run and its validated subprocess groups.",
    )
    stop_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    stop_parser.add_argument(
        "--grace-seconds",
        type=float,
        default=10.0,
        help="Seconds to wait after SIGTERM before escalating to SIGKILL.",
    )

    approve_parser = subparsers.add_parser(
        "approve", help="Approve a pending manual gate."
    )
    approve_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    approve_parser.add_argument(
        "--gate",
        help="Gate name to approve. Defaults to the current pending gate inferred from run state.",
    )
    approve_parser.add_argument(
        "--variant",
        default="",
        help="Frontend prototype variant ID. Required non-interactively when multiple candidates exist.",
    )

    reject_parser = subparsers.add_parser(
        "reject", help="Reject a pending manual gate and provide feedback."
    )
    reject_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    reject_parser.add_argument(
        "--gate",
        help="Gate name to reject. Defaults to the current pending gate inferred from run state.",
    )
    reject_parser.add_argument(
        "--reason",
        default="",
        help="Reason for rejection. This feedback will be provided to the agent on the next run.",
    )
    reject_target = reject_parser.add_mutually_exclusive_group()
    reject_target.add_argument(
        "--variant",
        action="append",
        default=[],
        help="Frontend prototype variant to reject and delete. May be repeated.",
    )
    reject_target.add_argument(
        "--all-except",
        default="",
        help="Reject and delete every frontend prototype candidate except this ID.",
    )
    reject_parser.add_argument(
        "--reselect-design",
        action="store_true",
        help="Deprecated compatibility hint; describe the desired visual change in --reason instead.",
    )

    preview_parser = subparsers.add_parser(
        "prototype-preview",
        help="Compatibility alias for the frontend prototype comparison gallery.",
    )
    preview_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    preview_parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host. Defaults to loopback."
    )
    preview_parser.add_argument(
        "--port", type=int, default=0, help="Bind port. Defaults to an available port."
    )

    prototype_parser = subparsers.add_parser(
        "prototype",
        help="Generate, list, and preview frontend prototype variants.",
    )
    prototype_subparsers = prototype_parser.add_subparsers(
        dest="prototype_command",
        required=True,
    )
    prototype_generate = prototype_subparsers.add_parser(
        "generate",
        help="Generate an additional candidate without overwriting existing variants.",
    )
    prototype_generate.add_argument(
        "--project", required=True, help="Target project directory."
    )
    prototype_generate.add_argument(
        "--prompt", required=True, help="Visual direction for the new candidate."
    )
    prototype_generate.add_argument(
        "--name", default="", help="Optional display name for the candidate."
    )
    prototype_generate.add_argument(
        "--from", dest="base_variant", default="", help="Base candidate ID."
    )

    prototype_list = prototype_subparsers.add_parser(
        "list", help="List frontend prototype variants."
    )
    prototype_list.add_argument(
        "--project", required=True, help="Target project directory."
    )
    prototype_list.add_argument(
        "--all", action="store_true", help="Include rejected tombstones."
    )
    prototype_list.add_argument(
        "--json", action="store_true", help="Emit JSON (currently the default output)."
    )

    prototype_preview = prototype_subparsers.add_parser(
        "preview", help="Preview and compare live variants."
    )
    prototype_preview.add_argument(
        "--project", required=True, help="Target project directory."
    )
    prototype_preview.add_argument(
        "--host", default="127.0.0.1", help="Bind host. Defaults to loopback."
    )
    prototype_preview.add_argument(
        "--port", type=int, default=0, help="Bind port. Defaults to an available port."
    )

    status_parser = subparsers.add_parser(
        "status", help="Show the current orchestrator state."
    )
    status_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )

    performance_parser = subparsers.add_parser(
        "performance",
        help="Summarize persisted run or session performance spans.",
    )
    performance_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    performance_parser.add_argument(
        "--session",
        default="",
        help="Optional fix, collab, or provider-resolve session ID. Defaults to the current run.",
    )

    validate_parser = subparsers.add_parser(
        "validate", help="Validate config, plan, and required docs."
    )
    validate_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="Execute managed affected or release proof attestation.",
    )
    verify_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    verify_parser.add_argument(
        "--level",
        choices=("focused", "affected", "release"),
        default="affected",
    )
    verify_parser.add_argument(
        "--changed-from",
        help="Git ref used to calculate the affected path set.",
    )
    verify_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Bypass existing proof certificates.",
    )
    verify_parser.add_argument(
        "--test",
        action="append",
        default=[],
        help="Repository-local test target; repeat for multiple targets.",
    )
    verify_parser.add_argument(
        "--engine",
        action="store_true",
        help="Use the engine verification profile and interpreter.",
    )
    verify_parser.add_argument(
        "--explain",
        action="store_true",
        help="Explain selection without executing or changing project state.",
    )

    release_worker_parser = subparsers.add_parser(
        "release-worker",
        help="Process deferred release proofs and bounded automatic recovery.",
    )
    release_worker_parser.add_argument("--project", required=True)
    release_worker_parser.add_argument(
        "--wait", action="store_true", help=argparse.SUPPRESS
    )
    release_worker_parser.add_argument(
        "--once",
        action="store_true",
        help="Process the latest eligible candidate and exit.",
    )

    attest_parser = subparsers.add_parser(
        "attest",
        help="Require a passed release attestation for an immutable Git candidate.",
    )
    attest_parser.add_argument("--project", required=True)
    attest_parser.add_argument(
        "--require-release",
        default="HEAD",
        metavar="REF",
        help="Git ref that must have a passed release attestation. Defaults to HEAD.",
    )

    sync_parser = subparsers.add_parser(
        "sync-agent-instructions",
        help="Generate Codex and Copilot instruction files from .auto-agents/project-rules.md.",
    )
    sync_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )

    research_parser = subparsers.add_parser(
        "provider-research", help="Run centralized provider documentation research."
    )
    research_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    research_parser.add_argument(
        "--spec-file",
        help="Path to the input specification markdown. Defaults to <project>/spec.md.",
    )

    audit_parser = subparsers.add_parser(
        "audit-requirements", help="Run the requirements trace audit."
    )
    audit_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )

    fix_parser = subparsers.add_parser(
        "fix", help="Conversational bug fix for a completed project."
    )
    fix_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    fix_parser.add_argument(
        "--session",
        help="Resume an existing fix session by ID.",
    )
    fix_parser.add_argument(
        "--provider",
        help="Use a configured provider entry by name for this session.",
    )
    fix_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Print each agent output to stderr as it completes.",
    )
    fix_parser.add_argument(
        "--full-verify",
        action="store_true",
        help="Bypass incremental gate certificates for this fix session.",
    )
    fix_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "Automatically approve this fix's eligible actions and inherit the "
            "same policy into routed workflows."
        ),
    )
    fix_parser.add_argument(
        "--autonomy",
        choices=("off", "guarded", "max"),
        help="Override autonomous repair mode for this session.",
    )
    fix_parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help="Disable proactive health supervision for this invocation.",
    )

    collab_parser = subparsers.add_parser(
        "collab", help="User-agent collaborative debugging session."
    )
    collab_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    collab_parser.add_argument(
        "--session",
        help="Resume an existing collab session by ID.",
    )
    collab_parser.add_argument(
        "--provider",
        help="Use a configured provider entry by name for this session.",
    )
    collab_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Print each agent output to stderr as it completes.",
    )
    collab_parser.add_argument(
        "--full-verify",
        action="store_true",
        help=(
            "Bypass incremental gate certificates only for collab's final "
            "completion attestation."
        ),
    )
    collab_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "Automatically authorize safe in-scope implementation, engine "
            "maintenance, tests, local commits, compatible state upgrades, and "
            "workflow recovery. Goal choices, credentials, unbudgeted external "
            "costs, destructive changes, irreversible product decisions, and "
            "external observations only the user can perform still require "
            "the user."
        ),
    )
    collab_parser.add_argument(
        "--autonomy",
        choices=("off", "guarded", "max"),
        help="Override autonomous repair mode for this session.",
    )
    collab_parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help="Disable proactive health supervision for this invocation.",
    )

    persistence_parser = subparsers.add_parser(
        "persistence-configure",
        help="Register a human-classified persistence target.",
    )
    persistence_parser.add_argument("--project", required=True)
    persistence_parser.add_argument("--id", dest="target_id", default="")
    persistence_parser.add_argument(
        "--environment",
        choices=("development", "test", "production"),
        default="",
    )
    persistence_parser.add_argument(
        "--kind",
        choices=("local_file", "compose_service"),
        default="",
    )
    persistence_parser.add_argument("--path", default="")
    persistence_parser.add_argument("--path-env", default="")
    persistence_parser.add_argument("--compose-file", default="")
    persistence_parser.add_argument("--service", action="append", default=[])
    persistence_parser.add_argument("--associated-path", action="append", default=[])
    persistence_parser.add_argument(
        "--interface-version", type=int, choices=(1, 2), default=0
    )
    persistence_parser.add_argument(
        "--lifecycle", choices=("pending_bootstrap", "ready"), default=""
    )
    persistence_parser.add_argument("--status-command", default="")
    persistence_parser.add_argument("--migrate-command", default="")
    persistence_parser.add_argument("--apply-command", default="")
    persistence_parser.add_argument("--initialize-command", default="")
    persistence_parser.add_argument("--reset-command", default="")
    persistence_parser.add_argument("--verify-command", default="")
    persistence_parser.add_argument("--migration-root", action="append", default=[])
    persistence_parser.add_argument("--replace", action="store_true")
    persistence_parser.add_argument("--timeout-seconds", type=int, default=300)
    persistence_parser.add_argument("--auto-approve", action="store_true")

    persistence_rebind_parser = subparsers.add_parser(
        "persistence-rebind",
        help=(
            "Explicitly bind a legacy REQ-* persistence decision to "
            "registered persistence targets."
        ),
    )
    persistence_rebind_parser.add_argument("--project", required=True)
    persistence_rebind_parser.add_argument("--decision", required=True)
    persistence_rebind_parser.add_argument(
        "--target",
        action="append",
        required=True,
        help="Registered persistence target id; repeat for multiple targets.",
    )

    persistence_upgrade_parser = subparsers.add_parser(
        "persistence-upgrade-contract",
        help="Atomically upgrade active project persistence metadata to contract v2.",
    )
    persistence_upgrade_parser.add_argument("--project", required=True)
    persistence_upgrade_parser.add_argument(
        "--decision-policy",
        action="append",
        default=[],
        metavar="PERSIST-NNN:TRANSITION:POLICY",
    )
    persistence_upgrade_parser.add_argument("--resume-interrupted", action="store_true")

    provider_resolve_parser = subparsers.add_parser(
        "provider-resolve",
        help="Conversational recovery for a blocked provider_research stage.",
    )
    provider_resolve_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    provider_resolve_parser.add_argument(
        "--session",
        help="Resume an existing provider-resolution session by ID.",
    )
    provider_resolve_parser.add_argument(
        "--provider",
        help="Use a configured provider entry by name for this session.",
    )
    provider_resolve_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Print each agent output to stderr as it completes.",
    )
    provider_resolve_parser.add_argument(
        "--autonomy",
        choices=("off", "guarded", "max"),
        help="Override autonomous repair mode for this session.",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume the active nested workflow from its deepest durable checkpoint.",
    )
    resume_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    resume_parser.add_argument(
        "--workflow",
        default="",
        help="Explicit workflow ID when more than one resumable root exists.",
    )
    resume_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Stream resumed agent output to stderr.",
    )
    resume_parser.add_argument(
        "--full-verify",
        action="store_true",
        help="Force the resumed root session's final attestation.",
    )
    resume_parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help="Disable proactive health supervision for this invocation.",
    )

    # ── sessions (list) ──────────────────────────────────────────
    sessions_parser = subparsers.add_parser(
        "sessions", help="List sessions for a completed project."
    )
    sessions_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    sessions_parser.add_argument(
        "--mode",
        choices=["fix", "collab", "provider-resolve"],
        help="Filter by session mode.",
    )
    sessions_parser.add_argument(
        "--all",
        action="store_true",
        help="Include completed and failed sessions (default: active only).",
    )

    sessions_delete_parser = subparsers.add_parser(
        "sessions-delete",
        help="Delete one saved session record without touching project code changes.",
    )
    sessions_delete_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )
    sessions_delete_parser.add_argument(
        "--session", required=True, help="Session ID to delete."
    )

    sessions_clear_parser = subparsers.add_parser(
        "sessions-clear",
        help="Delete all saved session records without touching project code changes.",
    )
    sessions_clear_parser.add_argument(
        "--project", required=True, help="Target project directory."
    )

    cluster_parser = subparsers.add_parser(
        "cluster",
        help="Initialize and pair trusted LAN worker computers.",
    )
    cluster_subparsers = cluster_parser.add_subparsers(
        dest="cluster_command",
        required=True,
    )
    cluster_init = cluster_subparsers.add_parser("init")
    cluster_init.add_argument("--name", default="")
    cluster_pair = cluster_subparsers.add_parser("pair")
    cluster_pair.add_argument("--host", default="")
    cluster_pair.add_argument("--port", type=int, default=WORKER_API_PORT)
    cluster_pair.add_argument("--ttl-seconds", type=int, default=600)
    cluster_subparsers.add_parser("status")

    workers_parser = subparsers.add_parser(
        "workers",
        help="Inspect and maintain automatically discovered LAN workers.",
    )
    workers_subparsers = workers_parser.add_subparsers(
        dest="workers_command",
        required=True,
    )
    workers_doctor = workers_subparsers.add_parser(
        "doctor",
        help="Validate worker connectivity, environment, and capacity.",
    )
    workers_doctor.add_argument("--project")
    workers_status = workers_subparsers.add_parser(
        "status",
        help="Show worker pool health and capacity.",
    )
    workers_cleanup = workers_subparsers.add_parser(
        "cleanup",
        help="Remove stale terminal worker job records and artifacts.",
    )
    workers_cleanup.add_argument("--max-age-seconds", type=float, default=86400.0)

    worker_parser = subparsers.add_parser(
        "worker",
        help="Run this computer as a foreground LAN gate worker.",
    )
    worker_subparsers = worker_parser.add_subparsers(
        dest="worker_command",
        required=True,
    )
    worker_serve = worker_subparsers.add_parser("serve")
    worker_serve.add_argument("--join", default="")
    worker_serve.add_argument("--slots", default="auto")
    worker_serve.add_argument("--bind", default="0.0.0.0")
    worker_serve.add_argument("--port", type=int, default=WORKER_API_PORT)

    storage_parser = subparsers.add_parser(
        "storage", help="Inspect and maintain registered generated files."
    )
    storage_sub = storage_parser.add_subparsers(dest="storage_action", required=True)
    storage_sub.add_parser(
        "clean",
        help="Clean all eligible local auto-agents resources, including verified legacy caches.",
    )
    for action in ("status", "plan", "maintain"):
        child = storage_sub.add_parser(action)
        child.add_argument("--project")
        child.add_argument(
            "--scope", choices=("user", "worker", "repair"), default="user"
        )
        child.add_argument("--format", choices=("json",), default="json")
    child = storage_sub.add_parser("apply")
    child.add_argument("--plan", required=True)
    for action in ("pin", "unpin", "restore"):
        child = storage_sub.add_parser(action)
        child.add_argument("artifact")
        if action == "pin":
            child.add_argument("--reason", required=True)

    foreground = {
        "run",
        "fix",
        "collab",
        "provider-resolve",
        "resume",
        "answer",
        "approve",
        "reject",
        "verify",
        "provider-research",
        "audit-requirements",
        "sync-agent-instructions",
    }
    for name in foreground:
        subparsers.choices[name].add_argument(
            "--log-mode",
            choices=("auto", "plain", "debug"),
            default=None,
            help="Console output: auto progress display, plain user logs, or debug diagnostics.",
        )
    prototype_parser = subparsers.choices.get("prototype")
    if prototype_parser is not None:
        for action in prototype_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    if name != "list":
                        child.add_argument(
                            "--log-mode",
                            choices=("auto", "plain", "debug"),
                            default=None,
                        )
    commands = subparsers.choices
    for name in (
        "run",
        "fix",
        "collab",
        "resume",
        "provider-resolve",
        "provider-research",
    ):
        command = commands[name]
        command.add_argument("--goal")
        command.add_argument("--max-provider-calls", type=int)
        command.add_argument("--verify-command")
        command.add_argument("--json", action="store_true")
        if "--session" not in command._option_string_actions:
            command.add_argument("--session")
        if "--provider" not in command._option_string_actions:
            command.add_argument("--provider")
        if "--auto-approve" not in command._option_string_actions:
            command.add_argument("--auto-approve", action="store_true")
    for name in ("fix", "collab", "provider-resolve"):
        commands[name].add_argument("--allow-dirty-tree", action="store_true")
    for name in ("approve", "reject", "answer", "verify", "cancel"):
        if name == "cancel":
            commands[name] = subparsers.add_parser(name)
            commands[name].add_argument("--project", required=True)
        commands[name].add_argument("--session")
        commands[name].add_argument("--workflow")
    for name in (
        "status",
        "sessions",
        "init",
        "approve",
        "reject",
        "answer",
        "cancel",
        "validate",
        "verify",
        "stop",
    ):
        if "--json" not in commands[name]._option_string_actions:
            commands[name].add_argument("--json", action="store_true")
    for name in (
        "capabilities",
        "business-status",
        "execution-request",
        "snapshot",
        "resume-check",
        "migrate-state",
        "reconcile-call",
        "quiesce",
        "checkpoint",
        "upgrade",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name == "capabilities":
            continue
        command.add_argument("--project", required=True)
        if name == "snapshot":
            command.add_argument("--output", required=True)
        if name == "resume-check":
            command.add_argument("--resume-token", required=True)
        if name == "migrate-state":
            command.add_argument("--check", action="store_true")
        if name == "reconcile-call":
            command.add_argument("--call", required=True)
            command.add_argument("--result")
            command.add_argument("--confirm-cancelled", action="store_true")
        if name == "quiesce":
            command.add_argument("--process-file")
        if name == "checkpoint":
            command.add_argument("--observation", required=True)
            command.add_argument("--invocation", required=True)
        if name == "execution-request":
            command.add_argument("--invocation", required=True)
    return parser
