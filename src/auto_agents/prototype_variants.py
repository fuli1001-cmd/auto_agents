from __future__ import annotations

import copy
import hashlib
import html
import json
import mimetypes
import re
import shutil
import uuid
from http.server import SimpleHTTPRequestHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .config import (
    design_md_path,
    frontend_design_docs_dir,
    frontend_design_lock_path,
    frontend_prototype_dir,
    frontend_prototype_variants_dir,
    frontend_prototype_variants_registry_path,
    requirements_trace_path,
)
from .frontend_design import (
    frontend_design_artifact_hashes,
    load_frontend_design_lock,
    selected_surface_specs,
    sha256_file,
    validate_prototype_manifest,
)
from .io_utils import read_json, write_json


REGISTRY_VERSION = 1
LIVE_VARIANT_STATUSES = {"candidate", "approved"}
VARIANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def variant_dir(project_root: Path, variant_id: str) -> Path:
    validate_variant_id(variant_id)
    return frontend_prototype_variants_dir(project_root) / variant_id


def variant_design_path(project_root: Path, variant_id: str) -> Path:
    return variant_dir(project_root, variant_id) / "DESIGN.md"


def variant_design_docs_dir(project_root: Path, variant_id: str) -> Path:
    return variant_dir(project_root, variant_id) / "frontend_design"


def variant_prototype_dir(project_root: Path, variant_id: str) -> Path:
    return variant_dir(project_root, variant_id) / "frontend_prototype"


def validate_variant_id(variant_id: str) -> str:
    value = str(variant_id).strip()
    if not VARIANT_ID_PATTERN.fullmatch(value):
        raise ValueError(f"invalid frontend prototype variant id: {variant_id}")
    return value


def new_variant_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"proto-{stamp}-{uuid.uuid4().hex[:6]}"


def empty_registry() -> Dict[str, object]:
    return {"version": REGISTRY_VERSION, "approved_variant_id": "", "variants": []}


def load_registry(project_root: Path, *, include_virtual_legacy: bool = True) -> Dict[str, object]:
    payload = read_json(frontend_prototype_variants_registry_path(project_root), default=None)
    if isinstance(payload, dict):
        normalized = copy.deepcopy(payload)
        normalized.setdefault("version", REGISTRY_VERSION)
        normalized.setdefault("approved_variant_id", "")
        normalized.setdefault("variants", [])
        return normalized
    if include_virtual_legacy:
        legacy = _legacy_variant_entry(project_root, virtual=True)
        if legacy is not None:
            registry = empty_registry()
            registry["variants"] = [legacy]
            if legacy["status"] == "approved":
                registry["approved_variant_id"] = legacy["id"]
            return registry
    return empty_registry()


def save_registry(project_root: Path, registry: Mapping[str, object]) -> None:
    write_json(frontend_prototype_variants_registry_path(project_root), dict(registry))


def registry_variants(
    registry: Mapping[str, object],
    *,
    statuses: Optional[Iterable[str]] = None,
) -> List[Dict[str, object]]:
    allowed = set(statuses) if statuses is not None else None
    result: List[Dict[str, object]] = []
    for raw in registry.get("variants", []) or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if allowed is not None and str(item.get("status", "")) not in allowed:
            continue
        result.append(item)
    return result


def find_variant(registry: Mapping[str, object], variant_id: str) -> Dict[str, object]:
    wanted = validate_variant_id(variant_id)
    for item in registry_variants(registry):
        if str(item.get("id", "")) == wanted:
            return item
    raise ValueError(f"unknown frontend prototype variant: {wanted}")


def candidate_variants(registry: Mapping[str, object]) -> List[Dict[str, object]]:
    return registry_variants(registry, statuses={"candidate"})


def package_artifact_hashes(package_root: Path) -> Dict[str, str]:
    if not package_root.is_dir():
        return {}
    return {
        path.relative_to(package_root).as_posix(): sha256_file(path)
        for path in sorted(package_root.rglob("*"))
        if path.is_file()
    }


