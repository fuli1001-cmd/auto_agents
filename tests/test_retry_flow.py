import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    archived_run_state_path,
    archived_task_plan_path,
    gate_baseline_cache_path,
    load_project_config,
    load_run_state,
    provider_references_lock_path,
    requirements_trace_path,
    save_project_config,
    save_run_state,
    task_plan_path,
)
from auto_agents.gates import FailureExtraction
from auto_agents.git_ops import changed_paths, commit_all, worktree_fingerprint
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, CommandResult, GateParallelGroup, GateResult, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.validation import validation_report


class RetryingPlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0

    def run(self, request):
        if request.stage == "plan":
            self.plan_calls += 1
            if self.plan_calls == 1:
                write_json(task_plan_path(self.project_root), {"tasks": [{"task_id": "bad id"}]})
                write_text(request.output_path, "invalid plan\n")
            else:
                write_json(
                    task_plan_path(self.project_root),
                    {
                        "test_strategy": "python-pytest",
                        "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                        "tasks": [
                            {
                                "task_id": "task-001",
                                "title": "Add CLI entrypoint",
                                "description": "Add a runnable command line entrypoint.",
                                "acceptance": ["`python -m demo --help` exits successfully."],
                                "status": "pending",
                                "commit_message": "",
                            }
                        ]
                    },
                )
                write_text(request.output_path, "valid plan\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class VerificationPlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "plan":
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Add CLI entrypoint",
                            "description": "Add a runnable command line entrypoint.",
                            "acceptance": ["`python -m demo --help` exits successfully."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            write_text(request.output_path, "valid verification plan\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class VerifyFailureClassificationTests(unittest.TestCase):
    def test_repeated_non_comparable_failures_stop_as_unresolved_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-001",
                title="demo",
                description="",
                acceptance=[],
                verify_history=[
                    {
                        "attempt": 1,
                        "decision": "fail",
                        "summary": "non-comparable verification failure",
                        "failure_ids": ["cmd:conda run -p ./.conda python -m pytest -q tests"],
                        "comparable_failures": False,
                    }
                ],
            )

            analysis = orchestrator._analyze_verify_failure(
                task,
                ["cmd:conda run -p ./.conda python -m pytest -q tests"],
                comparable=False,
            )

            self.assertTrue(analysis["stop_retry"])
            self.assertIn("non-comparable", analysis["stats"])
            self.assertIn("stop-unresolved-identity", analysis["stats"])

    def test_task_verify_commands_follow_owned_proof_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Schema contract",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": [
                            "tests/test_public_api.py::test_contract",
                            "app/service.py::build_payload",
                        ],
                    }
                ],
            )

            commands = orchestrator._build_task_verify_commands(task)

            self.assertEqual(len(commands), 1)
            self.assertIn("tests/test_public_api.py::test_contract", commands[0])
            self.assertNotIn("app/service.py::build_payload", commands[0])

    def test_task_verify_prefers_owned_commands_over_global_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests"]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Owned gate",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    }
                ],
            )

            def fail_global(*args, **kwargs):
                raise AssertionError("global gate should not run for owned task verification")

            def pass_owned(commands, *, collect_all, context):
                self.assertTrue(collect_all)
                self.assertIn("tests/test_public_api.py::test_contract", commands[0])
                return (
                    GateResult(
                        ok=True,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=True,
                                returncode=0,
                                stdout="",
                                stderr="",
                            )
                        ],
                        summary="all commands passed",
                    ),
                    "",
                )

            with patch.object(orchestrator, "_run_gate_commands", side_effect=fail_global):
                with patch.object(
                    orchestrator,
                    "_run_gate_commands_for_commands",
                    side_effect=pass_owned,
                ):
                    with patch.object(orchestrator, "_quick_verify_failure", return_value=""):
                        result = orchestrator._run_task_verify(task)

            self.assertTrue(result["ok"], msg=str(result))

    def test_task_verify_marks_cross_domain_failure_as_scope_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Contract mismatch",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": ["app/stage_backends/text.py::PlanningBackend._schema"],
                    }
                ],
            )

            failed_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command="conda run -p ./.conda python -m pytest -q tests",
                        ok=False,
                        returncode=1,
                        stdout=(
                            "FAILED tests/test_real_voice_adapter_api.py::"
                            "RealVoiceAdapterApiTests::test_compose_resubmission\n"
                        ),
                        stderr="",
                    )
                ],
                summary="FAILED tests/test_real_voice_adapter_api.py::RealVoiceAdapterApiTests::test_compose_resubmission",
            )

            with patch.object(orchestrator, "_run_gate_commands", return_value=(failed_gate, "")):
                with patch(
                    "auto_agents.orchestrator.extract_failure_info",
                    return_value=FailureExtraction(
                        failure_ids=[
                            "tests/test_real_voice_adapter_api.py::RealVoiceAdapterApiTests::test_compose_resubmission"
                        ],
                        comparable=True,
                        non_comparable_ids=[],
                    ),
                ):
                    result = orchestrator._run_task_verify(task)

            self.assertFalse(result["ok"])
            self.assertTrue(result["contract_scope_issue"])
            self.assertIn("verification scope mismatch", str(result["reason"]))

    def test_task_verify_baseline_uses_owned_commands_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Owned baseline",
                description="",
                acceptance=[],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "planned",
                        "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    }
                ],
            )

            captured = {}

            def fake_owned(commands, *, collect_all, context):
                captured["commands"] = list(commands)
                return (
                    GateResult(
                        ok=False,
                        commands=[
                            CommandResult(
                                command=commands[0],
                                ok=False,
                                returncode=1,
                                stdout="FAILED tests/test_public_api.py::test_contract",
                                stderr="",
                            )
                        ],
                        summary="FAILED tests/test_public_api.py::test_contract",
                    ),
                    "",
                )

            with patch.object(orchestrator, "_run_gate_commands_for_commands", side_effect=fake_owned):
                changed = orchestrator._ensure_task_verify_baseline(task)

            self.assertTrue(changed)
            self.assertEqual(
                captured["commands"],
                [orchestrator._build_task_proof_evidence_command(["tests/test_public_api.py::test_contract"])],
            )
            self.assertEqual(
                task.verify_baseline_failures,
                ["tests/test_public_api.py::test_contract"],
            )


class OutOfScopePlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "plan":
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Plan slice",
                            "description": "A valid task plan entry.",
                            "acceptance": ["Plan remains valid."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            leaked = self.project_root / "tests" / "test_stage_leak.py"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            write_text(leaked, "def test_stage_leak():\n    assert True\n")
            summary = "plan with out-of-scope mutation\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class OutOfScopeProviderResearchAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "provider_research":
            reference_path = self.project_root / ".auto-agents" / "docs" / "provider_references" / "provider.md"
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(reference_path, "# Provider reference\n")
            write_json(
                provider_references_lock_path(self.project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": ".auto-agents/docs/provider_references/provider.md",
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )
            leaked = self.project_root / "tests" / "test_provider_stage_leak.py"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            write_text(leaked, "def test_provider_stage_leak():\n    assert True\n")
            summary = "provider research with out-of-scope mutation\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class OutOfScopeReviewAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "review":
            write_text(self.project_root / "notes.txt", "review should be read-only\n")
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReviewTsBuildInfoAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            write_text(self.project_root / "artifact.txt", "good\n")
            summary = "implemented good\n"
        elif request.stage == "review":
            self.review_calls += 1
            workbench = self.project_root / "workbench"
            workbench.mkdir(parents=True, exist_ok=True)
            write_text(workbench / "tsconfig.tsbuildinfo", '{"version":"incremental-2"}\n')
            summary = "DECISION: pass\nreview passed despite tooling cache churn\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReviewBuildLibAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            write_text(self.project_root / "artifact.txt", "good\n")
            summary = "implemented good\n"
        elif request.stage == "review":
            self.review_calls += 1
            write_text(
                self.project_root / "build" / "lib" / "app" / "__init__.py",
                "# generated build output\n",
            )
            summary = "DECISION: pass\nreview passed despite python build output churn\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class OutOfScopeImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(task_plan_path(self.project_root), "{\"tasks\": []}\n")
            summary = "implemented with forbidden auto-agents mutation\n"
        elif request.stage == "review":
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RecoveringOutOfScopeImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "good\n")
            if self.implement_calls == 1:
                write_text(task_plan_path(self.project_root), "{\"tasks\": []}\n")
                summary = "implemented with first-attempt auto-agents mutation\n"
            else:
                summary = "implemented clean retry\n"
        elif request.stage == "review":
            summary = "DECISION: pass\nLooks good.\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReadmeProposalMutationAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.calls = 0

    def run(self, request):
        if request.stage == "readme":
            self.calls += 1
            write_text(self.project_root / "README.md", "# premature write\n")
            summary = "proposal mutated readme\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RetryingVerificationCommandAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0

    def run(self, request):
        if request.stage == "plan":
            self.plan_calls += 1
            target = "tests/test_missing.py" if self.plan_calls == 1 else "tests/test_ok.py"
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_commands": [f"conda run -p ./.conda python -m pytest -q {target}"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Add CLI entrypoint",
                            "description": "Add a runnable command line entrypoint.",
                            "acceptance": ["`python -m demo --help` exits successfully."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            write_text(request.output_path, "verification plan\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class RetryingImplementAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            value = "bad" if self.implement_calls == 1 else "good"
            write_text(self.project_root / "artifact.txt", value + "\n")
            write_text(request.output_path, f"implemented {value}\n")
            summary = f"implemented {value}"
        elif request.stage == "review":
            current = (self.project_root / "artifact.txt").read_text(encoding="utf-8").strip()
            decision = "pass" if current == "good" else "fail"
            summary = f"DECISION: {decision}\nartifact is {current}\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ResumeReviewAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            raise AssertionError("implement should not be called when resuming an interrupted task")
        if request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nresume review passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class BlockedRetryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "fixed\n")
            summary = "implemented fixed\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nblocked task recovered\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class SequentialArtifactAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(
                self.project_root / f"artifact-{self.implement_calls}.txt",
                f"attempt-{self.implement_calls}\n",
            )
            summary = f"implemented attempt {self.implement_calls}\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nsequential task passed review\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class VerifyBeforeReviewAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "bad\n")
            summary = "implemented bad\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview should not run before verify passes\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)

        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class CachedReviewResumeAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            raise AssertionError("implement should not run when resuming cached review state")
        if request.stage == "review":
            self.review_calls += 1
            raise AssertionError("review should be reused from cache when worktree is unchanged")
        summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ReviewEffortAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_efforts = []

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            raise AssertionError("implement should not run when resuming for review effort checks")
        if request.stage == "review":
            self.review_efforts.append(request.effort)
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RetryFeedbackAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_prompts = []
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_prompts.append(request.prompt)
            write_text(self.project_root / "artifact.txt", "bad\n")
            summary = "implemented bad\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class SplitPlanAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == "plan":
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-child-a",
                            "title": "First child",
                            "description": "First split child.",
                            "acceptance": ["child a done"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-child-b",
                            "title": "Second child",
                            "description": "Second split child.",
                            "acceptance": ["child b done"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            summary = "plan split legacy task\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class StalePlanAuditRecoveryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts = []
        self.review_prompts = []

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            if self.implement_calls == 2:
                write_text(
                    self.project_root / "tests" / "test_plan_contract.py",
                    "EXPECTED_TASK = 'task-child-a'\n",
                )
            write_text(self.project_root / "artifact.txt", f"attempt-{self.implement_calls}\n")
            summary = f"implemented attempt {self.implement_calls}\n"
        elif request.stage == "review":
            self.review_calls += 1
            self.review_prompts.append(request.prompt)
            summary = "DECISION: pass\nreview passed after stale test migration\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class StaleTaskStatusAuditRecoveryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts = []
        self.review_prompts = []

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            if self.implement_calls == 2:
                write_text(
                    self.project_root / "tests" / "test_status_contract.py",
                    (
                        "EXPECTED = {\n"
                        "    'task-080': {\n"
                        "        'status': 'done',\n"
                        "    },\n"
                        "}\n"
                    ),
                )
            write_text(self.project_root / "artifact.txt", f"attempt-{self.implement_calls}\n")
            summary = f"implemented attempt {self.implement_calls}\n"
        elif request.stage == "review":
            self.review_calls += 1
            self.review_prompts.append(request.prompt)
            summary = "DECISION: pass\nreview passed after stale task-status migration\n"
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class SequencedVerifyFailureAdapter:
    def __init__(self, project_root: Path, values):
        self.project_root = project_root
        self.values = list(values)
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            index = min(self.implement_calls, len(self.values) - 1)
            value = self.values[index]
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", value + "\n")
            summary = f"implemented {value}\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class MissingCondaFastFailAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "hello\n")
            summary = "implemented hello\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class MissingPytestTargetFastFailAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "hello\n")
            summary = "implemented hello\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nreview passed\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class PermanentReviewFailureAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            summary = "implemented bad\n"
            write_text(self.project_root / "artifact.txt", "bad\n")
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: fail\nCore issue: health endpoint is not actually exercised.\n- Missing request test.\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class AuditRecoveryAdapter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0
        self.implement_calls = 0
        self.provider_research_calls = 0
        self.review_calls = 0
        self.stage_calls: list[str] = []

    def run(self, request):
        self.stage_calls.append(request.stage)
        if request.stage == "plan":
            self.plan_calls += 1
            write_json(
                task_plan_path(self.project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": [],
                            "status": "done",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Cover requirement",
                            "description": "Cover the missing mandatory requirement.",
                            "acceptance": ["coverage is explicit"],
                            "requirement_ids": ["REQ-001"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )
            summary = "plan updated\n"
            write_text(request.output_path, summary)
        elif request.stage == "provider_research":
            self.provider_research_calls += 1
            reference_path = self.project_root / ".auto-agents" / "docs" / "provider_references" / "provider.md"
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(reference_path, "# Provider reference\n")
            write_json(
                provider_references_lock_path(self.project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": ".auto-agents/docs/provider_references/provider.md",
                            "status": "verified",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )
            summary = "provider research updated\n"
            write_text(request.output_path, summary)
        elif request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "modern_backend\n")
            service_path = self.project_root / "app" / "service.py"
            if service_path.exists():
                write_text(service_path, "modern_backend = True\n")
            summary = "implemented audit recovery\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\naudit recovery review passed\n"
            write_text(request.output_path, summary)
        elif request.stage == "readme":
            if "Do NOT write the README yet. Only outline the planned sections." in request.prompt:
                summary = "- Overview\n- Architecture\n- Usage\n"
            else:
                write_text(
                    self.project_root / "README.md",
                    "# Demo\n## Overview\nRecovered project.\n## Architecture\nSimple test layout.\n## Usage\n```bash\npython -m demo\n```\n## Development\nRun tests.\n",
                )
                summary = "readme updated\n"
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class RetryFlowTests(unittest.TestCase):
    @staticmethod
    def _configure_git_identity(project_root: Path) -> None:
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(project_root),
            check=True,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(project_root),
            check=True,
            text=True,
            capture_output=True,
        )

    def _seed_verify_ready_state(self, project_root: Path, orchestrator: Orchestrator) -> None:
        state = load_run_state(project_root)
        state.status = "pending"
        state.current_stage = "implement"
        state.stage_summaries = {
            "clarify": "done",
            "design": "done",
            "plan": "done",
            "provider_research": "done",
            "implement": "done",
        }
        state.tasks = orchestrator._load_tasks_from_plan()
        save_run_state(project_root, state)

    def _disable_gates_and_approvals(self, project_root: Path) -> None:
        orchestrator = Orchestrator(project_root)
        config = orchestrator.config
        config.gates.commands = []
        config.approvals.enabled = []
        config.gates.require_clean_git_before_task = False
        save_project_config(project_root, config)
        (project_root / ".conda" / "conda-meta").mkdir(parents=True, exist_ok=True)

    def test_plan_stage_retries_on_invalid_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingPlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            state = orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertEqual(orchestrator.adapter.plan_calls, 2)
            self.assertEqual(state.agent_attempts["plan"], 2)
            self.assertEqual(state.tasks[0].task_id, "task-001")

    def test_plan_stage_applies_generated_verification_commands_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = VerificationPlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            orchestrator._run_agent_stage("plan", state, spec_file)

            config = load_project_config(project_root)
            self.assertEqual(config.gates.commands, ["conda run -p ./.conda python -m pytest -q tests"])

    def test_plan_stage_expands_pytest_directory_steps_to_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(tests_dir / "test_alpha.py", "def test_alpha():\n    assert True\n")
            write_text(tests_dir / "test_beta.py", "def test_beta():\n    assert True\n")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = VerificationPlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            orchestrator._run_agent_stage("plan", state, spec_file)

            config = load_project_config(project_root)
            self.assertEqual(
                config.gates.commands,
                [
                    "conda run -p ./.conda python -m pytest -q tests/test_alpha.py",
                    "conda run -p ./.conda python -m pytest -q tests/test_beta.py",
                ],
            )
            self.assertEqual(
                [step.targets for step in config.gates.steps],
                [["tests/test_alpha.py"], ["tests/test_beta.py"]],
            )

    def test_gate_run_syncs_drifted_config_commands_from_task_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "shell",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Keep verification synced",
                            "description": "Exercise drift recovery.",
                            "acceptance": ["Gate config is synced before verify."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            config = load_project_config(project_root)
            config.gates.commands = ["false"]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            gate, mutation_error = orchestrator._run_gate_commands(
                collect_all=False,
                context="test gate commands",
            )

            self.assertFalse(mutation_error)
            self.assertTrue(gate.ok, msg=gate.summary)
            self.assertEqual([item.command for item in gate.commands], ["true"])
            self.assertEqual(load_project_config(project_root).gates.commands, ["true"])

    def test_validation_warns_when_gate_commands_drift_from_task_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "shell",
                    "verification_commands": ["true"],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Keep verification synced",
                            "description": "Exercise drift warning.",
                            "acceptance": ["Validation surfaces gate drift."],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ],
                },
            )
            config = load_project_config(project_root)
            config.gates.commands = ["false"]
            save_project_config(project_root, config)

            report = validation_report(project_root)

            self.assertTrue(report["ok"], msg=str(report))
            self.assertTrue(
                any(
                    "gates.commands differ from task plan verification_commands" in warning
                    for warning in report["warnings"]
                ),
                msg=str(report["warnings"]),
            )

    def test_plan_stage_retries_when_verification_commands_reference_missing_pytest_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(tests_dir / "test_ok.py", "def test_ok():\n    assert True\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingVerificationCommandAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            orchestrator._run_agent_stage("plan", state, spec_file)

            config = load_project_config(project_root)
            self.assertEqual(orchestrator.adapter.plan_calls, 2)
            self.assertEqual(config.gates.commands, ["conda run -p ./.conda python -m pytest -q tests/test_ok.py"])

    def test_plan_stage_rejects_out_of_scope_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopePlanAdapter(project_root)

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertIn("stage plan modified files outside its ownership", str(ctx.exception))
            self.assertIn("tests/test_stage_leak.py", str(ctx.exception))

    def test_provider_research_rejects_out_of_scope_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Use official provider docs.",
                            "source": "spec.md",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["provider reference is verified"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": True,
                            "provider_reference": ".auto-agents/docs/provider_references/provider.md",
                            "notes": "",
                        }
                    ],
                },
            )
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopeProviderResearchAdapter(project_root)
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_provider_research(state, project_root / "spec.md")

            self.assertIn("stage provider_research modified files outside its ownership", str(ctx.exception))
            self.assertIn("tests/test_provider_stage_leak.py", str(ctx.exception))

    def test_review_stage_rejects_out_of_scope_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopeReviewAdapter(project_root)
            state = load_run_state(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_task_review(state.run_id, task)

            self.assertIn("stage review modified files outside its ownership", str(ctx.exception))
            self.assertIn("notes.txt", str(ctx.exception))

    def test_readme_proposal_stage_rejects_repository_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReadmeProposalMutationAdapter(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_readme(state, spec_file)

            self.assertIn("stage readme modified files outside its ownership during readme-propose", str(ctx.exception))
            self.assertIn("README.md", str(ctx.exception))

    def test_implement_stage_rejects_auto_agents_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = OutOfScopeImplementAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertIn("implement-task-001 exhausted retries", str(ctx.exception))
            self.assertIn(".auto-agents/state/task_plan.json", str(ctx.exception))
            self.assertEqual(
                orchestrator.adapter.implement_calls,
                orchestrator._max_attempts("implement"),
            )
            self.assertIn(
                "\"task_id\": \"task-001\"",
                task_plan_path(project_root).read_text(encoding="utf-8"),
            )

    def test_implement_stage_retries_after_auto_agents_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RecoveringOutOfScopeImplementAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(
                (project_root / "artifact.txt").read_text(encoding="utf-8"),
                "good\n",
            )
            self.assertIn(
                "\"status\": \"done\"",
                task_plan_path(project_root).read_text(encoding="utf-8"),
            )

    def test_task_verify_rejects_dirty_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = [
                "python -c \"from pathlib import Path; Path('verify-leak.txt').write_text('x', encoding='utf-8')\""
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            task = orchestrator._load_tasks_from_plan()[0]
            with patch.object(orchestrator, "_build_task_verify_commands", return_value=[]):
                result = orchestrator._run_task_verify(task)

            self.assertFalse(result["ok"])
            self.assertIn("task verification commands modified tracked or unignored files", str(result["reason"]))
            self.assertIn("verify-leak.txt", str(result["reason"]))

    def test_task_verify_runs_owned_proof_evidence_even_with_baseline_only_gate_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = ["python -c \"print('ERROR: test_legacy (tests.test_demo.LegacyTests.test_legacy)'); raise SystemExit(1)\""]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            from auto_agents.models import TaskSpec as _TaskSpec

            task = _TaskSpec(
                task_id="task-001",
                title="Verify proof evidence",
                description="Make sure proof evidence still runs after baseline-only verify.",
                acceptance=["proof evidence passes"],
                verify_baseline_failures=["test_legacy (tests.test_demo.LegacyTests.test_legacy)"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    }
                ],
            )

            proof_calls = []
            orchestrator._run_task_proof_evidence = lambda current_task: (
                proof_calls.append(current_task.task_id),
                {
                    "ok": False,
                    "reason": "owned proof evidence failed: tests/test_public_api.py::test_contract",
                    "summary": "Owned proof evidence failed (1 refs): tests/test_public_api.py::test_contract",
                    "evidence_refs": ["tests/test_public_api.py::test_contract"],
                    "passed_refs": [],
                    "failed_refs": ["tests/test_public_api.py::test_contract"],
                    "failure_ids": ["tests/test_public_api.py::test_contract"],
                    "command": "conda run -p ./.conda python -m pytest -q tests/test_public_api.py::test_contract",
                    "raw_output": "FAILED tests/test_public_api.py::test_contract",
                },
            )[1]

            with patch.object(orchestrator, "_build_task_verify_commands", return_value=[]):
                result = orchestrator._run_task_verify(task)

            self.assertEqual(proof_calls, ["task-001"])
            self.assertFalse(result["ok"])
            self.assertIn("owned proof evidence failed", str(result["reason"]))
            self.assertEqual(
                result["failure_ids"],
                ["tests/test_public_api.py::test_contract"],
            )

    def test_task_verify_runs_identity_diagnostic_for_killed_pytest_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            verify_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command="conda run -p ./.conda python -m pytest -q tests",
                        ok=False,
                        returncode=137,
                        stdout="...................F...\n",
                        stderr="Killed\n",
                    )
                ],
                summary="command failed: killed pytest suite",
            )

            orchestrator._run_gate_commands = lambda **_kwargs: (verify_gate, None)
            task = TaskSpec(
                task_id="task-001",
                title="diagnose verify identity",
                description="",
                acceptance=[],
            )

            diagnostic_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=(
                            "conda run -p ./.conda python -m pytest -vv -rA --tb=short "
                            "-o console_output_style=classic tests"
                        ),
                        ok=False,
                        returncode=1,
                        stdout="tests/test_demo.py::test_example FAILED                         [100%]\n",
                        stderr="",
                    )
                ],
                summary="command failed: diagnostic found identity",
            )

            import auto_agents.orchestrator as orch_mod
            original_collect = orch_mod.run_commands_collect_all
            try:
                captured = {}

                def _fake_collect(commands, cwd):
                    captured["commands"] = list(commands)
                    captured["cwd"] = cwd
                    return diagnostic_gate

                orch_mod.run_commands_collect_all = _fake_collect
                result = orchestrator._run_task_verify(task)
            finally:
                orch_mod.run_commands_collect_all = original_collect

            self.assertFalse(result["ok"])
            self.assertTrue(result["comparable_failures"])
            self.assertEqual(result["failure_ids"], ["tests/test_demo.py::test_example"])
            self.assertIn("identity diagnostic captured", str(result["reason"]))
            self.assertEqual(
                captured["commands"],
                [
                    "conda run -p ./.conda python -m pytest -vv -rA --tb=short -o console_output_style=classic tests"
                ],
            )

    def test_task_proof_evidence_supports_mixed_pytest_and_vitest_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            workbench_root = project_root / "workbench"
            (workbench_root / "src" / "components").mkdir(parents=True)
            write_text(
                workbench_root / "package.json",
                json.dumps(
                    {
                        "name": "demo-workbench",
                        "private": True,
                        "scripts": {"test": "vitest run"},
                        "devDependencies": {"vitest": "3.1.1"},
                    }
                ),
            )
            write_text(workbench_root / "vitest.config.ts", "export default {}\n")
            write_text(
                workbench_root / "src" / "components" / "project-detail-workbench.test.tsx",
                "export {};\n",
            )
            (project_root / "tests").mkdir()
            write_text(project_root / "tests" / "test_public_api.py", "def test_contract():\n    assert True\n")

            vitest_ref = (
                "workbench/src/components/project-detail-workbench.test.tsx::"
                "ProjectDetailWorkbench > 生成失败展示用户可理解原因和下一步动作"
            )
            task = TaskSpec(
                task_id="task-001",
                title="Verify mixed proof evidence",
                description="Make sure pytest and vitest proof refs both run.",
                acceptance=["proof evidence passes"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": [
                            "tests/test_public_api.py::test_contract",
                            vitest_ref,
                        ],
                    }
                ],
            )

            captured_commands = []

            def fake_run(commands, cwd):
                captured_commands.extend(commands)
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=command,
                            ok=True,
                            returncode=0,
                            stdout="",
                            stderr="",
                        )
                        for command in commands
                    ],
                    summary="all commands passed",
                )

            with patch("auto_agents.orchestrator.run_commands_collect_all", side_effect=fake_run):
                result = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(result["ok"])
            self.assertEqual(
                result["passed_refs"],
                ["tests/test_public_api.py::test_contract", vitest_ref],
            )
            self.assertEqual(len(captured_commands), 2)
            self.assertIn("pytest -q tests/test_public_api.py::test_contract", captured_commands[0])
            self.assertIn("npm --prefix workbench test --", captured_commands[1])
            self.assertIn("src/components/project-detail-workbench.test.tsx", captured_commands[1])
            self.assertIn("-t", captured_commands[1])
            self.assertIn("ProjectDetailWorkbench > 生成失败展示用户可理解原因和下一步动作", captured_commands[1])

    def test_task_proof_evidence_keeps_unittest_node_ids_and_skips_source_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Verify source-backed proof evidence",
                description="Run executable tests and keep source symbols as supporting evidence.",
                acceptance=["proof evidence passes"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": [
                            "tests/test_openai_strict_schema_contract.py::OpenAIStrictSchemaContractTests::test_planning_schema_passes_openai_strict_contract",
                            "app/stage_backends/text.py::OpenAICompatiblePlanningBackend._planning_schema",
                            "app/application/openai_strict_schema.py::ensure_openai_strict_json_schema",
                            ".auto-agents/docs/architecture.md",
                        ],
                    }
                ],
            )

            captured_commands = []

            def fake_run(commands, cwd):
                captured_commands.extend(commands)
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=command,
                            ok=True,
                            returncode=0,
                            stdout="",
                            stderr="",
                        )
                        for command in commands
                    ],
                    summary="all commands passed",
                )

            with patch("auto_agents.orchestrator.run_commands_collect_all", side_effect=fake_run):
                result = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(result["ok"])
            self.assertEqual(len(captured_commands), 1)
            self.assertIn(
                "tests/test_openai_strict_schema_contract.py::OpenAIStrictSchemaContractTests::"
                "test_planning_schema_passes_openai_strict_contract",
                captured_commands[0],
            )
            self.assertNotIn("app/stage_backends/text.py", captured_commands[0])
            self.assertEqual(
                result["supporting_refs"],
                [
                    "app/stage_backends/text.py::OpenAICompatiblePlanningBackend._planning_schema",
                    "app/application/openai_strict_schema.py::ensure_openai_strict_json_schema",
                    ".auto-agents/docs/architecture.md",
                ],
            )
            self.assertEqual(result["failed_refs"], [])

    def test_task_proof_evidence_cache_key_changes_when_refs_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Verify cache invalidation",
                description="Do not reuse proof evidence after refs are updated.",
                acceptance=["proof evidence passes"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {
                        "requirement_id": "REQ-001",
                        "oracle_index": 1,
                        "status": "verified",
                        "evidence_refs": ["tests/test_public_api.py::test_first"],
                    }
                ],
            )

            captured_commands = []

            def fake_run(commands, cwd):
                captured_commands.extend(commands)
                return GateResult(
                    ok=True,
                    commands=[
                        CommandResult(
                            command=command,
                            ok=True,
                            returncode=0,
                            stdout="",
                            stderr="",
                        )
                        for command in commands
                    ],
                    summary="all commands passed",
                )

            with patch("auto_agents.orchestrator.run_commands_collect_all", side_effect=fake_run):
                first = orchestrator._run_task_proof_evidence(task)
                task.requirement_proofs[0]["evidence_refs"] = ["tests/test_public_api.py::test_second"]
                second = orchestrator._run_task_proof_evidence(task)

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(len(captured_commands), 2)
            self.assertIn("tests/test_public_api.py::test_first", captured_commands[0])
            self.assertIn("tests/test_public_api.py::test_second", captured_commands[1])

    def test_persisted_tasks_keep_generated_verification_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "python-pytest",
                    "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ],
                },
            )

            tasks = orchestrator._load_tasks_from_plan()
            tasks[0].status = "in_progress"
            orchestrator._persist_tasks(tasks)

            payload = task_plan_path(project_root).read_text(encoding="utf-8")
            self.assertIn('"test_strategy": "python-pytest"', payload)
            self.assertIn('"verification_steps": [', payload)

    def test_implement_stage_retries_after_review_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingImplementAdapter(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryingImplementAdapter(project_root)

            state = load_run_state(project_root)
            state.tasks = []
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "good")
            reloaded_state = load_run_state(project_root)
            self.assertEqual(reloaded_state.tasks[0].status, "done")

    def test_resume_in_progress_task_skips_reimplementation_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ResumeReviewAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("hello\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertTrue(state.tasks[0].commit_sha)

    def test_implementation_loop_prefers_task_plan_when_run_state_tasks_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ResumeReviewAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("hello\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec.from_dict(
                    {
                        "task_id": "task-001",
                        "title": "Write artifact",
                        "description": "Write the artifact file.",
                        "acceptance": ["artifact.txt contains hello"],
                        "status": "pending",
                        "commit_message": "",
                        "test_generated": True,
                    }
                )
            ]
            state.agent_attempts["implement-task-001"] = 1

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            reloaded_state = load_run_state(project_root)
            self.assertEqual(reloaded_state.tasks[0].status, "done")

    def test_review_stage_cleans_ephemeral_tsbuildinfo_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.gates.require_clean_git_before_task = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewTsBuildInfoAdapter(project_root)

            workbench = project_root / "workbench"
            workbench.mkdir(exist_ok=True)
            tsbuildinfo_path = workbench / "tsconfig.tsbuildinfo"
            write_text(tsbuildinfo_path, '{"version":"incremental-1"}\n')
            commit_all(project_root, "chore: seed tsbuildinfo")

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(tsbuildinfo_path.read_text(encoding="utf-8").strip(), '{"version":"incremental-1"}')
            status = subprocess.run(
                ["git", "status", "--short", "--", "workbench/tsconfig.tsbuildinfo"],
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_review_stage_cleans_untracked_python_build_lib_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.gates.require_clean_git_before_task = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewBuildLibAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertFalse((project_root / "build" / "lib" / "app" / "__init__.py").exists())
            status = subprocess.run(
                ["git", "status", "--short", "--", "build"],
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_blocked_task_can_retry_with_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = BlockedRetryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "blocked",
                            "commit_message": "",
                            "review_summary": "previous review failure",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("bad\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "fixed")

    def test_pending_task_reports_changed_paths_when_clean_tree_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "notes.txt").write_text("dirty\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            message = str(ctx.exception)
            self.assertIn("task task-001", message)
            self.assertIn("notes.txt", message)
            self.assertIn("--allow-dirty-tree", message)

    def test_pending_task_can_run_with_allow_dirty_tree_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = BlockedRetryAdapter(project_root)
            orchestrator._allow_dirty_tree = True

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "notes.txt").write_text("dirty\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "fixed")

    def test_pending_repair_task_can_run_with_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = BlockedRetryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "repair-task-001-r1-1",
                            "title": "Repair proof evidence",
                            "description": "Repair failed verification evidence.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-001",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "notes.txt").write_text("dirty parent-task context\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "fixed")

    def test_verify_failure_skips_review_and_retries_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; artifact = Path('artifact.txt'); "
                    "raise SystemExit(0 if artifact.exists() and artifact.read_text().strip() == 'good' else "
                    "(1 if artifact.exists() else 0))\""
                )
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = VerifyBeforeReviewAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_resume_reuses_cached_pass_review_for_unchanged_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = CachedReviewResumeAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            (project_root / "artifact.txt").write_text("hello\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state.task_review_cache["task-001"] = {
                "fingerprint": worktree_fingerprint(project_root),
                "decision": "pass",
                "summary": "cached review passed",
            }
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_calls, 0)
            self.assertEqual(state.tasks[0].status, "done")

    def test_small_test_only_review_uses_balanced_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.efforts["review"] = "balanced"
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewEffortAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Update tests",
                            "description": "Adjust coverage.",
                            "acceptance": ["tests updated"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_efforts, ["balanced"])
            self.assertEqual(state.tasks[0].status, "done")

    def test_code_change_without_tests_escalates_review_to_deep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.efforts["review"] = "balanced"
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = ReviewEffortAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Update app",
                            "description": "Adjust behavior.",
                            "acceptance": ["app updated"],
                            "status": "in_progress",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )
            src_dir = project_root / "src"
            src_dir.mkdir(exist_ok=True)
            (src_dir / "app.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.agent_attempts["implement-task-001"] = 1
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertEqual(orchestrator.adapter.review_efforts, ["deep"])
            self.assertEqual(state.tasks[0].status, "done")

    def test_retry_feedback_uses_structured_failure_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; "
                    "raise SystemExit(1 if Path('artifact.txt').exists() else 0)\""
                )
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RetryFeedbackAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(len(orchestrator.adapter.implement_prompts), 2)
            self.assertIn("Failure type: local_verification", orchestrator.adapter.implement_prompts[1])
            self.assertIn("Verification triage:", orchestrator.adapter.implement_prompts[1])
            self.assertIn("Do not dismiss tightly coupled regressions", orchestrator.adapter.implement_prompts[1])
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_task_verify_baseline_ignores_preexisting_failure_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"print('ERROR: test_legacy (tests.test_demo.LegacyTests.test_legacy)'); "
                    "raise SystemExit(1)\""
                )
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = BlockedRetryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[0].verify_baseline_failures, [
                "test_legacy (tests.test_demo.LegacyTests.test_legacy)"
            ])
            self.assertIn("task baseline only: 1 pre-existing failure(s) remain", stream.getvalue())

    def test_task_verify_baseline_does_not_absorb_failures_from_prior_done_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"import json; from pathlib import Path; "
                    "tasks = json.loads(Path('.auto-agents/state/task_plan.json').read_text(encoding='utf-8')).get('tasks', []); "
                    "done = any(task.get('task_id') == 'task-001' and task.get('status') == 'done' for task in tasks); "
                    "print('FAILED tests/test_plan_state.py::test_task_001_stays_pending') if done else None; "
                    "raise SystemExit(1 if done else 0)\""
                )
            ]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequentialArtifactAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Start the next slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertIn(
                "new verification failure(s) vs task baseline: tests/test_plan_state.py::test_task_001_stays_pending",
                str(ctx.exception),
            )
            self.assertEqual(state.implement_verify_baseline_failures, [])
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[1].status, "blocked")
            self.assertEqual(state.tasks[1].verify_baseline_failures, [])
            self.assertNotIn("task baseline only", stream.getvalue())

    def test_commit_warms_next_clean_head_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = ["python -c \"print('ok')\""]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequentialArtifactAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            contexts = []
            original_run_gate_commands = orchestrator._run_gate_commands

            def tracking_run_gate_commands(*, collect_all, context):
                contexts.append(context)
                return original_run_gate_commands(collect_all=collect_all, context=context)

            orchestrator._run_gate_commands = tracking_run_gate_commands
            state = orchestrator._run_implementation_loop(state, max_tasks=2)

            baseline_runs = [
                item for item in contexts if item == "implement verify baseline commands"
            ]
            self.assertEqual(len(baseline_runs), 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[1].status, "done")

    def test_implement_baseline_uses_persistent_cache_across_orchestrators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            config = load_project_config(project_root)
            config.gates.commands = ["python -c \"print('ok')\""]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            self.assertTrue(orchestrator._ensure_implement_verify_baseline(state, state.tasks))
            self.assertTrue(gate_baseline_cache_path(project_root).exists())

            second = Orchestrator(project_root)

            def fail_run_gate_commands(*, collect_all, context):
                raise AssertionError(f"gate commands should be reused from cache during {context}")

            second._run_gate_commands = fail_run_gate_commands
            fresh_state = load_run_state(project_root)
            fresh_state.tasks = second._load_tasks_from_plan()
            self.assertTrue(second._ensure_implement_verify_baseline(fresh_state, fresh_state.tasks))
            self.assertEqual(fresh_state.implement_verify_baseline_failures, [])

    def test_run_gate_commands_uses_parallel_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            config = load_project_config(project_root)
            config.gates.commands = ["python3 -c \"print('before')\""]
            config.gates.parallel_groups = [
                GateParallelGroup(
                    name="checks",
                    commands=[
                        "python3 -c \"print('peer-a')\"",
                        "python3 -c \"print('peer-b')\"",
                    ],
                )
            ]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            gate, mutation_error = orchestrator._run_gate_commands(
                collect_all=True,
                context="parallel test commands",
            )

            self.assertEqual(mutation_error, "")
            self.assertTrue(gate.ok)
            self.assertEqual(
                [item.stdout for item in gate.commands],
                ["before", "peer-a", "peer-b"],
            )

    def test_plan_stage_records_split_task_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = SplitPlanAdapter(project_root)

            from auto_agents.models import TaskSpec

            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec(
                    task_id="task-legacy",
                    title="Legacy task",
                    description="Old task before split.",
                    acceptance=["legacy done"],
                    status="pending",
                )
            ]

            state = orchestrator._run_agent_stage("plan", state, spec_file)

            self.assertEqual(
                state.plan_task_replacements,
                {"task-legacy": ["task-child-a", "task-child-b"]},
            )

    def test_stale_plan_coupled_tests_retry_implement_until_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = StalePlanAuditRecoveryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-child-a",
                            "title": "First child",
                            "description": "Migrate stale test references.",
                            "acceptance": ["stale tests migrate to child ids"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-child-b",
                            "title": "Second child",
                            "description": "Sibling split child.",
                            "acceptance": ["sibling remains available"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-legacy",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(tests_dir / "test_plan_contract.py", "EXPECTED_TASK = 'task-legacy'\n")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.plan_task_replacements = {"task-legacy": ["task-child-a", "task-child-b"]}

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(state.tasks[1].status, "pending")
            self.assertIn("PLAN MIGRATION CONTEXT", orchestrator.adapter.implement_prompts[0])
            self.assertIn("`task-legacy` was replaced by: task-child-a, task-child-b", orchestrator.adapter.implement_prompts[0])
            self.assertIn(
                "Stale plan-coupled tests still reference retired task IDs",
                orchestrator.adapter.implement_prompts[1],
            )
            self.assertIn("tests/test_plan_contract.py", orchestrator.adapter.implement_prompts[1])
            self.assertIn("PLAN MIGRATION CONTEXT", orchestrator.adapter.review_prompts[0])
            self.assertNotIn(
                "task-legacy",
                (project_root / "tests" / "test_plan_contract.py").read_text(encoding="utf-8"),
            )

    def test_stale_plan_coupled_test_audit_ignores_split_child_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-078a",
                            "title": "First child",
                            "description": "Split child A.",
                            "acceptance": ["uses child ids only"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-078",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-078b",
                            "title": "Second child",
                            "description": "Split child B.",
                            "acceptance": ["uses child ids only"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-078",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_plan_contract.py",
                "EXPECTED_TASKS = ['task-078a', 'task-078b']\n",
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.plan_task_replacements = {"task-078": ["task-078a", "task-078b"]}

            audit = orchestrator._run_stale_plan_coupled_test_audit(state.tasks[0], state=state)

            self.assertIsNone(audit)

    def test_stale_plan_coupled_test_audit_ignores_parent_task_id_expectations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-079a",
                            "title": "First child",
                            "description": "Split child A.",
                            "acceptance": ["uses parent metadata"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-079",
                            "split_depth": 1,
                        },
                        {
                            "task_id": "task-079b",
                            "title": "Second child",
                            "description": "Split child B.",
                            "acceptance": ["uses parent metadata"],
                            "status": "pending",
                            "commit_message": "",
                            "parent_task_id": "task-079",
                            "split_depth": 1,
                        },
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_plan_contract.py",
                (
                    "EXPECTED = {\n"
                    "    'task-079a': {'parent_task_id': 'task-079'},\n"
                    "    'task-079b': {'parent_task_id': 'task-079'},\n"
                    "}\n"
                ),
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state.plan_task_replacements = {"task-079": ["task-079a", "task-079b"]}

            audit = orchestrator._run_stale_plan_coupled_test_audit(state.tasks[0], state=state)

            self.assertIsNone(audit)

    def test_task_status_coupled_test_audit_flags_stale_done_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-080",
                            "title": "Continuity acceptance",
                            "description": "Finish the acceptance slice.",
                            "acceptance": ["status reaches done"],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_status_contract.py",
                (
                    "EXPECTED = {\n"
                    "    'task-080': {\n"
                    "        'status': 'in_progress',\n"
                    "    },\n"
                    "}\n"
                ),
            )

            task = orchestrator._load_tasks_from_plan()[0]
            audit = orchestrator._run_task_status_coupled_test_audit(task, expected_status="done")

            self.assertIsNotNone(audit)
            assert audit is not None
            self.assertIn("task `task-080`", str(audit["reason"]))
            self.assertIn("`done`", str(audit["reason"]))
            self.assertIn("`in_progress`", str(audit["reason"]))
            self.assertIn("tests/test_status_contract.py", str(audit["reason"]))

    def test_status_coupled_tests_retry_implement_until_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = StaleTaskStatusAuditRecoveryAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-080",
                            "title": "Continuity acceptance",
                            "description": "Finish the acceptance slice.",
                            "acceptance": ["status reaches done"],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )
            tests_dir = project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            write_text(
                tests_dir / "test_status_contract.py",
                (
                    "EXPECTED = {\n"
                    "    'task-080': {\n"
                    "        'status': 'in_progress',\n"
                    "    },\n"
                    "}\n"
                ),
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertIn(
                "Plan-coupled repository tests still expect task `task-080` to have a stale status.",
                orchestrator.adapter.implement_prompts[1],
            )
            self.assertIn("tests/test_status_contract.py", orchestrator.adapter.implement_prompts[1])
            self.assertIn("`done`", orchestrator.adapter.implement_prompts[1])
            self.assertIn(
                "'status': 'done'",
                (project_root / "tests" / "test_status_contract.py").read_text(encoding="utf-8"),
            )

    def test_verify_failure_logs_repeat_statistics_for_same_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; artifact = Path('artifact.txt'); "
                    "print('FAILED tests/test_demo.py::test_same') if artifact.exists() else None; "
                    "raise SystemExit(1 if artifact.exists() else 0)\""
                )
            ]
            config.retries.per_stage["implement"] = 4
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequencedVerifyFailureAdapter(project_root, ["bad", "bad"])

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains bad"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertIn(
                "[task:task-001] verify decision=fail compare=first-failure-set failure_ids=1",
                rendered,
            )
            self.assertIn(
                "[task:task-001] verify decision=fail compare=same-failure-set-as-attempt-1 repeat=2 failure_ids=1 action=stop-unchanged-set",
                rendered,
            )
            self.assertIn(
                "unchanged verify failure set repeated from attempt-1 (repeat=2); stopping retries early",
                rendered,
            )

    def test_verify_failure_logs_changed_and_regression_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = [
                (
                    "python -c \"from pathlib import Path; value = Path('artifact.txt').read_text().strip(); "
                    "print('FAILED tests/test_demo.py::test_alpha' if value == 'alpha' else "
                    "('FAILED tests/test_demo.py::test_beta' if value == 'beta' else "
                    "'FAILED tests/test_demo.py::test_alpha')); raise SystemExit(1)\""
                )
            ]
            config.retries.per_stage["implement"] = 3
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequencedVerifyFailureAdapter(project_root, ["alpha", "beta", "alpha"])

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt changes"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertIn(
                "[task:task-001] verify decision=fail compare=changed-failure-set-vs-attempt-1 failure_ids=1 new=1 resolved=1",
                rendered,
            )
            self.assertIn(
                "[task:task-001] verify decision=fail compare=regression failure-set-from-attempt-1 previous=attempt-2 repeat=2 failure_ids=1",
                rendered,
            )

    def test_missing_conda_fast_fail_skips_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests"]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = MissingCondaFastFailAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as raised:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertIn(".conda/conda-meta", str(raised.exception))
            self.assertEqual(orchestrator.adapter.implement_calls, 2)
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_missing_pytest_target_fails_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = ["conda run -p ./.conda python -m pytest -q tests/test_missing.py"]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = MissingPytestTargetFastFailAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains hello"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as raised:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertIn("missing pytest target", str(raised.exception))
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual(orchestrator.adapter.review_calls, 0)

    def test_review_rejection_is_included_in_final_error_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 1
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = PermanentReviewFailureAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError) as raised:
                orchestrator._run_implementation_loop(state, max_tasks=1)

            error_text = str(raised.exception)
            self.assertIn("Task task-001 failed gates: review rejected the task", error_text)
            self.assertIn("Review: Core issue: health endpoint is not actually exercised.", error_text)

    def test_review_failure_is_emitted_before_task_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 1
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = PermanentReviewFailureAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write artifact",
                            "description": "Write the artifact file.",
                            "acceptance": ["artifact.txt contains good"],
                            "status": "pending",
                            "commit_message": "",
                            "test_generated": True,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertIn("[task:task-001] review decision=fail", rendered)
            self.assertIn("Core issue: health endpoint is not actually exercised.", rendered)
            self.assertIn("[task:task-001] blocked reason=review rejected the task", rendered)

    def test_blocked_retry_omits_stale_review_when_current_proof_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            class PromptCaptureAdapter:
                def __init__(self, root: Path) -> None:
                    self.root = root
                    self.implement_prompts = []

                def run(self, request):
                    if request.stage == "implement":
                        self.implement_prompts.append(request.prompt)
                        write_text(self.root / "artifact.txt", "fixed\n")
                        summary = "implemented\n"
                    elif request.stage == "review":
                        summary = "DECISION: pass\nreview passed\n"
                    else:
                        summary = f"{request.stage}\n"
                    write_text(request.output_path, summary)
                    return AgentResult(
                        ok=True,
                        command=["fake"],
                        output_path=request.output_path,
                        summary=summary.strip(),
                        returncode=0,
                    )

            config = orchestrator.config
            config.gates.commands = ["python -c \"print('ok')\""]
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            adapter = PromptCaptureAdapter(project_root)
            orchestrator.adapter = adapter

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Recover blocked proof task",
                            "description": "Resume after stale review.",
                            "acceptance": ["artifact.txt contains fixed"],
                            "status": "blocked",
                            "commit_message": "",
                            "review_summary": (
                                "Still failing: tests/test_public_api.py::test_contract"
                            ),
                            "review_history": [
                                {"attempt": 1, "summary": "Still failing: tests/test_public_api.py::test_contract"}
                            ],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "status": "verified",
                                    "evidence_refs": ["tests/test_public_api.py::test_contract"],
                                }
                            ],
                        }
                    ]
                },
            )

            orchestrator._run_task_proof_evidence = lambda task: {
                "ok": True,
                "reason": "",
                "summary": "Owned proof evidence passed (1 refs): tests/test_public_api.py::test_contract",
                "evidence_refs": ["tests/test_public_api.py::test_contract"],
                "passed_refs": ["tests/test_public_api.py::test_contract"],
                "failed_refs": [],
                "failure_ids": [],
                "command": "conda run -p ./.conda python -m pytest -q tests/test_public_api.py::test_contract",
                "raw_output": "",
            }
            orchestrator._build_task_verify_commands = lambda task: []

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(state.tasks[0].status, "done")
            self.assertEqual(len(adapter.implement_prompts), 1)
            self.assertIn("cited evidence_refs now pass", adapter.implement_prompts[0])
            self.assertIn("Current proof evidence:", adapter.implement_prompts[0])
            retry_feedback = adapter.implement_prompts[0].split("Previous attempt issues:\n", 1)[1]
            self.assertNotIn("Still failing: tests/test_public_api.py::test_contract", retry_feedback)

    def test_blocked_proof_failure_schedules_repair_task_before_retrying_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Parent proof task",
                            "description": "Parent task failed owned proof evidence.",
                            "acceptance": ["parent proof passes"],
                            "status": "blocked",
                            "commit_message": "",
                            "review_summary": (
                                "owned proof evidence failed: "
                                "tests/test_public_api.py::test_contract"
                            ),
                            "verify_history": [
                                {
                                    "attempt": 4,
                                    "decision": "fail",
                                    "summary": (
                                        "owned proof evidence failed: "
                                        "tests/test_public_api.py::test_contract"
                                    ),
                                    "failure_ids": ["tests/test_public_api.py::test_contract"],
                                    "comparable_failures": True,
                                }
                            ],
                            "requirement_ids": ["REQ-001"],
                            "requirement_proofs": [
                                {
                                    "requirement_id": "REQ-001",
                                    "oracle_index": 1,
                                    "status": "verified",
                                    "evidence_refs": ["tests/test_public_api.py::test_contract"],
                                }
                            ],
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.current_stage, "implement")
            self.assertEqual([task.task_id for task in result.tasks], ["repair-task-001-r1-1", "task-001"])
            repair, parent = result.tasks
            self.assertEqual(repair.parent_task_id, "task-001")
            self.assertEqual(repair.verification_refs, ["tests/test_public_api.py::test_contract"])
            self.assertEqual(parent.status, "pending")
            self.assertIn("repair-task-001-r1-1", parent.depends_on)
            self.assertEqual(parent.recovery_history[-1]["result"], "scheduled")
            self.assertIn("[recovery] scheduled parent=task-001", stream.getvalue())

    def test_run_logger_writes_to_current_run_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            state = load_run_state(project_root)

            orchestrator._attach_run_logger(state.run_id)
            orchestrator._emit_stage_start("implement")

            log_path = project_root / ".auto-agents" / "runs" / state.run_id / "run.log"
            self.assertTrue(log_path.exists())
            self.assertIn("[stage:implement] start", log_path.read_text(encoding="utf-8"))
            self.assertIn("[stage:implement] start", stream.getvalue())


    def test_reject_resets_stage_and_injects_feedback(self):
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Idea\nWe need a mock project.", encoding="utf-8")
            
            state = orchestrator.run(spec_file=spec_file)
            self.assertEqual(state.status, "paused")
            self.assertEqual(state.pending_approval, "requirements")
            
            state = orchestrator.reject("requirements", "Please add a database.")
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.pending_approval, "")
            self.assertEqual(state.rejection_reason, "Please add a database.")
            self.assertEqual(state.rejected_stage, "clarify")
            
            from unittest.mock import patch
            with patch.object(orchestrator, "_run_agent_with_retries") as mock_run:
                from auto_agents.models import AgentResult
                mock_run.return_value = AgentResult(
                    ok=True,
                    command=[],
                    output_path=Path("."),
                    summary="READY_TO_GENERATE",
                    stdout=""
                )
                state = orchestrator.run(spec_file=spec_file)
                
                found = False
                for call in mock_run.call_args_list:
                    if "clarify" in call.kwargs.get("stage", ""):
                        prompt = call.kwargs.get("prompt", "")
                        if "Please add a database." in prompt:
                            found = True
                
                self.assertTrue(found, "Rejection reason should be injected into clarify prompt")

    def test_requirements_audit_forbidden_pattern_routes_by_flagged_path_owner(self) -> None:
        cases = {
            ".auto-agents/docs/project_brief.md": "clarify",
            ".auto-agents/state/requirements_trace.json": "clarify",
            ".auto-agents/docs/architecture.md": "design",
            ".auto-agents/state/task_plan.json": "plan",
            ".auto-agents/docs/provider_references/provider.md": "provider_research",
            "app/service.py": "implement",
        }

        for path, expected_stage in cases.items():
            with self.subTest(path=path):
                stage, hard_failure = Orchestrator._audit_issue_route(
                    {
                        "kind": "forbidden_pattern",
                        "message": f"forbidden pattern found in {path}",
                        "path": path,
                    }
                )
                self.assertEqual(stage, expected_stage)
                self.assertEqual(hard_failure, "")

    def test_review_feedback_rewinds_to_design_for_architecture_owned_artifact(self) -> None:
        summary = (
            "DECISION: fail\n"
            "`.auto-agents/docs/architecture.md:146` still contradicts REQ-087 "
            "and must be updated before this task can pass."
        )

        self.assertEqual(Orchestrator._review_feedback_rewind_stage(summary), "design")

    def test_misrouted_project_brief_audit_recovery_rewinds_to_clarify_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            write_text(project_root / "spec.md", "# Spec\n")
            write_text(project_root / ".auto-agents" / "docs" / "project_brief.md", "legacy_gateway\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep legacy wording in the project brief.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["project brief uses current wording"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        },
                        {
                            "task_id": "fix-rejection-123",
                            "title": "Fix issues after release rejection",
                            "description": (
                                "The release was rejected with the following feedback:\n"
                                "The requirements audit failed. Use "
                                f"{project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'} "
                                "as the source of truth.\n\nPlease fix these issues."
                            ),
                            "acceptance": ["Feedback is fully addressed", "Tests pass"],
                            "requirement_ids": [],
                            "status": "blocked",
                            "commit_message": "",
                        },
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.status = "failed"
            state.current_stage = "implement"
            state.last_error = "Task fix-rejection-123 failed gates: review rejected the task"
            state.tasks = orchestrator._load_tasks_from_plan()

            changed = orchestrator._normalize_blocked_requirements_audit_recovery_resume(state)

            self.assertTrue(changed)
            self.assertEqual(state.current_stage, "clarify")
            self.assertEqual(state.rejected_stage, "clarify")
            self.assertIn("owned by clarify", state.rejection_reason)
            task_ids = [task.task_id for task in state.tasks]
            self.assertEqual(task_ids, ["task-001"])
            persisted_task_ids = [
                item["task_id"]
                for item in json.loads(task_plan_path(project_root).read_text(encoding="utf-8"))["tasks"]
            ]
            self.assertEqual(persisted_task_ids, ["task-001"])

    def test_requirements_audit_recovery_task_verify_fails_before_review_when_audit_still_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        },
                        {
                            "task_id": "fix-rejection-123",
                            "title": "Fix issues after release rejection",
                            "description": (
                                "The release was rejected with the following feedback:\n"
                                "The requirements audit failed. Use "
                                f"{project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'} "
                                "as the source of truth.\n\nPlease fix these issues."
                            ),
                            "acceptance": ["Feedback is fully addressed", "Tests pass"],
                            "requirement_ids": [],
                            "status": "in_progress",
                            "commit_message": "",
                        },
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            recovery_task = state.tasks[1]

            result = orchestrator._run_task_verify(recovery_task, state=state)

            self.assertFalse(result["ok"])
            self.assertIn("requirements audit still failed", str(result["reason"]))
            self.assertIn("REQ-001", result["failure_ids"])

    def test_requirements_audit_forbidden_pattern_routes_back_to_implement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertIn("requirements_audit", state.stage_summaries)
            self.assertEqual(
                (project_root / "app" / "service.py").read_text(encoding="utf-8").strip(),
                "modern_backend = True",
            )
            task_plan_text = task_plan_path(project_root).read_text(encoding="utf-8")
            run_state_text = (project_root / ".auto-agents" / "state" / "run_state.json").read_text(encoding="utf-8")
            self.assertNotIn("legacy_gateway still exists", task_plan_text)
            self.assertNotIn("legacy_gateway still exists", run_state_text)

    def test_requirements_audit_recovery_emits_verify_failure_before_rewind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")

            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            rendered = stream.getvalue()
            self.assertIn("[stage:verify] decision=fail route=implement", rendered)
            self.assertIn("requirements audit failed:", rendered)
            self.assertLess(
                rendered.index("[stage:verify] decision=fail route=implement"),
                rendered.index("[stage:implement] start"),
            )

    def test_requirements_audit_missing_coverage_routes_back_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Cover the requirement in at least one done task.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["task coverage exists"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": [],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.plan_calls, 1)
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertEqual([task.status for task in state.tasks], ["done", "done"])

    def test_requirements_audit_missing_provider_reference_routes_back_to_provider_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Use verified provider documentation.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["provider reference is verified"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": True,
                            "provider_reference": ".auto-agents/docs/provider_references/provider.md",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.provider_research_calls, 1)
            self.assertEqual(orchestrator.adapter.implement_calls, 0)
            self.assertIn("requirements_audit", state.stage_summaries)

    def test_pending_stages_reruns_explicitly_failed_verify(self) -> None:
        from auto_agents.models import RunState, TaskSpec

        state = RunState(run_id="run-123", status="failed", current_stage="verify")
        state.tasks = [
            TaskSpec(
                task_id="task-001",
                title="Done task",
                description="Already finished.",
                acceptance=["done"],
                status="done",
            )
        ]
        state.stage_summaries = {
            "clarify": "done",
            "design": "done",
            "plan": "done",
            "provider_research": "done",
            "implement": "done",
            "verify": "# Verify\n\nResult: fail\n\n- `pytest` -> failed",
        }

        pending = Orchestrator._pending_stages(object.__new__(Orchestrator), state)

        self.assertEqual(pending, ["verify", "readme"])

    def test_legacy_requirements_audit_failure_state_is_rewound_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\n", encoding="utf-8")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Do not keep the legacy backend path.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["artifact is modernized"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": ["legacy_gateway"],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )
            (project_root / "app").mkdir()
            write_text(project_root / "app" / "service.py", "legacy_gateway = True\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            state = load_run_state(project_root)
            state.status = "failed"
            state.current_stage = "verify"
            state.last_error = f"requirements audit failed: {project_root / '.auto-agents' / 'docs' / 'requirements_audit.md'}"
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
                "implement": "done",
                "verify": "done",
                "requirements_audit": "Result: pass",
            }
            state.tasks = orchestrator._load_tasks_from_plan()
            save_run_state(project_root, state)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertIn("requirements_audit", state.stage_summaries)
            self.assertNotIn("readme", state.rejected_stage)

    def test_requirements_audit_blocked_provider_reference_still_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._disable_gates_and_approvals(project_root)
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Use verified provider documentation.",
                            "source": "spec",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["provider reference is verified"],
                            "oracle_type": "deterministic_test",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "internal_state",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": True,
                            "provider_reference": ".auto-agents/docs/provider_references/provider.md",
                            "notes": "",
                        }
                    ],
                },
            )
            write_json(
                provider_references_lock_path(project_root),
                {
                    "version": 1,
                    "references": {
                        "provider": {
                            "path": ".auto-agents/docs/provider_references/provider.md",
                            "status": "blocked",
                            "retrieved_at": "2026-04-11T00:00:00Z",
                            "source_urls": ["https://example.com/official"],
                            "notes": "",
                        }
                    },
                },
            )
            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Existing done task",
                            "description": "Already finished.",
                            "acceptance": ["done"],
                            "requirement_ids": ["REQ-001"],
                            "status": "done",
                            "commit_message": "",
                        }
                    ]
                },
            )

            orchestrator = Orchestrator(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)
            state = load_run_state(project_root)

            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_verify(state)

            self.assertIn("Automatic recovery is unsafe", str(ctx.exception))

    def test_parallel_tasks_fall_back_to_sequential_without_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            config = orchestrator.config
            config.gates.commands = ["python3 -c \"print('ok')\""]
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            orchestrator.adapter = SequentialArtifactAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertEqual([task.status for task in state.tasks], ["done", "done"])
            self.assertIn("fallback to sequential", stream.getvalue())

    def test_parallel_tasks_strict_mode_requires_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.strict = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError) as ctx:
                orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertIn("depends_on", str(ctx.exception))

    def test_parallel_tasks_integrate_ready_batch_in_task_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["artifact-1.txt exists"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["artifact-2.txt exists"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            integrated = []

            def fake_run_task_in_worktree(state_snapshot, tasks_snapshot, task_id):
                task = next(item for item in tasks_snapshot if item.task_id == task_id)
                task.status = "done"
                task.review_summary = f"review for {task_id}"
                return {
                    "ok": True,
                    "task": task.to_dict(),
                    "reason": "",
                    "review": task.review_summary,
                    "commit_sha": f"worker-{task_id}",
                    "verify_current_failure_ids": [],
                }

            def fake_integrate(task, tasks, worker_commit_sha):
                integrated.append((task.task_id, worker_commit_sha))
                return f"main-{task.task_id}"

            with patch.object(orchestrator, "_run_task_in_worktree", side_effect=fake_run_task_in_worktree):
                with patch.object(orchestrator, "_integrate_parallel_task_result", side_effect=fake_integrate):
                    result = orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertEqual(
                integrated,
                [("task-001", "worker-task-001"), ("task-002", "worker-task-002")],
            )
            self.assertEqual([task.status for task in result.tasks], ["done", "done"])
            self.assertEqual(
                [task.commit_sha for task in result.tasks],
                ["main-task-001", "main-task-002"],
            )

    def test_parallel_tasks_defer_overlapping_worker_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            save_project_config(project_root, config)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            integrated = []
            sequential = []

            def fake_run_task_in_worktree(state_snapshot, tasks_snapshot, task_id):
                task = next(item for item in tasks_snapshot if item.task_id == task_id)
                task.status = "done"
                task.review_summary = f"review for {task_id}"
                return {
                    "ok": True,
                    "task": task.to_dict(),
                    "reason": "",
                    "review": task.review_summary,
                    "commit_sha": f"worker-{task_id}",
                    "changed_paths": ["shared.txt"],
                    "verify_current_failure_ids": [],
                }

            def fake_integrate(task, tasks, worker_commit_sha):
                integrated.append((task.task_id, worker_commit_sha))
                return f"main-{task.task_id}"

            def fake_execute_sequential(state_arg, tasks_arg, task):
                sequential.append(task.task_id)
                task.status = "done"
                task.commit_sha = f"main-{task.task_id}"
                return None

            with patch.object(orchestrator, "_run_task_in_worktree", side_effect=fake_run_task_in_worktree):
                with patch.object(orchestrator, "_integrate_parallel_task_result", side_effect=fake_integrate):
                    with patch.object(
                        orchestrator,
                        "_execute_task_in_main_worktree",
                        side_effect=fake_execute_sequential,
                    ):
                        result = orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertEqual(integrated, [("task-001", "worker-task-001")])
            self.assertEqual(sequential, ["task-002"])
            self.assertEqual([task.status for task in result.tasks], ["done", "done"])
            self.assertIn("defer integration task=task-002", stream.getvalue())

    def test_parallel_tasks_aggregate_failed_workers_and_copy_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            self._configure_git_identity(project_root)
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.gates.require_clean_git_before_task = False
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            config.execution.recovery.enabled = False
            save_project_config(project_root, config)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "First task",
                            "description": "Finish the first slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                        {
                            "task_id": "task-002",
                            "title": "Second task",
                            "description": "Finish the second slice.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        },
                    ]
                },
            )

            def fake_run_task_in_worktree(state_snapshot, tasks_snapshot, task_id):
                task = next(item for item in tasks_snapshot if item.task_id == task_id)
                task.review_summary = f"review for {task_id}"
                task.verify_history.append({
                    "attempt": 1,
                    "decision": "fail",
                    "summary": f"failed {task_id}",
                    "failure_ids": [f"reason:{task_id}"],
                })
                task.requirement_proofs = [{"requirement_id": "REQ-001", "status": "planned"}]
                return {
                    "ok": False,
                    "task": task.to_dict(),
                    "reason": f"failed {task_id}",
                    "review": task.review_summary,
                    "failure_ids": [f"reason:{task_id}"],
                    "comparable_failures": True,
                }

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with patch.object(orchestrator, "_run_task_in_worktree", side_effect=fake_run_task_in_worktree):
                with self.assertRaises(RuntimeError) as ctx:
                    orchestrator._run_implementation_loop(state, max_tasks=2)

            self.assertIn("task-001: failed task-001", str(ctx.exception))
            self.assertIn("task-002: failed task-002", str(ctx.exception))
            reloaded = orchestrator._load_tasks_from_plan()
            self.assertEqual([task.status for task in reloaded], ["blocked", "blocked"])
            self.assertEqual(reloaded[0].review_summary, "review for task-001")
            self.assertEqual(reloaded[0].requirement_proofs[0]["requirement_id"], "REQ-001")
            self.assertEqual(reloaded[1].verify_history[-1]["failure_ids"], ["reason:task-002"])

    def test_parallel_tasks_auto_workers_adapt_to_success_and_provider_pressure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = "auto"
            config.execution.parallel_tasks.max_auto_workers = 3
            config.execution.parallel_tasks.adaptive = True
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            self.assertEqual(orchestrator._parallel_worker_count(), 3)
            self.assertEqual(orchestrator._record_parallel_pressure(3), 1)
            self.assertEqual(orchestrator._parallel_worker_count(), 1)
            self.assertEqual(orchestrator._record_parallel_success(1), 2)
            self.assertEqual(orchestrator._parallel_worker_count(), 2)
            self.assertEqual(orchestrator._record_parallel_success(2), 3)
            self.assertEqual(orchestrator._parallel_worker_count(), 3)

    def test_parallel_provider_pressure_ignores_owned_proof_rate_limited_test_names(self) -> None:
        result = {
            "ok": False,
            "reason": (
                "owned proof evidence failed: tests/test_asset_consistency_runtime_api.py::"
                "AssetConsistencyRuntimeApiTests::"
                "test_rate_limited_asset_task_consistency_payload_preserves_retry_evidence"
            ),
            "review": "",
            "failure_ids": [
                "tests/test_asset_consistency_runtime_api.py::"
                "AssetConsistencyRuntimeApiTests::"
                "test_rate_limited_asset_task_consistency_payload_preserves_retry_evidence"
            ],
            "proof_evidence": {
                "ok": False,
                "failed_refs": [
                    "tests/test_asset_consistency_runtime_api.py::"
                    "AssetConsistencyRuntimeApiTests::"
                    "test_rate_limited_asset_task_consistency_payload_preserves_retry_evidence"
                ],
            },
        }

        self.assertFalse(Orchestrator._parallel_result_is_provider_pressure(result))

    def test_parallel_provider_pressure_detects_agent_provider_errors(self) -> None:
        pressure_reasons = [
            "All providers exhausted. Tried: codex. Last error: 429 rate limit exceeded",
            "parallel worktree execution failed: provider availability error",
            "implementation failed: stalled (no output) after 7200s",
        ]

        for reason in pressure_reasons:
            with self.subTest(reason=reason):
                self.assertTrue(
                    Orchestrator._parallel_result_is_provider_pressure(
                        {"ok": False, "reason": reason, "review": ""}
                    )
                )

    def test_parallel_tasks_auto_workers_support_copilot_pro_plus_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.active_provider = "copilot-cli"
            config.providers["copilot-cli"].subscription_tier = "pro+"
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = "auto"
            config.execution.parallel_tasks.max_auto_workers = 8
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            self.assertEqual(orchestrator._parallel_worker_count(), 2)
            self.assertEqual(orchestrator._record_parallel_success(2), 3)
            self.assertEqual(orchestrator._record_parallel_success(3), 4)
            self.assertEqual(orchestrator._parallel_worker_count(), 4)

    def test_parallel_tasks_fixed_workers_do_not_adapt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = 2
            config.execution.parallel_tasks.max_auto_workers = 8
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            self.assertEqual(orchestrator._parallel_worker_count(), 2)
            self.assertEqual(orchestrator._record_parallel_pressure(2), 2)
            self.assertEqual(orchestrator._parallel_worker_count(), 2)

    def test_parallel_tasks_logs_auto_resolution_and_single_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            config = orchestrator.config
            config.execution.parallel_tasks.enabled = True
            config.execution.parallel_tasks.workers = "auto"
            config.execution.parallel_tasks.max_auto_workers = 3
            config.gates.require_clean_git_before_task = False
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Only ready task",
                            "description": "Run one ready task.",
                            "acceptance": ["done"],
                            "depends_on": [],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with patch.object(orchestrator, "_execute_task_in_main_worktree", return_value=None):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            rendered = stream.getvalue()
            self.assertIn("auto mode resolved workers=3", rendered)
            self.assertIn("ready=1 batch=1; executing sequentially task=task-001", rendered)

    def test_run_stops_after_implement_when_max_task_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec = project_root / "SPEC.md"
            spec.write_text("# demo\n", encoding="utf-8")
            orchestrator = Orchestrator(project_root)
            config = orchestrator.config
            config.approvals.enabled = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            state = load_run_state(project_root)
            state.status = "pending"
            state.current_stage = "implement"
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "provider_research": "done",
            }
            save_run_state(project_root, state)

            def exhaust_budget(run_state, max_tasks=None):
                orchestrator._task_budget_exhausted = True
                run_state.stage_summaries["implement"] = "Processed 1 task(s)."
                return run_state

            with patch.object(orchestrator, "_run_implementation_loop", side_effect=exhaust_budget):
                with patch.object(orchestrator, "_run_verify") as verify_mock:
                    result = orchestrator.run(spec, auto_approve=True, max_tasks=1, skip_validate=True)

            self.assertEqual(result.status, "pending")
            self.assertEqual(result.current_stage, "implement")
            self.assertIn("implement", result.stage_summaries)
            verify_mock.assert_not_called()


