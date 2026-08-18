from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import os
import signal
import subprocess
import shlex
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

from .config import (
    architecture_path,
    design_md_path,
    frontend_design_docs_dir,
    frontend_design_lock_path,
    frontend_prototype_dir,
    load_project_config,
    load_run_state,
    project_brief_path,
    requirements_audit_path,
    requirements_trace_path,
    run_path,
    run_state_path,
    save_run_state,
    save_project_config,
    task_plan_path,
)
from .config import supported_provider_kinds
from .env import load_dotenv
from .io_utils import write_json
from .notifications import (
    notify_run_finished,
    notify_run_started,
    notify_self_repair_finished,
    notify_session_finished,
    notify_session_started,
)
from .orchestrator import Orchestrator
from .models import PersistenceTargetConfig
from .frontend_design import load_frontend_design_lock, validate_frontend_design_artifacts
from .foreground_activity import ForegroundActivity
from .git_ops import changed_paths
from .process_supervision import (
    ACTIVE_PROCESSES,
    RunInterruptedError,
    process_group_exists,
    terminate_process_group,
)
from .run_lock import (
    ProjectRunLock,
    RunAlreadyActiveError,
    stop_project_run,
)
from .release_attestation import (
    begin_release_verification,
    complete_release_verification,
    enqueue_release_verification,
)
from .release_worker import ensure_release_worker, run_release_worker
from .self_repair import (
    SELF_REPAIR_DISABLED_ENV,
    AutoAgentsSelfRepairRunner,
    SelfRepairDecision,
    SelfRepairTriageResult,
    adjudicate_auto_agents_error,
    append_self_repair_history,
    auto_agents_repo_root,
    classify_auto_agents_error,
)
from .validation import validate_persistence_config_payload, validation_report
from .worker_cluster import (
    WORKER_API_PORT,
    create_pairing_invite,
    init_cluster,
    join_cluster,
    load_cluster_state,
)
from .worker_service import (
    WorkerService,
    lan_workers_cleanup,
    lan_workers_doctor,
    lan_workers_status,
)


def _default_project_name(project: Path) -> str:
    candidate = project.expanduser().resolve().name
    return candidate or "unnamed-project"


def _default_spec_file(project: Path) -> Path:
    return project / "spec.md"


def _deferred_release_enabled(orchestrator: object) -> bool:
    config = getattr(orchestrator, "config", None)
    gates = getattr(config, "gates", None)
    return getattr(gates, "release_verification_mode", "") == "deferred"


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


def _load_cli_dotenv() -> None:
    load_dotenv([Path.cwd() / ".env"])


SELF_REPAIR_STRICT_ENV = "AUTO_AGENTS_SELF_REPAIR_STRICT"


def _truthy_environment_flag(values: dict[str, str], name: str) -> bool:
    return str(values.get(name, "")).strip().lower() in {"1", "true", "yes"}


