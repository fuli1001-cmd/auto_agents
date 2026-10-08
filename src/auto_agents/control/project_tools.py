"""Project tools extracted from CLI; no run/session execution or recovery paths."""

from __future__ import annotations
from .arguments import build_parser
import argparse
import functools
import http.server
import json
import os
import shlex
import sys
from pathlib import Path
from ..reporting import print_message as print
from ..config import load_project_config, save_project_config
from ..env import load_dotenv
from ..models import PersistenceTargetConfig
from ..persistence_rebind import rebind_legacy_persistence_decision
from ..persistence_upgrade import parse_decision_policies, upgrade_persistence_contract
from ..prototype_variants import (
    LIVE_VARIANT_STATUSES,
    PrototypeGalleryHandler,
    load_registry,
    registry_variants,
)
from ..run_lock import ProjectRunLock, RunAlreadyActiveError
from ..validation import validate_persistence_config_payload
from ..worker_cluster import (
    create_pairing_invite,
    init_cluster,
    join_cluster,
    load_cluster_state,
)
from ..worker_service import (
    WorkerService,
    lan_workers_cleanup,
    lan_workers_doctor,
    lan_workers_status,
)


def _confirm_prompt(project_root: Path, prompt: str, default: str = "n") -> str:
    if not sys.stdin.isatty():
        return default
    return input(prompt).strip() or default


