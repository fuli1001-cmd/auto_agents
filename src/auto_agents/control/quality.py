"""Domain validation reused by all phases; it cannot schedule or mutate state."""

from pathlib import Path
import json
from .types import ControlError
from .workspace import product_path, git, private_runtime_files
from .types import digest
from .access import file_roles, linked_ancestor, legacy_product_path


def safe_file(workspace, name):
    """Artifact paths must have owned ancestors, including ignored files."""
    root = Path(workspace).resolve()
    path = root / name
    if not product_path(name) or linked_ancestor(root, path):
        raise ControlError(
            "artifact_path", "Artifact leaves its owned workspace: " + str(name)
        )
    if not path.resolve().is_relative_to(root):
        raise ControlError(
            "artifact_path", "Artifact leaves its owned workspace: " + str(name)
        )
    return path


def workspace_guard(workspace):
    """Observe the owned checkout, without letting .gitignore hide mutations."""
    import hashlib
    import os
    from ..repository_guard import capture_repository_guard

    root = Path(workspace)
    observation = capture_repository_guard(root)
    paths = {}
    caches = {
        ".git",
        ".conda",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".vite",
        ".next",
    }
    for current, dirs, files in os.walk(root, followlinks=False):
        links = [name for name in dirs if (Path(current) / name).is_symlink()]
        dirs[:] = [
            name
            for name in dirs
            if name not in caches and not (Path(current) / name).is_symlink()
        ]
        for name in [*files, *links]:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                value = {"link": os.readlink(path)}
            elif path.is_file():
                hashed = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hashed.update(chunk)
                value = {
                    "sha256": hashed.hexdigest(),
                    "executable": bool(path.stat().st_mode & 0o111),
                }
            else:
                continue
            paths[relative] = digest(value)
    # Ignored build directories can contain explicitly tracked source inputs.
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    paths.update(
        {
            name: value
            for name, value in observation["paths"].items()
            if name in tracked
            and name not in paths
            and any(p in caches for p in Path(name).parts)
        }
    )
    return {
        **observation,
        "paths": paths,
        "private_runtime_paths": sorted(private_runtime_files(root)),
        "roles": {name: role.value for name, role in file_roles(root, paths).items()},
    }


def validate_plan(tasks):
    by_id = {t["task_id"]: t for t in tasks}
    visited = set()
    active = set()

    def visit(identity):
        if identity in active:
            raise ControlError(
                "model_output", "Task dependencies contain a cycle", category="model"
            )
        if identity in visited:
            return
        active.add(identity)
        for dependency in by_id[identity].get("depends_on", []):
            visit(dependency)
        active.remove(identity)
        visited.add(identity)

    for identity in by_id:
        visit(identity)
    for task in tasks:
        if not isinstance(task.get("goal"), str) or not task["goal"].strip():
            raise ControlError(
                "model_output", "Task needs a bounded goal", category="model"
            )
        paths = task.get("paths", [])
        if (
            not isinstance(paths, list)
            or not paths
            or any(not isinstance(p, str) or not product_path(p) for p in paths)
        ):
            raise ControlError(
                "model_output", "Task path leaves the workspace", category="model"
            )


def audit_requirements(workspace, task_plan, *, required=False, trace_payload=None):
    path = Path(workspace) / ".auto-agents/docs/requirements_trace.json"
    if trace_payload is None and not path.exists():
        if required:
            raise ControlError(
                "requirements_audit",
                "A structured requirements trace is required",
                category="model",
            )
        return {
            "required": False,
            "reason": "Retained work predates structured control contracts",
        }
    from ..requirements import (
        validate_requirements_trace_payload,
        validate_task_requirement_coverage,
    )
    from ..models import TaskSpec

    trace = trace_payload if trace_payload is not None else json.loads(path.read_text())
    errors = validate_requirements_trace_payload(trace)
    tasks = [
        TaskSpec.from_dict(
            {
                **task,
                "title": task.get("goal", task.get("title", "")),
                "verification_refs": [
                    ref
                    for spec in task.get("checks", [])
                    for ref in spec.get("targets", [])
                ],
            }
        )
        for task in task_plan
    ]
    errors.extend(validate_task_requirement_coverage({"tasks": task_plan}, trace))
    if errors:
        raise ControlError(
            "requirements_audit",
            "Requirements coverage is incomplete",
            category="model",
            details={"errors": errors},
        )
    return {"ok": True, "requirements": len(trace.get("requirements", []))}


def artifact_hashes(workspace, paths):
    hashes = {}
    for name in paths:
        path = safe_file(workspace, name)
        if not path.is_file():
            raise ControlError(
                "approval_artifact",
                "Approval requires actual workspace files",
                category="state",
            )
        import hashlib

        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def source_seal(workspace, outputs=(), *, legacy=False):
    observed = workspace_guard(Path(workspace))
    tracked = set(git(workspace, "ls-files", "-z").split("\0"))
    return digest(
        {
            name: value
            for name, value in observed["paths"].items()
            if (legacy_product_path(name) if legacy else product_path(name))
            and name not in set(outputs)
            and not (
                legacy
                and any(
                    p in {".next", ".conda", ".venv", "node_modules"}
                    for p in Path(name).parts
                )
            )
            and (legacy or observed["roles"].get(name) != "runtime")
            and (name in tracked or Path(name).parts[0] not in {".tmp", ".tmp-tests"})
        }
    )


from .policy import STAGES

STAGE_PATHS = {name: spec.writes for name, spec in STAGES.items() if spec.writes}