class IterationAdapter:
    """Adapter that tracks stage calls for iteration testing.

    On the plan stage it writes only the new active iteration tasks.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.stage_calls: list[str] = []

    def run(self, request):
        self.stage_calls.append(request.stage)
        if request.stage == "clarify":
            write_text(request.output_path, "Clarified iteration scope.\nREADY_TO_GENERATE\n")
        elif request.stage == "plan":
            tp = task_plan_path(self.project_root)
            new_task = {
                "task_id": "task-002",
                "title": "New iteration task",
                "description": "Task added in iteration.",
                "acceptance": ["new feature works"],
                "status": "pending",
                "commit_message": "",
                "test_generated": True,
            }
            write_json(tp, {
                "test_strategy": "python-pytest",
                "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
                "tasks": [new_task],
            })
            write_text(request.output_path, "iteration plan\n")
        elif request.stage == "implement":
            write_text(self.project_root / "iter_artifact.txt", "done\n")
            write_text(request.output_path, "implemented iteration task\n")
        elif request.stage == "review":
            summary = "DECISION: pass\niteration review passed\n"
            write_text(request.output_path, summary)
            return AgentResult(
                ok=True, command=["fake"], output_path=request.output_path,
                summary=summary.strip(), returncode=0,
            )
        elif request.stage == "readme":
            if "Do NOT write the README yet. Only outline the planned sections." in request.prompt:
                write_text(request.output_path, "- Overview\n- Architecture\n- Usage\n")
            else:
                readme_content = (
                    "# Demo\n## Overview\nA demo project.\n"
                    "## Architecture\nSimple layout.\n"
                    "## Usage\n```bash\npython main.py\n```\n"
                    "## Development\nRun tests.\n"
                )
                write_text(self.project_root / "README.md", readme_content)
                write_text(request.output_path, "readme updated\n")
        else:
            write_text(request.output_path, f"{request.stage}\n")

        return AgentResult(
            ok=True, command=["fake"], output_path=request.output_path,
            summary=request.output_path.read_text(encoding="utf-8").strip(),
            returncode=0,
        )


class IterationFlowTests(unittest.TestCase):
    """Tests for starting a new iteration from a completed project."""

    def _make_completed_project(self, tmp):
        """Create a project with status=completed and one done task."""
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")

        # Disable approval gates so run completes without pausing
        config = load_project_config(project_root)
        config.approvals.enabled = []
        config.gates.commands = []
        config.gates.require_clean_git_before_task = False
        config.gates.allow_agent_updates = False
        save_project_config(project_root, config)

        # Seed a completed run state with one done task
        from auto_agents.config import save_run_state as _save
        from auto_agents.models import RunState, TaskSpec
        state = load_run_state(project_root)
        state.status = "completed"
        state.current_stage = "readme"
        state.stage_summaries = {
            "clarify": "done", "design": "done", "plan": "done",
            "implement": "done", "verify": "done", "readme": "done",
        }
        state.approved_gates = ["requirements", "architecture", "release"]
        state.agent_attempts = {"clarify": 1, "design": 1, "plan": 1}
        state.task_review_cache = {"task-001": {"decision": "pass"}}
        state.tasks = [
            TaskSpec(
                task_id="task-001", title="Phase 1 task",
                description="Already done.", acceptance=["done"],
                status="done", commit_message="feat: phase1",
            )
        ]
        _save(project_root, state)

        # Persist the done task into task_plan.json too
        write_json(task_plan_path(project_root), {
            "tasks": [state.tasks[0].to_dict()]
        })

        spec_file = project_root / "spec.md"
        spec_file.write_text("# Spec\nPhase 2 features.", encoding="utf-8")

        # Create a fake conda env so verification fast-fail check passes
        (project_root / ".conda" / "conda-meta").mkdir(parents=True, exist_ok=True)

        return project_root, spec_file

    def test_iteration_resets_state_fields(self):
        """approved_gates, agent_attempts and task_review_cache must be
        cleared when a new iteration starts."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, spec_file = self._make_completed_project(tmp)

            # Add a distinctive old agent_attempts key that won't recur
            from auto_agents.config import save_run_state as _save
            state = load_run_state(project_root)
            state.agent_attempts["implement-task-001"] = 3
            _save(project_root, state)

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = IterationAdapter(project_root)

            old_run_id = state.run_id

            # Simulate user answering "y" to the iteration prompt
            orchestrator._user_input_fn = lambda _prompt: "y"
            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertNotEqual(state.run_id, old_run_id, "New run_id should be generated")
            self.assertEqual(state.status, "completed")
            task_archive = archived_task_plan_path(project_root, old_run_id)
            state_archive = archived_run_state_path(project_root, old_run_id)
            self.assertTrue(task_archive.exists())
            self.assertTrue(state_archive.exists())
            archived_plan = json.loads(task_archive.read_text(encoding="utf-8"))
            self.assertEqual(archived_plan["tasks"][0]["task_id"], "task-001")
            self.assertEqual(archived_plan["tasks"][0]["status"], "done")
            self.assertEqual(state.resume_context["previous_run_id"], old_run_id)
            self.assertEqual(state.resume_context["previous_task_plan_archive"], str(task_archive))
            # Old implement-task-001 attempt count should be gone
            self.assertNotIn("implement-task-001", state.agent_attempts,
                             "Old agent_attempts should have been cleared at iteration start")
            # Old task_review_cache should be gone
            self.assertNotIn("task-001", state.task_review_cache,
                             "Old task_review_cache should have been cleared")

    def test_iteration_runs_implement_for_new_tasks(self):
        """After plan appends new pending tasks during iteration, the
        implement stage must execute them (dynamic pending-stages loop)."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, spec_file = self._make_completed_project(tmp)
            orchestrator = Orchestrator(project_root)
            adapter = IterationAdapter(project_root)
            orchestrator.adapter = adapter

            orchestrator._user_input_fn = lambda _prompt: "y"
            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual([task.task_id for task in state.tasks], ["task-002"])
            self.assertEqual(state.tasks[0].title, "New iteration task")
            self.assertEqual(state.tasks[0].status, "done")
            # implement must have been called
            self.assertIn("implement", adapter.stage_calls,
                          "Implement stage should run for new pending tasks")

    def test_iteration_without_auto_approve_pauses_at_gate(self):
        """Without --auto-approve the iteration should pause at the first
        approval gate (requirements) after clarify."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, spec_file = self._make_completed_project(tmp)

            # Re-enable the requirements gate
            config = load_project_config(project_root)
            config.approvals.enabled = ["requirements"]
            save_project_config(project_root, config)

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = IterationAdapter(project_root)

            # First call returns "y" for iteration prompt; subsequent
            # calls return default (empty) which the interactive clarify
            # path interprets as "nothing to add, proceed".
            call_count = [0]
            def mock_input(prompt):
                call_count[0] += 1
                if call_count[0] == 1:
                    return "y"
                return ""
            orchestrator._user_input_fn = mock_input

            state = orchestrator.run(spec_file=spec_file, auto_approve=False)

            self.assertEqual(state.status, "paused")
            self.assertEqual(state.pending_approval, "requirements")
            # approved_gates should be empty (cleared at iteration start)
            self.assertEqual(state.approved_gates, [])

    def test_auto_approve_still_runs_interactive_clarify(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            spec_file = project_root / "spec.md"
            spec_file.write_text("# Spec\nPhase 1 features.\n", encoding="utf-8")

            config = load_project_config(project_root)
            config.approvals.enabled = ["requirements"]
            save_project_config(project_root, config)

            state = load_run_state(project_root)
            state.stage_summaries = {
                "design": "done",
                "plan": "done",
                "provider_research": "done",
                "implement": "done",
                "verify": "done",
                "readme": "done",
            }
            from auto_agents.models import TaskSpec

            state.tasks = [
                TaskSpec(
                    task_id="task-001",
                    title="Existing task",
                    description="Already complete.",
                    acceptance=["done"],
                    status="done",
                    commit_message="feat: done",
                )
            ]
            save_run_state(project_root, state)

            orchestrator = Orchestrator(project_root)
            interactive_calls: list[str] = []

            def fake_interactive(state, clarify_spec_file):
                interactive_calls.append(str(clarify_spec_file))
                state.current_stage = "clarify"
                state.stage_summaries["clarify"] = "clarified"
                state.last_error = ""
                return state

            orchestrator._run_interactive_clarify = fake_interactive

            state = orchestrator.run(spec_file=spec_file, auto_approve=True, skip_validate=True)

            self.assertEqual(interactive_calls, [str(spec_file)])
            self.assertEqual(state.status, "completed")
            self.assertEqual(state.pending_approval, "")
            self.assertIn("requirements", state.approved_gates)

    def test_reject_architecture_clears_downstream_state(self):
        """Rejecting architecture should clear design+ downstream summaries
        and remove architecture/release approvals."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root, _spec_file = self._make_completed_project(tmp)

            orchestrator = Orchestrator(project_root)
            state = orchestrator.reject("architecture", "Need to redesign iteration scope")

            self.assertEqual(state.status, "pending")
            self.assertEqual(state.rejected_stage, "design")
            self.assertEqual(state.rejection_reason, "Need to redesign iteration scope")

            # clarify should remain; design and downstream must be removed.
            self.assertIn("clarify", state.stage_summaries)
            self.assertNotIn("design", state.stage_summaries)
            self.assertNotIn("plan", state.stage_summaries)
            self.assertNotIn("implement", state.stage_summaries)
            self.assertNotIn("verify", state.stage_summaries)
            self.assertNotIn("readme", state.stage_summaries)

            # requirements can remain approved; architecture/release must reset.
            self.assertIn("requirements", state.approved_gates)
            self.assertNotIn("architecture", state.approved_gates)
            self.assertNotIn("release", state.approved_gates)


class RepeatReviewBlockerAdapter:
    """Implement touches code on every attempt; review always returns the same blockers.

    Used to trigger the scope-overflow fingerprint signal.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            (self.project_root / f"artifact-{self.implement_calls}.txt").write_text(
                f"attempt-{self.implement_calls}\n", encoding="utf-8"
            )
            summary = f"implement attempt {self.implement_calls}\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = (
                "DECISION: fail\n"
                "Core issue: task bundles backend, API, and UI.\n"
                "- Split backend lifecycle from API surface.\n"
                "- Split workbench UI from server changes.\n"
            )
            write_text(request.output_path, summary)
        else:
            summary = f"{request.stage}\n"
            write_text(request.output_path, summary)
        return AgentResult(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ScopeOverflowTests(unittest.TestCase):
    def test_review_fingerprint_normalizes_and_matches(self) -> None:
        a = Orchestrator._review_fingerprint(
            "DECISION: fail\nCore issue: scope too large.\n- Split backend from UI.\n"
        )
        b = Orchestrator._review_fingerprint(
            "  decision: fail\n  core issue: scope too large.  \n  - split backend from ui.  \n"
        )
        c = Orchestrator._review_fingerprint("DECISION: pass\n")
        self.assertTrue(a)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(Orchestrator._review_fingerprint("   "), "")

    def test_repeated_review_blockers_trigger_plan_rewind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.implement = 4
            save_project_config(project_root, config)
            subprocess.run(["git", "config", "user.name", "test"], cwd=str(project_root), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(project_root), check=True)
            commit_all(project_root, "baseline")
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepeatReviewBlockerAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-big",
                            "title": "Cross-cutting task",
                            "description": "Bundles too many concerns.",
                            "acceptance": ["all layers updated"],
                            "status": "pending",
                            "commit_message": "",
                            "split_depth": 0,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            # Rewind: rejected_stage set to plan, plan summary cleared.
            self.assertEqual(result.rejected_stage, "plan")
            self.assertIn("SPLIT_TASK:", result.rejection_reason)
            self.assertIn("task-big", result.rejection_reason)
            self.assertNotIn("plan", result.stage_summaries)
            # Task reset to pending (not blocked) so plan can split it.
            self.assertEqual(result.tasks[0].status, "pending")
            self.assertEqual(changed_paths(project_root), [])
            self.assertFalse((project_root / "artifact-1.txt").exists())
            self.assertFalse((project_root / "artifact-2.txt").exists())
            # Two review failures are enough to trigger the signal (attempt 1 records
            # fingerprint, attempt 2 matches it).
            self.assertGreaterEqual(orchestrator.adapter.review_calls, 2)

    def test_scope_overflow_rewind_failure_stops_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.implement = 4
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepeatReviewBlockerAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-big",
                            "title": "Cross-cutting task",
                            "description": "Bundles too many concerns.",
                            "acceptance": ["all layers updated"],
                            "status": "pending",
                            "commit_message": "",
                            "split_depth": 0,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            import auto_agents.orchestrator as orch_mod

            original_reset = orch_mod.hard_reset_clean
            try:
                orch_mod.hard_reset_clean = lambda *_args, **_kwargs: False
                with self.assertRaises(RuntimeError) as ctx:
                    orchestrator._run_implementation_loop(state, max_tasks=1)
            finally:
                orch_mod.hard_reset_clean = original_reset

            self.assertIn("scope-overflow rewind failed to restore the baseline", str(ctx.exception))

    def test_split_depth_cap_blocks_instead_of_rewinding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.implement = 4
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = RepeatReviewBlockerAdapter(project_root)

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-child",
                            "title": "Already-split child",
                            "description": "Split lineage has reached the cap.",
                            "acceptance": ["criterion"],
                            "status": "pending",
                            "commit_message": "",
                            "split_depth": Orchestrator.MAX_SPLIT_DEPTH,
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            reloaded_tasks = orchestrator._load_tasks_from_plan()
            self.assertEqual(reloaded_tasks[0].status, "blocked")
            reloaded_state = load_run_state(project_root)
            self.assertNotEqual(reloaded_state.rejected_stage, "plan")

    def test_expected_test_migrations_excluded_from_new_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)

            from auto_agents.models import TaskSpec as _TaskSpec
            task = _TaskSpec(
                task_id="t",
                title="t",
                description="",
                acceptance=[],
                verify_baseline_failures=["old:legacy_case"],
                expected_test_migrations=["new:migrated_case"],
            )
            # Monkeypatch gate runner to return a fixed failure set that includes
            # one pre-existing failure (baseline) and one expected migration.
            class _Gate:
                ok = False
                summary = "new:migrated_case FAILED\nold:legacy_case FAILED"
                stdout = summary
                stderr = ""
                returncode = 1
                commands = []

            import auto_agents.orchestrator as orch_mod
            original_collect = orch_mod.run_commands_collect_all
            original_extract = orch_mod.extract_failure_ids
            try:
                orch_mod.run_commands_collect_all = lambda *a, **kw: _Gate()
                orch_mod.extract_failure_ids = lambda gate: ["new:migrated_case", "old:legacy_case"]
                config.gates.commands = ["echo run"]
                orchestrator.config = config
                result = orchestrator._run_task_verify(task)
            finally:
                orch_mod.run_commands_collect_all = original_collect
                orch_mod.extract_failure_ids = original_extract

            # Migration is excluded; baseline failure is also excluded → verify passes.
            self.assertTrue(result["ok"], msg=str(result))

    def test_full_verify_failure_routes_to_implement_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec(
                    task_id="task-001",
                    title="Existing task",
                    description="Already implemented.",
                    acceptance=["current contract is implemented"],
                    status="done",
                )
            ]
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "implement": "Completed 1 tasks.",
            }
            save_run_state(project_root, state)
            gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command="fake test",
                        ok=False,
                        returncode=1,
                        stdout="FAILED tests/test_api.py::test_old_contract - AssertionError: old field",
                    )
                ],
                summary="FAILED tests/test_api.py::test_old_contract",
            )

            with patch.object(orchestrator, "_run_gate_commands", return_value=(gate, "")):
                updated = orchestrator._run_verify(state)

            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.current_stage, "implement")
            self.assertEqual(updated.rejected_stage, "implement")
            self.assertEqual(updated.agent_attempts["verify_recovery"], 1)
            self.assertNotIn("verify", updated.stage_summaries)
            self.assertIn("Failure type: full_verification", updated.rejection_reason)
            self.assertIn("update repository tests only when they are stale", updated.rejection_reason)
            self.assertIn("[stage:verify] decision=fail route=implement", stream.getvalue())

    def test_full_verify_recovery_exhaustion_routes_to_clarify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            stream = io.StringIO()
            orchestrator = Orchestrator(project_root, agent_output_stream=stream)
            state = load_run_state(project_root)
            state.tasks = [
                TaskSpec(
                    task_id="task-001",
                    title="Existing task",
                    description="Already implemented.",
                    acceptance=["current contract is implemented"],
                    status="done",
                )
            ]
            state.stage_summaries = {
                "clarify": "done",
                "design": "done",
                "plan": "done",
                "implement": "Completed 1 tasks.",
            }
            state.agent_attempts["verify_recovery"] = orchestrator._verify_gate_recovery_limit()
            gate = GateResult(
                ok=False,
                commands=[CommandResult(command="fake test", ok=False, returncode=1)],
                summary="FAILED tests/test_api.py::test_still_fails",
            )

            with patch.object(orchestrator, "_run_gate_commands", return_value=(gate, "")):
                updated = orchestrator._run_verify(state)

            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.current_stage, "clarify")
            self.assertEqual(updated.rejected_stage, "clarify")
            self.assertNotIn("verify_recovery", updated.agent_attempts)
            self.assertIn("Automatic full verification recovery was exhausted", updated.rejection_reason)
            self.assertIn("Use the clarify conversation", updated.rejection_reason)
            self.assertIn("[stage:verify] decision=fail route=clarify", stream.getvalue())


