"""Frozen incremental prototype contracts and their controller-owned proofs.

The producer proposes scope; it never owns baseline bytes, oracles, review
receipts, or certificates. All references are content addressed, including on
resume and at the human approval boundary.
"""

from pathlib import Path, PurePosixPath
import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time

from .types import ControlError, digest, canonical
from .quality import artifact_hashes, safe_file
from .workspace import git
from .prototype_pages import page_file

CANONICAL = ".auto-agents/docs/frontend_prototype"
PREPARED = ".auto-agents/docs/prototype_preparation/repaired"
PHASES = {
    "prototype_prepare",
    "prototype_scope_review",
    "prototype",
    "prototype_verify",
    "prototype_review",
}
RESET_FIELDS = (
    "prototype_certificate",
    "prototype_scope",
    "prototype_proposal",
    "prototype_browser",
    "prototype_package",
    "prototype_artifacts",
    "prototype_repairs",
    "prototype_prepared_initialized",
    "prototype_candidate_initialized",
)
SELECTOR = re.compile(r'#[A-Za-z_][\w-]*|\[data-prototype-region="[A-Za-z_][\w-]*"\]')


def fail(message, **details):
    raise ControlError(
        "prototype_scope", message, category="verification", details=details
    )


def put(store, kind, value):
    identity = digest(value)
    key = "prototype_" + kind + ":" + identity
    previous = store.meta(key)
    if previous is not None and previous != value:
        fail("Content-addressed prototype record was replaced")
    store.set_meta(key, value)
    return identity


def get(store, kind, identity):
    value = store.meta("prototype_" + kind + ":" + str(identity))
    if value is None or digest(value) != identity:
        fail("Frozen prototype " + kind + " is missing or changed")
    return value


def encoded(files):
    return {
        name: base64.b64encode(data).decode("ascii")
        for name, data in sorted(files.items())
    }


def decoded(files):
    return {name: base64.b64decode(data) for name, data in files.items()}


def relative(name):
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or any(p in {"", ".", ".."} for p in name.split("/"))
        or PurePosixPath(name).is_absolute()
    ):
        fail("Unsafe prototype package path", path=name)
    return name


def normalize(files, prefix):
    """Relabel only structured manifest refs, never silently rewrite HTML/JS."""
    files = dict(files)
    if "manifest.json" in files:

        def visit(value):
            if isinstance(value, str) and value.startswith(prefix + "/"):
                return "@package/" + value[len(prefix) + 1 :]
            if isinstance(value, list):
                return [visit(v) for v in value]
            if isinstance(value, dict):
                return {k: visit(v) for k, v in value.items()}
            return value

        try:
            files["manifest.json"] = canonical(
                visit(json.loads(files["manifest.json"]))
            ).encode()
        except (ValueError, UnicodeError):
            pass  # An invalid admitted manifest is evidence needing repair.
    return files


def materialize(files, prefix):
    files = dict(files)
    if "manifest.json" in files:

        def visit(value):
            if isinstance(value, str) and value.startswith("@package/"):
                return prefix + "/" + value[len("@package/") :]
            if isinstance(value, list):
                return [visit(v) for v in value]
            if isinstance(value, dict):
                return {k: visit(v) for k, v in value.items()}
            return value

        try:
            files["manifest.json"] = canonical(
                visit(json.loads(files["manifest.json"]))
            ).encode()
        except (ValueError, UnicodeError):
            pass
    return files


def package(root, prefix):
    directory = safe_file(root, prefix)
    if not directory.exists():
        return {}
    files = {}
    for path in directory.rglob("*"):
        safe_file(root, path.relative_to(root).as_posix())
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = path.read_bytes()
    return normalize(files, prefix)


def install(root, prefix, files):
    directory = safe_file(root, prefix)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for name, data in materialize(files, prefix).items():
        path = safe_file(root, prefix + "/" + relative(name))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def evidence_dir(engine, work, identity):
    # Not a worker output, and not scratch collected after a confirmed operation.
    path = engine.workspaces.root / "prototype_proofs" / work["id"] / identity
    path.mkdir(parents=True, exist_ok=True)
    return path


def seed(engine, work, ctx):
    if work["context"].get("prototype_seed"):
        return work["context"]["prototype_seed"], get(
            engine.store, "seed", work["context"]["prototype_seed"]
        )
    names = git(
        ctx.workspace_root, "ls-tree", "-r", "--name-only", ctx.contract.source
    ).splitlines()
    original, history = {}, {}
    for name in names:
        if not (
            name.startswith(CANONICAL + "/")
            or name.startswith(".auto-agents/docs/frontend_prototype_variants/")
        ):
            continue
        raw = subprocess.run(
            ["git", "show", ctx.contract.source + ":" + name],
            cwd=ctx.workspace_root,
            capture_output=True,
            check=True,
        ).stdout
        mode = git(
            ctx.workspace_root, "ls-tree", ctx.contract.source, "--", name
        ).split()[0]
        if mode not in {"100644", "100755"}:
            fail("Admitted prototype contains a link", path=name)
        if name.startswith(CANONICAL + "/"):
            original[name[len(CANONICAL) + 1 :]] = raw
        else:
            history[name] = hashlib.sha256(raw).hexdigest()
    # A source snapshot is evidence, not an invented approval. Verify approval
    # locks against these exact bytes, including for imported legacy sessions.
    approved = False
    lock_ref = ".auto-agents/docs/frontend_design.lock.json"
    if lock_ref in names:
        try:
            from ..frontend_design import validate_frontend_design_artifacts

            with tempfile.TemporaryDirectory(prefix="prototype-source-") as tmp:
                root = Path(tmp)
                for name in names:
                    if name == "DESIGN.md" or name.startswith(
                        ".auto-agents/docs/frontend_"
                    ):
                        data = subprocess.run(
                            ["git", "show", ctx.contract.source + ":" + name],
                            cwd=ctx.workspace_root,
                            capture_output=True,
                            check=True,
                        ).stdout
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                lock = json.loads((root / lock_ref).read_text())
                approved = not validate_frontend_design_artifacts(
                    root, lock, require_approved=True
                )
        except (ValueError, OSError, KeyError):
            approved = False
    from ..frontend_design import discover_existing_frontend
    from ..prototype_variants import (
        load_registry,
        artifact_bundle_sha256,
        validate_variant_id,
    )
    from .quality import workspace_guard

    approvals = []
    for row in load_registry(engine.project, include_virtual_legacy=False).get(
        "variants", []
    ):
        if not isinstance(row, dict) or row.get("status") != "approved":
            continue
        try:
            identity = validate_variant_id(row.get("id", ""))
        except (TypeError, ValueError):
            continue
        prefix = ".auto-agents/docs/frontend_prototype_variants/" + identity + "/"
        actual = {
            p[len(prefix) :]: "sha256:" + hashed
            for p, hashed in history.items()
            if p.startswith(prefix)
        }
        expected = row.get("artifact_sha256")
        verified = (
            bool(actual)
            and expected == actual
            and row.get("bundle_sha256") == artifact_bundle_sha256(actual)
        )
        approvals.append(
            {
                "id": identity,
                "approved_at": row.get("approved_at", ""),
                "verified": verified,
                "manifest_ref": row.get("prototype", {}).get("manifest_ref", ""),
                "source": ctx.contract.source,
            }
        )

    refs = list(discover_existing_frontend(ctx.workspace_root).evidence)
    has_implementation = bool(refs)
    refs += [
        p
        for p in names
        if (p.startswith("tests/") or "/__tests__/" in p)
        and not p.endswith((".png", ".jpg"))
    ]
    requirements = [
        p
        for p in (
            ".auto-agents/docs/requirements.md",
            ".auto-agents/docs/requirements_trace.json",
        )
        if (ctx.workspace_root / p).is_file()
    ]
    observation = workspace_guard(ctx.workspace_root)
    frozen_inputs = {
        p
        for p, role in observation["roles"].items()
        if role in {"source", "evidence"}
        and not p.startswith((CANONICAL, ".auto-agents/docs/prototype_preparation/"))
        and p != ".auto-agents/docs/frontend_design.lock.json"
    }
    value = {
        "version": 1,
        "source": ctx.contract.source,
        "clock": int(time.time() * 1000),
        "goal": ctx.contract.goal,
        "original": encoded(normalize(original, CANONICAL)),
        "source_hashes": {
            CANONICAL + "/" + name: hashlib.sha256(data).hexdigest()
            for name, data in original.items()
        },
        "approved": approved,
        "history": history,
        "historical_approvals": approvals,
        "inputs": artifact_hashes(
            ctx.workspace_root, sorted(frozen_inputs | set(refs + requirements))
        ),
        "requirements": requirements,
        "has_frontend": bool(has_implementation or original),
    }
    identity = put(engine.store, "seed", value)
    root = evidence_dir(engine, work, "source-" + identity)
    install(root, "original", decoded(value["original"]))
    (root / "source.json").write_text(canonical(value))
    return identity, value


