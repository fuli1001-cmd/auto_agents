import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import (
    load_project_config,
    load_run_state,
    provider_references_lock_path,
    requirements_trace_path,
    save_project_config,
    save_run_state,
    task_plan_path,
)
from auto_agents.git_ops import worktree_fingerprint
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator


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
                        "test_strategy": "python-unittest",
                        "verification_commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
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
                    "test_strategy": "python-unittest",
                    "verification_commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
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
                    "test_strategy": "python-unittest",
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
            summary = "implemented audit recovery\n"
            write_text(request.output_path, summary)
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\naudit recovery review passed\n"
            write_text(request.output_path, summary)
        elif request.stage == "readme":
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
            self.assertEqual(config.gates.commands, ["conda run -p ./.conda python -m unittest discover -s tests"])

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

    def test_persisted_tasks_keep_generated_verification_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            write_json(
                task_plan_path(project_root),
                {
                    "test_strategy": "python-unittest",
                    "verification_commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
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
            self.assertIn('"test_strategy": "python-unittest"', payload)
            self.assertIn('"verification_commands": [', payload)

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
            config.git.commit_each_task = False
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
            config.gates.commands = ["conda run -p ./.conda python -m unittest discover -s tests"]
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
                            "review_summary": "legacy_gateway still exists in the current implementation",
                            "review_history": [{"attempt": 1, "summary": "legacy_gateway still exists in the current implementation"}],
                        }
                    ]
                },
            )
            write_text(project_root / "artifact.txt", "legacy_gateway\n")

            orchestrator = Orchestrator(project_root)
            orchestrator.adapter = AuditRecoveryAdapter(project_root)
            self._seed_verify_ready_state(project_root, orchestrator)

            state = orchestrator.run(spec_file=spec_file, auto_approve=True)

            self.assertEqual(state.status, "completed")
            self.assertEqual(orchestrator.adapter.implement_calls, 1)
            self.assertIn("requirements_audit", state.stage_summaries)
            self.assertEqual((project_root / "artifact.txt").read_text(encoding="utf-8").strip(), "modern_backend")
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
                            "review_summary": "legacy_gateway still exists in the current implementation",
                            "review_history": [{"attempt": 1, "summary": "legacy_gateway still exists in the current implementation"}],
                        }
                    ]
                },
            )
            write_text(project_root / "artifact.txt", "legacy_gateway\n")

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
                            "review_summary": "legacy_gateway still exists in the current implementation",
                            "review_history": [{"attempt": 1, "summary": "legacy_gateway still exists in the current implementation"}],
                        }
                    ]
                },
            )
            write_text(project_root / "artifact.txt", "legacy_gateway\n")

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


class IterationAdapter:
    """Adapter that tracks stage calls for iteration testing.

    On the plan stage it writes a task_plan.json that preserves existing
    done tasks and appends new pending ones.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.stage_calls: list[str] = []

    def run(self, request):
        self.stage_calls.append(request.stage)
        if request.stage == "plan":
            # Read existing plan so we can preserve done tasks
            import json
            existing = {"tasks": []}
            tp = task_plan_path(self.project_root)
            if tp.exists():
                existing = json.loads(tp.read_text(encoding="utf-8"))

            done_tasks = [t for t in existing.get("tasks", []) if t.get("status") == "done"]
            new_task = {
                "task_id": f"task-{len(done_tasks) + 1:03d}",
                "title": "New iteration task",
                "description": "Task added in iteration.",
                "acceptance": ["new feature works"],
                "status": "pending",
                "commit_message": "",
                "test_generated": True,
            }
            write_json(tp, {
                "test_strategy": "python-unittest",
                "verification_commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
                "tasks": done_tasks + [new_task],
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
            # The old done task should be preserved
            done_tasks = [t for t in state.tasks if t.status == "done"]
            self.assertGreaterEqual(len(done_tasks), 2,
                                    "Both old and new tasks should be done")
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


if __name__ == "__main__":
    unittest.main()
