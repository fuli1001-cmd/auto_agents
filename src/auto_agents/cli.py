from __future__ import annotations

import argparse
import json
from pathlib import Path

from .orchestrator import Orchestrator
from .validation import validation_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quality-first orchestration for AI-assisted new projects.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Bootstrap a new target project.")
    init_parser.add_argument("--project", required=True, help="Target project directory.")
    init_parser.add_argument("--name", required=True, help="Project name.")
    init_parser.add_argument(
        "--provider",
        default="codex",
        help="Provider kind. Built-in: codex, mock, or a shell-wrapper kind.",
    )

    run_parser = subparsers.add_parser("run", help="Run the orchestration pipeline.")
    run_parser.add_argument("--project", required=True, help="Target project directory.")
    run_parser.add_argument("--idea-file", required=True, help="Path to the initial idea markdown.")
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

    approve_parser = subparsers.add_parser("approve", help="Approve a pending manual gate.")
    approve_parser.add_argument("--project", required=True, help="Target project directory.")
    approve_parser.add_argument("--gate", required=True, help="Gate name to approve.")

    status_parser = subparsers.add_parser("status", help="Show the current orchestrator state.")
    status_parser.add_argument("--project", required=True, help="Target project directory.")

    validate_parser = subparsers.add_parser("validate", help="Validate config, plan, and required docs.")
    validate_parser.add_argument("--project", required=True, help="Target project directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        root = Orchestrator.init_project(Path(args.project), args.name, args.provider)
        print(root)
        return 0

    if args.command == "approve":
        orchestrator = Orchestrator(Path(args.project))
        state = orchestrator.approve(args.gate)
        print(json.dumps(state.to_dict(), indent=2, ensure_ascii=True))
        return 0

    if args.command == "run":
        orchestrator = Orchestrator(Path(args.project))
        state = orchestrator.run(
            idea_file=Path(args.idea_file),
            auto_approve=bool(args.auto_approve),
            max_tasks=args.max_tasks,
        )
        print(json.dumps(state.to_dict(), indent=2, ensure_ascii=True))
        return 0

    if args.command == "status":
        orchestrator = Orchestrator(Path(args.project))
        print(json.dumps(orchestrator.status(), indent=2, ensure_ascii=True))
        return 0

    if args.command == "validate":
        report = validation_report(Path(args.project))
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 0 if report["ok"] else 1

    parser.error(f"Unsupported command: {args.command}")
    return 2