def _preflight_automatic_self_repair(args, *, env: Optional[dict[str, str]] = None) -> Optional[int]:
    """Warn early when a run cannot use the automatic self-repair path."""
    if getattr(args, "command", "") != "run":
        return None

    values = os.environ if env is None else env
    if _truthy_environment_flag(values, SELF_REPAIR_DISABLED_ENV):
        return None

    repo_root = auto_agents_repo_root()
    try:
        dirty = changed_paths(repo_root)
    except RuntimeError as error:
        detail = f"could not inspect {repo_root}: {error}"
    else:
        if not dirty:
            return None
        preview = ", ".join(dirty[:8])
        if len(dirty) > 8:
            preview += f", ... ({len(dirty)} paths total)"
        detail = f"working tree is not clean; changed paths: {preview}"

    message = (
        "automatic auto_agents self-repair is unavailable because "
        f"{detail}. Normal run can continue, but an auto_agents-owned failure "
        "cannot be repaired automatically until this repository is clean."
    )
    strict = bool(getattr(args, "strict_self_repair", False)) or _truthy_environment_flag(
        values,
        SELF_REPAIR_STRICT_ENV,
    )
    if strict:
        print(
            json.dumps(
                {"ok": False, "error": f"self-repair preflight failed: {message}"},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 2

    print(f"WARNING: {message}", file=sys.stderr)
    return None


def _configure_persistence_target(args: argparse.Namespace) -> dict:
    project_root = Path(args.project).expanduser().resolve()
    orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
    prompt = orchestrator._prompt_user

    target_id = str(args.target_id).strip() or prompt(
        "Persistence target id (for example local-sqlite): "
    ).strip()
    environment = str(args.environment).strip() or prompt(
        "Environment (development/test/production): "
    ).strip()
    kind = str(args.kind).strip() or prompt(
        "Target kind (local_file/compose_service): "
    ).strip()

    if kind == "local_file":
        path = str(args.path).strip()
        path_env = str(args.path_env).strip()
        if not path and not path_env:
            value = prompt(
                "Project-relative database path, or prefix an env name with '$' (for example .data/app.db): "
            ).strip()
            if value.startswith("$"):
                path_env = value[1:]
            else:
                path = value
        locator = {key: value for key, value in {"path": path, "path_env": path_env}.items() if value}
    else:
        compose_file = str(args.compose_file).strip() or prompt(
            "Project-relative compose file: "
        ).strip()
        services = list(args.service)
        if not services:
            services = [
                item.strip()
                for item in prompt("Comma-separated database service names: ").split(",")
                if item.strip()
            ]
        locator = {"compose_file": compose_file, "services": services}

    def command_argv(raw: str, label: str) -> list[str]:
        value = str(raw).strip()
        if not value and not bool(args.auto_approve):
            value = prompt(f"{label} command (blank when not applicable): ").strip()
        return shlex.split(value) if value else []

    target = PersistenceTargetConfig(
        target_id=target_id,
        environment=environment,
        kind=kind,
        locator=locator,
        associated_paths=[str(item) for item in args.associated_path],
        apply_argv=command_argv(args.apply_command, "Migration/apply"),
        initialize_argv=command_argv(args.initialize_command, "Initialize"),
        reset_argv=command_argv(args.reset_command, "Reset"),
        verify_argv=command_argv(args.verify_command, "Verify"),
        timeout_seconds=int(args.timeout_seconds),
    )
    config = load_project_config(project_root)
    remaining = [item for item in config.persistence.targets if item.target_id != target_id]
    candidate_targets = [*remaining, target]
    errors = validate_persistence_config_payload(
        {"targets": [item.to_dict() for item in candidate_targets]}
    )
    if errors:
        raise ValueError("invalid persistence target: " + "; ".join(errors))

    summary = json.dumps(target.to_dict(), indent=2, ensure_ascii=False)
    if not bool(args.auto_approve):
        answer = prompt(
            f"Register this persistence target?\n{summary}\n(y/n) [n]: ",
            default="n",
        )
        if answer.strip().lower() not in {"y", "yes"}:
            return {"ok": False, "cancelled": True, "target": target.to_dict()}
    config.persistence.targets = candidate_targets
    save_project_config(project_root, config)
    return {"ok": True, "target": target.to_dict()}


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
    elif pending_approval == "prototype":
        prototype_files = [
            design_md_path(project_root),
            frontend_design_docs_dir(project_root) / "selection.md",
            frontend_prototype_dir(project_root) / "index.html",
            frontend_design_lock_path(project_root),
        ]
        key_files.extend(path for path in prototype_files if path.exists())
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
            *(
                [
                    "- Preview prototypes: "
                    + _format_command(
                        "python3", "-m", "auto_agents", "prototype-preview",
                        "--project", str(project_root),
                    )
                ]
                if pending_approval == "prototype"
                else []
            ),
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

    if status == "blocked":
        blocker = (
            dict(state_payload.get("active_blocker", {}))
            if isinstance(state_payload.get("active_blocker", {}), dict)
            else {}
        )
        reason = str(blocker.get("reason") or state_payload.get("last_error", "")).strip()
        lines = [
            f"Run blocked at stage: {current_stage}.",
            *([f"Reason: {reason}"] if reason else []),
            "",
            "Key files to review:",
            *[f"- {item}" for item in key_files],
            "",
            "Next steps:",
            f"- Resolve the reported blocker, then rerun: {run_cmd}",
            f"- Inspect persisted status: {status_cmd}",
        ]
        return "\n".join(lines)

    lines = [
        f"Run finished with status: {status or 'unknown'} (stage: {current_stage}).",
        "",
        "Key files to review:",
        *[f"- {item}" for item in key_files],
        "",
        "Next steps:",
        *([f"- Retry/resume the run: {run_cmd}"] if status == "paused" else []),
        f"- Inspect persisted status: {status_cmd}",
    ]
    return "\n".join(lines)


def _safe_notify(callback, *args, **kwargs) -> None:
    try:
        callback(*args, **kwargs)
    except Exception:
        pass


def _notify_run_failure(project_root: Path, error: object) -> None:
    try:
        state_payload = load_run_state(project_root).to_dict()
    except Exception:
        state_payload = {
            "status": "failed",
            "current_stage": "unknown",
            "last_error": str(error),
        }
    _safe_notify(notify_run_finished, project_root, state_payload, status="failed", error=str(error))


def _notify_run_blocked(project_root: Path, error: object) -> None:
    try:
        state_payload = load_run_state(project_root).to_dict()
    except Exception:
        state_payload = {
            "status": "blocked",
            "current_stage": "unknown",
            "last_error": str(error),
        }
    _safe_notify(
        notify_run_finished,
        project_root,
        state_payload,
        status="blocked",
        error=str(error),
    )


def _mark_run_stopped(project_root: Path, reason: str) -> None:
    try:
        state = load_run_state(project_root)
        state.status = "blocked"
        state.last_error = reason
        state.active_blocker = {
            "owner": "user_input",
            "category": "run_interrupted",
            "reason": reason,
            "fingerprint": "run_interrupted",
            "occurrence_count": 1,
            "resume_attempts": 0,
            "status": "blocked",
        }
        save_run_state(project_root, state)
    except Exception:
        pass


def _saved_run_context(project_root: Path) -> dict[str, object]:
    try:
        state = load_run_state(project_root)
    except Exception:
        return {}
    if state.status not in {"blocked", "paused"} and not state.active_execution_incident_id:
        return {}
    return dict(state.resume_context)


def _apply_saved_run_context(args, project_root: Path) -> Path:
    context = _saved_run_context(project_root)
    spec_text = str(args.spec_file or context.get("spec_file") or "").strip()
    args.spec_file = spec_text or None
    args.auto_approve = bool(args.auto_approve or context.get("auto_approve", False))
    args.allow_dirty_tree = bool(
        args.allow_dirty_tree or context.get("allow_dirty_tree", False)
    )
    if args.max_tasks is None and isinstance(context.get("max_tasks"), int):
        args.max_tasks = int(context["max_tasks"])
    args.skip_validate = bool(args.skip_validate or context.get("skip_validate", False))
    args.print_agent_output = bool(
        args.print_agent_output or context.get("print_agent_output", False)
    )
    args.provider = args.provider or (str(context.get("provider_kind") or "") or None)
    args.doc_language = args.doc_language or (
        str(context.get("doc_language") or "") or None
    )
    return Path(spec_text) if spec_text else _default_spec_file(project_root)


@contextlib.contextmanager
def _run_signal_scope():
    previous = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def handle(signum, _frame):
        ACTIVE_PROCESSES.terminate_all()
        raise RunInterruptedError(signum)

    try:
        for signum in previous:
            signal.signal(signum, handle)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _try_load_run_state(project_root: Path):
    try:
        return load_run_state(project_root)
    except Exception:
        return None


def _session_mode_for_command(command: str) -> str:
    return "provider_resolve" if command == "provider-resolve" else command


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
    _safe_notify(
        notify_session_started,
        project_root,
        command="provider-resolve",
        mode="provider_resolve",
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
        _notify_run_failure(project_root, error)
        print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
        return 1
    if session_state.status != "completed":
        _notify_run_failure(
            project_root,
            (
                "automatic provider-resolve session did not complete; "
                f"session_id={session_state.session_id} status={session_state.status}"
            ),
        )
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
        _notify_run_failure(project_root, resumed_state.last_error or "run failed after automatic provider recovery")
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

    _safe_notify(notify_run_finished, project_root, resumed_state.to_dict())
    print(_render_run_summary(project_root, resumed_state.to_dict()))
    return 0


def _run_command_for_self_repair_resume(args) -> list[str]:
    command = [
        sys.executable,
        str(auto_agents_repo_root() / "auto_agents.py"),
        "run",
        "--project",
        str(args.project),
    ]
    if getattr(args, "spec_file", None):
        command.extend(["--spec-file", str(args.spec_file)])
    if bool(getattr(args, "auto_approve", False)):
        command.append("--auto-approve")
    if bool(getattr(args, "allow_dirty_tree", False)):
        command.append("--allow-dirty-tree")
    if getattr(args, "max_tasks", None) is not None:
        command.extend(["--max-tasks", str(args.max_tasks)])
    if bool(getattr(args, "skip_validate", False)):
        command.append("--skip-validate")
    if bool(getattr(args, "print_agent_output", False)):
        command.append("--print-agent-output")
    if getattr(args, "provider", None):
        command.extend(["--provider", str(args.provider)])
    if getattr(args, "doc_language", None):
        command.extend(["--doc-language", str(args.doc_language)])
    if bool(getattr(args, "no_repo_map", False)):
        command.append("--no-repo-map")
    if bool(getattr(args, "strict_self_repair", False)):
        command.append("--strict-self-repair")
    return command


def _run_self_repair_resume_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    pass_fd: int,
) -> int:
    """Run the repaired CLI under the same bounded process supervision."""
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        pass_fds=(pass_fd,),
        text=True,
        start_new_session=True,
    )
    record = ACTIVE_PROCESSES.register(process, kind="self-repair-resume")
    cleanup_incomplete = False
    try:
        while process.poll() is None:
            time.sleep(0.1)
        return int(process.returncode or 0)
    except BaseException:
        cleanup_incomplete = terminate_process_group(
            process, pgid=record.pgid
        ).cleanup_incomplete
        raise
    finally:
        ACTIVE_PROCESSES.unregister(
            process.pid,
            preserve_if_alive=(cleanup_incomplete or process_group_exists(record.pgid)),
        )


def _auto_repair_auto_agents_and_resume(
    project_root: Path,
    orchestrator: Orchestrator,
    error: object,
    decision: SelfRepairDecision,
    args,
    run_lock: ProjectRunLock,
) -> int:
    orchestrator.record_run_blocker(
        owner="auto_agents",
        category=decision.category or "auto_agents_error",
        reason=decision.reason or str(error),
        fingerprint=decision.fingerprint,
    )
    print(
        "Run hit an auto_agents-owned failure. Starting automatic auto_agents self-repair...",
        file=sys.stderr,
    )
    runner = AutoAgentsSelfRepairRunner(
        orchestrator,
        target_project_root=project_root,
        error=error,
        decision=decision,
        print_agent_output=bool(getattr(args, "print_agent_output", False)),
    )
    result = runner.run()
    _safe_notify(
        notify_self_repair_finished,
        project_root,
        auto_agents_root=runner.repo_root,
        status=result.status,
        reason=str(error),
        commit_sha=result.commit_sha,
        summary=result.summary or result.reason,
        verification=result.verification,
    )
    if not result.ok:
        message = f"automatic auto_agents self-repair failed: {result.reason}"
        orchestrator.record_run_blocker(
            owner="auto_agents",
            category=result.category or decision.category or "self_repair_failed",
            reason=message,
            fingerprint=decision.fingerprint,
        )
        _notify_run_blocked(project_root, message)
        print(json.dumps({"ok": False, "error": message}, indent=2, ensure_ascii=False))
        return 3

    orchestrator.mark_self_repair_applied(result.commit_sha)
    print(
        f"auto_agents self-repair committed {result.commit_sha[:12]}. Resuming run with repaired code...",
        file=sys.stderr,
    )
    return _run_self_repair_resume_process(
        _run_command_for_self_repair_resume(args),
        cwd=auto_agents_repo_root(),
        env=run_lock.inherited_environment(append_self_repair_history(decision)),
        pass_fd=run_lock.fileno,
    )


def _block_terminal_run_error(
    project_root: Path,
    orchestrator: Optional[Orchestrator],
    error: object,
    triage: SelfRepairTriageResult,
) -> None:
    judgment = triage.judgment
    owner = judgment.owner if judgment is not None else "unknown"
    category = (
        triage.decision.category
        or (judgment.category if judgment is not None else "")
        or type(error).__name__.lower()
    )
    reason = str(error).strip() or triage.decision.reason or triage.reason
    if orchestrator is not None and hasattr(orchestrator, "record_run_blocker"):
        orchestrator.record_run_blocker(
            owner=owner,
            category=category,
            reason=reason,
            fingerprint=triage.decision.fingerprint,
        )
        return
    try:
        state = load_run_state(project_root)
        state.status = "blocked"
        state.last_error = reason
        state.active_blocker = {
            "owner": owner,
            "category": category,
            "reason": reason,
            "fingerprint": triage.decision.fingerprint,
            "occurrence_count": 1,
            "resume_attempts": 0,
            "status": "blocked",
        }
        save_run_state(project_root, state)
    except Exception:
        pass


def _triage_terminal_run_error(
    project_root: Path,
    orchestrator: Optional[Orchestrator],
    error: object,
) -> SelfRepairTriageResult:
    state = _try_load_run_state(project_root)
    if orchestrator is None:
        fallback = classify_auto_agents_error(error, state=state)
        return SelfRepairTriageResult(
            decision=fallback,
            source="heuristic_fallback",
            reason="provider triage is unavailable before orchestrator initialization",
            provider_error="orchestrator initialization did not complete",
        )
    result = adjudicate_auto_agents_error(
        orchestrator,
        target_project_root=project_root,
        error=error,
        state=state,
        traceback_text=traceback.format_exc(),
    )
    if state is not None and state.run_id.strip():
        try:
            write_json(
                run_path(project_root, state.run_id) / "outputs" / "self-repair-triage.json",
                result.to_dict(),
            )
        except Exception:
            pass
    judgment = result.judgment
    detail = (
        f" owner={judgment.owner} confidence={judgment.confidence:.2f}"
        if judgment is not None
        else ""
    )
    print(
        f"Self-repair triage source={result.source} eligible={result.decision.eligible}"
        f" category={result.decision.category or '-'}{detail}: {result.reason}",
        file=sys.stderr,
    )
    if result.provider_error:
        print(f"Self-repair triage provider error: {result.provider_error}", file=sys.stderr)
    return result


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
        help="Automatically pass manual gates except the mandatory frontend prototype review.",
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
        "--strict-self-repair",
        action="store_true",
        help=(
            "Fail before starting when automatic self-repair is enabled but the "
            "auto_agents repository is not clean."
        ),
    )

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop the active run and its validated subprocess groups.",
    )
    stop_parser.add_argument("--project", required=True, help="Target project directory.")
    stop_parser.add_argument(
        "--grace-seconds",
        type=float,
        default=10.0,
        help="Seconds to wait after SIGTERM before escalating to SIGKILL.",
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
    reject_parser.add_argument(
        "--reselect-design",
        action="store_true",
        help="For a prototype rejection, select a new catalog DESIGN.md before regenerating pages.",
    )

    preview_parser = subparsers.add_parser(
        "prototype-preview",
        help="Serve the generated static frontend prototype for local review.",
    )
    preview_parser.add_argument("--project", required=True, help="Target project directory.")
    preview_parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to loopback.")
    preview_parser.add_argument("--port", type=int, default=0, help="Bind port. Defaults to an available port.")

    status_parser = subparsers.add_parser("status", help="Show the current orchestrator state.")
    status_parser.add_argument("--project", required=True, help="Target project directory.")

    validate_parser = subparsers.add_parser("validate", help="Validate config, plan, and required docs.")
    validate_parser.add_argument("--project", required=True, help="Target project directory.")

    verify_parser = subparsers.add_parser(
        "verify",
        help="Execute managed affected or release proof attestation.",
    )
    verify_parser.add_argument("--project", required=True, help="Target project directory.")
    verify_parser.add_argument(
        "--level",
        choices=("affected", "release"),
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

    release_worker_parser = subparsers.add_parser(
        "release-worker",
        help="Process deferred release proofs and bounded automatic recovery.",
    )
    release_worker_parser.add_argument("--project", required=True)
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
    fix_parser.add_argument(
        "--full-verify",
        action="store_true",
        help="Bypass incremental gate certificates for this fix session.",
    )
    fix_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve a declared development/test persistence action.",
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
        help="Automatically approve a declared development/test persistence action.",
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
    persistence_parser.add_argument("--apply-command", default="")
    persistence_parser.add_argument("--initialize-command", default="")
    persistence_parser.add_argument("--reset-command", default="")
    persistence_parser.add_argument("--verify-command", default="")
    persistence_parser.add_argument("--timeout-seconds", type=int, default=300)
    persistence_parser.add_argument("--auto-approve", action="store_true")

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _load_cli_dotenv()

    self_repair_preflight_exit = _preflight_automatic_self_repair(args)
    if self_repair_preflight_exit is not None:
        return self_repair_preflight_exit

    if args.command == "persistence-configure":
        try:
            payload = _configure_persistence_target(args)
        except (OSError, RuntimeError, ValueError) as error:
            payload = {"ok": False, "error": str(error)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if bool(payload.get("ok")) else 1

    if args.command == "cluster":
        try:
            if args.cluster_command == "init":
                state = init_cluster(name=args.name)
                payload = {
                    "ok": True,
                    "cluster_id": state.cluster_id,
                    "node_id": state.node_id,
                    "hostname": state.hostname,
                }
            elif args.cluster_command == "pair":
                payload = {
                    "ok": True,
                    "pairing_code": create_pairing_invite(
                        host=args.host,
                        port=args.port,
                        ttl_seconds=args.ttl_seconds,
                    ),
                    "expires_in_seconds": max(30, args.ttl_seconds),
                    "note": "the inviter worker service must be running",
                }
            else:
                state = load_cluster_state()
                payload = {
                    "ok": state is not None,
                    "paired": state is not None,
                    "cluster_id": state.cluster_id if state else "",
                    "node_id": state.node_id if state else "",
                    "hostname": state.hostname if state else "",
                }
        except (OSError, RuntimeError, ValueError) as error:
            payload = {"ok": False, "error": str(error)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if bool(payload.get("ok")) else 1

    if args.command == "workers":
        if args.workers_command == "doctor":
            payload = lan_workers_doctor(
                project_root=(
                    Path(args.project).expanduser().resolve()
                    if args.project
                    else None
                ),
            )
        elif args.workers_command == "status":
            payload = lan_workers_status()
        else:
            payload = lan_workers_cleanup(args.max_age_seconds)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if bool(payload.get("ok")) else 1

    if args.command == "worker":
        if args.join:
            try:
                state = join_cluster(args.join)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"worker pairing failed: {error}", file=sys.stderr)
                return 2
            print(
                json.dumps(
                    {
                        "ok": True,
                        "paired": True,
                        "cluster_id": state.cluster_id,
                        "node_id": state.node_id,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        elif load_cluster_state() is None:
            print(
                "worker is not paired; use --join <pairing-code> or run "
                "`auto-agents cluster init`",
                file=sys.stderr,
            )
            return 2
        os.environ["AUTO_AGENTS_WORKER_SLOTS"] = str(args.slots)
        service = WorkerService(bind=args.bind, port=args.port)
        print(
            f"auto_agents worker listening on {args.bind}:{args.port}",
            file=sys.stderr,
        )
        try:
            service.serve_forever()
        except KeyboardInterrupt:
            service.close()
        return 0

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
        state = orchestrator.reject(
            args.gate,
            args.reason,
            reselect_design=bool(args.reselect_design),
        )
        print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "prototype-preview":
        project_root = Path(args.project).expanduser().resolve()
        if args.port < 0 or args.port > 65535:
            parser.error("--port must be between 0 and 65535")
        lock = load_frontend_design_lock(project_root)
        errors = validate_frontend_design_artifacts(
            project_root,
            lock,
            require_approved=False,
        )
        if errors:
            print(
                json.dumps({"ok": False, "errors": errors}, indent=2, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
        prototype_root = frontend_prototype_dir(project_root)
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(prototype_root),
        )
        server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
        host, port = server.server_address[:2]
        print(f"Prototype preview: http://{host}:{port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.command == "run":
        orchestrator = None
        project_root = Path(args.project)
        run_lock = ProjectRunLock(project_root)
        try:
            run_lock.acquire()
        except RunAlreadyActiveError as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 2
        signal_scope = _run_signal_scope()
        signal_scope.__enter__()
        try:
            spec_file = _apply_saved_run_context(args, project_root)
            orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
            if run_lock.interrupted_snapshot:
                interrupted_state = orchestrator.reconcile_runtime_interruption(
                    run_lock.interrupted_snapshot
                )
                if (
                    interrupted_state.status == "paused"
                    and interrupted_state.pending_approval
                ):
                    print(_render_run_summary(project_root, interrupted_state.to_dict()))
                    return 3
            if getattr(args, "no_repo_map", False):
                orchestrator.config.repo_map.enabled = False
            _safe_notify(notify_run_started, project_root)
            state = orchestrator.run(
                spec_file=spec_file,
                auto_approve=bool(args.auto_approve),
                allow_dirty_tree=bool(args.allow_dirty_tree),
                max_tasks=args.max_tasks,
                skip_validate=bool(args.skip_validate),
                print_agent_output=bool(args.print_agent_output),
                doc_language=args.doc_language,
                provider_kind=args.provider,
                restart_blocked=bool(getattr(args, "restart_blocked", False)),
                full_verify=bool(getattr(args, "full_verify", False)),
            )
            state_payload = state.to_dict()
            state_status = str(state_payload.get("status", ""))
            blocker = (
                dict(state_payload.get("active_blocker", {}))
                if isinstance(state_payload.get("active_blocker", {}), dict)
                else {}
            )
            if state_status == "blocked" and str(
                blocker.get("owner", "")
            ) == "auto_agents":
                decision = SelfRepairDecision(
                    eligible=True,
                    category=str(blocker.get("category", "") or "execution_incident"),
                    reason=str(blocker.get("reason", "") or state_payload.get("last_error", "")),
                    fingerprint=str(blocker.get("fingerprint", "")),
                    repeat_count=int(blocker.get("occurrence_count", 0) or 0),
                )
                return _auto_repair_auto_agents_and_resume(
                    project_root,
                    orchestrator,
                    RuntimeError(str(state_payload.get("last_error", ""))),
                    decision,
                    args,
                    run_lock,
                )
            if state_status == "blocked":
                print(_render_run_summary(project_root, state_payload))
                return 3
            if (
                state_status == "completed"
                and _deferred_release_enabled(orchestrator)
                and not bool(getattr(args, "full_verify", False))
            ):
                enqueue_release_verification(
                    project_root,
                    source=f"run:{state.run_id}",
                    affected_proof_ids=[],
                )
                ensure_release_worker(project_root)
            _safe_notify(notify_run_finished, project_root, state_payload)
            print(_render_run_summary(project_root, state_payload))
            return 0
        except RunInterruptedError as error:
            _mark_run_stopped(project_root, str(error))
            _notify_run_blocked(project_root, error)
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return error.exit_code
        except KeyboardInterrupt:
            reason = "run interrupted by SIGINT"
            ACTIVE_PROCESSES.terminate_all()
            _mark_run_stopped(project_root, reason)
            _notify_run_blocked(project_root, reason)
            print(json.dumps({"ok": False, "error": reason}, indent=2, ensure_ascii=False))
            return 130
        except Exception as error:
            if (
                orchestrator is not None
                and hasattr(orchestrator, "is_provider_research_blocked_error")
                and orchestrator.is_provider_research_blocked_error(str(error))
            ):
                return _auto_resolve_provider_blocker(
                    project_root,
                    orchestrator,
                    print_agent_output=bool(args.print_agent_output),
                )
            project_root = Path(args.project)
            triage = _triage_terminal_run_error(project_root, orchestrator, error)
            decision = triage.decision
            if orchestrator is not None and decision.eligible:
                return _auto_repair_auto_agents_and_resume(
                    project_root,
                    orchestrator,
                    error,
                    decision,
                    args,
                    run_lock,
                )
            _block_terminal_run_error(project_root, orchestrator, error, triage)
            _notify_run_blocked(project_root, error)
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 3
        finally:
            signal_scope.__exit__(None, None, None)
            run_lock.release()

    if args.command == "stop":
        project_root = Path(args.project)
        if args.grace_seconds < 0:
            parser.error("--grace-seconds must be >= 0")
        payload, exit_code = stop_project_run(
            project_root,
            grace_seconds=float(args.grace_seconds),
        )
        if payload.get("status") == "stopped":
            _mark_run_stopped(project_root, "run stopped by user")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return exit_code

    if args.command == "status":
        orchestrator = Orchestrator(Path(args.project))
        print(json.dumps(orchestrator.status(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "validate":
        report = validation_report(Path(args.project))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1

    if args.command == "verify":
        try:
            project_root = Path(args.project).expanduser().resolve()
            changed_path_set = None
            if args.changed_from:
                process = subprocess.run(
                    ["git", "diff", "--name-only", str(args.changed_from)],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                )
                if process.returncode != 0:
                    raise RuntimeError(process.stderr.strip() or "git diff failed")
                changed_path_set = [
                    line.strip() for line in process.stdout.splitlines() if line.strip()
                ]
            orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
            if args.level == "release":
                begin_release_verification(project_root)
            result = orchestrator.run_verification(
                level=args.level,
                changed_path_set=changed_path_set,
                fresh=bool(args.fresh),
            )
            if args.level == "release":
                result["release_attestation"] = complete_release_verification(
                    project_root,
                    result,
                )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if bool(result.get("ok")) else 1
        except (OSError, RuntimeError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1

    if args.command == "release-worker":
        return run_release_worker(
            Path(args.project),
            once=bool(args.once),
            output=sys.stderr,
        )

    if args.command == "attest":
        from .release_jobs import ReleaseJobStore, candidate_identity

        project_root = Path(args.project).expanduser().resolve()
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", str(args.require_release)],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if resolved.returncode != 0:
            payload = {"ok": False, "error": resolved.stderr.strip() or "Git ref not found"}
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1
        required_sha = resolved.stdout.strip()
        latest = ReleaseJobStore(project_root).latest()
        current_id, current_sha = candidate_identity(project_root)
        ok = bool(
            latest.get("status") == "passed"
            and latest.get("candidate_sha") == required_sha
            and (required_sha != current_sha or latest.get("candidate_id") == current_id)
        )
        payload = {
            "ok": ok,
            "required_ref": str(args.require_release),
            "required_sha": required_sha,
            "release": latest,
        }
        if not ok:
            payload["error"] = "required Git candidate is not release_verified"
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if ok else 1

    if args.command == "sync-agent-instructions":
        try:
            orchestrator = Orchestrator(Path(args.project), agent_output_stream=sys.stderr)
            result = orchestrator._ensure_agent_instructions_synced()
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
        foreground = ForegroundActivity(Path(args.project))
        try:
            foreground.acquire()
            from .session import Session

            project_root = Path(args.project)
            orchestrator = Orchestrator(project_root, agent_output_stream=sys.stderr)
            orchestrator._ensure_agent_instructions_synced()
            if getattr(args, "provider", None):
                orchestrator._set_active_provider(args.provider)
            orchestrator._print_agent_output = bool(args.print_agent_output)
            mode = _session_mode_for_command(args.command)
            _safe_notify(
                notify_session_started,
                project_root,
                command=args.command,
                session_id=args.session or "",
                mode=mode,
            )
            session_kwargs = {
                "mode": mode,
                "print_agent_output": bool(args.print_agent_output),
            }
            if bool(getattr(args, "auto_approve", False)):
                session_kwargs["auto_approve"] = True
            if bool(getattr(args, "full_verify", False)):
                session_kwargs["full_verify"] = True
            session = Session(orchestrator, **session_kwargs)
            if args.session:
                state = session.resume(args.session)
            else:
                state = session.offer_resume_or_new()
            _safe_notify(
                notify_session_finished,
                project_root,
                state.to_dict(),
                command=args.command,
            )
            if (
                state.status == "completed"
                and _deferred_release_enabled(orchestrator)
            ):
                ensure_release_worker(project_root)
            print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            project_root = Path(args.project)
            _safe_notify(
                notify_session_finished,
                project_root,
                {
                    "status": "failed",
                    "mode": _session_mode_for_command(args.command),
                },
                command=args.command,
                status="failed",
                error=str(error),
            )
            print(json.dumps({"ok": False, "error": str(error)}, indent=2, ensure_ascii=False))
            return 1
        finally:
            foreground.release()

    parser.error(f"Unsupported command: {args.command}")
    return 2