def check_schema(value, baseline, inputs, *, original_path=None):
    try:
        return _check_schema(value, baseline, inputs, original_path=original_path)
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        fail("Malformed prototype scope", reason=str(error))


def historical_requirement_ids(ctx, baseline, trace):
    """Read preservation identities from admitted bytes, never candidate files."""
    manifests = [base64.b64decode(baseline.get("original", {}).get("manifest.json", ""))]
    manifests.extend(
        git(ctx.workspace_root, "show", baseline["source"] + ":" + name)
        for name in sorted(baseline["history"])
        if name.endswith("/manifest.json")
    )
    identities = set()
    for raw in manifests:
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
            continue
        for page in manifest["pages"]:
            refs = page.get("requirement_ids") if isinstance(page, dict) else None
            if isinstance(refs, list):
                identities.update(ref for ref in refs if isinstance(ref, str) and ref.strip())
    inactive = {
        row["id"] for row in trace.get("requirements", [])
        if row.get("status", "active") != "active"
    }
    return sorted(identities - inactive)


def _check_schema(value, baseline, inputs, *, original_path=None):
    required = {
        "version",
        "mode",
        "reason",
        "repairs",
        "changes",
        "flows",
        "checks",
        "geometry",
        "viewports",
    }
    if not isinstance(value, dict) or set(value) != required or value["version"] != 1:
        fail("Prototype scope must use the complete version-1 schema")
    if (
        value["mode"] not in {"reuse", "incremental", "redesign", "create"}
        or not isinstance(value["reason"], str)
        or not value["reason"].strip()
    ):
        fail("Prototype scope needs a mode and evidence-based reason")
    if baseline["has_frontend"] and value["mode"] == "create":
        fail("An existing frontend cannot be classified as a new design")
    if not baseline["has_frontend"] and value["mode"] != "create":
        fail("A new frontend needs create mode")
    for key in ("repairs", "changes", "flows", "checks", "geometry", "viewports"):
        if not isinstance(value[key], list):
            fail("Prototype scope field must be a list", field=key)
    if not value["viewports"] or any(
        not isinstance(v, str) or not re.fullmatch(r"[1-9]\d{1,3}x[1-9]\d{1,3}", v)
        for v in value["viewports"]
    ):
        fail("Prototype scope needs concrete browser viewports")
    try:
        admitted_manifest = json.loads(
            base64.b64decode(baseline["original"].get("manifest.json", ""))
        )
    except (ValueError, UnicodeError):
        admitted_manifest = {}
    if not isinstance(admitted_manifest, dict):
        admitted_manifest = {}
    if not set(admitted_manifest.get("viewports", [])) <= set(value["viewports"]):
        fail("Scope cannot drop admitted browser viewport coverage")
    trace = inputs.get(".auto-agents/docs/requirements_trace.json")
    if not trace:
        fail("Active requirement trace is required before prototype scope")
    active = baseline["active_requirements"]

    def records(key):
        rows = value[key]
        if any(
            not isinstance(r, dict)
            or not isinstance(r.get("id"), str)
            or not r["id"].strip()
            for r in rows
        ) or len({r["id"] for r in rows}) != len(rows):
            fail("Prototype scope identities must be unique", field=key)
        return {r["id"]: r for r in rows}

    changes, repairs, flows, checks = (
        records(k) for k in ("changes", "repairs", "flows", "checks")
    )
    for required in baseline.get("required_checks", []):
        current = checks.get(required["id"], {})
        if {k: v for k, v in current.items() if k != "asserts"} != {
            k: v for k, v in required.items() if k != "asserts"
        } or any(
            rule not in current.get("asserts", []) for rule in required["asserts"]
        ):
            fail(
                "A revised scope cannot remove or weaken frozen behavior checks",
                check=required["id"],
            )
    if not set(baseline.get("required_viewports", [])) <= set(value["viewports"]):
        fail("A revised scope cannot reduce frozen viewport coverage")
    if not flows or not checks:
        fail("Every prototype needs a nonempty behavior inventory and browser checks")
    if value["mode"] == "reuse" and changes:
        fail("Reuse cannot authorize new UI changes")
    if value["mode"] in {"incremental", "redesign", "create"} and not changes:
        fail("Changed prototypes need explicit regions")
    known_evidence = set(inputs) | set(baseline["history"]) | {"goal"}
    known_evidence |= {CANONICAL + "/" + p for p in baseline["original"]}
    # The controller supplies this read-only snapshot path to the producer.
    # Accept only its exact admitted files, without resolving arbitrary paths
    # or rewriting the producer's retained receipt. Keep their original
    # provenance when checking preservation below.
    snapshot_evidence = {
        str(Path(original_path) / p): CANONICAL + "/" + p
        for p in baseline["original"]
    } if original_path is not None else {}
    product_evidence = known_evidence - {
        "goal", ".auto-agents/docs/requirements.md", ".auto-agents/docs/requirements_trace.json",
    }
    frontend_evidence = set(baseline["history"]) | {
        CANONICAL + "/" + p for p in baseline["original"]
    } | {
        p for p in inputs if Path(p).suffix.lower() in {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".astro"}
    }

    def evidence(row):
        refs = row.get("evidence")
        invalid = [
            ref for ref in refs
            if not isinstance(ref, str)
            or snapshot_evidence.get(ref, ref) not in known_evidence
        ] if isinstance(refs, list) else refs
        if (
            not isinstance(refs, list)
            or not refs
            or invalid
        ):
            fail(
                "Scope evidence must identify admitted source or current requirements",
                record=row.get("id"),
                invalid_evidence=invalid,
                hint="Use exact admitted_baseline.inputs/history keys, canonical original files, original_path/admitted-file, or goal",
            )
        return [snapshot_evidence.get(ref, ref) for ref in refs]

    def reqs(row, *, preserved=False):
        refs = row.get("requirement_ids")
        allowed = set(active)
        if preserved:
            allowed.update(baseline.get("historical_requirements", []))
        if (
            not isinstance(refs, list)
            or any(not isinstance(ref, str) for ref in refs)
            or (not preserved and not refs)
            or not set(refs) <= allowed
        ):
            fail(
                "Preserved flows need admitted requirement identities"
                if preserved else "Scope change needs active requirement identities",
                record=row.get("id"),
            )

    for row in changes.values():
        if set(row) - {"permission", "whole_page"} != {
            "id",
            "kind",
            "page",
            "selector",
            "files",
            "requirement_ids",
            "evidence",
            "because",
            "reason",
        }:
            fail("Invalid change region fields")
        if row["kind"] not in {
            "add",
            "change",
            "remove",
            "linkage",
        } or not (
            SELECTOR.fullmatch(row["selector"])
            or row.get("whole_page") is True
            and row["kind"] == "remove"
            and row["selector"] in {"html", "body"}
        ):
            fail(
                "Use a stable, bounded region selector; wildcards and whole-page scope are forbidden"
            )
        relative(row["page"])
        if (
            not row["page"].endswith(".html")
            or page_file(row["page"]) != row["page"]
            or not isinstance(row["files"], list)
            or not row["files"]
        ):
            fail("Regions need an HTML page and exact editable file paths")
        for name in row["files"]:
            relative(name)
        reqs(row)
        evidence(row)
        if (
            not isinstance(row["reason"], str)
            or not row["reason"].strip()
            or not isinstance(row["because"], list)
            or any(i not in changes or i == row["id"] for i in row["because"])
        ):
            fail("Scope dependencies need explicit causal links")
        if row["kind"] == "linkage" and not row["because"]:
            fail("Related changes must depend on a requested region")
        if row["kind"] == "remove" or value["mode"] == "redesign":
            permission = row.get("permission", "")
            if (
                not isinstance(permission, str)
                or len(permission.strip()) < 6
                or permission not in baseline["permissions"]
                or not re.search(
                    r"\b(?:remove|delete|drop|retire|replace|redesign|restyle|rebrand)\b|删除|移除|取消|下线|重做|重新设计|整体改版|更换",
                    permission,
                    re.I,
                )
                or re.search(
                    r"(?:do not|must not|never|no)\s|不(?:得|要|能)|禁止|保留",
                    permission,
                    re.I,
                )
            ):
                fail(
                    "Removal or redesign requires an exact explicit user permission excerpt"
                )

        if value["mode"] == "redesign" and not re.search(
            r"redesign|restyle|rebrand|重新设计|整体改版|重做|更换.*风格",
            row.get("permission", ""),
            re.I,
        ):
            fail("A local removal does not authorize a redesign")
        if row.get("whole_page"):
            permission = row.get("permission", "")
            titles = [
                p.get("title", "")
                for p in admitted_manifest.get("pages", [])
                if p.get("html_ref") == "@package/" + row["page"]
            ]
            if (
                row["kind"] != "remove"
                or row["page"] not in baseline["original"]
                or not re.search(r"\bpage\b|\bscreen\b|页面|界面", permission, re.I)
                or not any(
                    label and label.casefold() in permission.casefold()
                    for label in [row["page"], *titles]
                )
            ):
                fail(
                    "Whole-page removal needs explicit permission naming the admitted page"
                )
        if "whole_page" in row and type(row["whole_page"]) is not bool:
            fail("whole_page must be a boolean")

    def visit(identity, pending):
        if identity in pending:
            fail("Scope dependency cycle")
        for parent in changes[identity]["because"]:
            visit(parent, pending | {identity})

    for identity in changes:
        visit(identity, set())
    for row in repairs.values():
        if (
            set(row) - {"regions", "files", "geometry"}
            != {"id", "flow_ids", "evidence", "reason"}
            or not row["flow_ids"]
            or not set(row["flow_ids"]) <= set(flows) | {"package-structure"}
        ):
            fail("Baseline repairs must identify preserved flows")
        evidence(row)
        if (
            "package-structure" in row["flow_ids"]
            and baseline.get("original_manifest_errors") == []
        ):
            fail(
                "Package-structure repair requires an invalid admitted manifest",
                record=row["id"],
            )
        if row["evidence"] == ["goal"]:
            fail("Baseline repairs require existing product evidence")
        if any(
            not isinstance(row.get(key, []), list)
            for key in ("regions", "files", "geometry")
        ):
            fail("Baseline repair bounds must be lists")
        for region in row.get("regions", []):
            if (
                not isinstance(region, dict)
                or set(region) != {"page", "selector", "kind"}
                or not SELECTOR.fullmatch(region["selector"])
                or region["kind"] not in {"add", "change", "remove"}
            ):
                fail("Baseline repairs need bounded regions")
            if page_file(region["page"]) != region["page"]:
                fail("Repair regions must name package HTML files")
        for name in row.get("files", []):
            relative(name)
        for geometry in row.get("geometry", []):
            if (
                not isinstance(geometry, dict)
                or set(geometry) != {"page", "selector", "properties"}
                or not (
                    SELECTOR.fullmatch(geometry["selector"])
                    or geometry["selector"] in {"html", "body"}
                )
                or not geometry["properties"]
                or not set(geometry["properties"]) <= {"x", "y", "width", "height"}
            ):
                fail("Baseline repair geometry must be explicit")
            if page_file(geometry["page"]) != geometry["page"]:
                fail("Repair geometry must name package HTML files")
    for row in flows.values():
        if (
            set(row) != {"id", "page", "requirement_ids", "evidence", "checks"}
            or not row["checks"]
            or not set(row["checks"]) <= set(checks)
        ):
            fail("Each inventoried flow needs executable checks")
        page_file(row["page"])
        refs = evidence(row)
        preserved = baseline["has_frontend"] and all(
            checks[i].get("kind") == "preservation" for i in row["checks"]
        )
        if preserved and not set(refs) & product_evidence:
            fail("Preserved flows require existing product evidence", record=row["id"])
        reqs(row, preserved=preserved)
        if (
            baseline["has_frontend"]
            and set(refs) & frontend_evidence
            and not any(checks[i]["kind"] == "preservation" for i in row["checks"])
        ):
            fail("Existing flows need preservation checks", record=row["id"])
    if baseline["has_frontend"] and not any(
        c["kind"] == "preservation" for c in checks.values()
    ):
        fail("Existing frontends need preserved behavior oracles")
    for row in checks.values():
        if (
            set(row) != {"id", "flow", "page", "kind", "steps", "asserts"}
            or row["flow"] not in flows
            or row["id"] not in flows[row["flow"]]["checks"]
            or row["kind"] not in {"preservation", "new"}
        ):
            fail("Checks must map back to their exact flow")
        page_file(row["page"])
        if page_file(row["page"]) != page_file(flows[row["flow"]]["page"]):
            fail("Checks must stay on their flow's same HTML file", record=row["id"])
        if (
            not isinstance(row["steps"], list)
            or not isinstance(row["asserts"], list)
            or not row["asserts"]
        ):
            fail("Checks need actual browser assertions", record=row["id"])
        for index, step in enumerate(row["steps"]):
            if (
                not isinstance(step, dict)
                or set(step) - {"action", "selector", "value"}
                or step.get("action")
                not in {"click", "fill", "select", "check", "seek", "press"}
                or not isinstance(step.get("selector"), str)
                or not step["selector"].strip()
            ):
                fail("Unsupported browser action")
            if step["action"] != "click" and "value" not in step:
                fail(
                    "Browser action needs its frozen input",
                    check=row["id"], step=index + 1,
                    action=step["action"], selector=step["selector"],
                )
            if step["action"] == "press" and not isinstance(step["value"], str):
                fail("Browser key must be text")
        for rule in row["asserts"]:
            if (
                not isinstance(rule, dict)
                or set(rule) - {"selector", "property", "equals", "name"}
                or rule.get("property")
                not in {
                    "text",
                    "value",
                    "checked",
                    "enabled",
                    "visible",
                    "count",
                    "attribute",
                    "currentTime",
                }
                or "equals" not in rule
                or not isinstance(rule.get("selector"), str)
                or not rule["selector"].strip()
            ):
                fail("Unsupported browser assertion")
    for row in value["geometry"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"page", "selector", "properties", "change_ids"}
            or not (
                SELECTOR.fullmatch(row["selector"])
                or row["selector"] in {"html", "body"}
            )
            or not row["change_ids"]
            or not set(row["change_ids"]) <= set(changes)
            or not row["properties"]
            or not set(row["properties"]) <= {"x", "y", "width", "height"}
        ):
            fail("Layout shifts need exact properties, regions and causal changes")
        if page_file(row["page"]) != row["page"]:
            fail("Geometry must name package HTML files")
    # Every changed requirement is covered by a new behavior oracle. Preservation
    # checks are still mandatory, even for a user-authorized full restyle.
    covered = {
        req
        for f in flows.values()
        if any(checks[i]["kind"] == "new" for i in f["checks"])
        for req in f["requirement_ids"]
    }
    uncovered = [
        {"change": c["id"], "requirement_ids": sorted(set(c["requirement_ids"]) - covered)}
        for c in changes.values() if not set(c["requirement_ids"]) <= covered
    ]
    if uncovered:
        fail("New requirements need new behavior checks", uncovered=uncovered)
    return value


