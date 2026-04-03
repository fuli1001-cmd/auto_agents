from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .orchestrator import Orchestrator
from .validation import validation_report


def _default_project_name(project: Path) -> str:
    candidate = project.expanduser().resolve().name
    return candidate or "unnamed-project"


def _default_spec_file(project: Path) -> Path:
    return project / "spec.md"


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
        "--provider",
        default="codex",
        help="Provider kind. Defaults to codex. Built-in: codex, mock, or a shell-wrapper kind.",
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
        "--doc-language",
        choices=("en", "zh"),
        help="Override and persist the language for generated documents.",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        project_root = Path(args.project)
        name = args.name or _default_project_name(project_root)
        root = Orchestrator.init_project(project_root, name, args.provider, doc_language=args.doc_language)
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
        try:
            project_root = Path(args.project)
            spec_file = Path(args.spec_file) if args.spec_file else _default_spec_file(project_root)
            orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
            state = orchestrator.run(
                spec_file=spec_file,
                auto_approve=bool(args.auto_approve),
                max_tasks=args.max_tasks,
                skip_validate=bool(args.skip_validate),
                print_agent_output=bool(args.print_agent_output),
                doc_language=args.doc_language,
            )
            print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
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

    parser.error(f"Unsupported command: {args.command}")
    return 2
