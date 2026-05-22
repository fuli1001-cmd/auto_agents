import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gates import (
    build_failure_identity_diagnostic_command,
    command_from_verification_step,
    extract_failure_ids,
    extract_failure_info,
    run_commands,
    run_gate_plan,
)
from auto_agents.models import CommandResult, GateParallelGroup, GateResult, VerificationStep


class GateTests(unittest.TestCase):
    def test_run_commands_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(["python3 -c \"print('ok')\""], Path(tmp))
            self.assertTrue(result.ok)
            self.assertEqual(len(result.commands), 1)
            self.assertEqual(result.commands[0].stdout, "ok")

    def test_run_commands_stops_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(
                [
                    "python3 -c \"print('before')\"",
                    "python3 -c \"import sys; sys.exit(3)\"",
                    "python3 -c \"print('after')\"",
                ],
                Path(tmp),
            )
            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 2)
            self.assertEqual(result.commands[1].returncode, 3)
            self.assertIn("command failed:", result.summary)
            self.assertIn("python3 -c \"import sys; sys.exit(3)\"", result.summary)

    def test_run_commands_includes_stderr_in_failure_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(
                ["python3 -c \"import sys; sys.stderr.write('boom\\n'); sys.exit(2)\""],
                Path(tmp),
            )

            self.assertFalse(result.ok)
            self.assertIn("boom", result.summary)

    def test_extract_unittest_failure_ids(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m unittest",
                    ok=False,
                    returncode=1,
                    stdout=(
                        "ERROR: test_publish_flow (tests.test_api.PublishTests.test_publish_flow)\n"
                        "FAIL: test_subtitles (tests.test_api.SubtitleTests.test_subtitles)\n"
                    ),
                    stderr="",
                )
            ],
        )

        ids = extract_failure_ids(gate)

        self.assertEqual(
            ids,
            [
                "test_publish_flow (tests.test_api.PublishTests.test_publish_flow)",
                "test_subtitles (tests.test_api.SubtitleTests.test_subtitles)",
                ],
            )

    def test_extract_unittest_failure_ids_ignores_summary_line(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m unittest discover -s tests",
                    ok=False,
                    returncode=1,
                    stdout=(
                        "ERROR: test_publish_flow (tests.test_api.PublishTests.test_publish_flow)\n"
                        "FAIL: test_subtitles (tests.test_api.SubtitleTests.test_subtitles)\n"
                        "FAILED (failures=5, errors=2)\n"
                    ),
                    stderr="",
                )
            ],
        )

        ids = extract_failure_ids(gate)

        self.assertEqual(
            ids,
            [
                "test_publish_flow (tests.test_api.PublishTests.test_publish_flow)",
                "test_subtitles (tests.test_api.SubtitleTests.test_subtitles)",
            ],
        )
        self.assertNotIn("(failures=5,", ids)

    def test_extract_unittest_summary_only_falls_back_to_command(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m unittest discover -s tests",
                    ok=False,
                    returncode=1,
                    stdout="FAILED (failures=5, errors=2)\n",
                    stderr="",
                )
            ],
        )

        ids = extract_failure_ids(gate)

        self.assertEqual(ids, ["cmd:python -m unittest discover -s tests"])

    def test_unparsed_command_failure_is_non_comparable(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m unittest discover -s tests",
                    ok=False,
                    returncode=1,
                    stdout="FAILED (failures=5, errors=2)\n",
                    stderr="",
                )
            ],
        )

        info = extract_failure_info(gate)

        self.assertFalse(info.comparable)
        self.assertEqual(info.failure_ids, ["cmd:python -m unittest discover -s tests"])

    def test_test_case_failure_is_comparable(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m pytest -q tests",
                    ok=False,
                    returncode=1,
                    stdout="FAILED tests/test_demo.py::test_example - AssertionError\n",
                    stderr="",
                )
            ],
        )

        info = extract_failure_info(gate)

        self.assertTrue(info.comparable)
        self.assertEqual(info.failure_ids, ["tests/test_demo.py::test_example"])

    def test_verbose_pytest_failure_line_is_comparable(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m pytest -x -vv tests",
                    ok=False,
                    returncode=1,
                    stdout="tests/test_demo.py::test_example FAILED                         [100%]\n",
                    stderr="",
                )
            ],
        )

        info = extract_failure_info(gate)

        self.assertTrue(info.comparable)
        self.assertEqual(info.failure_ids, ["tests/test_demo.py::test_example"])

    def test_verbose_pytest_passed_line_is_ignored(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m pytest -x -vv tests",
                    ok=False,
                    returncode=1,
                    stdout=(
                        "tests/test_demo.py::test_ok PASSED\n"
                        "tests/test_demo.py::test_example FAILED                         [100%]\n"
                    ),
                    stderr="",
                )
            ],
        )

        info = extract_failure_info(gate)

        self.assertTrue(info.comparable)
        self.assertEqual(info.failure_ids, ["tests/test_demo.py::test_example"])

    def test_mixed_test_case_and_unparsed_failures_are_non_comparable_but_keep_known_ids(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m pytest -q tests",
                    ok=False,
                    returncode=1,
                    stdout="FAILED tests/test_demo.py::test_example - AssertionError\n",
                    stderr="",
                ),
                CommandResult(command="npm run lint", ok=False, returncode=1, stdout="boom", stderr=""),
            ],
        )

        info = extract_failure_info(gate)

        self.assertFalse(info.comparable)
        self.assertEqual(
            info.failure_ids,
            ["tests/test_demo.py::test_example", "cmd:npm run lint"],
        )

    def test_verification_step_derives_pytest_command(self) -> None:
        command = command_from_verification_step(
            VerificationStep(kind="test", runner="pytest", targets=["tests/test_demo.py"], args=["-x"])
        )

        self.assertEqual(command, "conda run -p ./.conda python -m pytest -q -x tests/test_demo.py")

    def test_verification_step_ignores_freeform_command_field(self) -> None:
        command = command_from_verification_step(
            VerificationStep(
                kind="test",
                runner="pytest",
                targets=["tests"],
                command="echo should-not-run",
            )
        )

        self.assertEqual(command, "conda run -p ./.conda python -m pytest -q tests")

    def test_verification_step_serializes_without_freeform_command(self) -> None:
        payload = VerificationStep(
            kind="test",
            runner="pytest",
            targets=["tests"],
            command="echo should-not-persist",
        ).to_dict()

        self.assertNotIn("command", payload)

    def test_identity_diagnostic_command_upgrades_pytest_verbosity(self) -> None:
        command = build_failure_identity_diagnostic_command(
            "conda run -p ./.conda python -m pytest -q tests"
        )

        self.assertEqual(
            command,
            "conda run -p ./.conda python -m pytest -vv -rA --tb=short -o console_output_style=classic tests",
        )

    def test_identity_diagnostic_command_removes_pytest_fail_fast(self) -> None:
        command = build_failure_identity_diagnostic_command(
            "conda run -p ./.conda python -m pytest -q -x tests"
        )

        self.assertEqual(
            command,
            "conda run -p ./.conda python -m pytest -vv -rA --tb=short -o console_output_style=classic tests",
        )

    def test_run_gate_plan_runs_parallel_group_and_preserves_config_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_gate_plan(
                ["python3 -c \"print('first')\""],
                [
                    GateParallelGroup(
                        name="checks",
                        commands=[
                            "python3 -c \"import time; time.sleep(0.2); print('second')\"",
                            "python3 -c \"print('third')\"",
                        ],
                    )
                ],
                Path(tmp),
                collect_all=True,
            )
            self.assertTrue(result.ok)
            self.assertEqual([item.stdout for item in result.commands], ["first", "second", "third"])

    def test_run_gate_plan_stops_after_failed_parallel_group_when_not_collecting_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_gate_plan(
                ["python3 -c \"print('before')\""],
                [
                    GateParallelGroup(
                        name="checks",
                        commands=[
                            "python3 -c \"import sys; sys.exit(4)\"",
                            "python3 -c \"print('peer')\"",
                        ],
                    ),
                    GateParallelGroup(
                        name="later",
                        commands=["python3 -c \"print('after')\""],
                    ),
                ],
                Path(tmp),
                collect_all=False,
            )
            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 3)
            self.assertNotIn("after", [item.stdout for item in result.commands])

    def test_run_gate_plan_collect_all_runs_later_groups_after_parallel_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_gate_plan(
                [],
                [
                    GateParallelGroup(
                        name="checks",
                        commands=[
                            "python3 -c \"import sys; sys.exit(2)\"",
                            "python3 -c \"print('peer')\"",
                        ],
                    ),
                    GateParallelGroup(
                        name="later",
                        commands=["python3 -c \"print('after')\""],
                    ),
                ],
                Path(tmp),
                collect_all=True,
            )
            self.assertFalse(result.ok)
            self.assertEqual([item.stdout for item in result.commands], ["", "peer", "after"])

    def test_extract_vitest_failure_ids(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="npm test",
                    ok=False,
                    returncode=1,
                    stdout=(
                        " FAIL  workbench/src/components/project-detail-workbench.test.tsx > "
                        "ProjectDetailWorkbench > 生成失败展示用户可理解原因和下一步动作\n"
                    ),
                    stderr="",
                )
            ],
        )

        ids = extract_failure_ids(gate)

        self.assertEqual(
            ids,
            [
                "workbench/src/components/project-detail-workbench.test.tsx > "
                "ProjectDetailWorkbench > 生成失败展示用户可理解原因和下一步动作"
            ],
        )


if __name__ == "__main__":
    unittest.main()
