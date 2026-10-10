"""Prototype candidates use the same work ledger and explicit approvals."""

from pathlib import Path
import functools
import http.server
import json
import shutil
import subprocess
import urllib.parse
import uuid

from .types import ControlError
from .workspace import git
from .quality import artifact_hashes


def owner(store, identity=None):
    selected = [
        w
        for w in ([store.work(identity)] if identity else store.works())
        if w["mode"] == "run"
        and w["context"].get("frontend")
        and w["status"] not in {"COMPLETED", "CANCELLED"}
    ]
    if len(selected) != 1:
        raise ControlError("selection", "Choose an active frontend run with --session")
    return selected[0]


def candidates(engine, work):
    ctx = engine.context(work)
    rows = list(work["context"].get("variants", []))
    canonical = ".auto-agents/docs/frontend_prototype/home.html"
    if not rows and (ctx.workspace_root / canonical).exists():
        paths = work["context"].get("approval_artifacts") or [canonical]
        rows = [
            {
                "id": "initial",
                "name": "Initial prototype",
                "path": canonical,
                "status": "candidate",
                "hashes": artifact_hashes(ctx.workspace_root, paths),
            }
        ]
    return rows


def generate(engine, work, prompt, *, name="", base=""):
    if work["status"] != "WAITING" or work["context"].get("approval") != "prototype":
        raise ControlError(
            "prototype", "Additional candidates require the prototype decision gate"
        )
    ctx = engine.context(work)
    rows = candidates(engine, work)
    if base and not any(row["id"] == base for row in rows):
        raise ControlError("prototype", "Unknown base candidate")
    identity = uuid.uuid4().hex[:12]
    prefix = ".auto-agents/docs/frontend_prototype_variants/" + identity
    addon = engine.store.addon(
        work,
        "Additional prototype: " + prompt,
        git(ctx.workspace_root, "rev-parse", "HEAD"),
        scope=[prefix],
        inputs={"variant_only": identity, "base_variant": base},
        phase="prototype",
    )
    result = engine.drive(addon["id"])
    if result["status"] != "COMPLETED":
        return result
    engine.workspaces.adopt(work, result["result"]["candidate"])
    paths = result["result"]["artifacts"]
    rows.append(
        {
            "id": identity,
            "name": name or identity,
            "path": prefix + "/home.html",
            "status": "candidate",
            "hashes": artifact_hashes(ctx.workspace_root, paths),
        }
    )
    candidate = engine.workspaces.candidate(
        work, ctx.workspace_root, ctx.contract.source
    )
    return engine.store.transition(
        work,
        "WAITING",
        context={**work["context"], "variants": rows, "candidate": candidate},
    )


