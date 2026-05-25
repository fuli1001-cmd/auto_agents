from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from .agent_instructions import ensure_agent_instructions_synced, sync_agent_instructions
from .config import (
    architecture_path,
    load_run_state,
    project_brief_path,
    requirements_audit_path,
    requirements_trace_path,
    run_path,
    run_state_path,
    task_plan_path,
)
from .config import supported_provider_kinds
from .orchestrator import Orchestrator
from .validation import validation_report


def _default_project_name(project: Path) -> str:
    candidate = project.expanduser().resolve().name
    return candidate or "unnamed-project"


def _default_spec_file(project: Path) -> Path:
    return project / "spec.md"


def _confirm_prompt(project_root: Path, prompt: str, default: str = "n") -> str:
    orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
    return orchestrator._prompt_user(prompt, default=default)


def _display_path(project_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


def _format_command(*parts: str) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def _render_key_files(project_root: Path, state_payload: dict[str, object]) -> list[str]:
    run_id = str(state_payload.get("run_id", "")).strip()
    pending_approval = str(state_payload.get("pending_approval", "")).strip()

    key_files: list[Path] = []
    if pending_approval == "requirements":
        key_files.extend([
            project_brief_path(project_root),
            requirements_trace_path(project_root),
        ])
    elif pending_approval == "architecture":
        key_files.append(architecture_path(project_root))
    elif pending_approval == "release":
        key_files.extend([
            requirements_audit_path(project_root),
            task_plan_path(project_root),
        ])
    else:
        key_files.extend([
            project_root / "README.md",
            task_plan_path(project_root),
        ])

    key_files.append(run_state_path(project_root))
    rendered = [_display_path(project_root, path) for path in key_files]
    if run_id:
        rendered.append(_display_path(project_root, run_path(project_root, run_id) / "outputs"))
    return rendered


def _render_run_summary(project_root: Path, state_payload: dict[str, object]) -> str:
    status = str(state_payload.get("status", "")).strip()
    pending_approval = str(state_payload.get("pending_approval", "")).strip()
    current_stage = str(state_payload.get("current_stage", "")).strip() or "unknown"

    key_files = _render_key_files(project_root, state_payload)
    status_cmd = _format_command("python3", "-m", "auto_agents", "status", "--project", str(project_root))
    run_cmd = _format_command("python3", "-m", "auto_agents", "run", "--project", str(project_root))

    if status == "paused" and pending_approval:
        approve_cmd = _format_command(
            "python3",
            "-m",
            "auto_agents",
            "approve",
            "--project",
            str(project_root),
            "--gate",
            pending_approval,
        )
        reject_cmd = _format_command(
            "python3",
            "-m",
            "auto_agents",
            "reject",
            "--project",
            str(project_root),
            "--gate",
            pending_approval,
            "--reason",
            "<feedback>",
        )
        lines = [
            f"Run paused: approval required for {pending_approval}.",
            "",
            "Key files to review:",
            *[f"- {item}" for item in key_files],
            "",
            "Next steps:",
            f"- Approve and continue: {approve_cmd} && {run_cmd}",
            f"- Reject and revise: {reject_cmd} && {run_cmd}",
            f"- Inspect persisted status: {status_cmd}",
        ]
        return "\n".join(lines)

    if status == "completed":
        lines = [
            "Run completed successfully.",
            "",
            "Key files to review:",
            *[f"- {item}" for item in key_files],
            "",
            "Next steps:",
            "- Review the generated files above.",
            f"- Inspect persisted status: {status_cmd}",
            f"- Start another iteration later: {run_cmd}",
        ]
        return "\n".join(lines)

    lines = [
        f"Run finished with status: {status or 'unknown'} (stage: {current_stage}).",
        "",
        "Key files to review:",
        *[f"- {item}" for item in key_files],
        "",
        "Next steps:",
        f"- Inspect persisted status: {status_cmd}",
    ]
    return "\n".join(lines)


def _auto_resolve_provider_blocker(
    project_root: Path,
    orchestrator: Orchestrator,
    *,
    print_agent_output: bool,
) -> int:
    from .session import Session

    print(
        "Run hit a provider_research blocker. Starting automatic provider recovery...",
        file=sys.stderr,
    )
    orchestrator._print_agent_output = bool(print_agent_output)
    session = Session(
        orchestrator,
        mode="provider_resolve",
        print_agent_output=bool(print_agent_output),
    )
    try:
        session_state = session.start()
    except (RuntimeError, FileNotFoundError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
        return 1
    if session_state.status != "completed":
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "automatic provider-resolve session did not complete; "
                        f"session_id={session_state.session_id} status={session_state.status}"
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1

    resumed_state = load_run_state(project_root)
    if resumed_state.status == "failed":
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": resumed_state.last_error or "run failed after automatic provider recovery",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1

    print(_render_run_summary(project_root, resumed_state.to_dict()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quality-first orchestration for AI-assisted project delivery.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Bootstrap a new target project.")
    init_parser.add_argument("--project", required=True, help="Target project directory.")
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
    run_parser.add_argument("--project", required=True, help="Target project directory.")
    run_parser.add_argument(
        "--spec-file",
        help="Path to the input specification markdown. Defaults to <project>/spec.md.",
    )
    run_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically pass all manual approval gates.",
    )
    run_parser.add_argument(
        "--allow-dirty-tree",
        action="store_true",
        help="Allow implementation to start even when the project git tree already has local changes.",
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
        choices=supported_provider_kinds(),
        help="Override provider for this run and persist it as the new default provider.",
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

    approve_parser = subparsers.add_parser("approve", help="Approve a pending manual gate.")
    approve_parser.add_argument("--project", required=True, help="Target project directory.")
    approve_parser.add_argument(
        "--gate",
        help="Gate name to approve. Defaults to the current pending gate inferred from run state.",
    )

    reject_parser = subparsers.add_parser("reject", help="Reject a pending manual gate and provide feedback.")
    reject_parser.add_argument("--project", required=True, help="Target project directory.")
    reject_parser.add_argument(
        "--gate",
        help="Gate name to reject. Defaults to the current pending gate inferred from run state.",
    )
    reject_parser.add_argument(
        "--reason",
        default="",
        help="Reason for rejection. This feedback will be provided to the agent on the next run.",
    )

    status_parser = subparsers.add_parser("status", help="Show the current orchestrator state.")
    status_parser.add_argument("--project", required=True, help="Target project directory.")

    validate_parser = subparsers.add_parser("validate", help="Validate config, plan, and required docs.")
    validate_parser.add_argument("--project", required=True, help="Target project directory.")

    sync_parser = subparsers.add_parser(
        "sync-agent-instructions",
        help="Generate Codex and Copilot instruction files from .auto-agents/project-rules.md.",
    )
    sync_parser.add_argument("--project", required=True, help="Target project directory.")

    research_parser = subparsers.add_parser("provider-research", help="Run centralized provider documentation research.")
    research_parser.add_argument("--project", required=True, help="Target project directory.")
    research_parser.add_argument(
        "--spec-file",
        help="Path to the input specification markdown. Defaults to <project>/spec.md.",
    )

    audit_parser = subparsers.add_parser("audit-requirements", help="Run the requirements trace audit.")
    audit_parser.add_argument("--project", required=True, help="Target project directory.")

    fix_parser = subparsers.add_parser("fix", help="Conversational bug fix for a completed project.")
    fix_parser.add_argument("--project", required=True, help="Target project directory.")
    fix_parser.add_argument(
        "--session",
        help="Resume an existing fix session by ID.",
    )
    fix_parser.add_argument(
        "--provider",
        choices=supported_provider_kinds(),
        help="Override provider for this session.",
    )
    fix_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Print each agent output to stderr as it completes.",
    )

    collab_parser = subparsers.add_parser("collab", help="User-agent collaborative debugging session.")
    collab_parser.add_argument("--project", required=True, help="Target project directory.")
    collab_parser.add_argument(
        "--session",
        help="Resume an existing collab session by ID.",
    )
    collab_parser.add_argument(
        "--provider",
        choices=supported_provider_kinds(),
        help="Override provider for this session.",
    )
    collab_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Print each agent output to stderr as it completes.",
    )

    provider_resolve_parser = subparsers.add_parser(
        "provider-resolve",
        help="Conversational recovery for a blocked provider_research stage.",
    )
    provider_resolve_parser.add_argument("--project", required=True, help="Target project directory.")
    provider_resolve_parser.add_argument(
        "--session",
        help="Resume an existing provider-resolution session by ID.",
    )
    provider_resolve_parser.add_argument(
        "--provider",
        choices=supported_provider_kinds(),
        help="Override provider for this session.",
    )
    provider_resolve_parser.add_argument(
        "--print-agent-output",
        action="store_true",
        help="Print each agent output to stderr as it completes.",
    )

    # ── sessions (list) ──────────────────────────────────────────
    sessions_parser = subparsers.add_parser("sessions", help="List sessions for a completed project.")
    sessions_parser.add_argument("--project", required=True, help="Target project directory.")
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
    sessions_delete_parser.add_argument("--project", required=True, help="Target project directory.")
    sessions_delete_parser.add_argument("--session", required=True, help="Session ID to delete.")

    sessions_clear_parser = subparsers.add_parser(
        "sessions-clear",
        help="Delete all saved session records without touching project code changes.",
    )
    sessions_clear_parser.add_argument("--project", required=True, help="Target project directory.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        project_root = Path(args.project)
        name = args.name or _default_project_name(project_root)
        root = Orchestrator.init_project(project_root, name, doc_language=args.doc_language)
        print(root)
        return 0

    if args.command == "approve":
        orchestrator = Orchestrator(Path(args.project))
        state = orchestrator.approve(args.gate)
        print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "reject":
        orchestrator = Orchestrator(Path(args.project))
        state = orchestrator.reject(args.gate, args.reason)
        print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "run":
        orchestrator = None
        try:
            project_root = Path(args.project)
            spec_file = Path(args.spec_file) if args.spec_file else _default_spec_file(project_root)
            orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
            if getattr(args, "no_repo_map", False):
                orchestrator.config.repo_map.enabled = False
            state = orchestrator.run(
                spec_file=spec_file,
                auto_approve=bool(args.auto_approve),
                allow_dirty_tree=bool(args.allow_dirty_tree),
                max_tasks=args.max_tasks,
                skip_validate=bool(args.skip_validate),
                print_agent_output=bool(args.print_agent_output),
                doc_language=args.doc_language,
                provider_kind=args.provider,
            )
            print(_render_run_summary(project_root, state.to_dict()))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            if orchestrator is not None and orchestrator.is_provider_research_blocked_error(str(error)):
                return _auto_resolve_provider_blocker(
                    project_root,
                    orchestrator,
                    print_agent_output=bool(args.print_agent_output),
                )
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command == "status":
        orchestrator = Orchestrator(Path(args.project))
        print(json.dumps(orchestrator.status(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "validate":
        report = validation_report(Path(args.project))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1

    if args.command == "sync-agent-instructions":
        try:
            result = sync_agent_instructions(Path(args.project))
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command == "provider-research":
        try:
            project_root = Path(args.project)
            spec_file = Path(args.spec_file) if args.spec_file else _default_spec_file(project_root)
            orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
            state = orchestrator.run_provider_research(spec_file=spec_file)
            print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command == "audit-requirements":
        try:
            orchestrator = Orchestrator(Path(args.project), agent_output_stream=sys.stderr)
            result = orchestrator.audit_requirements()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["ok"] else 1
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command == "sessions":
        try:
            from .config import list_sessions

            _SESSIONS_OMIT = {
                "conversation", "execution_log", "current_attempt",
                "max_attempts", "updated_at", "stall_count", "last_diff_hash",
                "last_verify_sig", "consecutive_agent_errors", "hard_ceiling",
                "baseline_failures", "baseline_git_ref", "fix_verify_command",
            }
            project_root = Path(args.project)
            sessions = list_sessions(project_root)
            if getattr(args, "mode", None):
                selected_mode = "provider_resolve" if args.mode == "provider-resolve" else args.mode
                sessions = [s for s in sessions if s.mode == selected_mode]
            if not getattr(args, "all", False):
                sessions = [s for s in sessions if s.status not in ("completed", "failed")]
            rows = []
            for s in sessions:
                d = {k: v for k, v in s.to_dict().items() if k not in _SESSIONS_OMIT}
                rows.append(d)
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command == "sessions-delete":
        try:
            from .config import delete_session

            project_root = Path(args.project)
            answer = _confirm_prompt(
                project_root,
                (
                    f"Delete saved session {args.session}? "
                    "This only removes .auto-agents session state and does not revert code changes. (y/n) [n]: "
                ),
                default="n",
            )
            if answer.strip().lower() not in ("y", "yes"):
                print(json.dumps({"ok": False, "error": "Session deletion cancelled."}, indent=2, ensure_ascii=False))
                return 1
            delete_session(project_root, args.session)
            print(json.dumps({"ok": True, "deleted_session": args.session}, indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command == "sessions-clear":
        try:
            from .config import clear_sessions

            project_root = Path(args.project)
            answer = _confirm_prompt(
                project_root,
                (
                    "Delete ALL saved sessions? This only removes .auto-agents session state "
                    "and does not revert code changes. (y/n) [n]: "
                ),
                default="n",
            )
            if answer.strip().lower() not in ("y", "yes"):
                print(json.dumps({"ok": False, "error": "Session clear cancelled."}, indent=2, ensure_ascii=False))
                return 1
            deleted = clear_sessions(project_root)
            print(json.dumps({"ok": True, "deleted_sessions": deleted}, indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command in ("fix", "collab", "provider-resolve"):
        try:
            from .session import Session

            project_root = Path(args.project)
            orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
            ensure_agent_instructions_synced(project_root)
            if getattr(args, "provider", None):
                orchestrator._set_active_provider(args.provider)
            orchestrator._print_agent_output = bool(args.print_agent_output)
            session = Session(
                orchestrator,
                mode="provider_resolve" if args.command == "provider-resolve" else args.command,
                print_agent_output=bool(args.print_agent_output),
            )
            if args.session:
                state = session.resume(args.session)
            else:
                state = session.offer_resume_or_new()
            print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    parser.error(f"Unsupported command: {args.command}")
    return 2
