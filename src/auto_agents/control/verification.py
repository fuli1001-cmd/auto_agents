"""Trusted configured gate executors, independent of execution controllers."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from .types import digest


@contextmanager
def executor(context, config, specs, *, fresh=False):
    if not config.gates.isolation.enabled:
        yield None
        return
    from ..gate_execution import LocalGatePlanExecutor
    from ..gates import (
        GateCommandMetadata,
        command_from_verification_step,
        resolve_gate_plan_from_verification_steps,
    )
    from ..workers import gate_environment_fingerprint
    from ..operator_inputs import OperatorInputStore
    from .store import Store
    from . import cleanup

    root = (
        context.control_root / ".auto-agents/state/owned/verification" / context.work_id
    )
    root.mkdir(parents=True, exist_ok=True)
    store = Store(context.control_root)
    cleanup.register(
        store, root, "cache", context.work_id, references=[context.work_id]
    )
    gates = replace(
        config.gates,
        isolation=replace(
            config.gates.isolation, worktree_root=str(root / "worktrees")
        ),
    )
    steps = [
        s
        for s in gates.steps
        if command_from_verification_step(s, project_root=context.workspace_root)
        in {spec.command for spec in specs}
    ]
    metadata = (
        resolve_gate_plan_from_verification_steps(
            steps, context.workspace_root
        ).metadata
        if steps
        else {}
    )
    for spec in specs:
        metadata[spec.command] = replace(
            metadata.get(spec.command, GateCommandMetadata()),
            artifact_globs=list(spec.outputs),
            cache_scope=(
                "run_context" if spec.cache_scope == "candidate" else spec.cache_scope
            ),
            result_cache_scope=spec.result_cache_scope,
        )
    bindings = [b for step in steps for b in step.operator_input_bindings]
    supplied, missing = OperatorInputStore(context.control_root).environment(bindings)
    if missing:
        from .types import ControlError

        raise ControlError(
            "operator_input",
            "Verification needs declared operator inputs",
            category="operator",
            details={"missing": missing},
        )
    environment = {**context.environment, **supplied}
    identity = gate_environment_fingerprint(
        isolation_mode=gates.isolation.mode,
        environment_id=gates.distributed.mode,
        distributed=gates.distributed.enabled,
        extra_denylist=gates.distributed.extra_environment_denylist,
        project_root=context.workspace_root,
        environment=environment,
    )
    common = dict(
        environment_fingerprint=identity,
        result_context_fingerprint=digest(
            [
                context.contract.identity,
                OperatorInputStore(context.control_root).fingerprint(),
            ]
        ),
        environment_overrides=supplied,
        use_result_cache=not fresh,
    )
    import os

    if (
        gates.distributed.enabled
        and os.environ.get("AUTO_AGENTS_OFFLINE_RESUME") != "1"
    ):
        from ..distributed_gates import DistributedGatePlanExecutor

        selected = DistributedGatePlanExecutor(
            context.workspace_root, gates, metadata, **common
        )
    else:
        selected = LocalGatePlanExecutor(
            context.workspace_root,
            gates,
            metadata,
            cache_path=root / "cache.sqlite3",
            execution_environment=environment,
            **common,
        )
    if context.operation_id:
        diagnostics = (
            context.control_root
            / ".auto-agents/state/owned/operations"
            / context.operation_id
        )
        diagnostics.mkdir(parents=True, exist_ok=True)
        cleanup.register(
            store, diagnostics, "scratch", context.work_id, references=[context.work_id]
        )
        getattr(selected, "local", selected).diagnostic_root = diagnostics
    with selected:
        yield selected