class VaryingReviewArbiterAdapter:
    """Implement touches code; review always fails with VARYING wording so the
    static fingerprint signal never matches; arbiter returns a configurable
    decision."""

    def __init__(self, project_root: Path, arbiter_decision: str = "SPLIT", arbiter_text: Optional[str] = None) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.arbiter_calls = 0
        self.arbiter_decision = arbiter_decision
        self.arbiter_text = arbiter_text

    def run(self, request):
        from auto_agents.adapters.base import AgentResult as _AR
        if request.stage == "implement":
            self.implement_calls += 1
            (self.project_root / f"artifact-{self.implement_calls}.txt").write_text(
                f"attempt-{self.implement_calls}\n", encoding="utf-8"
            )
            summary = f"implement attempt {self.implement_calls}\n"
        elif request.stage == "review":
            self.review_calls += 1
            summary = (
                "DECISION: fail\n"
                f"This is review #{self.review_calls} with unique wording {self.review_calls}.\n"
                f"Acceptance criterion {self.review_calls} is not satisfied.\n"
            )
        elif request.stage == "arbiter":
            self.arbiter_calls += 1
            if self.arbiter_text is not None:
                summary = self.arbiter_text
            elif self.arbiter_decision == "SPLIT":
                summary = (
                    "DECISION: SPLIT\n"
                    "RATIONALE: task spans backend and UI which keep alternating as blockers.\n"
                    "SPLIT_AXIS:\n"
                    "- backend: extract data layer change\n"
                    "- UI: extract surface change\n"
                )
            else:
                summary = (
                    "DECISION: CONTINUE\n"
                    "RATIONALE: implementer is close; one more sharp attempt should converge.\n"
                )
        else:
            summary = f"{request.stage}\n"
        write_text(request.output_path, summary)
        return _AR(
            ok=True,
            command=["fake"],
            output_path=request.output_path,
            summary=summary.strip(),
            returncode=0,
        )


