import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import load_project_config, load_run_state, save_project_config, task_plan_path
from auto_agents.git_ops import commit_all
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, TaskSpec
from auto_agents.orchestrator import Orchestrator


class TestWriterAdapter:
    """Adapter that simulates a test_writer agent creating a test file,
    then a successful implement + review cycle."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.test_writer_calls = 0
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if "test_writer" in (request.stage + getattr(request, "prompt", "")):
            # Detect test_writer by the prompt content since stage is "implement"
            pass

        prompt = getattr(request, "prompt", "")
        if "Test-Writer agent" in prompt:
            self.test_writer_calls += 1
            tests_dir = self.project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "test_contract.py").write_text(
                "import unittest\n\nclass TestContract(unittest.TestCase):\n"
                "    def test_placeholder(self):\n        self.fail('Not implemented yet')\n",
                encoding="utf-8",
            )
            summary = "Created test_contract.py with acceptance tests"
            write_text(request.output_path, summary + "\n")
        elif request.stage == "implement":
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


class TamperingImplementAdapter:
    """Adapter that simulates an implement agent that tampers with contract files."""

    def __init__(self, project_root: Path, contract_files: list) -> None:
        self.project_root = project_root
        self.contract_files = contract_files
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        prompt = getattr(request, "prompt", "")
        if "Test-Writer agent" in prompt:
            tests_dir = self.project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            for f in self.contract_files:
                filepath = self.project_root / f
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(
                    "import unittest\n\nclass TestContract(unittest.TestCase):\n"
                    "    def test_original(self):\n        self.fail('Not implemented yet')\n",
                    encoding="utf-8",
                )
            summary = "Created test contract files"
            write_text(request.output_path, summary + "\n")
        elif request.stage == "implement":
            self.implement_calls += 1
            # Tamper with contract files
            for f in self.contract_files:
                filepath = self.project_root / f
                filepath.write_text("# TAMPERED\n", encoding="utf-8")
            write_text(self.project_root / "artifact.txt", "done\n")
            summary = "implemented (with tampering)"
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


class NoTamperImplementAdapter:
    """Adapter that does NOT tamper with contract files."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        prompt = getattr(request, "prompt", "")
        if "Test-Writer agent" in prompt:
            tests_dir = self.project_root / "tests"
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "test_contract.py").write_text(
                "import unittest\n\nclass TestContract(unittest.TestCase):\n"
                "    def test_original(self):\n        self.fail('Not implemented yet')\n",
                encoding="utf-8",
            )
            summary = "Created test files"
            write_text(request.output_path, summary + "\n")
        elif request.stage == "implement":
            self.implement_calls += 1
            write_text(self.project_root / "feature.py", "def hello(): return 'world'\n")
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


class TDDPipelineTests(unittest.TestCase):

    def test_task_spec_new_fields_default(self) -> None:
        task = TaskSpec(
            task_id="t1",
            title="Test",
            description="desc",
            acceptance=["a"],
        )
        self.assertFalse(task.test_generated)
        self.assertEqual(task.contract_files, [])

    def test_task_spec_round_trip_serialization(self) -> None:
        task = TaskSpec(
            task_id="t1",
            title="Test",
            description="desc",
            acceptance=["a"],
            test_generated=True,
            contract_files=["tests/test_foo.py"],
        )
        data = task.to_dict()
        self.assertTrue(data["test_generated"])
        self.assertEqual(data["contract_files"], ["tests/test_foo.py"])

        restored = TaskSpec.from_dict(data)
        self.assertTrue(restored.test_generated)
        self.assertEqual(restored.contract_files, ["tests/test_foo.py"])

    def test_task_spec_from_dict_missing_new_fields(self) -> None:
        data = {
            "task_id": "t1",
            "title": "Test",
            "description": "desc",
            "acceptance": ["a"],
        }
        task = TaskSpec.from_dict(data)
        self.assertFalse(task.test_generated)
        self.assertEqual(task.contract_files, [])

    def test_test_writer_runs_before_implement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            adapter = TestWriterAdapter(project_root)
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

            self.assertEqual(adapter.test_writer_calls, 1)
            self.assertGreaterEqual(adapter.implement_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")
            self.assertTrue(state.tasks[0].test_generated)
            self.assertIn("tests/test_contract.py", state.tasks[0].contract_files)

    def test_test_writer_skipped_when_already_generated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            adapter = TestWriterAdapter(project_root)
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
                            "test_generated": True,
                            "contract_files": ["tests/test_existing.py"],
                        }
                    ]
                },
            )

            state = load_run_state(project_root)
            state.tasks = orchestrator._load_tasks_from_plan()
            state = orchestrator._run_implementation_loop(state, max_tasks=1)

            self.assertEqual(adapter.test_writer_calls, 0)
            self.assertEqual(state.tasks[0].status, "done")

    def test_contract_file_tampering_rejects_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            config.retries.per_stage["implement"] = 2
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            contract_files = ["tests/test_contract.py"]
            adapter = TamperingImplementAdapter(project_root, contract_files)
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

            with self.assertRaises(RuntimeError):
                orchestrator._run_implementation_loop(state, max_tasks=1)

            # The implement agent should have been called for each retry attempt
            self.assertEqual(adapter.implement_calls, 2)
            # Review should never have been reached due to contract tampering
            self.assertEqual(adapter.review_calls, 0)

    def test_no_tampering_passes_through_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)

            config = orchestrator.config
            config.gates.commands = []
            save_project_config(project_root, config)
            orchestrator = Orchestrator(project_root)
            adapter = NoTamperImplementAdapter(project_root)
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

            self.assertEqual(adapter.implement_calls, 1)
            self.assertEqual(adapter.review_calls, 1)
            self.assertEqual(state.tasks[0].status, "done")

    def test_build_task_prompt_test_writer_stage(self) -> None:
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

            prompt = orchestrator._build_task_prompt(task, "test_writer")
            self.assertIn("Test-Writer agent", prompt)
            self.assertIn("black-box acceptance tests", prompt)
            self.assertIn("Do NOT implement any business logic", prompt)


if __name__ == "__main__":
    unittest.main()
