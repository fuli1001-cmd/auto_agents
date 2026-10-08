"""Optional release checks are ordinary work items, not another repair engine."""

from pathlib import Path
from .types import ControlError, VerificationSpec
from .store import Store
from .engine import Engine
from .effects import compile_checks
from .workspace import git


def enqueue(engine, completed):
    settings = engine.config.gates
    contract = engine.store.contract(completed["contract"])
    if (
        settings.release_verification_mode != "deferred"
        or not settings.release_worker.enabled
        or contract.inputs.get("force_release")
    ):
        return None
    revision = completed["result"]["delivery"]["revision"]
    existing = [
        w
        for w in engine.store.works()
        if engine.store.contract(w["contract"]).inputs.get("release_revision")
        == revision
    ]
    if existing:
        return existing[-1]
    specs = configured_checks(engine.config, engine.project, level="release")
    if not specs:
        return None
    source = engine.workspaces.snapshot(
        allow_dirty=True, extra_paths=[p for s in specs for p in s.outputs]
    )
    # One immutable verification node per delivered revision. No background
    # model repair, candidate editing, goal expansion, or new call budget.
    work = engine.store.create_workflow(
        "run",
        "Release proof for " + revision,
        source,
        checks=specs,
        inputs={
            "release_only": True,
            "release_revision": revision,
            "release_full": True,
        },
        node_limit=1,
    )
    work = engine.store.transition(work, "RUNNING", phase="verify")
    work = engine.store.transition(work, "READY")
    engine.store.event(
        completed["id"],
        "release_enqueued",
        {"work_id": work["id"], "revision": revision},
    )
    return work


def ensure_worker(project):
    import os
    import subprocess
    import sys

    store = Store(project)
    from ..config import load_project_config

    settings = load_project_config(project).gates.release_worker
    if not settings.enabled or not settings.auto_start:
        return False
    pending = [
        w
        for w in store.works()
        if store.contract(w["contract"]).inputs.get("release_only")
        and w["status"] == "READY"
    ]
    if not pending:
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith("AUTO_AGENTS_RUN_")}
    env.update(
        AUTO_AGENTS_NO_SUPERVISOR="1",
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
    )
    # The worker starts after the foreground lock is released. Once idle, it
    # claims the same project lock and runs the same controller's check phase.
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "auto_agents",
            "release-worker",
            "--project",
            str(project),
            "--wait",
        ],
        cwd="/tmp",
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True


def configured_checks(
    config, workspace, *, level="release", tests=(), changed_from=None
):
    from ..verification_selection import select_verification_steps
    from ..gates import command_from_verification_step
    from ..managed_verification import selected_tests
    from dataclasses import replace

    if tests:
        targets = selected_tests(workspace, tests)
        steps = []
        for target in targets:
            owners = [
                s
                for s in config.gates.steps
                if s.runner in {"pytest", "vitest"}
                and (
                    not s.targets
                    or any(target.split("::")[0] == t.split("::")[0] for t in s.targets)
                )
            ]
            if not owners:
                raise ControlError(
                    "proof_owner", "Test target has no configured runner: " + target
                )
            steps.append(replace(owners[0], targets=[target]))
    else:
        changed = git(
            workspace, "diff", "--name-only", changed_from or "HEAD"
        ).splitlines()
        steps = select_verification_steps(
            config.gates.steps,
            workspace,
            config.gates,
            level=level,
            changed_paths=changed,
        ).steps
    specs = []
    for step in steps:
        command = command_from_verification_step(step, project_root=workspace)
        purpose = (
            "behavior"
            if step.kind == "test"
            else "artifact" if step.kind in {"artifact", "integrity"} else "environment"
        )
        specs.extend(
            compile_checks(
                [
                    {
                        "command": command,
                        "purpose": purpose,
                        "targets": step.targets,
                        "cache_scope": step.cache_scope,
                        "result_cache_scope": step.result_cache_scope,
                        "outputs": [
                            str(p.relative_to(workspace))
                            for pattern in step.artifact_globs
                            for p in Path(workspace).glob(pattern)
                            if p.is_file()
                        ],
                    }
                ],
                workspace,
            )
        )
    specs.extend(compile_checks(config.gates.commands, workspace))
    return tuple(specs)


def verify(engine, *, work_id=None, level="release", tests=(), changed_from=None):
    source = engine.workspaces.snapshot(allow_dirty=True)
    specs = configured_checks(
        engine.config,
        engine.project,
        level=level,
        tests=tests,
        changed_from=changed_from,
    )
    if not specs and work_id:
        specs = engine.store.contract(engine.store.work(work_id)["contract"]).checks
    if not specs:
        raise ControlError(
            "verification_missing",
            "Configure checks or select a retained verified work item",
        )
    work = engine.store.create_workflow(
        "run",
        "Verify admitted " + level + " source",
        source,
        checks=specs,
        inputs={"release_only": True, "release_full": level == "release"},
        authorization={"auto_approve": True},
        node_limit=1,
    )
    work = engine.store.transition(work, "RUNNING", phase="verify")
    engine.store.transition(work, "READY")
    return engine.drive(work["id"])


def worker(project, *, once=False):
    from ..config import load_project_config

    store = Store(project)
    engine = Engine(
        project, store, load_project_config(project), print_fn=lambda *a: None
    )
    pending = [
        w
        for w in store.works()
        if store.contract(w["contract"]).inputs.get("release_only")
        and w["status"] in {"READY", "RUNNING"}
    ]
    for work in pending[:1] if once else pending:
        result = engine.drive(work["id"])
        if result["status"] == "BLOCKED":
            return 3
    return 0


def attest(store, reference):
    revision = git(store.project, "rev-parse", reference)
    from .workspace import product_path

    if any(
        product_path(line[3:])
        for line in git(
            store.project, "status", "--porcelain", "--untracked-files=all"
        ).splitlines()
    ):
        return {
            "ok": False,
            "required_sha": revision,
            "work_ids": [],
            "reason": "Repository has unverified changes",
        }
    matches = [
        w
        for w in store.works()
        if w["status"] == "COMPLETED"
        and (
            w["result"].get("delivery", {}).get("revision") == revision
            or w["result"].get("verified_revision") == revision
        )
    ]
    matches = [
        w
        for w in matches
        if store.contract(w["contract"]).inputs.get("release_full")
        or store.contract(w["contract"]).inputs.get("force_release")
    ]
    return {
        "ok": bool(matches),
        "required_sha": revision,
        "work_ids": [w["id"] for w in matches],
    }
