import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import load_project_config, load_run_state, save_project_config, task_plan_path
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


class RetryFlowTests(unittest.TestCase):
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

    def test_verify_failure_skips_review_and_retries_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = ["python -c \"from pathlib import Path; raise SystemExit(0 if Path('artifact.txt').read_text().strip() == 'good' else 1)\""]
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

            self.assertEqual(orchestrator.adapter.implement_calls, orchestrator.config.retries.per_stage["implement"])
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
            config.gates.commands = ["python -c \"raise SystemExit(1)\""]
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

            self.assertEqual(
                len(orchestrator.adapter.implement_prompts),
                orchestrator.config.retries.per_stage["implement"],
            )
            self.assertIn("Failure type: local_verification", orchestrator.adapter.implement_prompts[1])
            self.assertEqual(orchestrator.adapter.review_calls, 0)

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
            self.assertEqual(orchestrator.adapter.implement_calls, orchestrator.config.retries.per_stage["implement"])
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


if __name__ == "__main__":
    unittest.main()