def artifact_bundle_sha256(hashes: Mapping[str, str]) -> str:
    encoded = json.dumps(dict(hashes), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def frontend_scope_sha256(project_root: Path, *, max_pages: int) -> str:
    trace = read_json(requirements_trace_path(project_root), default={})
    payload = selected_surface_specs(trace, max_pages=max_pages)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def package_ref(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def _rewrite_manifest_refs(
    project_root: Path,
    manifest: Mapping[str, object],
    prototype_root: Path,
) -> Dict[str, object]:
    result = copy.deepcopy(dict(manifest))
    result["index_ref"] = package_ref(project_root, prototype_root / "index.html")
    pages = []
    for raw in result.get("pages", []) or []:
        if not isinstance(raw, Mapping):
            pages.append(raw)
            continue
        page = dict(raw)
        old_ref = str(page.get("html_ref", "")).strip()
        name = Path(old_ref).name if old_ref else ""
        if name:
            page["html_ref"] = package_ref(project_root, prototype_root / name)
        pages.append(page)
    result["pages"] = pages
    return result


def _variant_prototype_payload(project_root: Path, variant_id: str) -> Dict[str, object]:
    root = variant_prototype_dir(project_root, variant_id)
    manifest = read_json(root / "manifest.json", default={})
    if not isinstance(manifest, dict):
        return {}
    return {
        "manifest_ref": package_ref(project_root, root / "manifest.json"),
        "index_ref": str(manifest.get("index_ref", "")),
        "viewports": list(manifest.get("viewports", []) or []),
        "pages": list(manifest.get("pages", []) or []),
    }


def build_variant_entry(
    project_root: Path,
    variant_id: str,
    *,
    name: str,
    status: str,
    run_id: str,
    prompt: str,
    parent_variant_id: str,
    source: Mapping[str, object],
    candidates: Iterable[Mapping[str, object]],
    design_decision: Mapping[str, object],
    max_pages: int,
) -> Dict[str, object]:
    root = variant_dir(project_root, variant_id)
    hashes = package_artifact_hashes(root)
    return {
        "id": variant_id,
        "name": str(name).strip() or variant_id,
        "status": status,
        "created_at": utc_now_iso(),
        "run_id": str(run_id),
        "prompt": str(prompt),
        "parent_variant_id": str(parent_variant_id),
        "design_decision": dict(design_decision),
        "source": dict(source),
        "candidates": [dict(item) for item in candidates],
        "design_path": package_ref(project_root, variant_design_path(project_root, variant_id)),
        "selection_path": package_ref(
            project_root, variant_design_docs_dir(project_root, variant_id) / "selection.md"
        ) if (variant_design_docs_dir(project_root, variant_id) / "selection.md").is_file() else "",
        "prototype": _variant_prototype_payload(project_root, variant_id),
        "scope_sha256": frontend_scope_sha256(project_root, max_pages=max_pages),
        "artifact_sha256": hashes,
        "bundle_sha256": artifact_bundle_sha256(hashes),
        "size_bytes": sum(path.stat().st_size for path in root.rglob("*") if path.is_file()),
    }


def validate_variant(
    project_root: Path,
    entry: Mapping[str, object],
    *,
    max_pages: int,
    require_current_scope: bool = True,
) -> List[str]:
    errors: List[str] = []
    variant_id = str(entry.get("id", "")).strip()
    try:
        root = variant_dir(project_root, variant_id)
    except ValueError as error:
        return [str(error)]
    if not root.is_dir():
        return [f"frontend prototype variant {variant_id} artifacts are missing"]
    if not variant_design_path(project_root, variant_id).is_file():
        errors.append(f"frontend prototype variant {variant_id} is missing DESIGN.md")
    manifest_path = variant_prototype_dir(project_root, variant_id) / "manifest.json"
    manifest = read_json(manifest_path, default=None)
    if not isinstance(manifest, dict):
        errors.append(f"frontend prototype variant {variant_id} is missing manifest.json")
    else:
        errors.extend(
            validate_prototype_manifest(
                project_root,
                manifest,
                max_pages=max_pages,
                prototype_root=variant_prototype_dir(project_root, variant_id),
            )
        )
    recorded = entry.get("artifact_sha256")
    current = package_artifact_hashes(root)
    if not isinstance(recorded, Mapping) or {
        str(key): str(value) for key, value in recorded.items()
    } != current:
        errors.append(f"frontend prototype variant {variant_id} artifacts have drifted")
    if require_current_scope and str(entry.get("scope_sha256", "")) != frontend_scope_sha256(
        project_root, max_pages=max_pages
    ):
        errors.append(f"frontend prototype variant {variant_id} no longer covers the current frontend scope")
    return errors


def _legacy_variant_entry(project_root: Path, *, virtual: bool) -> Optional[Dict[str, object]]:
    lock = load_frontend_design_lock(project_root)
    if not lock or not frontend_prototype_dir(project_root).is_dir():
        return None
    hashes = frontend_design_artifact_hashes(project_root)
    if not hashes:
        return None
    digest = artifact_bundle_sha256(hashes).split(":", 1)[-1][:12]
    status = "approved" if lock.get("status") == "approved" else "candidate"
    return {
        "id": f"legacy-{digest}",
        "name": "Legacy prototype",
        "status": status,
        "created_at": str(lock.get("created_at", "")) or utc_now_iso(),
        "run_id": "",
        "prompt": "",
        "parent_variant_id": "",
        "design_decision": {
            "design_action": "legacy",
            "base_variant_id": "",
            "rationale": "Migrated from the single-prototype layout.",
            "prompt_signals": [],
        },
        "source": copy.deepcopy(lock.get("source", {})),
        "candidates": copy.deepcopy(lock.get("candidates", [])),
        "design_path": str(lock.get("design_path", "DESIGN.md")),
        "selection_path": str(lock.get("selection_path", "")),
        "prototype": copy.deepcopy(lock.get("prototype", {})),
        "scope_sha256": "",
        "artifact_sha256": hashes,
        "bundle_sha256": artifact_bundle_sha256(hashes),
        "size_bytes": sum(
            path.stat().st_size
            for root in (frontend_design_docs_dir(project_root), frontend_prototype_dir(project_root))
            if root.is_dir()
            for path in root.rglob("*")
            if path.is_file()
        ) + (design_md_path(project_root).stat().st_size if design_md_path(project_root).is_file() else 0),
        "legacy_virtual": bool(virtual),
    }


def ensure_registry(project_root: Path, *, max_pages: int) -> Dict[str, object]:
    registry_path = frontend_prototype_variants_registry_path(project_root)
    if registry_path.is_file():
        return load_registry(project_root, include_virtual_legacy=False)
    legacy = _legacy_variant_entry(project_root, virtual=False)
    registry = empty_registry()
    if legacy is None:
        save_registry(project_root, registry)
        return registry
    variant_id = str(legacy["id"])
    target = variant_dir(project_root, variant_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        staging = target.with_name(f".{target.name}.staging-{uuid.uuid4().hex[:6]}")
        staging.mkdir(parents=True)
        if design_md_path(project_root).is_file():
            shutil.copy2(design_md_path(project_root), staging / "DESIGN.md")
        if frontend_design_docs_dir(project_root).is_dir():
            shutil.copytree(frontend_design_docs_dir(project_root), staging / "frontend_design")
        shutil.copytree(frontend_prototype_dir(project_root), staging / "frontend_prototype")
        manifest_path = staging / "frontend_prototype" / "manifest.json"
        manifest = read_json(manifest_path, default={})
        if isinstance(manifest, dict):
            rewritten = _rewrite_manifest_refs(
                project_root, manifest, target / "frontend_prototype"
            )
            write_json(manifest_path, rewritten)
        staging.rename(target)
    source = copy.deepcopy(legacy.get("source", {}))
    if isinstance(source, dict) and source.get("license_path"):
        source["license_path"] = package_ref(
            project_root, variant_design_docs_dir(project_root, variant_id) / "awesome-design-md.LICENSE"
        )
    legacy["source"] = source
    legacy["design_path"] = package_ref(project_root, variant_design_path(project_root, variant_id))
    selection = variant_design_docs_dir(project_root, variant_id) / "selection.md"
    legacy["selection_path"] = package_ref(project_root, selection) if selection.is_file() else ""
    legacy["prototype"] = _variant_prototype_payload(project_root, variant_id)
    legacy["scope_sha256"] = frontend_scope_sha256(project_root, max_pages=max_pages)
    legacy["artifact_sha256"] = package_artifact_hashes(target)
    legacy["bundle_sha256"] = artifact_bundle_sha256(legacy["artifact_sha256"])
    legacy["size_bytes"] = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
    legacy.pop("legacy_virtual", None)
    registry["variants"] = [legacy]
    if legacy["status"] == "approved":
        registry["approved_variant_id"] = variant_id
    save_registry(project_root, registry)
    return registry


def add_variant(project_root: Path, registry: Dict[str, object], entry: Mapping[str, object]) -> None:
    variants = registry.setdefault("variants", [])
    if not isinstance(variants, list):
        raise ValueError("frontend prototype variants registry is invalid")
    variant_id = str(entry.get("id", ""))
    if any(isinstance(item, Mapping) and str(item.get("id", "")) == variant_id for item in variants):
        raise ValueError(f"frontend prototype variant already exists: {variant_id}")
    bundle = str(entry.get("bundle_sha256", ""))
    duplicate = next(
        (
            item
            for item in variants
            if isinstance(item, Mapping)
            and str(item.get("status", "")) == "candidate"
            and str(item.get("bundle_sha256", "")) == bundle
        ),
        None,
    )
    if duplicate is not None:
        raise ValueError(
            "generated frontend prototype is identical to existing variant "
            + str(duplicate.get("id", ""))
        )
    variants.append(dict(entry))
    save_registry(project_root, registry)


def reject_variants(
    project_root: Path,
    registry: Dict[str, object],
    variant_ids: Iterable[str],
    *,
    reason: str,
) -> List[str]:
    targets = {validate_variant_id(item) for item in variant_ids}
    by_id = {str(item.get("id", "")): item for item in registry_variants(registry)}
    invalid = [
        variant_id
        for variant_id in sorted(targets)
        if variant_id not in by_id or str(by_id[variant_id].get("status", "")) != "candidate"
    ]
    if invalid:
        raise ValueError(
            "unknown or non-candidate frontend prototype variants: " + ", ".join(invalid)
        )
    rejected: List[str] = []
    for entry in registry_variants(registry):
        variant_id = str(entry.get("id", ""))
        if variant_id not in targets:
            continue
        if str(entry.get("status", "")) != "candidate":
            raise ValueError(f"frontend prototype variant {variant_id} is not a candidate")
        root = variant_dir(project_root, variant_id)
        if root.is_dir():
            shutil.rmtree(root)
        entry["status"] = "rejected"
        entry["rejected_at"] = utc_now_iso()
        entry["rejection_reason"] = str(reason)
        entry["artifacts_deleted"] = True
        rejected.append(variant_id)
        variants = registry.get("variants", [])
        if isinstance(variants, list):
            for index, raw in enumerate(variants):
                if isinstance(raw, Mapping) and str(raw.get("id", "")) == variant_id:
                    variants[index] = entry
                    break
    save_registry(project_root, registry)
    return rejected


def materialize_variant(
    project_root: Path,
    entry: Mapping[str, object],
    *,
    max_pages: int,
) -> Dict[str, object]:
    errors = validate_variant(project_root, entry, max_pages=max_pages, require_current_scope=True)
    if errors:
        raise RuntimeError("Cannot approve the frontend prototype variant:\n" + "\n".join(f"- {e}" for e in errors))
    variant_id = str(entry["id"])
    source_root = variant_dir(project_root, variant_id)
    design_source = source_root / "DESIGN.md"
    design_docs_source = source_root / "frontend_design"
    prototype_source = source_root / "frontend_prototype"

    temp_root = project_root / ".auto-agents" / "tmp" / f"activate-{variant_id}-{uuid.uuid4().hex[:6]}"
    temp_design_docs = temp_root / "frontend_design"
    temp_prototype = temp_root / "frontend_prototype"
    temp_root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(design_source, temp_root / "DESIGN.md")
    if design_docs_source.is_dir():
        shutil.copytree(design_docs_source, temp_design_docs)
    else:
        temp_design_docs.mkdir()
    shutil.copytree(prototype_source, temp_prototype)
    manifest_path = temp_prototype / "manifest.json"
    manifest = read_json(manifest_path, default={})
    if not isinstance(manifest, dict):
        raise RuntimeError("selected frontend prototype variant has an invalid manifest")
    canonical_manifest = _rewrite_manifest_refs(
        project_root, manifest, frontend_prototype_dir(project_root)
    )
    write_json(manifest_path, canonical_manifest)

    backups = project_root / ".auto-agents" / "tmp" / f"activate-backup-{uuid.uuid4().hex[:6]}"
    backups.mkdir(parents=True)
    targets: List[Tuple[Path, Path]] = [
        (design_md_path(project_root), temp_root / "DESIGN.md"),
        (frontend_design_docs_dir(project_root), temp_design_docs),
        (frontend_prototype_dir(project_root), temp_prototype),
    ]
    moved: List[Tuple[Path, Path]] = []
    try:
        for target, _staged in targets:
            if target.exists():
                backup = backups / target.name
                target.rename(backup)
                moved.append((target, backup))
        for target, staged in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.rename(target)
    except Exception:
        for target, _staged in reversed(targets):
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        for target, backup in reversed(moved):
            if backup.exists():
                backup.rename(target)
        raise
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)
    if backups.exists():
        shutil.rmtree(backups)

    source = copy.deepcopy(entry.get("source", {}))
    if isinstance(source, dict) and source.get("license_path"):
        source["license_path"] = ".auto-agents/docs/frontend_design/awesome-design-md.LICENSE"
    lock: Dict[str, object] = {
        "version": 1,
        "status": "pending_approval",
        "variant_id": variant_id,
        "created_at": str(entry.get("created_at", "")) or utc_now_iso(),
        "source": source,
        "candidates": copy.deepcopy(entry.get("candidates", [])),
        "design_path": "DESIGN.md",
        "selection_path": (
            ".auto-agents/docs/frontend_design/selection.md"
            if (frontend_design_docs_dir(project_root) / "selection.md").is_file()
            else ""
        ),
        "prototype": {
            "manifest_ref": ".auto-agents/docs/frontend_prototype/manifest.json",
            "index_ref": str(canonical_manifest.get("index_ref", "")),
            "viewports": list(canonical_manifest.get("viewports", []) or []),
            "pages": list(canonical_manifest.get("pages", []) or []),
        },
    }
    lock["artifact_sha256"] = frontend_design_artifact_hashes(project_root)
    write_json(frontend_design_lock_path(project_root), lock)
    return lock


def approve_variant_in_registry(
    project_root: Path,
    registry: Dict[str, object],
    variant_id: str,
) -> List[str]:
    selected = find_variant(registry, variant_id)
    if str(selected.get("status", "")) != "candidate":
        raise ValueError(f"frontend prototype variant {variant_id} is not a candidate")
    other_ids = [
        str(item.get("id", ""))
        for item in candidate_variants(registry)
        if str(item.get("id", "")) != variant_id
    ]
    if other_ids:
        reject_variants(
            project_root,
            registry,
            other_ids,
            reason=f"not selected when {variant_id} was approved",
        )
    variants = registry.get("variants", [])
    if isinstance(variants, list):
        for index, raw in enumerate(variants):
            if isinstance(raw, Mapping) and str(raw.get("id", "")) == variant_id:
                updated = dict(raw)
                updated["status"] = "approved"
                updated["approved_at"] = utc_now_iso()
                variants[index] = updated
                break
    registry["approved_variant_id"] = variant_id
    save_registry(project_root, registry)
    return other_ids


def preview_root_for_variant(project_root: Path, entry: Mapping[str, object]) -> Path:
    if bool(entry.get("legacy_virtual")):
        return frontend_prototype_dir(project_root)
    return variant_prototype_dir(project_root, str(entry.get("id", "")))


def gallery_html(project_root: Path, registry: Mapping[str, object]) -> str:
    variants = registry_variants(registry, statuses=LIVE_VARIANT_STATUSES)
    data: List[Dict[str, object]] = []
    for entry in variants:
        prototype = entry.get("prototype", {})
        raw_pages = prototype.get("pages", []) if isinstance(prototype, Mapping) else []
        pages = []
        for raw in raw_pages if isinstance(raw_pages, list) else []:
            if not isinstance(raw, Mapping):
                continue
            filename = Path(str(raw.get("html_ref", ""))).name
            if not filename:
                continue
            pages.append(
                {
                    "id": str(raw.get("id", "")),
                    "title": str(raw.get("title", raw.get("id", ""))),
                    "url": f"/variants/{entry['id']}/{filename}",
                }
            )
        decision = entry.get("design_decision", {})
        data.append(
            {
                "id": str(entry.get("id", "")),
                "name": str(entry.get("name", "")),
                "status": str(entry.get("status", "")),
                "prompt": str(entry.get("prompt", "")),
                "created_at": str(entry.get("created_at", "")),
                "design_action": (
                    str(decision.get("design_action", ""))
                    if isinstance(decision, Mapping)
                    else ""
                ),
                "pages": pages,
            }
        )
    encoded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    cards = "".join(
        "<article><h3>" + html.escape(str(item["name"])) + "</h3>"
        "<code>" + html.escape(str(item["id"])) + "</code>"
        "<p><span>" + html.escape(str(item["status"])) + "</span> · "
        + html.escape(str(item["design_action"])) + "</p>"
        "<p>" + html.escape(str(item["prompt"]) or "Initial prototype") + "</p></article>"
        for item in data
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Frontend prototype variants</title>
<style>
:root{{--bg:#f4f5f7;--panel:#fff;--line:#d9dde5;--text:#172033;--muted:#667085}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif}}
header{{padding:24px 28px 12px}} h1{{margin:0 0 6px;font-size:24px}} header p{{margin:0;color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;padding:12px 28px}}
article{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}} article h3{{margin:0 0 6px}}
article p{{margin:8px 0 0;color:var(--muted)}} .compare{{padding:14px 28px 28px}}
.toolbar,.pane-toolbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}}
select,a{{font:inherit}} select{{padding:7px;border:1px solid var(--line);border-radius:6px;background:#fff}}
.panes{{display:grid;grid-template-columns:1fr 1fr;gap:12px}} .pane{{min-width:0}}
iframe{{width:100%;height:720px;border:1px solid var(--line);border-radius:8px;background:#fff}}
@media(max-width:900px){{.panes{{grid-template-columns:1fr}} iframe{{height:600px}}}}
</style></head><body>
<header><h1>Frontend prototype variants</h1><p>Compare live candidates. Approval and rejection remain CLI-only.</p></header>
<section class="cards">{cards or '<p>No live prototype variants.</p>'}</section>
<section class="compare"><h2>Side-by-side comparison</h2><div class="panes">
<div class="pane"><div class="pane-toolbar"><select id="v0"></select><select id="p0"></select><a id="a0" target="_blank">Open full page</a></div><iframe id="f0" sandbox="allow-scripts allow-forms"></iframe></div>
<div class="pane"><div class="pane-toolbar"><select id="v1"></select><select id="p1"></select><a id="a1" target="_blank">Open full page</a></div><iframe id="f1" sandbox="allow-scripts allow-forms"></iframe></div>
</div></section>
<script>const variants={encoded};
function setup(n,initial){{const vs=document.getElementById('v'+n),ps=document.getElementById('p'+n),frame=document.getElementById('f'+n),link=document.getElementById('a'+n);
variants.forEach((v,i)=>vs.add(new Option(v.name+' · '+v.id,v.id,i===initial,i===initial)));
function pages(){{const v=variants.find(x=>x.id===vs.value);ps.innerHTML='';(v?.pages||[]).forEach((p,i)=>ps.add(new Option(p.title,p.url,i===0,i===0)));show()}}
function show(){{frame.src=ps.value||'about:blank';link.href=ps.value||'#'}} vs.onchange=pages;ps.onchange=show;pages()}}
if(variants.length){{setup(0,Math.max(0,variants.length-2));setup(1,variants.length-1)}}
</script></body></html>"""


class PrototypeGalleryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, project_root: Path, registry: Mapping[str, object], **kwargs):
        self.project_root = project_root.resolve()
        self.registry = registry
        super().__init__(*args, directory=str(project_root), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        clean_path = self.path.split("?", 1)[0].split("#", 1)[0]
        if clean_path in {"", "/"}:
            payload = gallery_html(self.project_root, self.registry).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        match = re.fullmatch(r"/variants/([^/]+)/([^/]+)", clean_path)
        if match is None:
            self.send_error(404)
            return
        try:
            entry = find_variant(self.registry, match.group(1))
        except ValueError:
            self.send_error(404)
            return
        if str(entry.get("status", "")) not in LIVE_VARIANT_STATUSES:
            self.send_error(404)
            return
        root = preview_root_for_variant(self.project_root, entry).resolve()
        candidate = (root / match.group(2)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self.send_error(404)
            return
        if candidate.suffix.lower() != ".html" or not candidate.is_file():
            self.send_error(404)
            return
        payload = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
