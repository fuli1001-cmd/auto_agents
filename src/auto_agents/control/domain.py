"""Domain inputs and validators; they never select execution or recovery paths."""

from pathlib import Path
import json
from .types import ControlError, digest
from . import cleanup


def context_inputs(context, phase, config, store):
    result = {
        "document_language": config.docs.language,
        "frontend_design": config.frontend_design.to_dict(),
    }
    root = store.project / ".auto-agents/state/owned/domain" / context.work_id
    root.mkdir(parents=True, exist_ok=True)
    cleanup.register(
        store, root, "cache", context.work_id, references=[context.work_id]
    )
    if phase in {"research", "provider_research"}:
        from ..requirements import (
            load_requirements_trace,
            load_provider_references_lock,
            external_doc_requirements,
            provider_reference_paths,
        )
        from ..provider_reference_review import prepare, INSTRUCTION

        trace = load_requirements_trace(context.workspace_root)
        lock = load_provider_references_lock(context.workspace_root)
        references = sorted(
            {
                path
                for requirement in external_doc_requirements(trace)
                for path in provider_reference_paths(requirement)
            }
        )
        result["provider_review_context"] = prepare(
            context.workspace_root, trace, lock, references
        )
        result["provider_review_policy"] = INSTRUCTION
    if config.repo_map.enabled and phase in {
        "classify",
        "diagnose",
        "implement",
        "review",
    }:
        from ..repomap import RepoMapBuilder
        from ..repomap.cache import RepoMapCache
        from ..models import TaskSpec

        task = TaskSpec(
            task_id=context.work_id,
            title=context.contract.goal,
            description=context.contract.goal,
            verification_refs=[
                ref for spec in context.contract.checks for ref in spec.targets
            ],
        )
        mapping = RepoMapBuilder(
            context.workspace_root,
            config.repo_map,
            cache=RepoMapCache(
                context.workspace_root, cache_path=root / "repomap.json"
            ),
        ).build(task)
        result["repository_map"] = mapping.text
    if phase == "prototype" and config.frontend_design.mode != "off":
        from ..frontend_design import (
            AwesomeDesignCatalogClient,
            FrontendDesignUnavailable,
            user_design_assets,
        )

        trace = context.workspace_root / ".auto-agents/docs/requirements_trace.json"
        payload = json.loads(trace.read_text()) if trace.exists() else {}
        assets = user_design_assets(
            context.workspace_root, payload, spec_text=context.contract.goal
        )
        if context.contract.inputs.get("variant_only"):
            result["user_design_assets"] = assets or (
                ["DESIGN.md"]
                if (context.workspace_root / "DESIGN.md").is_file()
                else []
            )
            return result
        result["user_design_assets"] = assets
        if not assets:
            settings = config.frontend_design
            client = AwesomeDesignCatalogClient(
                root,
                repository=settings.catalog_repository,
                requested_ref=settings.catalog_ref,
                timeout_seconds=settings.network_timeout_seconds,
            )
            try:
                catalog = client.load()
            except FrontendDesignUnavailable as error:
                raise ControlError(
                    "design_reference", str(error), category="environment"
                ) from error
            result["design_catalog"] = {
                "repository": catalog.repository,
                "commit": catalog.commit_sha,
                "root": str(catalog.root),
                "entries": [entry.to_dict() for entry in catalog.entries],
            }
    return result


def validate_provider_documents(context, proposal, store, operation):
    """Domain proof validation never chooses a different business route."""
    from ..requirements import (
        load_requirements_trace,
        load_provider_references_lock,
        external_doc_requirements,
        provider_reference_paths,
        provider_reference_effective_status,
        stamp_provider_reference_consumer_hashes,
        write_provider_reference_lock,
    )
    from ..provider_contract import (
        validate_provider_reference_v2,
        provider_reference_lock_entry,
    )
    from ..provider_reference_review import validate, finish
    from .quality import safe_file, source_seal

    trace = load_requirements_trace(context.workspace_root)
    lock = load_provider_references_lock(context.workspace_root)
    required = sorted(
        {
            path
            for requirement in external_doc_requirements(trace)
            for path in provider_reference_paths(requirement)
        }
    )
    retained = next(
        o for o in store.operations(context.work_id) if o["id"] == operation
    )
    review = (
        (retained.get("inputs") or {})
        .get("inputs", {})
        .get("provider_review_context", {})
    )
    errors = validate(lock, trace, review)
    refreshed = set(proposal.get("artifacts", []))
    for reference in required:
        entry = provider_reference_lock_entry(lock, reference)
        if provider_reference_effective_status(lock, trace, reference) not in {
            "verified",
            "assumption_approved",
        }:
            # A new entry receives its controller-owned consumer hash below;
            # verified status still needs the actual reference and v2 contract.
            if not entry or entry.get("status") not in {
                "verified",
                "assumption_approved",
            }:
                errors.append(reference + ": required protocol evidence is unresolved")
        if (
            reference in refreshed
            or not entry
            or int(entry.get("contract_version", 0)) >= 2
        ):
            errors.extend(
                reference + ": " + reason
                for reason in validate_provider_reference_v2(
                    safe_file(context.workspace_root, reference), entry
                )
            )
    if proposal.get("not_required") and required:
        errors.append(
            "Active requirements need provider references; this stage cannot be skipped"
        )
    if errors:
        raise ControlError(
            "provider_contract",
            "Provider protocol evidence is incomplete",
            category="model",
            details={"errors": errors},
        )
    if required:
        lock = finish(context.workspace_root, lock, review)
        lock, _ = stamp_provider_reference_consumer_hashes(
            lock, trace, reference_paths=required
        )
        write_provider_reference_lock(context.workspace_root, lock)
        store.derive_receipt(
            operation,
            {
                **retained["result"],
                "workspace_hash": source_seal(context.workspace_root),
            },
        )


