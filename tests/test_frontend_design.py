from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.adapters.base import AgentAdapter
from auto_agents.config import (
    conversation_history_path,
    design_md_path,
    frontend_design_lock_path,
    frontend_prototype_dir,
    frontend_prototype_variants_registry_path,
    load_run_state,
    requirements_trace_path,
    save_run_state,
)
from auto_agents.cli import build_parser, main
from auto_agents.frontend_design import (
    CatalogEntry,
    CatalogSnapshot,
    AwesomeDesignCatalogClient,
    FrontendDesignUnavailable,
    discover_existing_frontend,
    frontend_design_artifact_hashes,
    frontend_scope_requested,
    load_frontend_design_lock,
    parse_catalog_entries,
    selected_surface_specs,
    validate_catalog_selection,
    validate_frontend_design_artifacts,
    validate_prototype_manifest,
)
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentRequest, AgentResult, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.prototype_variants import (
    candidate_variants,
    ensure_registry,
    gallery_html,
    load_registry,
    registry_variants,
    variant_dir,
)


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
            match = re.search(r"Write JSON only to: (.+)", request.prompt)
            selection_path = Path(match.group(1).strip()) if match else self.project_root / ".auto-agents/docs/frontend_design/selection.json"
            write_json(
                selection_path,
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
            match = re.search(r"Write all output only inside: (.+)", request.prompt)
            root = Path(match.group(1).strip()) if match else frontend_prototype_dir(self.project_root)
            variant_marker = root.parent.name
            variant_html = HTML.replace("ok", variant_marker)
            write_text(root / "index.html", variant_html)
            trace = json.loads(
                requirements_trace_path(self.project_root).read_text(encoding="utf-8")
            )
            surfaces = selected_surface_specs(trace, max_pages=3)
            pages = []
            for index, surface in enumerate(surfaces, start=1):
                filename = "home.html" if index == 1 else f"surface-{index}.html"
                write_text(root / filename, variant_html)
                pages.append(
                    {
                        "id": surface["id"],
                        "title": surface["name"],
                        "route": surface["route"],
                        "html_ref": (root / filename).relative_to(self.project_root).as_posix(),
                        "requirement_ids": surface["requirement_ids"],
                    }
                )
            write_json(
                root / "manifest.json",
                {
                    "version": 1,
                    "index_ref": (root / "index.html").relative_to(self.project_root).as_posix(),
                    "viewports": ["1440x900", "390x844"],
                    "pages": pages,
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


def write_frontend_fidelity_trace(project_root: Path) -> None:
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
            "frontend_surfaces": [
                {
                    "name": "Home",
                    "route": "/",
                    "prototype_refs": ["specs/prototype/home.html", "DESIGN.md"],
                    "viewports": ["1440x900"],
                    "requirement_ids": ["REQ-001"],
                }
            ],
            "requirements": [
                {
                    "id": "REQ-001",
                    "status": "active",
                    "priority": "mandatory",
                    "text": "The home page must match the supplied prototype.",
                    "acceptance_oracles": ["The rendered home page matches the prototype."],
                    "oracle_type": "mixed",
                    "oracle_strength": "human",
                    "evidence_boundary": "system_boundary",
                    "forbidden_proxy_oracles": [],
                }
            ],
        },
    )


def frontend_task(*, status: str = "in_progress") -> TaskSpec:
    return TaskSpec(
        task_id="task-frontend",
        title="Implement the home surface",
        description="Implement the approved home surface.",
        acceptance=["The rendered home page matches the prototype."],
        requirement_ids=["REQ-001"],
        status=status,
        evidence_preflight={
            "decision": "READY",
            "reason": "Browser evidence is feasible.",
            "checklist": ["Capture the rendered page."],
            "fingerprint": "cached-ready",
        },
    )


class FrontendDesignTests(unittest.TestCase):
    def test_prototype_cli_parses_generate_and_lists_virtual_legacy_without_migration(self) -> None:
        args = build_parser().parse_args(
            ["prototype", "generate", "--project", "/tmp/demo", "--prompt", "calmer"]
        )
        self.assertEqual(args.prototype_command, "generate")
        self.assertEqual(args.prompt, "calmer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(design_md_path(root), "# User design\n")
            prototype = frontend_prototype_dir(root)
            write_text(prototype / "index.html", HTML)
            write_text(prototype / "home.html", HTML)
            pages = [{"id": "home", "title": "Home", "route": "/", "html_ref": ".auto-agents/docs/frontend_prototype/home.html", "requirement_ids": ["REQ-001"]}]
            write_json(prototype / "manifest.json", {"version": 1, "index_ref": ".auto-agents/docs/frontend_prototype/index.html", "viewports": ["1440x900"], "pages": pages})
            lock = {"version": 1, "status": "pending_approval", "source": {"kind": "user", "refs": ["DESIGN.md"]}, "design_path": "DESIGN.md", "prototype": {"manifest_ref": ".auto-agents/docs/frontend_prototype/manifest.json", "index_ref": ".auto-agents/docs/frontend_prototype/index.html", "viewports": ["1440x900"], "pages": pages}}
            lock["artifact_sha256"] = frontend_design_artifact_hashes(root)
            write_json(frontend_design_lock_path(root), lock)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["prototype", "list", "--project", str(root)]), 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["variants"][0]["legacy_virtual"])
            self.assertFalse(frontend_prototype_variants_registry_path(root).exists())

    def test_multiple_variants_are_compared_and_approval_deletes_unselected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# New frontend\n")
            write_text(design_md_path(root), "# User design\n")
            write_frontend_trace(root)
            state = load_run_state(root)
            state.stage_summaries = {"clarify": "done"}
            state.approved_gates = ["requirements"]
            state.resume_context["spec_file"] = str(root / "spec.md")
            save_run_state(root, state)

            orchestrator = Orchestrator(root)
            orchestrator.adapter = PrototypeAdapter(root)
            paused = orchestrator.run(root / "spec.md", auto_approve=True, skip_validate=True)
            first = candidate_variants(load_registry(root))[0]
            second = orchestrator.generate_prototype_variant(
                prompt="Reduce controls and add more whitespace.",
                name="Calm",
                base_variant_id=str(first["id"]),
            )
            registry = load_registry(root)
            self.assertEqual(len(candidate_variants(registry)), 2)
            self.assertEqual(second["design_decision"]["design_action"], "reuse")
            self.assertIn(str(first["id"]), gallery_html(root, registry))
            self.assertIn(str(second["id"]), gallery_html(root, registry))

            approved = orchestrator.approve(
                "prototype",
                prototype_variant_id=str(second["id"]),
            )
            self.assertIn("prototype", approved.approved_gates)
            registry = load_registry(root)
            statuses = {str(item["id"]): str(item["status"]) for item in registry_variants(registry)}
            self.assertEqual(statuses[str(second["id"])], "approved")
            self.assertEqual(statuses[str(first["id"])], "rejected")
            self.assertFalse(variant_dir(root, str(first["id"])).exists())
            self.assertEqual(load_frontend_design_lock(root)["variant_id"], second["id"])
            with self.assertRaisesRegex(RuntimeError, "only be generated while the prototype gate is paused"):
                orchestrator.generate_prototype_variant(prompt="another")

    def test_prompt_can_automatically_reselect_design(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# New frontend\n")
            write_text(design_md_path(root), "# User design\n")
            write_frontend_trace(root)
            state = load_run_state(root)
            state.status = "paused"
            state.current_stage = "prototype"
            state.pending_approval = "prototype"
            state.resume_context["spec_file"] = str(root / "spec.md")
            save_run_state(root, state)
            orchestrator = Orchestrator(root)
            orchestrator.adapter = PrototypeAdapter(root)
            orchestrator._run_prototype_stage(state, root / "spec.md")
            base = candidate_variants(load_registry(root))[0]

            catalog = Path(tmp) / "catalog"
            write_text(catalog / "LICENSE", "MIT\n")
            entries = []
            for slug in ("alpha", "beta", "gamma"):
                write_text(catalog / f"design-md/{slug}/DESIGN.md", f"# {slug}\n")
                entries.append(CatalogEntry(slug.title(), slug, "SaaS", slug, f"design-md/{slug}/DESIGN.md"))
            snapshot = CatalogSnapshot("VoltAgent/awesome-design-md", "main", "d" * 40, catalog, tuple(entries), False)
            with patch("auto_agents.orchestrator.AwesomeDesignCatalogClient.load", return_value=snapshot):
                variant = orchestrator.generate_prototype_variant(
                    prompt="换一套视觉语言，使用极简风格。",
                    base_variant_id=str(base["id"]),
                )
            self.assertEqual(variant["design_decision"]["design_action"], "reselect")
            self.assertEqual(variant["source"]["kind"], "awesome-design-md")

    def test_reject_deletes_candidate_and_keeps_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# New frontend\n")
            write_text(design_md_path(root), "# User design\n")
            write_frontend_trace(root)
            state = load_run_state(root)
            state.status = "pending"
            state.current_stage = "prototype"
            state.resume_context["spec_file"] = str(root / "spec.md")
            orchestrator = Orchestrator(root)
            orchestrator.adapter = PrototypeAdapter(root)
            orchestrator._run_prototype_stage(state, root / "spec.md")
            state.status = "paused"
            state.pending_approval = "prototype"
            save_run_state(root, state)
            variant = candidate_variants(load_registry(root))[0]

            rejected = orchestrator.reject(
                "prototype",
                "not suitable",
                prototype_variant_ids=[str(variant["id"])],
            )
            self.assertEqual(rejected.status, "pending")
            self.assertEqual(rejected.rejected_stage, "prototype")
            self.assertFalse(variant_dir(root, str(variant["id"])).exists())
            tombstone = registry_variants(load_registry(root))[0]
            self.assertEqual(tombstone["status"], "rejected")
            self.assertTrue(tombstone["artifacts_deleted"])

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
            variants = candidate_variants(load_registry(root))
            self.assertEqual(len(variants), 1)
            self.assertEqual(variants[0]["source"]["kind"], "user")
            trace = json.loads(requirements_trace_path(root).read_text(encoding="utf-8"))
            self.assertNotIn("frontend_surfaces", trace)

    def test_missing_contract_rewinds_before_frontend_task_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_frontend_fidelity_trace(root)
            task = frontend_task()
            state = load_run_state(root)
            state.status = "pending"
            state.current_stage = "implement"
            state.stage_summaries = {
                "clarify": "done",
                "prototype": "Skipped: existing frontend surfaces were discovered.",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
            }
            state.approved_gates = ["requirements", "architecture", "release"]
            state.tasks = [task]
            state.resume_context["implementation_ready_tasks"] = {
                task.task_id: False
            }

            orchestrator = Orchestrator(root)
            with patch.object(
                orchestrator, "_ensure_evidence_preflight"
            ) as evidence_preflight, patch.object(
                orchestrator, "_execute_task_with_retries"
            ) as task_execution:
                result = orchestrator._execute_task_in_main_worktree(
                    state, [task], task
                )

            self.assertIs(result, state)
            evidence_preflight.assert_not_called()
            task_execution.assert_not_called()
            self.assertEqual(task.status, "pending")
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.current_stage, "prototype")
            self.assertEqual(state.pending_approval, "")
            self.assertNotIn("prototype", state.stage_summaries)
            self.assertEqual(state.approved_gates, ["requirements"])
            self.assertTrue(
                state.resume_context[
                    Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
                ]
            )
            self.assertNotIn(
                task.task_id,
                state.resume_context.get("implementation_ready_tasks", {}),
            )

    def test_stale_approved_contract_rewinds_before_frontend_task_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# Existing frontend redesign\n")
            write_text(root / "src/pages/Home.tsx", "export const Home = () => <main />")
            write_text(root / "specs/prototype/home.html", HTML)
            write_text(design_md_path(root), "# User design\n")
            write_frontend_fidelity_trace(root)

            orchestrator = Orchestrator(root)
            orchestrator.adapter = PrototypeAdapter(root)
            state = load_run_state(root)
            state.resume_context[
                Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
            ] = True
            orchestrator._run_prototype_stage(state, root / "spec.md")
            approved = orchestrator.approve("prototype")

            trace = json.loads(requirements_trace_path(root).read_text(encoding="utf-8"))
            trace["frontend_scope"]["surfaces"][0]["requirement_ids"].append("REQ-002")
            trace["requirements"].append(
                {
                    "id": "REQ-002",
                    "status": "active",
                    "priority": "mandatory",
                    "text": "Add a new interaction to the existing home surface.",
                    "acceptance_oracles": ["The interaction matches a newly approved prototype."],
                    "oracle_type": "mixed",
                    "oracle_strength": "human",
                    "evidence_boundary": "system_boundary",
                    "forbidden_proxy_oracles": [],
                    "notes": "",
                }
            )
            write_json(requirements_trace_path(root), trace)
            task = TaskSpec(
                task_id="task-redesign",
                title="Implement the new interaction",
                description="Consume the redesigned approved surface.",
                acceptance=["The interaction matches the new prototype."],
                requirement_ids=["REQ-002"],
                status="pending",
            )
            approved.current_stage = "implement"
            approved.stage_summaries = {
                "clarify": "done",
                "prototype": "Reused the approved contract.",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
            }
            approved.approved_gates = ["requirements", "prototype", "architecture", "release"]
            approved.tasks = [task]

            with patch.object(
                orchestrator, "_ensure_evidence_preflight"
            ) as evidence_preflight:
                result = orchestrator._execute_task_in_main_worktree(
                    approved, [task], task
                )

            evidence_preflight.assert_not_called()
            self.assertIs(result, approved)
            self.assertEqual(approved.current_stage, "prototype")
            self.assertEqual(approved.approved_gates, ["requirements"])
            self.assertTrue(
                approved.resume_context[
                    Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
                ]
            )

    def test_existing_frontend_redesign_regenerates_stale_contract_and_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# Existing frontend redesign\n")
            write_text(root / "src/pages/Home.tsx", "export const Home = () => <main />")
            write_text(root / "specs/prototype/home.html", HTML)
            write_text(design_md_path(root), "# User design\n")
            write_frontend_fidelity_trace(root)

            orchestrator = Orchestrator(root)
            orchestrator.adapter = PrototypeAdapter(root)
            state = load_run_state(root)
            state.resume_context[
                Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
            ] = True
            orchestrator._run_prototype_stage(state, root / "spec.md")
            approved = orchestrator.approve("prototype")

            trace = json.loads(requirements_trace_path(root).read_text(encoding="utf-8"))
            trace["frontend_scope"]["surfaces"][0]["requirement_ids"].append("REQ-002")
            trace["requirements"].append(
                {
                    "id": "REQ-002",
                    "status": "active",
                    "priority": "mandatory",
                    "text": "Add a new interaction to the existing home surface.",
                    "acceptance_oracles": ["The interaction matches a newly approved prototype."],
                    "oracle_type": "mixed",
                    "oracle_strength": "human",
                    "evidence_boundary": "system_boundary",
                    "forbidden_proxy_oracles": [],
                    "notes": "",
                }
            )
            write_json(requirements_trace_path(root), trace)
            write_text(
                conversation_history_path(root, approved.run_id),
                json.dumps(
                    [
                        {
                            "role": "user",
                            "content": (
                                "The previous requirements output was rejected.\n"
                                "Feedback:\nEvidence preflight requested CLARIFY for "
                                "task-redesign: approved prototype/manifest does not bind REQ-002."
                            ),
                        },
                        {
                            "role": "agent",
                            "content": "Please provide approved prototype artifacts or defer the UI.",
                        },
                    ],
                    ensure_ascii=False,
                ),
            )
            approved.stage_summaries = {}
            approved.current_stage = "clarify"
            approved.approved_gates = [
                "requirements",
                "prototype",
                "architecture",
                "release",
            ]
            save_run_state(root, approved)

            adapter = PrototypeAdapter(root)
            orchestrator.adapter = adapter
            result = orchestrator.run(
                root / "spec.md",
                auto_approve=True,
                skip_validate=True,
            )

            self.assertEqual(result.status, "paused")
            self.assertEqual(result.pending_approval, "prototype")
            self.assertEqual(result.approved_gates, ["requirements"])
            self.assertNotIn("design", result.stage_summaries)
            self.assertTrue(
                any(call.startswith("prototype-generate") for call in adapter.calls)
            )
            self.assertFalse(
                any(call.startswith("clarify-conv") for call in adapter.calls)
            )
            variant = candidate_variants(load_registry(root))[0]
            self.assertIn("REQ-002", variant["prototype"]["pages"][0]["requirement_ids"])
            self.assertIn("specs/prototype/home.html", variant["source"]["refs"])

    def test_preservation_only_requirement_reuses_approved_contract_without_redesign(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# Preserve the existing frontend")
            write_text(root / "src/pages/Home.tsx", "export const Home = () => <main />")
            write_text(root / "specs/prototype/home.html", HTML)
            write_text(design_md_path(root), "# User design")
            write_frontend_fidelity_trace(root)

            orchestrator = Orchestrator(root)
            adapter = PrototypeAdapter(root)
            orchestrator.adapter = adapter
            state = load_run_state(root)
            state.resume_context[
                Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
            ] = True
            orchestrator._run_prototype_stage(state, root / "spec.md")
            approved = orchestrator.approve("prototype")

            trace = json.loads(
                requirements_trace_path(root).read_text(encoding="utf-8")
            )
            trace["frontend_scope"]["surfaces"][0]["requirement_ids"].append(
                "REQ-002"
            )
            trace["requirements"].append(
                {
                    "id": "REQ-002",
                    "status": "active",
                    "priority": "mandatory",
                    "text": (
                        "Preserve the approved home layout and copy; "
                        "the backend change must not change the UI."
                    ),
                    "acceptance_oracles": [
                        "The existing rendered home page remains unchanged."
                    ],
                    "oracle_type": "mixed",
                    "oracle_strength": "human",
                    "evidence_boundary": "system_boundary",
                    "forbidden_proxy_oracles": [],
                    "notes": "preservation-only frontend contract",
                }
            )
            write_json(requirements_trace_path(root), trace)
            adapter.calls.clear()

            result = orchestrator._run_prototype_stage(
                approved,
                root / "spec.md",
            )

            self.assertIs(result, approved)
            self.assertEqual(result.pending_approval, "")
            self.assertIn("prototype", result.approved_gates)
            self.assertIn(
                "preservation-only",
                result.stage_summaries["prototype"],
            )
            self.assertFalse(
                any(call.startswith("prototype-generate") for call in adapter.calls)
            )
            lock = load_frontend_design_lock(root)
            self.assertEqual(lock["status"], "approved")
            self.assertIn(
                "REQ-002",
                lock["prototype"]["pages"][0]["requirement_ids"],
            )
            updated_trace = json.loads(
                requirements_trace_path(root).read_text(encoding="utf-8")
            )
            self.assertFalse(updated_trace["frontend_scope"]["requested"])
            self.assertEqual(updated_trace["frontend_scope"]["surfaces"], [])
            self.assertFalse(frontend_scope_requested(updated_trace))
            self.assertEqual(orchestrator._frontend_design_prompt_lines(), [])
            task_prompt = orchestrator._build_task_prompt(
                TaskSpec(
                    task_id="task-preservation",
                    title="Verify preserved home",
                    description="Run preservation regression.",
                    acceptance=["The approved home remains unchanged."],
                    requirement_ids=["REQ-002"],
                ),
                "implement",
            )
            self.assertIn("APPROVED FRONTEND DESIGN CONTRACT", task_prompt)
            self.assertIn(
                ".auto-agents/state/frontend_design.lock.json",
                task_prompt,
            )

    def test_paused_legacy_preservation_reapproval_is_normalized_without_prompt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# Preserve the existing frontend")
            write_text(root / "src/pages/Home.tsx", "export const Home = () => <main />")
            write_text(root / "specs/prototype/home.html", HTML)
            write_text(design_md_path(root), "# User design")
            write_frontend_fidelity_trace(root)

            orchestrator = Orchestrator(root)
            orchestrator.adapter = PrototypeAdapter(root)
            state = load_run_state(root)
            state.resume_context[
                Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
            ] = True
            orchestrator._run_prototype_stage(state, root / "spec.md")
            approved = orchestrator.approve("prototype")

            trace = json.loads(
                requirements_trace_path(root).read_text(encoding="utf-8")
            )
            trace["frontend_scope"]["surfaces"][0]["requirement_ids"].append(
                "REQ-002"
            )
            trace["requirements"].append(
                {
                    "id": "REQ-002",
                    "status": "active",
                    "priority": "mandatory",
                    "text": "保持批准的首页视觉合同，不得修改或重设计 UI。",
                    "acceptance_oracles": ["现有页面布局和文案保持不变。"],
                    "oracle_type": "mixed",
                    "oracle_strength": "human",
                    "evidence_boundary": "system_boundary",
                    "forbidden_proxy_oracles": [],
                    "notes": "",
                }
            )
            write_json(requirements_trace_path(root), trace)
            lock = load_frontend_design_lock(root)
            lock["status"] = "pending_approval"
            lock["redesign_requested_at"] = "2026-08-23T00:00:00+00:00"
            lock["redesign_requirement_ids"] = ["REQ-002"]
            lock.pop("approved_at", None)
            lock.pop("approval", None)
            lock.pop("contract_sha256", None)
            write_json(frontend_design_lock_path(root), lock)
            frontend_prototype_variants_registry_path(root).unlink()
            ensure_registry(root, max_pages=3)
            approved.status = "paused"
            approved.current_stage = "prototype"
            approved.pending_approval = "prototype"

            normalized = orchestrator._normalize_preservation_only_prototype_pause(
                approved
            )

            self.assertTrue(normalized)
            self.assertEqual(approved.status, "pending")
            self.assertEqual(approved.pending_approval, "")
            self.assertIn("prototype", approved.approved_gates)
            restored = load_frontend_design_lock(root)
            self.assertEqual(restored["status"], "approved")
            self.assertNotIn("redesign_requested_at", restored)
            self.assertIn(
                "REQ-002",
                restored["prototype"]["pages"][0]["requirement_ids"],
            )
            self.assertIn(
                "approved DESIGN.md",
                (root / "AGENTS.md").read_text(encoding="utf-8"),
            )

    def test_parallel_scheduler_checks_contract_before_evidence_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_frontend_fidelity_trace(root)
            task = frontend_task(status="pending")
            backend_task = TaskSpec(
                task_id="task-backend",
                title="Implement an API",
                description="Add one API endpoint.",
                acceptance=["The endpoint returns a response."],
            )
            tasks = [task, backend_task]
            state = load_run_state(root)
            state.status = "pending"
            state.current_stage = "implement"
            state.tasks = tasks

            orchestrator = Orchestrator(root)
            with patch.object(
                orchestrator, "_parallel_execution_fallback_reason", return_value=""
            ), patch.object(
                orchestrator, "_parallel_worker_count", return_value=2
            ), patch.object(
                orchestrator, "_log_parallel_worker_resolution"
            ), patch.object(
                orchestrator,
                "_process_next_parallel_pending_integration",
                return_value=None,
            ), patch.object(
                orchestrator, "_ensure_evidence_preflight"
            ) as evidence_preflight, patch.object(
                orchestrator, "_run_parallel_task_batch"
            ) as parallel_batch:
                result = orchestrator._run_parallel_implementation_loop(
                    state, tasks, max_tasks=None
                )

            self.assertIs(result, state)
            evidence_preflight.assert_not_called()
            parallel_batch.assert_not_called()
            self.assertEqual(task.status, "pending")
            self.assertEqual(state.current_stage, "prototype")

    def test_contract_recovery_handles_existing_frontend_then_routes_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            write_text(root / "spec.md", "# Existing frontend fidelity\n")
            write_text(root / "src/pages/Home.tsx", "export const Home = () => <main />")
            write_text(root / "specs/prototype/home.html", HTML)
            write_text(design_md_path(root), "# User design\n")
            write_frontend_fidelity_trace(root)

            orchestrator = Orchestrator(root)
            adapter = PrototypeAdapter(root)
            orchestrator.adapter = adapter
            state = load_run_state(root)
            state.resume_context[
                Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT
            ] = True

            state = orchestrator._run_prototype_stage(state, root / "spec.md")

            variant = candidate_variants(load_registry(root))[0]
            self.assertEqual(variant["status"], "candidate")
            self.assertTrue(
                any(call.startswith("prototype-generate") for call in adapter.calls)
            )
            self.assertNotIn(
                Orchestrator.FRONTEND_CONTRACT_RECOVERY_CONTEXT,
                state.resume_context,
            )

            task = frontend_task()
            state.status = "pending"
            state.current_stage = "implement"
            state.stage_summaries.update(
                {
                    "design": "done",
                    "plan": "done",
                    "provider_research": "done",
                }
            )
            state.approved_gates = ["requirements", "architecture"]
            state.tasks = [task]
            with patch.object(
                orchestrator, "_ensure_evidence_preflight"
            ) as evidence_preflight:
                result = orchestrator._execute_task_in_main_worktree(
                    state, [task], task
                )

            self.assertIs(result, state)
            evidence_preflight.assert_not_called()
            self.assertEqual(state.status, "paused")
            self.assertEqual(state.current_stage, "prototype")
            self.assertEqual(state.pending_approval, "prototype")
            self.assertEqual(state.approved_gates, ["requirements"])
            self.assertEqual(task.status, "pending")

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

            variant = candidate_variants(load_registry(root))[0]
            self.assertEqual((variant_dir(root, variant["id"]) / "DESIGN.md").read_bytes(), b"# Exact upstream bytes\r\n")
            self.assertEqual(variant["source"]["commit_sha"], "b" * 40)
            self.assertEqual(variant["source"]["slug"], "alpha")

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

            with self.assertRaisesRegex(RuntimeError, "No frontend prototype candidate"):
                Orchestrator(root).reject("prototype", "make it calmer")

            write_text(prototype / "home.html", HTML.replace("ok", "tampered"))
            errors = validate_frontend_design_artifacts(
                root,
                load_frontend_design_lock(root),
                require_approved=True,
            )
            self.assertIn("approved frontend design artifacts have drifted from their locked hashes", errors)


if __name__ == "__main__":
    unittest.main()
