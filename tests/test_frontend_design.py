from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.adapters.base import AgentAdapter
from auto_agents.config import (
    design_md_path,
    frontend_design_lock_path,
    frontend_prototype_dir,
    load_run_state,
    requirements_trace_path,
    save_run_state,
)
from auto_agents.frontend_design import (
    CatalogEntry,
    CatalogSnapshot,
    AwesomeDesignCatalogClient,
    FrontendDesignUnavailable,
    discover_existing_frontend,
    frontend_design_artifact_hashes,
    load_frontend_design_lock,
    parse_catalog_entries,
    validate_catalog_selection,
    validate_frontend_design_artifacts,
    validate_prototype_manifest,
)
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentRequest, AgentResult
from auto_agents.orchestrator import Orchestrator


HTML = "<!doctype html><html><head><meta name='viewport' content='width=device-width'></head><body>ok</body></html>"


class PrototypeAdapter(AgentAdapter):
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def run(self, request: AgentRequest) -> AgentResult:
        self.calls.append(request.attempt_id)
        if request.attempt_id.startswith("prototype-select"):
            write_json(
                self.project_root / ".auto-agents/docs/frontend_design/selection.json",
                {
                    "selected_slug": "alpha",
                    "candidates": [
                        {"slug": "alpha", "score": 95, "rationale": "best domain fit", "risks": []},
                        {"slug": "beta", "score": 82, "rationale": "good fallback", "risks": []},
                        {"slug": "gamma", "score": 70, "rationale": "usable but generic", "risks": []},
                    ],
                },
            )
        elif request.attempt_id.startswith("prototype-generate"):
            root = frontend_prototype_dir(self.project_root)
            write_text(root / "index.html", HTML)
            write_text(root / "home.html", HTML)
            write_json(
                root / "manifest.json",
                {
                    "version": 1,
                    "index_ref": ".auto-agents/docs/frontend_prototype/index.html",
                    "viewports": ["1440x900", "390x844"],
                    "pages": [
                        {
                            "id": "surface-home",
                            "title": "Home",
                            "route": "/",
                            "html_ref": ".auto-agents/docs/frontend_prototype/home.html",
                            "requirement_ids": ["REQ-001"],
                        }
                    ],
                },
            )
        write_text(request.output_path, "ok\n")
        return AgentResult(
            ok=True,
            command=["prototype-test"],
            output_path=request.output_path,
            summary="ok",
            stdout="ok",
            returncode=0,
        )


def write_frontend_trace(project_root: Path) -> None:
    write_json(
        requirements_trace_path(project_root),
        {
            "version": 1,
            "frontend_scope": {
                "requested": True,
                "surfaces": [
                    {
                        "id": "surface-home",
                        "name": "Home",
                        "route": "/",
                        "priority": "core",
                        "purpose": "Primary landing page",
                        "key_states": ["default"],
                        "requirement_ids": ["REQ-001"],
                    }
                ],
            },
            "requirements": [],
        },
    )