def validate_inputs(ctx, scope):
    if artifact_hashes(ctx.workspace_root, scope["inputs"]) != scope["inputs"]:
        fail("Frozen requirements or implementation inputs changed")
    if scope.get("design_hashes") and (ctx.workspace_root / "DESIGN.md").exists():
        if (
            artifact_hashes(ctx.workspace_root, scope["design_hashes"])
            != scope["design_hashes"]
        ):
            fail("Frozen catalog design bytes changed")


def manifest_errors(root, prefix, config):
    from ..frontend_design import validate_prototype_manifest

    path = root / prefix / "manifest.json"
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return ["Missing or malformed manifest"]
    return validate_prototype_manifest(
        root,
        manifest,
        max_pages=config.frontend_design.max_pages,
        prototype_root=root / prefix,
    )


def decisions(engine, work):
    return [r for r in engine.input_references(work) if r["answers"] or r["bindings"]]


def validate_decisions(engine, work, scope):
    if scope.get("decisions", []) != decisions(engine, work):
        fail("Scope must be frozen again after a user decision changes")


def verify_package(files, scope):
    baseline = decoded(scope["baseline"])
    allowed = {"manifest.json"} | {p for c in scope["changes"] for p in c["files"]}
    if scope["mode"] != "create":
        for name in set(baseline) | set(files):
            if name not in allowed and baseline.get(name) != files.get(name):
                fail("Prototype changed an undeclared file", path=name)
    if scope["mode"] == "reuse" and files != baseline:
        fail("Reuse must preserve the repaired baseline exactly")
    for change in scope["changes"]:
        if (
            change.get("whole_page")
            and change["kind"] == "remove"
            and change["page"] in files
        ):
            fail("An explicitly removed page is still present", page=change["page"])


