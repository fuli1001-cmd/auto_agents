from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auto_agents.config import (
    create_session,
    provider_references_lock_path,
    requirements_trace_path,
)
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.provider_contract import (
    PROVIDER_REFERENCE_CONTRACT_VERSION,
    PROVIDER_REFERENCE_V2_HEADINGS,
    provider_policy_prompt_lines,
    validate_provider_reference_v2,
)
from auto_agents.session import Session


def _reference_markdown() -> str:
    lines = ["# Provider contract"]
    for heading in PROVIDER_REFERENCE_V2_HEADINGS:
        lines.extend(["", f"## {heading}", "", "Not applicable: covered by this fixture."])
    return "\n".join(lines) + "\n"


def _sourced_reference_markdown() -> str:
    sections: dict[str, str] = {
        "Prompt / Content Construction": (
            "**[Provider official]** The request accepts a prompt.\n\n"
            "- **[Local policy]** Compile the prompt before sending it."
        ),
        "Safety / Content Policy": (
            "**[Provider official]** Structured refusals use a stable code.\n\n"
            "**[Project observed]** A compatibility gateway returned a wrapped refusal.\n\n"
            "**[Local policy]** Ambiguous refusals fail closed."
        ),
        "Semantic Error Routing": (
            "**[Local policy]** Parse the bounded body before HTTP fallback.\n\n"
            "1. **[Provider official]** A stable refusal code is documented; "
            "**[Local policy]** map it to the safety category.\n"
            "2. **[Compatibility assumption]** A wrapped status token is equivalent."
        ),
        "Retry / Recovery Matrix": (
            "| Outcome | Retry | Provenance |\n"
            "| --- | --- | --- |\n"
            "| Safety refusal | Forbidden | **[Local policy]** |\n\n"
            "**[Local policy]** Retry budgets remain independent."
        ),
    }
    lines = ["# Provider contract"]
    for heading in PROVIDER_REFERENCE_V2_HEADINGS:
        lines.extend(
            [
                "",
                f"## {heading}",
                "",
                sections.get(heading, "Not applicable: covered by this fixture."),
            ]
        )
    return "\n".join(lines) + "\n"