class FrontendDesignTests(unittest.TestCase):
    def test_catalog_network_failure_uses_complete_cache_or_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = AwesomeDesignCatalogClient(
                root,
                repository="VoltAgent/awesome-design-md",
                requested_ref="main",
                timeout_seconds=1,
            )
            with patch.object(client, "_resolve_ref", side_effect=urllib.error.URLError("offline")):
                with self.assertRaises(FrontendDesignUnavailable):
                    client.load()

            sha = "c" * 40
            cache = root / ".auto-agents/cache/awesome-design-md" / sha
            write_text(cache / ".complete", "done\n")
            write_text(cache / "LICENSE", "MIT\n")
            write_text(cache / "design-md/alpha/DESIGN.md", "# Alpha\n")
            write_text(
                cache / "README.md",
                "### SaaS\n- [**Alpha**](https://getdesign.md/alpha/design-md) - Cached design\n",
            )
            with patch.object(client, "_resolve_ref", side_effect=urllib.error.URLError("offline")):
                snapshot = client.load()
            self.assertTrue(snapshot.from_cache)
            self.assertEqual(snapshot.commit_sha, sha)
            self.assertEqual(snapshot.entries[0].slug, "alpha")

    def test_frontend_discovery_ignores_docs_and_detects_real_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_text(root / "docs/example.html", HTML)
            write_text(root / "tests/fixture.tsx", "export const Fixture = () => null")
            self.assertFalse(discover_existing_frontend(root).existing_frontend)

            write_text(root / "src/pages/Home.tsx", "export const Home = () => <main />")
            result = discover_existing_frontend(root)
            self.assertTrue(result.existing_frontend)
            self.assertIn("src/pages/Home.tsx", result.evidence)

    def test_catalog_parser_and_selection_require_unique_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for slug in ("alpha", "beta", "gamma"):
                write_text(root / f"design-md/{slug}/DESIGN.md", f"# {slug}\n")
            readme = "\n".join(
                [
                    "### SaaS",
                    "- [**Alpha**](https://getdesign.md/alpha/design-md) - Dense dashboard",
                    "- [**Beta**](https://getdesign.md/beta/design-md) - Friendly workspace",
                    "- [**Gamma**](https://getdesign.md/gamma/design-md) - Minimal product",
                ]
            )
            entries = parse_catalog_entries(readme, root)
            snapshot = CatalogSnapshot("VoltAgent/awesome-design-md", "main", "a" * 40, root, tuple(entries), False)
            selected, candidates = validate_catalog_selection(
                {
                    "selected_slug": "alpha",
                    "candidates": [
                        {"slug": "alpha", "score": 90, "rationale": "best", "risks": []},
                        {"slug": "beta", "score": 80, "rationale": "second", "risks": []},
                        {"slug": "gamma", "score": 70, "rationale": "third", "risks": []},
                    ],
                },
                snapshot,
            )
            self.assertEqual(selected.slug, "alpha")
            self.assertEqual(len(candidates), 3)

            with self.assertRaisesRegex(ValueError, "unique highest-scoring"):
                validate_catalog_selection(
                    {
                        "selected_slug": "alpha",
                        "candidates": [
                            {"slug": "alpha", "score": 90, "rationale": "best", "risks": []},
                            {"slug": "beta", "score": 90, "rationale": "tie", "risks": []},
                            {"slug": "gamma", "score": 70, "rationale": "third", "risks": []},
                        ],
                    },
                    snapshot,
                )

    def test_manifest_rejects_remote_assets_in_page_and_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prototype = frontend_prototype_dir(root)
            write_text(prototype / "index.html", HTML.replace("</body>", "<script src='https://cdn.test/x.js'></script></body>"))
            write_text(prototype / "home.html", HTML.replace("</body>", "<img src='https://cdn.test/x.png'></body>"))
            payload = {
                "version": 1,
                "index_ref": ".auto-agents/docs/frontend_prototype/index.html",
                "viewports": ["1440x900"],
                "pages": [
                    {
                        "id": "home",
                        "title": "Home",
                        "route": "/",
                        "html_ref": ".auto-agents/docs/frontend_prototype/home.html",
                        "requirement_ids": ["REQ-001"],
                    }
                ],
            }
            errors = validate_prototype_manifest(root, payload, max_pages=3)
            self.assertTrue(any("remote or file URL" in error for error in errors))
            self.assertTrue(any("script src" in error for error in errors))

    def test_user_design_flow_pauses_for_manual_prototype_even_with_auto_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# New frontend\n")
            write_text(design_md_path(root), "# User design\n")
            write_frontend_trace(root)
            state = load_run_state(root)
            state.stage_summaries = {"clarify": "done"}
            state.current_stage = "clarify"
            state.approved_gates = ["requirements"]
            save_run_state(root, state)

            orchestrator = Orchestrator(root)
            adapter = PrototypeAdapter(root)
            orchestrator.adapter = adapter
            result = orchestrator.run(root / "spec.md", auto_approve=True, skip_validate=True)

            self.assertEqual(result.status, "paused")
            self.assertEqual(result.pending_approval, "prototype")
            self.assertNotIn("design", result.stage_summaries)
            self.assertEqual(load_frontend_design_lock(root)["source"]["kind"], "user")
            trace = json.loads(requirements_trace_path(root).read_text(encoding="utf-8"))
            self.assertEqual(trace["frontend_surfaces"][0]["prototype_refs"][0], ".auto-agents/docs/frontend_prototype/home.html")

    def test_external_catalog_design_is_copied_byte_for_byte_and_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# New frontend\n")
            write_frontend_trace(root)
            catalog = Path(tmp) / "catalog"
            write_text(catalog / "LICENSE", "MIT license text\n")
            entries = []
            for slug in ("alpha", "beta", "gamma"):
                content = b"# Exact upstream bytes\r\n" if slug == "alpha" else f"# {slug}\n".encode()
                path = catalog / f"design-md/{slug}/DESIGN.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                entries.append(CatalogEntry(slug.title(), slug, "SaaS", slug, f"design-md/{slug}/DESIGN.md"))
            snapshot = CatalogSnapshot(
                "VoltAgent/awesome-design-md", "main", "b" * 40, catalog, tuple(entries), False
            )
            orchestrator = Orchestrator(root)
            orchestrator.adapter = PrototypeAdapter(root)
            state = load_run_state(root)
            with patch("auto_agents.orchestrator.AwesomeDesignCatalogClient.load", return_value=snapshot):
                state = orchestrator._run_prototype_stage(state, root / "spec.md")

            self.assertEqual(design_md_path(root).read_bytes(), b"# Exact upstream bytes\r\n")
            lock = load_frontend_design_lock(root)
            self.assertEqual(lock["source"]["commit_sha"], "b" * 40)
            self.assertEqual(lock["source"]["slug"], "alpha")
            self.assertFalse(validate_frontend_design_artifacts(root, lock, require_approved=False))

    def test_approval_pins_artifacts_and_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            prototype = frontend_prototype_dir(root)
            write_text(design_md_path(root), "# User design\n")
            write_text(prototype / "index.html", HTML)
            write_text(prototype / "home.html", HTML)
            pages = [
                {
                    "id": "home",
                    "title": "Home",
                    "route": "/",
                    "html_ref": ".auto-agents/docs/frontend_prototype/home.html",
                    "requirement_ids": ["REQ-001"],
                }
            ]
            write_json(
                prototype / "manifest.json",
                {
                    "version": 1,
                    "index_ref": ".auto-agents/docs/frontend_prototype/index.html",
                    "viewports": ["1440x900"],
                    "pages": pages,
                },
            )
            lock = {
                "version": 1,
                "status": "pending_approval",
                "source": {"kind": "user", "refs": ["DESIGN.md"]},
                "design_path": "DESIGN.md",
                "prototype": {
                    "manifest_ref": ".auto-agents/docs/frontend_prototype/manifest.json",
                    "index_ref": ".auto-agents/docs/frontend_prototype/index.html",
                    "viewports": ["1440x900"],
                    "pages": pages,
                },
            }
            lock["artifact_sha256"] = frontend_design_artifact_hashes(root)
            write_json(frontend_design_lock_path(root), lock)
            state = load_run_state(root)
            state.current_stage = "prototype"
            state.pending_approval = "prototype"
            state.status = "paused"
            save_run_state(root, state)

            approved = Orchestrator(root).approve("prototype")
            self.assertIn("prototype", approved.approved_gates)
            self.assertIn("approved DESIGN.md", (root / "AGENTS.md").read_text(encoding="utf-8"))

            rejected = Orchestrator(root).reject("prototype", "make it calmer")
            self.assertFalse(rejected.resume_context["reselect_frontend_design"])
            self.assertEqual(load_frontend_design_lock(root)["status"], "pending_approval")
            rejected = Orchestrator(root).reject(
                "prototype",
                "choose a different system",
                reselect_design=True,
            )
            self.assertTrue(rejected.resume_context["reselect_frontend_design"])

            write_text(prototype / "home.html", HTML.replace("ok", "tampered"))
            errors = validate_frontend_design_artifacts(
                root,
                load_frontend_design_lock(root),
                require_approved=True,
            )
            self.assertIn("approved frontend design artifacts have drifted from their locked hashes", errors)


if __name__ == "__main__":
    unittest.main()