def approve(engine, work, identity=""):
    ctx = engine.context(work)
    rows = candidates(engine, work)
    live = [r for r in rows if r["status"] == "candidate"]
    if not identity and len(live) == 1:
        identity = live[0]["id"]
    selected = next((r for r in live if r["id"] == identity), None)
    if not selected:
        raise ControlError("prototype", "Pass --variant for the candidate to approve")
    if (
        artifact_hashes(ctx.workspace_root, list(selected["hashes"]))
        != selected["hashes"]
    ):
        raise ControlError("prototype", "Candidate changed since it was presented")
    canonical = ctx.workspace_root / ".auto-agents/docs/frontend_prototype"
    source = (ctx.workspace_root / selected["path"]).parent
    if source != canonical:
        from .quality import safe_file

        for name in work["context"].get("approval_artifacts", []):
            if name.startswith(".auto-agents/docs/frontend_prototype/"):
                safe_file(ctx.workspace_root, name).unlink(missing_ok=True)
        for name in selected["hashes"]:
            src = safe_file(ctx.workspace_root, name)
            relative = src.relative_to(source)
            dst = canonical / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        import json

        manifest = canonical / "manifest.json"
        prefix = source.relative_to(ctx.workspace_root).as_posix()
        data = manifest.read_text().replace(
            prefix, ".auto-agents/docs/frontend_prototype"
        )
        manifest.write_text(data)
    for row in rows:
        row["status"] = "approved" if row["id"] == identity else "rejected"
    # Rejected metadata remains a small tombstone. Candidate working copies
    # are removed now; immutable Git objects provide audit, never execution.
    for row in rows:
        path = ctx.workspace_root / row["path"]
        if "/frontend_prototype_variants/" in row["path"] and path.parent.is_dir():
            shutil.rmtree(path.parent)
    state = {**work["context"], "variants": rows, "approved_variant": identity}
    from ..frontend_design import (
        frontend_design_artifact_hashes,
        frontend_design_contract_sha256,
    )
    import json

    lock_path = ctx.workspace_root / ".auto-agents/docs/frontend_design.lock.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text())
        lock["status"] = "approved"
        lock["variant_id"] = identity
        lock["prototype"] = {
            "manifest_ref": ".auto-agents/docs/frontend_prototype/manifest.json",
            **json.loads((canonical / "manifest.json").read_text()),
        }
        lock["artifact_sha256"] = frontend_design_artifact_hashes(ctx.workspace_root)
        lock["contract_sha256"] = frontend_design_contract_sha256(lock)
        lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2))
    state["approval_artifacts"] = list(
        frontend_design_artifact_hashes(ctx.workspace_root)
    )
    if lock_path.is_file():
        state["approval_artifacts"].append(
            lock_path.relative_to(ctx.workspace_root).as_posix()
        )
    for row in state["variants"]:
        if row["status"] == "approved":
            row["path"] = ".auto-agents/docs/frontend_prototype/home.html"
            row["hashes"] = artifact_hashes(
                ctx.workspace_root, state["approval_artifacts"]
            )
    state["candidate"] = engine.workspaces.candidate(
        work, ctx.workspace_root, ctx.contract.source
    )
    return state


def reject(engine, work, identities=(), *, all_except="", reason=""):
    rows = candidates(engine, work)
    ctx = engine.context(work)
    selected = (
        {r["id"] for r in rows if r["status"] == "candidate" and r["id"] != all_except}
        if all_except
        else set(identities)
    )
    if not selected or not selected <= {
        r["id"] for r in rows if r["status"] == "candidate"
    }:
        raise ControlError("prototype", "Unknown prototype candidate")
    for row in rows:
        if row["id"] not in selected:
            continue
        row["status"] = "rejected"
        row["reason"] = reason
        path = ctx.workspace_root / row["path"]
        if (
            "/frontend_prototype_variants/" in row["path"]
            and not path.parent.is_symlink()
        ):
            shutil.rmtree(path.parent)
    return engine.store.transition(
        work,
        "WAITING",
        context={**work["context"], "variants": rows, "feedback": reason},
    )


def preview_file(workspace, ref, revision=""):
    """Serve owned package files, including immutable source versions."""
    from .quality import safe_file
    from .access import product_path

    if not product_path(ref):
        return None
    if revision:
        entry = git(workspace, "ls-tree", "-z", revision, "--", ref)
        if not entry.startswith(("100644 blob ", "100755 blob ")):
            return None
        result = subprocess.run(
            ["git", "-C", str(workspace), "show", revision + ":" + ref],
            capture_output=True,
        )
        return result.stdout if result.returncode == 0 else None
    try:
        path = safe_file(workspace, ref)
    except ControlError:
        return None
    return path.read_bytes() if path.is_file() else None