class ScopeArbiterTests(unittest.TestCase):
    def _make_project(self, tmp: str, with_review_history: int = 0) -> Tuple[Path, Orchestrator]:
        project_root = Path(tmp) / "demo"
        Orchestrator.init_project(project_root, "demo", "mock")
        orchestrator = Orchestrator(project_root)
        config = orchestrator.config
        config.gates.commands = []
        config.retries.implement = 4
        save_project_config(project_root, config)
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(project_root), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(project_root), check=True)
        commit_all(project_root, "baseline")
        orchestrator = Orchestrator(project_root)
        history = []
        for i in range(with_review_history):
            history.append({
                "attempt": i + 1,
                "summary": f"DECISION: fail\nprior review {i+1}",
            })
        write_json(
            task_plan_path(project_root),
            {
                "tasks": [
                    {
                        "task_id": "task-arb",
                        "title": "Cross-cutting task",
                        "description": "Bundles several layers.",
                        "acceptance": ["all layers updated"],
                        "status": "blocked" if with_review_history else "pending",
                        "commit_message": "",
                        "split_depth": 0,
                        "review_history": history,
                    }
                ]
            },
        )
        if with_review_history:
            commit_all(project_root, "test: persist blocked task baseline")
        return project_root, orchestrator

    def test_arbiter_parses_split_and_continue(self) -> None:
        split = Orchestrator._parse_arbiter_decision(
            "DECISION: SPLIT\nRATIONALE: too coupled.\nSPLIT_AXIS:\n- a\n- b\n"
        )
        self.assertEqual(split["decision"], "SPLIT")
        self.assertEqual(split["rationale"], "too coupled.")
        self.assertEqual(split["split_axis"], ["a", "b"])

        cont = Orchestrator._parse_arbiter_decision("DECISION: CONTINUE\nRATIONALE: close.\n")
        self.assertEqual(cont["decision"], "CONTINUE")
        self.assertEqual(cont["split_axis"], [])

        bad = Orchestrator._parse_arbiter_decision("garbage output")
        self.assertEqual(bad["decision"], "")

    def test_arbiter_split_triggers_rewind_when_fingerprints_vary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp)
            orchestrator.adapter = VaryingReviewArbiterAdapter(project_root, arbiter_decision="SPLIT")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.rejected_stage, "plan")
            self.assertIn("SPLIT_TASK:", result.rejection_reason)
            self.assertIn("Scope arbiter verdict: SPLIT", result.rejection_reason)
            self.assertIn("backend", result.rejection_reason)
            self.assertGreaterEqual(orchestrator.adapter.arbiter_calls, 1)
            self.assertEqual(result.tasks[0].status, "pending")
            self.assertTrue(result.tasks[0].arbitration_history)
            self.assertEqual(result.tasks[0].arbitration_history[-1]["decision"], "SPLIT")

    def test_arbiter_continue_lets_loop_exhaust_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp)
            orchestrator.adapter = VaryingReviewArbiterAdapter(project_root, arbiter_decision="CONTINUE")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            reloaded_tasks = orchestrator._load_tasks_from_plan()
            self.assertEqual(reloaded_tasks[0].status, "blocked")
            reloaded_state = load_run_state(project_root)
            self.assertNotEqual(reloaded_state.rejected_stage, "plan")
            self.assertEqual(orchestrator.adapter.review_calls, 4)
            self.assertGreaterEqual(orchestrator.adapter.arbiter_calls, 3)

    def test_arbiter_consulted_on_first_fail_when_history_already_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp, with_review_history=2)
            orchestrator.adapter = VaryingReviewArbiterAdapter(project_root, arbiter_decision="SPLIT")

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            result = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(result.rejected_stage, "plan")
            self.assertIn("Scope arbiter verdict: SPLIT", result.rejection_reason)
            self.assertEqual(orchestrator.adapter.review_calls, 1)
            self.assertEqual(orchestrator.adapter.arbiter_calls, 1)

    def test_arbiter_unparseable_output_falls_back_to_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, orchestrator = self._make_project(tmp)
            orchestrator.adapter = VaryingReviewArbiterAdapter(
                project_root, arbiter_text="this is not parseable at all"
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            reloaded_tasks = orchestrator._load_tasks_from_plan()
            self.assertEqual(reloaded_tasks[0].status, "blocked")
            self.assertEqual(orchestrator.adapter.review_calls, 4)


if __name__ == "__main__":
    unittest.main()