def browser_effect(engine, work, ctx, files, scope, label, *, baseline=False):
    inputs = {
        "scope": digest(scope),
        "package": digest(encoded(files)),
        "baseline": baseline,
    }

    def invoke(identity):
        baseline_pages = [n for n in scope["baseline"] if n.endswith(".html")]
        plan = {
            **scope["scope"],
            "baseline_pages": baseline_pages,
            "clock": scope.get("clock", 1577836800000),
        }
        if label in {"baseline", "original"} and scope["scope"]["repairs"]:
            regions, geometry, _ = repair_bounds(scope["scope"])
            plan.update(
                repair_regions=regions,
                repair_geometry=geometry,
                repair_original=label == "original",
                repair_has_original=bool(scope["original"]),
            )
        report = engine.verifier.prototype(
            ctx, files, plan, evidence_dir(engine, work, identity), baseline=baseline
        )
        engine.store.record_native(
            identity,
            report,
            {
                "contract": ctx.contract.identity,
                "input_hash": digest(inputs),
                "kind": "prototype_verification",
            },
        )
        engine.store.validate_outcome(identity)
        return report

    retained = next(
        (
            o
            for o in reversed(engine.store.operations(work["id"]))
            if o["kind"] == "prototype_browser:" + label and not o["consumed"]
        ),
        None,
    )
    native = engine.store.outcome(retained["id"]) if retained else None
    if retained and native and native["native_state"] == "confirmed":
        if retained["input_hash"] != digest(inputs):
            fail("Retained browser inputs changed")
        report, operation = native["payload"], retained["id"]
        if native["validation"].get("state") != "accepted":
            engine.store.validate_outcome(operation)
        if retained["state"] != "CONFIRMED":
            engine.store.settle(operation, report)
    else:
        if engine.offline:
            raise ControlError(
                "offline_boundary",
                "Offline mode cannot launch a new prototype browser check",
                category="offline",
            )
        report, operation = engine.effect(
            work, "prototype_browser:" + label, inputs, invoke
        )
    directory = evidence_dir(engine, work, operation)
    for name, hashed in report["screenshots"].items():
        path = directory / relative(name)
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != hashed
        ):
            fail("Browser evidence is missing or changed")
    report_path = directory / "report.json"
    serialized = canonical(report)
    if not report_path.exists():
        report_path.write_text(serialized)
    elif report_path.read_text() != serialized:
        fail("Controller browser report is missing or changed")
    return report, operation


