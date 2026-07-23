import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.validation import task_plan_warnings, validate_task_plan_payload


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
        self.assertTrue(any("verification step" in item for item in errors))

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

    def test_duplicate_titles_warn_instead_of_fail(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Fix full verification failure",
                    "description": "Handle one verification failure bucket.",
                    "acceptance": ["first slice is covered"],
                    "status": "pending",
                    "commit_message": "",
                },
                {
                    "task_id": "task-002",
                    "title": "Fix full verification failure",
                    "description": "Handle another verification failure bucket.",
                    "acceptance": ["second slice is covered"],
                    "status": "pending",
                    "commit_message": "",
                },
            ]
        }

        self.assertEqual(validate_task_plan_payload(payload), [])
        warnings = task_plan_warnings(payload)
        self.assertTrue(any("duplicate titles" in item for item in warnings))

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

    def test_accepts_python_pytest_verification_step(self) -> None:
        payload = {
            "test_strategy": "python-pytest",
            "verification_steps": [{"kind": "test", "runner": "pytest", "targets": ["tests"]}],
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

    def test_rejects_unknown_verification_cadence_and_cache_scope(self) -> None:
        payload = {
            "test_strategy": "python-pytest",
            "verification_steps": [
                {
                    "kind": "test",
                    "runner": "pytest",
                    "targets": ["tests"],
                    "cadence": "sometimes",
                    "cache_scope": "global",
                }
            ],
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Add CLI entrypoint",
                    "description": "Add a runnable command line entrypoint.",
                    "acceptance": ["done"],
                    "status": "pending",
                    "commit_message": "feat(task-001): add CLI entrypoint",
                }
            ],
        }

        errors = validate_task_plan_payload(payload, require_verification=True)
        self.assertTrue(any(".cadence must be one of" in item for item in errors))
        self.assertTrue(any(".cache_scope must be one of" in item for item in errors))

    def test_allows_large_task_count_without_hard_failure(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": f"task-{index:03d}",
                    "title": f"Feature slice {index}",
                    "description": f"Implement verifiable slice {index} for the MVP.",
                    "acceptance": [f"slice {index} can be verified"],
                    "status": "pending",
                    "commit_message": f"feat(task-{index:03d}): add slice {index}",
                }
                for index in range(1, 31)
            ]
        }

        self.assertEqual(validate_task_plan_payload(payload), [])

    def test_warns_when_large_plan_looks_oversliced(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": f"task-{index:03d}",
                    "title": f"Step {index}",
                    "description": "Tiny change.",
                    "acceptance": ["one check"],
                    "status": "pending",
                    "commit_message": "",
                }
                for index in range(1, 31)
            ]
        }

        warnings = task_plan_warnings(payload)
        self.assertTrue(any("contains 30 active tasks" in item for item in warnings))
        self.assertTrue(any("over-fragmented" in item or "oversliced" in item for item in warnings))

    def test_warns_when_task_has_too_many_acceptance_criteria(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "God task",
                    "description": "Does everything.",
                    "acceptance": [f"criterion {i}" for i in range(8)],
                    "status": "pending",
                    "commit_message": "",
                }
            ]
        }

        warnings = task_plan_warnings(payload)
        self.assertTrue(any(">5 acceptance criteria" in item for item in warnings))
        self.assertIn("task-001", " ".join(warnings))

    def test_done_tasks_do_not_emit_task_size_warnings(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": f"task-{index:03d}",
                    "title": "Fix full verification failure" if index in (1, 2) else f"Historical slice {index}",
                    "description": "x" * 600,
                    "acceptance": [f"criterion {i}" for i in range(8)],
                    "status": "done",
                    "commit_message": "",
                }
                for index in range(1, 31)
            ]
        }

        warnings = task_plan_warnings(payload)
        self.assertEqual(warnings, [])

    def test_active_tasks_require_scope_boundaries_for_six_or_seven_acceptance_criteria(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Wide but coherent task",
                    "description": "Implement one coherent API behavior.",
                    "acceptance": [f"criterion {i}" for i in range(6)],
                    "status": "pending",
                    "commit_message": "",
                }
            ]
        }

        errors = validate_task_plan_payload(payload, enforce_active_task_granularity=True)
        self.assertTrue(any("scope_boundaries" in item for item in errors))

        payload["tasks"][0]["scope_boundaries"] = (
            "All criteria cover the same endpoint contract; persistence and UI are out of scope."
        )
        self.assertEqual(validate_task_plan_payload(payload, enforce_active_task_granularity=True), [])

    def test_active_tasks_with_more_than_seven_acceptance_criteria_must_split(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Oversized active task",
                    "description": "Does too much.",
                    "acceptance": [f"criterion {i}" for i in range(8)],
                    "scope_boundaries": "This is intentionally broad.",
                    "status": "pending",
                    "commit_message": "",
                }
            ]
        }

        errors = validate_task_plan_payload(payload, enforce_active_task_granularity=True)
        self.assertTrue(any("must be split" in item for item in errors))

    def test_warns_when_task_description_is_very_long(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Broad task",
                    "description": "x" * 600,
                    "acceptance": ["something"],
                    "status": "pending",
                    "commit_message": "",
                }
            ]
        }

        warnings = task_plan_warnings(payload)
        self.assertTrue(any(">500 chars" in item for item in warnings))
        self.assertIn("task-001", " ".join(warnings))

    def test_accepts_planner_generated_depends_on(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "Base slice",
                    "description": "Build the prerequisite slice.",
                    "acceptance": ["base slice works"],
                    "depends_on": [],
                    "status": "done",
                    "commit_message": "",
                },
                {
                    "task_id": "task-002",
                    "title": "Follow-up slice",
                    "description": "Build on the prerequisite slice.",
                    "acceptance": ["follow-up works"],
                    "depends_on": ["task-001"],
                    "status": "pending",
                    "commit_message": "",
                },
            ]
        }

        self.assertEqual(validate_task_plan_payload(payload), [])

    def test_rejects_invalid_depends_on_graph(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "First",
                    "description": "First task.",
                    "acceptance": ["first works"],
                    "depends_on": ["task-002"],
                    "status": "pending",
                    "commit_message": "",
                },
                {
                    "task_id": "task-002",
                    "title": "Second",
                    "description": "Second task.",
                    "acceptance": ["second works"],
                    "depends_on": ["task-001", "task-001"],
                    "status": "pending",
                    "commit_message": "",
                },
                {
                    "task_id": "task-003",
                    "title": "Third",
                    "description": "Third task.",
                    "acceptance": ["third works"],
                    "depends_on": ["missing-task", "task-003"],
                    "status": "pending",
                    "commit_message": "",
                },
            ]
        }

        errors = validate_task_plan_payload(payload)
        self.assertTrue(any("must not contain duplicates" in item for item in errors))
        self.assertTrue(any("unknown task 'missing-task'" in item for item in errors))
        self.assertTrue(any("cannot depend on itself" in item for item in errors))
        self.assertTrue(any("cyclic depends_on relationship" in item for item in errors))

    def test_strict_parallel_mode_requires_depends_on_on_non_done_tasks(self) -> None:
        payload = {
            "tasks": [
                {
                    "task_id": "task-001",
                    "title": "First",
                    "description": "First task.",
                    "acceptance": ["first works"],
                    "status": "pending",
                    "commit_message": "",
                }
            ]
        }

        errors = validate_task_plan_payload(payload, require_depends_on_for_pending=True)
        self.assertTrue(any("depends_on must be present" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