def preview_gallery(engine, work):
    """Build a comparison view without writing or approving any artifacts."""
    workspace = engine.context(work).workspace_root
    canonical = ".auto-agents/docs/frontend_prototype"
    source = engine.store.contract(work["contract"]).source
    entries, routes = [], {}

    def package(row, prefix, revision=""):
        raw = preview_file(workspace, prefix + "/manifest.json", revision)
        if raw is None:
            return
        try:
            manifest = json.loads(raw)
        except ValueError as error:
            raise ControlError("prototype_manifest", "Prototype manifest is invalid") from error
        if not isinstance(manifest, dict):
            raise ControlError("prototype_manifest", "Prototype manifest must be an object")
        raw_pages = manifest.get("pages", [])
        artifacts = manifest.get("artifacts", [])
        if not isinstance(raw_pages, list) or not isinstance(artifacts, list):
            raise ControlError("prototype_manifest", "Prototype pages and artifacts must be arrays")
        pages = [
            p for p in raw_pages
            if isinstance(p, dict) and isinstance(p.get("html_ref"), str)
        ]
        index = manifest.get("index_ref")
        if isinstance(index, str) and index and not any(p.get("html_ref") == index for p in pages):
            pages.append({"id": "index", "title": "原型总览", "html_ref": index})
        refs = {p["html_ref"] for p in pages}
        refs.update(ref for ref in artifacts if isinstance(ref, str))
        refs.add(prefix + "/manifest.json")
        urls = {}
        from .access import product_path

        for ref in refs:
            if not isinstance(ref, str) or not product_path(ref):
                continue
            try:
                relative = Path(ref).relative_to(prefix).as_posix()
            except ValueError:
                continue
            url = "/variants/" + row["id"] + "/" + relative
            routes[url] = (workspace, ref, revision)
            urls[ref] = urllib.parse.quote(url, safe="/")
            if prefix == canonical and not revision:
                # Keep existing direct links to the current package working.
                routes["/" + relative] = (workspace, ref, revision)
        entries.append(
            {
                **row,
                "prototype": {"pages": [
                    {**p, "preview_url": urls[p["html_ref"]]}
                    for p in pages if p["html_ref"] in urls
                ]},
            }
        )

    from ..prototype_variants import load_registry, preview_root_for_variant

    registry = load_registry(engine.store.project, include_virtual_legacy=False)
    for row in registry.get("variants", []):
        if not isinstance(row, dict) or row.get("status") != "approved":
            continue
        try:
            prefix = preview_root_for_variant(engine.store.project, row).relative_to(
                engine.store.project
            ).as_posix()
        except ValueError:
            continue
        package(
            {"id": "history-" + row["id"], "name": "历史原型 · " + row.get("name", row["id"]),
             "status": "baseline"},
            prefix, source,
        )
    package(
        {"id": "baseline", "name": "本轮开始前的原型", "status": "baseline"},
        canonical, source,
    )
    for row in candidates(engine, work):
        if row["status"] in {"candidate", "approved"}:
            package(row, Path(row["path"]).parent.as_posix())
    if not entries:
        raise ControlError("prototype", "No prototype package is available to preview")
    from ..prototype_variants import gallery_html

    return gallery_html(workspace, {"variants": entries}).encode("utf-8"), routes


class PrototypeGalleryHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, gallery, routes, **kwargs):
        self.gallery, self.routes = gallery, routes
        super().__init__(*args, **kwargs)

    def do_GET(self):
        import mimetypes

        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/":
            payload, content_type = self.gallery, "text/html; charset=utf-8"
        elif path in self.routes:
            workspace, ref, revision = self.routes[path]
            payload = preview_file(workspace, ref, revision)
            content_type = mimetypes.guess_type(ref)[0] or "application/octet-stream"
        else:
            payload = None
        if payload is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def preview(engine, work, *, host="127.0.0.1", port=0):
    if not 0 <= port <= 65535:
        raise ControlError("port", "Invalid preview port")
    gallery, routes = preview_gallery(engine, work)
    handler = functools.partial(
        PrototypeGalleryHandler, gallery=gallery, routes=routes
    )
    server = http.server.ThreadingHTTPServer((host, port), handler)
    print(
        "原型对比：http://"
        + host
        + ":"
        + str(server.server_address[1])
        + "/",
        flush=True,
    )
    print("在浏览器打开上述地址；按 Ctrl+C 停止预览，然后执行审批命令。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