def repair_bounds(plan):
    regions, geometry, files = [], [], {"manifest.json"}
    for repair in plan["repairs"]:
        checks = [
            c
            for c in plan["checks"]
            if c["flow"] in repair["flow_ids"] and c["kind"] == "preservation"
        ]
        declared = repair.get("regions")
        if declared is None:
            unique = {
                (page_file(c["page"]), r["selector"])
                for c in checks
                for r in [*c["steps"], *c["asserts"]]
                if SELECTOR.fullmatch(r["selector"])
            }
            declared = [
                {"page": page, "selector": selector, "kind": "change"}
                for page, selector in sorted(unique)
            ]
        regions.extend(declared)
        geometry.extend(repair.get("geometry", []))
        files.update(repair.get("files", [page_file(c["page"]) for c in checks]))
    return regions, geometry, files


def verify_repairs(value, original_report, repaired_report):
    original, repaired = decoded(value["original"]), decoded(value["baseline"])
    if not original:
        return  # First static baseline for an evidenced existing implementation.
    _, _, allowed = repair_bounds(value["scope"])
    for name in set(original) | set(repaired):
        if name not in allowed and original.get(name) != repaired.get(name):
            fail("Baseline restoration changed an undeclared file", path=name)
    before = original_report["repair_observations"]
    after = repaired_report["repair_observations"]
    changed = [key for key, observed in before.items() if after.get(key) != observed]
    if changed:
        fail(
            "Baseline restoration changed UI outside its bounded repairs",
            observations=changed,
        )


def report_input(engine, operation):
    native = engine.store.outcome(operation)
    record = next(o for o in engine.store.operations() if o["id"] == operation)
    report = native["payload"]
    path = (
        evidence_dir(engine, engine.store.work(record["work"]), operation)
        / "report.json"
    )
    if not path.is_file() or path.read_text() != canonical(report):
        fail("Controller browser report is missing or changed")
    return {
        "identity": digest(report),
        "operation": operation,
        "executed": report["executed"],
        "failures": report["failures"],
        "observations": {k: digest(v) for k, v in report["observations"].items()},
        "screenshots": report["screenshots"],
        "report_path": str(path),
    }


def record_input(engine, work, value):
    """Providers inspect read-only files; binary assets never become prompt text."""
    identity = digest(value)
    if value.get("scope_review"):
        owner = engine.store.work(
            next(
                o["work"]
                for o in engine.store.operations()
                if o["id"] == value["scope_review"]
            )
        )
        root = evidence_dir(engine, owner, "scope-" + identity)
    else:
        root = evidence_dir(engine, work, "proposal-" + identity)
    public = {
        k: v
        for k, v in value.items()
        if k not in {"baseline", "original", "baseline_observations", "design_catalog"}
    }
    for key in ("baseline", "original"):
        files = decoded(value[key])
        public[key + "_hashes"] = {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        }
        public[key + "_path"] = str(
            root / ("repaired" if key == "baseline" else "original")
        )
    public["identity"] = identity
    return public


def certificate(engine, work, ctx, files, scope_id, browser, review):
    scope = get(engine.store, "scope", scope_id)
    return put(
        engine.store,
        "certificate",
        {
            "version": 1,
            "scope": scope_id,
            "candidate": digest(encoded(files)),
            "browser": browser,
            "review": review,
            "inputs": scope["inputs"],
        },
    )


def images(engine, operation):
    record = next(o for o in engine.store.operations() if o["id"] == operation)
    native = engine.store.outcome(operation)
    directory = evidence_dir(engine, engine.store.work(record["work"]), operation)
    paths = []
    for name, hashed in native["payload"]["screenshots"].items():
        path = directory / relative(name)
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != hashed
        ):
            fail("Prototype screenshot evidence is missing or changed")
        paths.append(path)
    return tuple(paths)


def require_certificate(engine, work, ctx, files, identity=None):
    identity = identity or work["context"].get("prototype_certificate")
    if not identity:
        raise ControlError(
            "prototype_validation_required",
            "This candidate needs frozen scope and browser review; resume the session before approving",
            category="verification",
        )
    cert = get(engine.store, "certificate", identity)
    scope = get(engine.store, "scope", cert["scope"])
    validate_inputs(ctx, scope)
    browser_operation = next(
        o for o in engine.store.operations() if o["id"] == cert["browser"]
    )
    owner = engine.store.work(browser_operation["work"])
    validate_decisions(engine, owner, scope)
    ancestor = owner
    while ancestor["id"] != work["id"] and ancestor["parent"]:
        ancestor = engine.store.work(ancestor["parent"])
    if ancestor["id"] != work["id"]:
        fail("Prototype certificate belongs to another work graph")
    if cert["candidate"] != digest(encoded(files)) or cert["inputs"] != scope["inputs"]:
        fail(
            "Prototype certificate is stale; reject this candidate and resume for a verified revision"
        )
    scope_review = engine.store.outcome(scope["scope_review"])
    baseline_browser = engine.store.outcome(scope["baseline_browser"])
    report_input(engine, scope["baseline_browser"])
    images(engine, scope["baseline_browser"])
    from .effects import parse_reply

    if (
        not scope_review
        or scope_review["binding"].get("phase") != "prototype_scope_review"
        or scope_review["validation"].get("state") != "accepted"
        or parse_reply(scope_review["payload"].get("text")).get("coverage_complete")
        is not True
        or parse_reply(scope_review["payload"].get("text")).get("decision") != "pass"
        or not baseline_browser
        or baseline_browser["payload"]["observations"] != scope["baseline_observations"]
        or baseline_browser["payload"]["failures"]
    ):
        fail("Prototype scope lacks its independent baseline review")
    proposed = {
        k: v
        for k, v in scope.items()
        if k not in {"scope_review", "baseline_browser", "baseline_observations"}
    }
    scope_operation = next(
        o for o in engine.store.operations() if o["id"] == scope["scope_review"]
    )
    if scope_operation["inputs"].get("inputs", {}).get("proposed_scope", {}).get(
        "identity"
    ) != digest(proposed):
        fail("Scope review belongs to a different proposal")
    for key in ("browser", "review"):
        native = engine.store.outcome(cert[key])
        if (
            not native
            or native["native_state"] != "confirmed"
            or native["validation"].get("state") != "accepted"
        ):
            fail("Prototype certificate has no validated independent evidence")
        if key == "browser":
            report = native["payload"]
            expected = len(scope["scope"]["checks"]) * len(scope["scope"]["viewports"])
            if (
                report["failures"]
                or report["executed"] != expected
                or not report["screenshots"]
            ):
                fail("Prototype browser verification did not pass")
            operation = next(
                o for o in engine.store.operations() if o["id"] == cert[key]
            )
            if operation["inputs"] != {
                "scope": cert["scope"],
                "package": cert["candidate"],
                "baseline": False,
            }:
                fail("Browser evidence belongs to another candidate")
            images(engine, cert[key])
            report_input(engine, cert[key])
        else:
            reply = parse_reply(native["payload"].get("text"))
            review_operation = next(
                o for o in engine.store.operations() if o["id"] == cert[key]
            )
            reviewed = review_operation["inputs"].get("inputs", {})
            if (
                native["binding"].get("phase") != "prototype_review"
                or native["binding"].get("readonly") is not True
                or review_operation["work"] != browser_operation["work"]
                or reviewed.get("candidate_digest") != cert["candidate"]
                or reviewed.get("frozen_prototype_scope", {}).get("identity")
                != cert["scope"]
                or reviewed.get("browser_verification", {}).get("identity")
                != digest(engine.store.outcome(cert["browser"])["payload"])
            ):
                fail(
                    "Independent review belongs to another candidate, scope or browser result"
                )
            if (
                reply.get("decision") != "pass"
                or reply.get("coverage_complete") is not True
            ):
                fail("Independent prototype review did not pass")
    return cert


