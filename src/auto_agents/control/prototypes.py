"""Prototype candidates use the same work ledger and explicit approvals."""

from pathlib import Path
import functools
import http.server
import shutil
import uuid

from .types import ControlError
from .workspace import git
from .quality import artifact_hashes


def owner(store):
    selected = [
        w
        for w in store.works()
        if w["mode"] == "run"
        and w["context"].get("frontend")
        and w["status"] not in {"COMPLETED", "CANCELLED"}
    ]
    if len(selected) != 1:
        raise ControlError("selection", "Choose a unique active frontend run")
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


def preview(root, *, host="127.0.0.1", port=0):
    if not 0 <= port <= 65535:
        raise ControlError("port", "Invalid preview port")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(root)
    )
    server = http.server.ThreadingHTTPServer((host, port), handler)
    print(
        "Prototype: http://"
        + host
        + ":"
        + str(server.server_address[1])
        + "/home.html",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
