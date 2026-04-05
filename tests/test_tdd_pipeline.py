import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import load_run_state, save_project_config, task_plan_path
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, TaskSpec
from auto_agents.orchestrator import Orchestrator


class SimpleImplementAdapter:
    """Adapter that simulates a successful implement + review cycle."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "artifact.txt", "done\n")
            summary = "implemented feature"
            write_text(request.output_path, summary + "\n")
        elif request.stage == "review":
            self.review_calls += 1
            summary = "DECISION: pass\nAll good"
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


class ImplementPipelineTests(unittest.TestCase):

    def test_implement_prompt_includes_test_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Write feature",
                description="Write a feature.",
                acceptance=["feature works"],
            )

            prompt = orchestrator._build_task_prompt(task, "implement")
            self.assertIn("MUST also write or update tests", prompt)
            self.assertIn("observable behavior", prompt)

    def test_review_prompt_includes_test_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Write feature",
                description="Write a feature.",
                acceptance=["feature works"],
            )

            prompt = orchestrator._build_task_prompt(task, "review")
            self.assertIn("TEST AUDIT", prompt)
            self.assertIn("observable behavior", prompt)

    def test_implement_runs_without_test_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            adapter = SimpleImplementAdapter(project_root)
            orchestrator.adapter = adapter

            write_json(
                task_plan_path(project_root),
                {
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "title": "Write feature",
                            "description": "Write a feature.",
                            "acceptance": ["feature works"],
                            "status": "pending",
                            "commit_message": "",
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertGreaterEqual(adapter.implement_calls, 1)
            self.assertGreaterEqual(adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")

    def test_build_task_prompt_unsupported_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            task = TaskSpec(
                task_id="task-001",
                title="Write feature",
                description="Write a feature.",
                acceptance=["feature works"],
            )

            with self.assertRaises(RuntimeError):
                orchestrator._build_task_prompt(task, "test_writer")


if __name__ == "__main__":
    unittest.main()
