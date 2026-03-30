import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.validation import validate_task_plan_payload


class TaskPlanValidationTests(unittest.TestCase):
    def test_accepts_valid_plan(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Add CLI entrypoint",
                    "description": "Add a runnable command line entrypoint.",
                    "acceptance": ["`python -m demo --help` exits successfully."],
                    "status": "pending",
                    "commit_message": "feat(task-001): add CLI entrypoint",
                }
            ]
        }
        self.assertEqual(validate_task_plan_payload(payload), [])

    def test_accepts_in_progress_status(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Add CLI entrypoint",
                    "description": "Add a runnable command line entrypoint.",
                    "acceptance": ["`python -m demo --help` exits successfully."],
                    "status": "in_progress",
                    "commit_message": "feat(task-001): add CLI entrypoint",
                }
            ]
        }
        self.assertEqual(validate_task_plan_payload(payload), [])

    def test_requires_verification_contract_when_requested(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Add CLI entrypoint",
                    "description": "Add a runnable command line entrypoint.",
                    "acceptance": ["`python -m demo --help` exits successfully."],
                    "status": "pending",
                    "commit_message": "feat(task-001): add CLI entrypoint",
                }
            ]
        }
        errors = validate_task_plan_payload(payload, require_verification=True)
        self.assertTrue(any("test_strategy" in item for item in errors))
        self.assertTrue(any("verification command" in item for item in errors))

    def test_rejects_duplicate_ids_and_empty_acceptance(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "One",
                    "description": "Desc",
                    "acceptance": [""],
                    "status": "pending",
                    "commit_message": "",
                },
                {
                    "task_id": "task-001",
                    "title": "Two",
                    "description": "",
                    "acceptance": [],
                    "status": "unknown",
                    "commit_message": "",
                },
            ]
        }
        errors = validate_task_plan_payload(payload)
        self.assertTrue(any("duplicates task_id" in item for item in errors))
        self.assertTrue(any("acceptance items" in item for item in errors))
        self.assertTrue(any("non-empty acceptance list" in item for item in errors))
        self.assertTrue(any("status must be one of" in item for item in errors))

    def test_rejects_python_verification_outside_project_local_conda(self) -> None:
        payload = {
            "test_strategy": "python-unittest",
            "verification_commands": ["python3 -m unittest discover -s tests"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Add CLI entrypoint",
                    "description": "Add a runnable command line entrypoint.",
                    "acceptance": ["`python -m demo --help` exits successfully."],
                    "status": "pending",
                    "commit_message": "feat(task-001): add CLI entrypoint",
                }
            ],
        }

        errors = validate_task_plan_payload(payload, require_verification=True)
        self.assertTrue(any("project-local conda env" in item for item in errors))

    def test_accepts_python_verification_inside_project_local_conda(self) -> None:
        payload = {
            "test_strategy": "python-unittest",
            "verification_commands": ["conda run -p ./.conda python -m unittest discover -s tests"],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Add CLI entrypoint",
                    "description": "Add a runnable command line entrypoint.",
                    "acceptance": ["`python -m demo --help` exits successfully."],
                    "status": "pending",
                    "commit_message": "feat(task-001): add CLI entrypoint",
                }
            ],
        }

        self.assertEqual(validate_task_plan_payload(payload, require_verification=True), [])


if __name__ == "__main__":
    unittest.main()