def step(engine, work, ctx):
    """Handle only the explicit prototype subgraph; all effects use its ledger."""
    state, phase = dict(work["context"]), work["phase"]
    prefix = CANONICAL + (
        "_variants/" + ctx.contract.inputs["variant_only"]
        if ctx.contract.inputs.get("variant_only")
        else ""
    )
    retained = any(
        o["kind"] == "model:" + phase
        and not o["consumed"]
        and o["state"] in {"CONFIRMED", "DISPATCHED", "UNKNOWN", "FAILED"}
        for o in engine.store.operations(work["id"])
    )
    if phase == "prototype" and not state.get("prototype_scope"):
        if not state.get("frontend") and not ctx.contract.inputs.get("variant_only"):
            return engine.advance(work, "design")
        return engine.advance(work, "prototype_prepare")
    if phase == "prototype_prepare":
        if not state.get("frontend") and not ctx.contract.inputs.get("variant_only"):
            return engine.advance(work, "design")
        seed_id, baseline = seed(engine, work, ctx)
        # On rejection the original immutable admission remains the source. A
        # rejected candidate can never become a new behavioral baseline.
        baseline = dict(baseline)
        original_path = evidence_dir(engine, work, "source-" + seed_id) / "original"
        baseline["original_manifest_errors"] = manifest_errors(
            original_path.parent, original_path.name, engine.config
        )
        trace = json.loads(
            safe_file(
                ctx.workspace_root, ".auto-agents/docs/requirements_trace.json"
            ).read_text()
        )
        baseline["active_requirements"] = [
            r["id"]
            for r in trace.get("requirements", [])
            if r.get("status", "active") == "active"
        ]
        baseline["historical_requirements"] = historical_requirement_ids(ctx, baseline, trace)
        inherited = ctx.contract.inputs.get("prototype_parent_scope") or state.get(
            "prototype_previous_scope"
        )
        inherited_value = get(engine.store, "scope", inherited) if inherited else None
        if inherited_value:
            baseline["required_checks"] = [
                c
                for c in inherited_value["scope"]["checks"]
                if ctx.contract.inputs.get("variant_only")
                or c["kind"] == "preservation"
            ]
            baseline["required_viewports"] = inherited_value["scope"]["viewports"]
        from .interaction import resolved_answers

        baseline["permissions"] = (
            ctx.contract.goal
            + "\n"
            + json.dumps(
                resolved_answers(engine.project, state.get("operator_answers", [])),
                ensure_ascii=False,
            )
        )
        if not retained and not state.get("prototype_prepared_initialized"):
            install(ctx.workspace_root, PREPARED, decoded(baseline["original"]))
            state.update(prototype_seed=seed_id, prototype_prepared_initialized=True)
            work = engine.store.transition(work, "RUNNING", context=state)
        proposal, op = engine.model(
            work,
            ctx,
            phase,
            readonly=False,
            inputs={
                "admitted_baseline": {
                    **{k: v for k, v in baseline.items() if k != "original"},
                    "original_hashes": {
                        name: hashlib.sha256(data).hexdigest()
                        for name, data in decoded(baseline["original"]).items()
                    },
                },
                "original_path": str(original_path),
                "repair_path": PREPARED,
                "previous_frozen_scope": (
                    record_input(engine, work, inherited_value)
                    if inherited_value
                    else None
                ),
            },
        )
        if proposal.get("questions"):
            # Retain the admission before asking; answers cannot change its source.
            updated = engine.store.transition(
                work, "RUNNING", context={**state, "prototype_seed": seed_id}
            )
            return engine.wait_user(updated, proposal["questions"], op)
        plan = check_schema(
            proposal.get("scope"), baseline, baseline["inputs"],
            original_path=original_path,
        )
        for repair in plan["repairs"]:
            regions, geometry, files = repair_bounds({**plan, "repairs": [repair]})
            repair.setdefault("regions", regions)
            repair.setdefault("geometry", geometry)
            repair.setdefault("files", sorted(files))
        repaired = package(ctx.workspace_root, PREPARED)
        if plan["mode"] == "create":
            if repaired or plan["repairs"]:
                fail("Create preparation cannot pre-generate the candidate")
        else:
            if not repaired:
                fail("Existing prototypes need a repaired static baseline")
            if not plan["repairs"] and repaired != decoded(baseline["original"]):
                fail("Baseline changed without a declared restoration")
        value = {
            "version": 1,
            "seed": seed_id,
            "scope": plan,
            "baseline": encoded(repaired),
            "original": baseline["original"],
            "inputs": baseline["inputs"],
            "source": baseline["source"],
            "clock": baseline.get("clock", 1577836800000),
            "decisions": decisions(engine, work),
        }
        operation = next(
            o for o in engine.store.operations(work["id"]) if o["id"] == op
        )
        catalog = operation["inputs"].get("inputs", {}).get("design_catalog")
        if catalog:
            from ..frontend_design import (
                CatalogSnapshot,
                CatalogEntry,
                validate_catalog_selection,
            )

            snapshot = CatalogSnapshot(
                catalog["repository"],
                "",
                catalog["commit"],
                Path(catalog["root"]),
                tuple(CatalogEntry(**row) for row in catalog["entries"]),
                True,
            )
            try:
                entry, _ = validate_catalog_selection(
                    proposal.get("selection"), snapshot
                )
            except ValueError as error:
                fail(
                    "Prototype preparation must freeze its catalog selection",
                    reason=str(error),
                )
            design = snapshot.root / entry.design_path
            license_path = snapshot.root / "LICENSE"
            if (
                not design.resolve().is_relative_to(snapshot.root.resolve())
                or not design.is_file()
                or not license_path.is_file()
                or not license_path.resolve().is_relative_to(snapshot.root.resolve())
            ):
                fail("Frozen catalog source is unavailable")
            value["design_catalog"] = catalog
            value["selection"] = proposal["selection"]
            value["design_hashes"] = {
                "DESIGN.md": hashlib.sha256(design.read_bytes()).hexdigest(),
                ".auto-agents/docs/frontend_design/LICENSE": hashlib.sha256(
                    license_path.read_bytes()
                ).hexdigest(),
            }
        proposal_id = put(engine.store, "proposal", value)
        state.update(prototype_seed=seed_id, prototype_proposal=proposal_id)
        return engine.advance(
            work, "prototype_scope_review", context=state, operation=op
        )
    if phase == "prototype_scope_review":
        value = get(engine.store, "proposal", state["prototype_proposal"])
        validate_inputs(ctx, value)
        validate_decisions(engine, work, value)
        baseline, plan = decoded(value["baseline"]), value["scope"]
        frozen_package = evidence_dir(
            engine, work, "proposal-" + state["prototype_proposal"]
        )
        install(frozen_package, "repaired", baseline)
        install(frozen_package, "original", decoded(value["original"]))
        if plan["mode"] != "create":
            errors = manifest_errors(frozen_package, "repaired", engine.config)
            if errors:
                fail(
                    "Repaired baseline manifest must be valid before scope freezes",
                    errors=errors,
                )
        report, browser = browser_effect(
            engine, work, ctx, baseline, value, "baseline", baseline=True
        )
        expected = len(
            [c for c in plan["checks"] if c["kind"] == "preservation"]
        ) * len(plan["viewports"])
        if (
            report["failures"]
            or report["executed"] != expected
            or expected
            and not report["screenshots"]
        ):
            fail(
                "Repaired baseline does not satisfy existing behavior",
                failures=report["failures"],
            )
        original_report = None
        if plan["repairs"]:
            original_report, original_op = browser_effect(
                engine,
                work,
                ctx,
                decoded(value["original"]),
                value,
                "original",
                baseline=True,
            )
            failed_flows = {
                c["flow"]
                for c in plan["checks"]
                if any(f.get("check") == c["id"] for f in original_report["failures"])
            }
            if manifest_errors(frozen_package, "original", engine.config):
                failed_flows.add("package-structure")
            repair_flows = {f for r in plan["repairs"] for f in r["flow_ids"]}
            if not repair_flows <= failed_flows or not failed_flows <= repair_flows:
                fail(
                    "Baseline restoration must explain the observed original failures",
                    observed=sorted(failed_flows),
                    repairs=sorted(repair_flows),
                )
            verify_repairs(value, original_report, report)
        proposal, op = engine.model(
            work,
            ctx,
            phase,
            inputs={
                "proposed_scope": record_input(engine, work, value),
                "baseline_browser": report_input(engine, browser),
                "original_browser": (
                    report_input(engine, original_op) if plan["repairs"] else None
                ),
                "review_checklist": [
                    "admitted approvals and active implementation",
                    "complete preserved flow inventory",
                    "retired features and user edits",
                    "minimal region and dependency scope",
                    "new and preserved assertions resist false positives",
                    "explicit goal permission for removal or redesign",
                    "baseline repair evidence",
                ],
            },
            attachments=images(engine, browser)
            + (images(engine, original_op) if plan["repairs"] else ()),
        )
        if proposal.get("questions"):
            work = engine.store.transition(
                work,
                "RUNNING",
                context={**state, "prototype_scope_reset_on_answer": True},
            )
            return engine.wait_user(work, proposal["questions"], op)
        if (
            proposal.get("decision") != "pass"
            or proposal.get("coverage_complete") is not True
            or not str(proposal.get("reason", "")).strip()
        ):
            fail("Independent scope review did not pass", review=proposal)
        value["scope_review"] = op
        value["baseline_browser"] = browser
        value["baseline_observations"] = report["observations"]
        scope_id = put(engine.store, "scope", value)
        state.update(prototype_scope=scope_id)
        frozen = evidence_dir(engine, work, "scope-" + scope_id)
        install(frozen, "original", decoded(value["original"]))
        install(frozen, "repaired", decoded(value["baseline"]))
        state["prototype_scope_summary"] = (
            plan["mode"]
            + "；允许区域："
            + (
                "、".join(c["page"] + " " + c["selector"] for c in plan["changes"])
                or "无（复用基线）"
            )
            + "；保留流程 "
            + str(len(plan["flows"]))
        )
        state["prototype_baseline_repairs"] = (
            "；".join(r["reason"] for r in plan["repairs"]) or "无"
        )
        engine.print(
            "原型修改范围已冻结："
            + plan["mode"]
            + "；新增/改动区域 "
            + str(len(plan["changes"]))
            + "；保留流程 "
            + str(len(plan["flows"]))
        )
        engine.print(
            "基线补全：" + ("；".join(r["reason"] for r in plan["repairs"]) or "无")
        )
        return engine.advance(
            work,
            "prototype",
            context=state,
            operation=op,
            operations=[browser, *([original_op] if plan["repairs"] else [])],
        )
    scope_id = state["prototype_scope"]
    value = get(engine.store, "scope", scope_id)
    validate_inputs(ctx, value)
    validate_decisions(engine, work, value)
    plan = value["scope"]
    if phase == "prototype":
        if not retained:
            shutil.rmtree(
                ctx.workspace_root / ".auto-agents/docs/prototype_preparation",
                ignore_errors=True,
            )
        if plan["mode"] == "reuse":
            install(ctx.workspace_root, prefix, decoded(value["baseline"]))
            state["prototype_artifacts"] = [
                prefix + "/" + n for n in decoded(value["baseline"])
            ]
            return engine.advance(work, "prototype_verify", context=state)
        # A legacy unapproved result keeps its original receipt and gets the new
        # checks first. Failure may request a bounded repair, never replays it.
        if state.pop("prototype_legacy_candidate", False):
            state["prototype_artifacts"] = [
                prefix + "/" + n for n in package(ctx.workspace_root, prefix)
            ]
            return engine.advance(work, "prototype_verify", context=state)
        if not retained and not state.get("prototype_candidate_initialized"):
            install(ctx.workspace_root, prefix, decoded(value["baseline"]))
            state["prototype_candidate_initialized"] = True
            work = engine.store.transition(work, "RUNNING", context=state)
        generation_inputs = {
            "frozen_prototype_scope": record_input(engine, work, value),
            "scope_identity": scope_id,
            "candidate_path": prefix,
        }
        if value.get("design_catalog") and not ctx.contract.inputs.get("variant_only"):
            generation_inputs["design_catalog"] = value["design_catalog"]
        proposal, op = engine.model(
            work, ctx, phase, readonly=False, inputs=generation_inputs
        )
        if proposal.get("questions"):
            work = engine.store.transition(
                work,
                "RUNNING",
                context={**state, "prototype_scope_reset_on_answer": True},
            )
            return engine.wait_user(work, proposal["questions"], op)
        engine.validate_artifacts(ctx, phase, proposal)
        from .domain import validate_prototype

        if (
            value.get("selection")
            and (
                not ctx.contract.inputs.get("variant_only")
                or proposal.get("selection") is not None
            )
            and proposal.get("selection") != value["selection"]
        ):
            fail("Producer cannot change the frozen design catalog selection")
        proposal = validate_prototype(ctx, proposal, engine.config, engine.store, op)
        verify_package(
            package(ctx.workspace_root, prefix), {**plan, "baseline": value["baseline"]}
        )
        state["prototype_artifacts"] = proposal["artifacts"]
        return engine.advance(work, "prototype_verify", context=state, operation=op)
    files = package(ctx.workspace_root, prefix)
    verify_package(files, {**plan, "baseline": value["baseline"]})
    if phase == "prototype_verify":
        # Structural validation applies to reuse and imported candidates too.
        from ..frontend_design import validate_prototype_manifest

        manifest = json.loads(
            safe_file(ctx.workspace_root, prefix + "/manifest.json").read_text()
        )
        errors = validate_prototype_manifest(
            ctx.workspace_root,
            manifest,
            max_pages=engine.config.frontend_design.max_pages,
            prototype_root=ctx.workspace_root / prefix,
        )
        if errors:
            fail("Candidate manifest is invalid", errors=errors)
        if set(manifest["viewports"]) != set(plan["viewports"]):
            fail("Candidate cannot change frozen viewport coverage")
        if not ctx.contract.inputs.get("variant_only"):
            from ..frontend_design import frontend_design_artifact_hashes

            lock_path = (
                ctx.workspace_root / ".auto-agents/docs/frontend_design.lock.json"
            )
            lock = (
                json.loads(lock_path.read_text())
                if lock_path.is_file()
                else {
                    "version": 1,
                    "source": {"kind": "user", "refs": []},
                    "candidates": [],
                }
            )
            lock.update(
                status="pending_approval",
                prototype={"manifest_ref": prefix + "/manifest.json", **manifest},
                artifact_sha256=frontend_design_artifact_hashes(ctx.workspace_root),
                design_path=(
                    "DESIGN.md" if (ctx.workspace_root / "DESIGN.md").is_file() else ""
                ),
                prototype_scope=scope_id,
            )
            lock.pop("contract_sha256", None)
            lock.pop("prototype_certificate", None)
            lock_path.write_text(canonical(lock))
            state["prototype_artifacts"] = sorted(
                set(state["prototype_artifacts"])
                | set(lock["artifact_sha256"])
                | {lock_path.relative_to(ctx.workspace_root).as_posix()}
            )
        report, op = browser_effect(engine, work, ctx, files, value, "candidate")
        expected = len(plan["checks"]) * len(plan["viewports"])
        if (
            report["failures"]
            or report["executed"] != expected
            or not report["screenshots"]
        ):
            fail("Candidate browser checks failed", failures=report["failures"])
        if plan["mode"] != "create":
            excluded_pages = {
                c["page"]
                for c in plan["changes"]
                if c.get("whole_page") or plan["mode"] == "redesign"
            }
            excluded_keys = {
                "/" + page + "/initial"
                for page in {
                    *[p for p in value["baseline"] if p.endswith(".html")],
                    *[c["page"] for c in plan["checks"]],
                    *[c["page"] for c in plan["changes"]],
                }
                if page_file(page) in excluded_pages
            } | {
                "/" + c["id"] for c in plan["checks"]
                if page_file(c["page"]) in excluded_pages
            }

            def preserved(observations):
                return {
                    k: v
                    for k, v in observations.items()
                    if not any(k.endswith(suffix) for suffix in excluded_keys)
                }

            if preserved(report["observations"]) != preserved(
                value["baseline_observations"]
            ):
                fail(
                    "Candidate changed DOM, defaults, behavior or style outside frozen scope"
                )
        verified = digest([scope_id, encoded(files)])
        progressed = state.get("prototype_verified") != verified
        state.update(
            prototype_browser=op,
            prototype_package=digest(encoded(files)),
            prototype_verified=verified,
        )
        updated = engine.advance(work, "prototype_review", context=state, operation=op)
        if progressed:
            engine.store.event(
                work["id"],
                "verified_progress",
                {"best": verified, "kind": "prototype", "checks": report["executed"]},
            )
        return updated
    if phase == "prototype_review":
        if digest(encoded(files)) != state["prototype_package"]:
            fail("Candidate changed after browser verification")
        native = engine.store.outcome(state["prototype_browser"])
        report = native["payload"]
        proposal, op = engine.model(
            work,
            ctx,
            phase,
            inputs={
                "frozen_prototype_scope": record_input(engine, work, value),
                "browser_verification": report_input(
                    engine, state["prototype_browser"]
                ),
                "candidate_package": {
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in files.items()
                },
                "candidate_path": prefix,
                "candidate_digest": state["prototype_package"],
                "baseline_screenshots": [
                    str(p) for p in images(engine, value["baseline_browser"])
                ],
            },
            attachments=images(engine, value["baseline_browser"])
            + images(engine, state["prototype_browser"]),
        )
        if proposal.get("questions"):
            work = engine.store.transition(
                work,
                "RUNNING",
                context={**state, "prototype_scope_reset_on_answer": True},
            )
            return engine.wait_user(work, proposal["questions"], op)
        if (
            proposal.get("decision") != "pass"
            or proposal.get("coverage_complete") is not True
            or not str(proposal.get("reason", "")).strip()
        ):
            fail("Independent prototype review did not pass", review=proposal)
        cert = certificate(
            engine, work, ctx, files, scope_id, state["prototype_browser"], op
        )
        state["prototype_certificate"] = cert
        require_certificate(engine, work, ctx, files, cert)
        candidate = engine.workspaces.candidate(
            work, ctx.workspace_root, ctx.contract.source, ctx.contract.scope
        )
        state["candidate"] = candidate
        artifacts = state["prototype_artifacts"]
        if ctx.contract.inputs.get("variant_only"):
            return engine.complete(
                work,
                {
                    "candidate": candidate,
                    "artifacts": artifacts,
                    "prototype_certificate": cert,
                },
                operation=op,
            )
        if state.get("variants"):
            state["variants"] = [
                *state["variants"],
                {
                    "id": "revision-" + op[:12],
                    "name": "修改后的原型",
                    "path": prefix + "/home.html",
                    "status": "candidate",
                    "hashes": artifact_hashes(ctx.workspace_root, artifacts),
                    "certificate": cert,
                },
            ]
        return engine.store.transition(
            work,
            "WAITING",
            phase="design",
            context={
                **state,
                "waiting_for": "approval",
                "approval": "prototype",
                "approval_phase": "prototype_prepare",
                "approval_artifacts": artifacts,
            },
            operation=op,
        )
    fail("Unknown prototype phase")