def _display_path(project_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


def _format_command(*parts: str) -> str:
    return " ".join((shlex.quote(part) for part in parts))


def _load_cli_dotenv() -> None:
    load_dotenv([Path.cwd() / ".env"])


def _serve_prototype_gallery(project_root: Path, host: str, port: int) -> int:
    registry = load_registry(project_root, include_virtual_legacy=True)
    live = registry_variants(registry, statuses=LIVE_VARIANT_STATUSES)
    if not live:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "No live frontend prototype variants are available.",
                },
                indent=2,
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    handler = functools.partial(
        PrototypeGalleryHandler, project_root=project_root, registry=registry
    )
    server = http.server.ThreadingHTTPServer((host, port), handler)
    bound_host, bound_port = server.server_address[:2]
    print(f"Prototype gallery: http://{bound_host}:{bound_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _truthy_environment_flag(values: dict[str, str], name: str) -> bool:
    return str(values.get(name, "")).strip().lower() in {"1", "true", "yes"}


def _configure_persistence_target(args: argparse.Namespace) -> dict:
    project_root = Path(args.project).expanduser().resolve()

    def prompt(text, default=""):
        if not sys.stdin.isatty():
            raise ValueError("Supply the required persistence configuration arguments")
        return input(text).strip() or default

    target_id = (
        str(args.target_id).strip()
        or prompt("Persistence target id (for example local-sqlite): ").strip()
    )
    config = load_project_config(project_root)
    existing = config.persistence.target(target_id)
    environment = (
        str(args.environment).strip()
        or (existing.environment if existing is not None and (not args.replace) else "")
        or prompt("Environment (development/test/production): ").strip()
    )
    kind = (
        str(args.kind).strip()
        or (existing.kind if existing is not None and (not args.replace) else "")
        or prompt("Target kind (local_file/compose_service): ").strip()
    )
    if kind == "local_file":
        path = str(args.path).strip()
        path_env = str(args.path_env).strip()
        if (
            not path
            and (not path_env)
            and (existing is not None)
            and (not args.replace)
        ):
            path = str(existing.locator.get("path", ""))
            path_env = str(existing.locator.get("path_env", ""))
        if not path and (not path_env):
            value = prompt(
                "Project-relative database path, or prefix an env name with '$' (for example .data/app.db): "
            ).strip()
            if value.startswith("$"):
                path_env = value[1:]
            else:
                path = value
        locator = {
            key: value
            for key, value in {"path": path, "path_env": path_env}.items()
            if value
        }
    else:
        compose_file = (
            str(args.compose_file).strip()
            or prompt("Project-relative compose file: ").strip()
        )
        services = list(args.service)
        if not services:
            services = [
                item.strip()
                for item in prompt("Comma-separated database service names: ").split(
                    ","
                )
                if item.strip()
            ]
        locator = {"compose_file": compose_file, "services": services}

    def command_argv(raw: str, label: str) -> list[str]:
        value = str(raw).strip()
        if not value and (not bool(args.auto_approve)):
            value = prompt(f"{label} command (blank when not applicable): ").strip()
        return shlex.split(value) if value else []

    def prior_or(value: list[str], field_name: str) -> list[str]:
        if value or bool(getattr(args, "replace", False)) or existing is None:
            return value
        return list(getattr(existing, field_name))

    target = PersistenceTargetConfig(
        target_id=target_id,
        environment=environment,
        kind=kind,
        locator=locator,
        associated_paths=(
            [str(item) for item in args.associated_path]
            if args.associated_path
            or bool(getattr(args, "replace", False))
            or existing is None
            else list(existing.associated_paths)
        ),
        interface_version=(
            int(args.interface_version)
            if int(args.interface_version) > 0
            else existing.interface_version if existing is not None else 1
        ),
        lifecycle=str(args.lifecycle)
        or (existing.lifecycle if existing is not None else "ready"),
        status_argv=prior_or(
            command_argv(args.status_command, "Status"), "status_argv"
        ),
        migrate_argv=prior_or(
            command_argv(args.migrate_command, "Migrate"), "migrate_argv"
        ),
        apply_argv=prior_or(
            command_argv(args.apply_command, "Legacy migration/apply"), "apply_argv"
        ),
        initialize_argv=prior_or(
            command_argv(args.initialize_command, "Initialize"), "initialize_argv"
        ),
        reset_argv=prior_or(command_argv(args.reset_command, "Reset"), "reset_argv"),
        verify_argv=prior_or(
            command_argv(args.verify_command, "Verify"), "verify_argv"
        ),
        migration_roots=(
            [str(item) for item in args.migration_root]
            if args.migration_root
            or bool(getattr(args, "replace", False))
            or existing is None
            else list(existing.migration_roots)
        ),
        timeout_seconds=int(args.timeout_seconds),
    )
    remaining = [
        item for item in config.persistence.targets if item.target_id != target_id
    ]
    candidate_targets = [*remaining, target]
    errors = validate_persistence_config_payload(
        {"targets": [item.to_dict() for item in candidate_targets]}
    )
    if errors:
        raise ValueError("invalid persistence target: " + "; ".join(errors))
    summary = json.dumps(target.to_dict(), indent=2, ensure_ascii=False)
    if not bool(args.auto_approve):
        answer = prompt(
            f"Register this persistence target?\n{summary}\n(y/n) [n]: ", default="n"
        )
        if answer.strip().lower() not in {"y", "yes"}:
            return {"ok": False, "cancelled": True, "target": target.to_dict()}
    config.persistence.targets = candidate_targets
    save_project_config(project_root, config)
    return {"ok": True, "target": target.to_dict()}


def dispatch(args):
    if args.command == "prompt-eval":
        from ..prompting.evaluate import main as evaluate_prompts

        _load_cli_dotenv()
        try:
            evaluate_prompts(args.eval_args)
        except (OSError, ValueError) as error:
            print(f"prompt evaluation failed: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "persistence-configure":
        try:
            project_root = Path(args.project).expanduser().resolve()
            with ProjectRunLock(project_root):
                payload = _configure_persistence_target(args)
        except (OSError, RuntimeError, ValueError, RunAlreadyActiveError) as error:
            payload = {"ok": False, "error": str(error)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if bool(payload.get("ok")) else 1
    if args.command == "persistence-rebind":
        try:
            project_root = Path(args.project).expanduser().resolve()
            with ProjectRunLock(project_root):
                from .projection import current

                if current(project_root):
                    from .persistence_tools import execute

                    payload = execute(project_root, args)
                else:
                    payload = rebind_legacy_persistence_decision(
                        project_root, decision_id=args.decision, target_ids=args.target
                    )
        except (OSError, RuntimeError, ValueError, RunAlreadyActiveError) as error:
            payload = {"ok": False, "error": str(error)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if bool(payload.get("ok")) else 1
    if args.command == "persistence-upgrade-contract":
        try:
            project_root = Path(args.project).expanduser().resolve()
            with ProjectRunLock(project_root):
                from .projection import current

                if current(project_root):
                    from .persistence_tools import execute

                    payload = execute(project_root, args)
                else:
                    payload = upgrade_persistence_contract(
                        project_root,
                        decision_policies=parse_decision_policies(args.decision_policy),
                        resume_interrupted=bool(args.resume_interrupted),
                    )
        except (OSError, RuntimeError, ValueError, RunAlreadyActiveError) as error:
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
                        host=args.host, port=args.port, ttl_seconds=args.ttl_seconds
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
                    Path(args.project).expanduser().resolve() if args.project else None
                )
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
                "worker is not paired; use --join <pairing-code> or run `auto-agents cluster init`",
                file=sys.stderr,
            )
            return 2
        os.environ["AUTO_AGENTS_WORKER_SLOTS"] = str(args.slots)
        service = WorkerService(bind=args.bind, port=args.port)
        print(
            f"auto_agents worker listening on {args.bind}:{args.port}", file=sys.stderr
        )
        try:
            service.serve_forever()
        except KeyboardInterrupt:
            service.close()
        return 0
    if args.command == "prototype-preview":
        project_root = Path(args.project).expanduser().resolve()
        if args.port < 0 or args.port > 65535:
            parser.error("--port must be between 0 and 65535")
        return _serve_prototype_gallery(project_root, args.host, args.port)
    raise ValueError("Unsupported project tool")


def storage(args):
    """User/worker caches keep their existing ownership-checked storage API."""
    from ..artifact_store import ArtifactStore
    from ..artifact_runtime import maintain

    store = ArtifactStore()
    action = args.storage_action
    scope = (args.scope + ":") if getattr(args, "scope", "user") != "user" else None
    if action == "status":
        payload = store.status(scope)
    elif action == "plan":
        payload = store.plan(scope)
    elif action == "maintain":
        payload = maintain(scope)
    elif action == "clean":
        from ..artifact_cleanup import clean

        payload = clean()
    elif action == "apply":
        payload = store.apply(args.plan)
    elif action == "restore":
        payload = store.restore(args.artifact)
    else:
        store.pin(args.artifact, args.reason if action == "pin" else "")
        payload = {"ok": True}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 3