class ProviderContractPolicyTests(unittest.TestCase):
    def test_v2_reference_requires_version_nonempty_sections_and_allows_explained_na(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "provider.md"
            write_text(reference, "# Provider\n\n## Status\n\nverified\n")

            errors = validate_provider_reference_v2(
                reference,
                {"path": "provider.md", "status": "verified", "contract_version": 1},
            )

            self.assertTrue(any("contract_version" in item for item in errors), errors)
            self.assertTrue(any("Safety / Content Policy" in item for item in errors), errors)

            write_text(reference, _reference_markdown())
            self.assertEqual(
                validate_provider_reference_v2(
                    reference,
                    {
                        "path": "provider.md",
                        "status": "verified",
                        "contract_version": PROVIDER_REFERENCE_CONTRACT_VERSION,
                    },
                ),
                [],
            )

    def test_v2_reference_requires_rule_and_recovery_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "provider.md"
            lock_entry = {
                "path": "provider.md",
                "status": "verified",
                "contract_version": PROVIDER_REFERENCE_CONTRACT_VERSION,
            }

            write_text(reference, _sourced_reference_markdown())
            self.assertEqual(validate_provider_reference_v2(reference, lock_entry), [])

            missing_routing_source = _sourced_reference_markdown().replace(
                "2. **[Compatibility assumption]** A wrapped status token is equivalent.",
                "2. A wrapped status token is equivalent.",
            )
            write_text(reference, missing_routing_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertTrue(
                any("Semantic Error Routing" in item and "provenance" in item for item in errors),
                errors,
            )

            missing_matrix_source = _sourced_reference_markdown().replace(
                "| Outcome | Retry | Provenance |",
                "| Outcome | Retry |",
            ).replace(
                "| --- | --- | --- |\n| Safety refusal | Forbidden | **[Local policy]** |",
                "| --- | --- |\n| Safety refusal | Forbidden |",
            )
            write_text(reference, missing_matrix_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertTrue(
                any("Source or Provenance column" in item for item in errors),
                errors,
            )

            malformed_routing_source = _sourced_reference_markdown().replace(
                "1. **[Provider official]** A stable refusal code is documented; ",
                "1. **Provenance: [Provider official]** A stable refusal code is documented; ",
            )
            write_text(reference, malformed_routing_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertEqual(
                sum("unsupported provenance syntax" in item for item in errors),
                1,
                errors,
            )
            self.assertTrue(any("**[Provider official]**" in item for item in errors))

            malformed_matrix_source = _sourced_reference_markdown().replace(
                "| Safety refusal | Forbidden | **[Local policy]** |",
                "| Safety refusal | Forbidden | **Source: [Local policy]** |",
            )
            write_text(reference, malformed_matrix_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertTrue(
                any(
                    "Retry / Recovery Matrix row 3 uses unsupported provenance syntax"
                    in item
                    for item in errors
                ),
                errors,
            )

    def test_v2_reference_accepts_composite_recovery_provenance_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "provider.md"
            lock_entry = {
                "path": "provider.md",
                "status": "verified",
                "contract_version": PROVIDER_REFERENCE_CONTRACT_VERSION,
            }
            composite_matrix_header = _sourced_reference_markdown().replace(
                "| Outcome | Retry | Provenance |",
                "| Outcome | Retry | Source or Provenance |",
            )

            write_text(reference, composite_matrix_header)

            self.assertEqual(validate_provider_reference_v2(reference, lock_entry), [])

    def test_provider_research_validation_deduplicates_shared_reference_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            reference = ".auto-agents/docs/provider_references/image.md"
            malformed = _sourced_reference_markdown().replace(
                "1. **[Provider official]** A stable refusal code is documented; ",
                "1. **Provenance: [Provider official]** A stable refusal code is documented; ",
            )
            write_text(project_root / reference, malformed)
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": requirement_id,
                            "status": "active",
                            "external_docs_required": True,
                            "provider_reference": reference,
                        }
                        for requirement_id in ("REQ-001", "REQ-002")
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "image": {
                            "path": reference,
                            "status": "verified",
                            "contract_version": PROVIDER_REFERENCE_CONTRACT_VERSION,
                            "retrieved_at": "2026-08-20T00:00:00Z",
                            "source_urls": ["https://provider.example/docs"],
                            "notes": "shared fixture",
                        }
                    },
                },
            )
            orchestrator = Orchestrator(project_root)
            result = AgentResult(
                ok=True,
                command=[],
                output_path=project_root / "out.md",
                summary="",
            )

            feedback = orchestrator._provider_research_validation_feedback(
                result,
                upgrade_reference_paths=[reference],
            )

            self.assertIsNotNone(feedback)
            self.assertEqual(
                (feedback or "").count("unsupported provenance syntax"),
                1,
                feedback,
            )

    def test_provider_research_enforces_v2_only_for_created_or_refreshed_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            reference = ".auto-agents/docs/provider_references/image.md"
            reference_path = project_root / reference
            write_text(reference_path, "# Legacy provider note\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "status": "active",
                            "external_docs_required": True,
                            "provider_reference": reference,
                        }
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "image": {
                            "path": reference,
                            "status": "verified",
                            "retrieved_at": "2026-08-17T00:00:00Z",
                            "source_urls": ["https://provider.example/docs"],
                            "notes": "legacy fixture",
                        }
                    },
                },
            )
            orchestrator = Orchestrator(project_root)
            result = AgentResult(
                ok=True,
                command=[],
                output_path=project_root / "out.md",
                summary="",
            )

            self.assertIsNone(
                orchestrator._provider_research_validation_feedback(result)
            )
            feedback = orchestrator._provider_research_validation_feedback(
                result,
                upgrade_reference_paths=[reference],
            )
            self.assertIsNotNone(feedback)
            self.assertIn("contract_version", feedback or "")
            self.assertIn("Safety / Content Policy", feedback or "")

            write_text(reference_path, _reference_markdown())
            lock = {
                "version": 1,
                "references": {
                    "image": {
                        "path": reference,
                        "status": "verified",
                        "contract_version": PROVIDER_REFERENCE_CONTRACT_VERSION,
                        "retrieved_at": "2026-08-17T00:00:00Z",
                        "source_urls": ["https://provider.example/docs"],
                        "notes": "v2 fixture",
                    }
                },
            }
            write_json(provider_references_lock_path(project_root), lock)
            self.assertIsNone(
                orchestrator._provider_research_validation_feedback(
                    result,
                    upgrade_reference_paths=[reference],
                )
            )

            write_text(reference_path, "# Broken v2 provider reference\n")
            blockers = orchestrator.provider_research_blockers()
            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["status"], "invalid_contract")
            self.assertIn("Safety / Content Policy", blockers[0]["reason"])

    def test_pipeline_and_lightweight_session_prompts_share_provider_safety_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            write_text(spec_file, "# Demo\n\nIntegrate an external image provider.\n")
            orchestrator = Orchestrator(project_root)

            for stage in ("clarify", "design", "plan"):
                prompt = orchestrator._build_prompt(stage, spec_file)
                self.assertIn("PROVIDER CONTENT-SAFETY CONTRACT", prompt)
                self.assertIn(provider_policy_prompt_lines(stage)[-1], prompt)

            requirement = {
                "id": "REQ-001",
                "status": "active",
                "external_docs_required": True,
                "provider_reference": ".auto-agents/docs/provider_references/image.md",
            }
            research_prompt = orchestrator._build_provider_research_prompt([requirement])
            self.assertIn("contract_version=2", research_prompt)
            self.assertIn("Semantic Error Routing", research_prompt)
            self.assertIn("**[OpenAI official]**", research_prompt)
            self.assertIn("**Provenance: [OpenAI official]**", research_prompt)
            self.assertIn("is invalid", research_prompt)

            task = TaskSpec(
                task_id="task-001",
                title="Provider boundary",
                description="Implement a provider boundary.",
                acceptance=["Provider boundary works."],
            )
            for stage in ("implement", "review"):
                prompt = orchestrator._build_task_prompt(task, stage)
                self.assertIn("PROVIDER CONTENT-SAFETY CONTRACT", prompt)
                self.assertIn(provider_policy_prompt_lines(stage)[-1], prompt)

            fix_state = create_session(project_root, "fix")
            fix_state.goal = "Fix provider safety classification"
            fix_prompt = Session(orchestrator, mode="fix")._build_fix_prompt(fix_state, "")
            self.assertIn("PROVIDER CONTENT-SAFETY CONTRACT", fix_prompt)

            collab_state = create_session(project_root, "collab")
            collab_state.goal = "Debug provider safety classification"
            collab_prompt = Session(orchestrator, mode="collab")._build_collab_prompt(
                collab_state,
                "",
            )
            self.assertIn("PROVIDER CONTENT-SAFETY CONTRACT", collab_prompt)


if __name__ == "__main__":
    unittest.main()