def trace_transition(context, before, after):
    if before == after:
        return
    from ..requirements import (
        validate_requirements_trace_payload,
        validate_requirement_contract_transitions,
    )

    previous = json.loads(before) if before else {"version": 1, "requirements": []}
    current = json.loads(after)
    errors = validate_requirements_trace_payload(current)
    errors += validate_requirement_contract_transitions(previous, current)
    if errors:
        raise ControlError(
            "requirement_contract",
            "Requirements changed incompatibly",
            category="model",
            details={"errors": errors},
        )


def validate_prototype(context, proposal, config, store, operation):
    from ..frontend_design import (
        validate_prototype_manifest,
        CatalogSnapshot,
        CatalogEntry,
        validate_catalog_selection,
        frontend_design_artifact_hashes,
    )
    from .quality import safe_file, artifact_hashes
    import shutil

    prefix = ".auto-agents/docs/frontend_prototype"
    if context.contract.inputs.get("variant_only"):
        prefix += "_variants/" + context.contract.inputs["variant_only"]
    root = safe_file(context.workspace_root, prefix)
    manifest_path = safe_file(context.workspace_root, prefix + "/manifest.json")
    if not manifest_path.is_file():
        raise ControlError(
            "prototype_manifest",
            "A static package manifest is required",
            category="model",
        )
    manifest = json.loads(manifest_path.read_text())
    errors = validate_prototype_manifest(
        context.workspace_root,
        manifest,
        max_pages=config.frontend_design.max_pages,
        prototype_root=root,
    )
    if errors:
        raise ControlError(
            "prototype_manifest",
            "Prototype package is invalid",
            category="model",
            details={"errors": errors},
        )
    actual = [
        p.relative_to(context.workspace_root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
    ]
    proposal = {
        **proposal,
        "artifacts": sorted(set(proposal["artifacts"]) | set(actual)),
    }
    retained = next(
        o for o in store.operations(context.work_id) if o["id"] == operation
    )
    inputs = (retained.get("inputs") or {}).get("inputs", {})
    catalog = inputs.get("design_catalog")
    source = {"kind": "user", "refs": inputs.get("user_design_assets", [])}
    if catalog:
        snapshot = CatalogSnapshot(
            catalog["repository"],
            "",
            catalog["commit"],
            Path(catalog["root"]),
            tuple(CatalogEntry(**row) for row in catalog["entries"]),
            True,
        )
        try:
            entry, candidates = validate_catalog_selection(
                proposal.get("selection"), snapshot
            )
        except ValueError as error:
            raise ControlError(
                "design_selection", str(error), category="model"
            ) from error
        upstream = (snapshot.root / entry.design_path).resolve()
        if (
            not upstream.is_relative_to(snapshot.root.resolve())
            or not upstream.is_file()
        ):
            raise ControlError(
                "design_reference",
                "Upstream design bytes are unavailable",
                category="environment",
            )
        design = context.workspace_root / (
            prefix + "/DESIGN.md"
            if context.contract.inputs.get("variant_only")
            else "DESIGN.md"
        )
        shutil.copy2(upstream, design)
        license_path = context.workspace_root / (
            prefix + "/LICENSE"
            if context.contract.inputs.get("variant_only")
            else ".auto-agents/docs/frontend_design/LICENSE"
        )
        license_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot.root / "LICENSE", license_path)
        source = {
            "kind": "awesome-design-md",
            "repository": snapshot.repository,
            "commit_sha": snapshot.commit_sha,
            "slug": entry.slug,
            "content_sha256": artifact_hashes(
                context.workspace_root,
                [design.relative_to(context.workspace_root).as_posix()],
            )[design.relative_to(context.workspace_root).as_posix()],
            "license_path": license_path.relative_to(context.workspace_root).as_posix(),
        }
        proposal["artifacts"] += [
            design.relative_to(context.workspace_root).as_posix(),
            source["license_path"],
        ]
    if not context.contract.inputs.get("variant_only"):
        lock_path = (
            context.workspace_root / ".auto-agents/docs/frontend_design.lock.json"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = {
            "version": 1,
            "status": "pending_approval",
            "source": source,
            "candidates": proposal.get("selection", {}).get("candidates", []),
            "design_path": (
                "DESIGN.md" if (context.workspace_root / "DESIGN.md").is_file() else ""
            ),
            "prototype": {"manifest_ref": prefix + "/manifest.json", **manifest},
            "artifact_sha256": frontend_design_artifact_hashes(context.workspace_root),
        }
        lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2))
        proposal["artifacts"].append(
            lock_path.relative_to(context.workspace_root).as_posix()
        )
    # A confirmed provider receipt remains replayable after the controller
    # derives its checked manifest/lock. Preserve the worker's original seal.
    from .quality import source_seal

    result = retained["result"]
    store.derive_receipt(
        operation,
        {
            **result,
            "worker_workspace_hash": result.get(
                "worker_workspace_hash", result.get("workspace_hash")
            ),
            "workspace_hash": source_seal(context.workspace_root),
        },
    )
    return proposal