def stage_writes(before, after, phase, scope=()):
    from ..repository_guard import changed_guard_paths

    changes = changed_guard_paths(before, after)
    allowed = scope or STAGE_PATHS.get(phase)
    runtime = set()
    if phase == "acceptance":
        # New, changed and deleted private data are allowed. Tracked files and
        # links outside private runtime remain subject to the ordinary guard.
        old = set(before.get("private_runtime_paths", ()))
        new = set(after.get("private_runtime_paths", ()))
        runtime = {
            p
            for p in old | new
            if (p not in before["paths"] or p in old)
            and (p not in after["paths"] or p in new)
        }
    if "<HEAD>" in changes or "<index>" in changes:
        raise ControlError(
            "worker_git",
            "Only the controller may change Git checkpoints or indexes",
            category="model",
        )
    protected = [
        p
        for p in changes
        if p not in {"<HEAD>", "<index>"}
        and p not in runtime
        and (not product_path(p) or after.get("roles", {}).get(p) == "protected")
    ]
    if protected:
        raise ControlError(
            "stage_scope",
            "Worker changed protected runtime or operator paths",
            category="model",
            details={"paths": protected},
        )
    if allowed is not None:
        invalid = [
            p
            for p in changes
            if p not in runtime
            and product_path(p)
            and not any(
                p == s.rstrip("/") or p.startswith(s.rstrip("/") + "/") for s in allowed
            )
        ]
        if invalid:
            raise ControlError(
                "stage_scope",
                "Worker changed paths outside the current phase",
                category="model",
                details={"paths": invalid},
            )


def verification_inputs(workspace, specs):
    """Freeze the exact launch inputs, including untracked temporary tests."""
    root = Path(workspace)
    names = set()
    for spec in specs:
        from .proof import launches

        targets = spec.targets or tuple(
            t for invocation in launches(spec.command) for t in invocation["targets"]
        )
        for selector in targets:
            pattern = selector.split("::", 1)[0]
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ControlError(
                    "verification_spec", "Verification input leaves its workspace"
                )
            for path in root.glob(pattern):
                if path.is_dir():
                    names.update(
                        p.relative_to(root).as_posix()
                        for p in path.rglob("*")
                        if p.is_file()
                        and "__pycache__" not in p.parts
                        and p.suffix
                        in {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
                    )
                elif path.is_file():
                    names.add(path.relative_to(root).as_posix())
    return artifact_hashes(workspace, sorted(names))


def verify_approved(workspace, approved):
    for gate, receipt in approved.items():
        if artifact_hashes(workspace, list(receipt["hashes"])) != receipt["hashes"]:
            raise ControlError(
                "approved_contract",
                "Worker changed the approved " + gate + " artifacts",
                category="model",
            )


def validate_persistence(context, candidate, change):
    from ..persistence import (
        detect_persistence_schema_changes,
        persistence_storage_transition,
    )

    delta = git(
        context.workspace_root,
        "diff",
        "--unified=0",
        candidate["source"],
        candidate["revision"],
    )
    findings = detect_persistence_schema_changes(
        context.workspace_root, diff_text=delta
    )
    if findings and not change:
        raise ControlError(
            "persistence_contract",
            "Schema changes require a declared persistence decision",
            category="model",
            details={"findings": [f.to_dict() for f in findings]},
        )
    if not change:
        return None
    transition = persistence_storage_transition(change)
    if transition not in {"none", "initialize", "migrate", "rebuild"}:
        raise ControlError(
            "persistence_contract", "Invalid persistence transition", category="model"
        )
    if transition == "none" and findings:
        raise ControlError(
            "persistence_contract",
            "Persistence decision omits detected schema changes",
            category="model",
        )
    if transition != "none" and not change.get("target_ids"):
        raise ControlError(
            "persistence_contract",
            "Declare persistence target_ids from operator configuration",
            category="model",
        )
    # Existing migration bytes are immutable; only append new migrations.
    changed = set(candidate["paths"])
    for name in changed:
        if any(p in {"migrations", "migration"} for p in Path(name).parts):
            import subprocess

            if (
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(context.workspace_root),
                        "cat-file",
                        "-e",
                        candidate["source"] + ":" + name,
                    ],
                    capture_output=True,
                ).returncode
                == 0
            ):
                raise ControlError(
                    "immutable_migration",
                    "Existing migration modified: " + name,
                    category="model",
                )
    return {
        "change": dict(change),
        "candidate": candidate["tree"],
        "fingerprint": digest({"change": change, "candidate": candidate["tree"]}),
    }


def validate_references(proposal):
    refs = proposal.get("references", [])
    if not refs:
        raise ControlError(
            "model_output",
            "Provider research requires primary-source references",
            category="model",
        )
    if not isinstance(refs, list) or any(
        not isinstance(ref, dict)
        or not str(ref.get("url", "")).startswith("https://")
        or not ref.get("claim")
        for ref in refs
    ):
        raise ControlError(
            "model_output",
            "Provider references require a primary-source URL and supported claim",
            category="model",
        )


def verify_review(proposal, context, candidate):
    if proposal.get("decision") != "pass":
        raise ControlError(
            "review_failed",
            proposal.get("reason", "Review rejected"),
            category="verification",
        )
    if (
        requires_test_assessment(context, candidate)
        and proposal.get("test_changes_valid") is not True
    ):
        raise ControlError(
            "proof_review",
            "Test changes require explicit independent assessment",
            category="verification",
        )


def requires_test_assessment(context, candidate):
    paths = candidate.get("paths", []) if candidate else []
    targets = {
        t.split("::", 1)[0] for spec in context.contract.checks for t in spec.targets
    }
    return any(
        "tests" in Path(p).parts
        or Path(p).name.startswith("test_")
        or ".test." in Path(p).name
        or ".spec." in Path(p).name
        or p in targets
        for p in paths
    )
