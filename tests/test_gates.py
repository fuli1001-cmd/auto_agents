import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gates import (
    build_failure_identity_diagnostic_command,
    classify_reported_infrastructure_failure,
    command_from_verification_step,
    extract_failure_ids,
    extract_failure_info,
    expand_pytest_directory_steps,
    resolve_gate_plan_from_verification_steps,
    run_commands,
    run_gate_plan,
)
from auto_agents.models import (
    CommandResult,
    GateParallelGroup,
    GateResult,
    InfrastructureFailureMarker,
    VerificationStep,
)


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

    def test_run_commands_enforces_hard_timeout_and_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            result = run_commands(
                [
                    "python3 -c \"import time; "
                    "print('started', flush=True); time.sleep(30)\""
                ],
                Path(tmp),
                command_timeout_seconds=0.2,
            )

            self.assertFalse(result.ok)
            self.assertLess(time.monotonic() - started, 3)
            command = result.commands[0]
            self.assertEqual(command.termination_reason, "timeout")
            self.assertEqual(command.timeout_seconds, 0.2)
            self.assertEqual(command.stdout, "started")
            self.assertIn("timed out after 0.2s", result.summary)

    def test_collect_all_stops_after_timeout(self) -> None:
        from auto_agents.gates import run_commands_collect_all

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "must-not-run"
            result = run_commands_collect_all(
                [
                    "python3 -c \"import time; time.sleep(30)\"",
                    f"python3 -c \"from pathlib import Path; Path({str(marker)!r}).touch()\"",
                ],
                Path(tmp),
                command_timeout_seconds=0.2,
            )

            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 1)
            self.assertFalse(marker.exists())

    def test_adaptive_timeout_detects_an_idle_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(
                ["python3 -c \"import time; time.sleep(30)\""],
                Path(tmp),
                command_timeout_seconds=2,
                adaptive_timeout_enabled=True,
                command_idle_timeout_seconds=0.2,
            )

            command = result.commands[0]
            self.assertEqual(command.termination_reason, "stalled")
            self.assertIn("stalled without observable activity", result.summary)
            self.assertTrue(command.process_snapshot)

    def test_cpu_activity_renews_lease_but_absolute_ceiling_still_applies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(
                ["python3 -c \"while True: pass\""],
                Path(tmp),
                command_timeout_seconds=0.5,
                adaptive_timeout_enabled=True,
                command_idle_timeout_seconds=0.2,
            )

            command = result.commands[0]
            self.assertEqual(command.termination_reason, "timeout")
            self.assertEqual(command.activity_kind, "cpu")

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

    def test_standard_reported_infrastructure_failure_is_non_comparable(self) -> None:
        result = CommandResult(
            command="npm test",
            ok=False,
            returncode=1,
            stdout=(
                "FAIL src/e2e/create-modal.test.ts > contract\n"
                "Error: AUTO_AGENTS_INFRA_FAILURE id=browser_launch_failed\n"
            ),
        )

        classify_reported_infrastructure_failure(result)
        info = extract_failure_info(GateResult(ok=False, commands=[result]))

        self.assertTrue(result.infrastructure_error)
        self.assertEqual(result.infrastructure_failure_id, "browser_launch_failed")
        self.assertFalse(info.comparable)
        self.assertEqual(
            info.failure_ids,
            ["infra:browser_launch_failed:npm test"],
        )

    def test_builtin_browser_infrastructure_failure_beats_vitest_id(self) -> None:
        result = CommandResult(
            command="npm test",
            ok=False,
            returncode=1,
            stdout=(
                "FAIL src/e2e/create-modal-prototype-fidelity.test.ts > "
                "create_modal_close_and_submit_contract\n"
                "→ browser_verification_infrastructure_failed: launch 3/3 failed\n"
                "BrowserVerificationInfrastructureError: SIGTRAP\n"
            ),
        )

        classify_reported_infrastructure_failure(result)
        info = extract_failure_info(GateResult(ok=False, commands=[result]))

        self.assertEqual(
            result.infrastructure_failure_id,
            "browser_verification_infrastructure_failed",
        )
        self.assertFalse(info.comparable)
        self.assertNotIn("create_modal_close_and_submit_contract", info.failure_ids[0])

    def test_configured_reported_infrastructure_marker(self) -> None:
        result = CommandResult(
            command="pytest",
            ok=False,
            returncode=1,
            stderr="RuntimeError: ephemeral display server refused the session",
        )

        classify_reported_infrastructure_failure(
            result,
            [
                InfrastructureFailureMarker(
                    marker_id="display_server_failed",
                    contains="ephemeral display server refused",
                )
            ],
        )

        self.assertEqual(
            result.infrastructure_failure_id,
            "display_server_failed",
        )

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

    def test_verification_step_prefers_project_conda_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_python = root / ".conda" / "bin" / "python"
            local_python.parent.mkdir(parents=True)
            local_python.write_text("", encoding="utf-8")

            command = command_from_verification_step(
                VerificationStep(kind="test", runner="pytest", targets=["tests/test_demo.py"], args=["-x"]),
                root,
            )

            self.assertEqual(command, "./.conda/bin/python -m pytest -q -x tests/test_demo.py")

    def test_expand_pytest_directory_steps_splits_existing_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            nested_dir = tests_dir / "nested"
            nested_dir.mkdir(parents=True)
            (tests_dir / "test_alpha.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
            (nested_dir / "beta_test.py").write_text("def test_beta():\n    assert True\n", encoding="utf-8")

            steps = expand_pytest_directory_steps(
                [VerificationStep(kind="test", runner="pytest", targets=["tests"])],
                root,
            )

            self.assertEqual(
                [step.targets for step in steps],
                [["tests/nested/beta_test.py"], ["tests/test_alpha.py"]],
            )

    def test_expand_pytest_directory_steps_keeps_missing_targets_for_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            steps = expand_pytest_directory_steps(
                [VerificationStep(kind="test", runner="pytest", targets=["tests"])],
                root,
            )

            self.assertEqual([step.targets for step in steps], [["tests"]])

    def test_expand_pytest_directory_steps_preserves_dynamic_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_alpha.py").write_text("", encoding="utf-8")

            steps = expand_pytest_directory_steps(
                [
                    VerificationStep(
                        kind="test",
                        runner="pytest",
                        targets=["tests"],
                        cpu_slots=2,
                        memory_mb=1024,
                        memory_reserve_mb=256,
                        memory_guard="advisory",
                        dynamic_ports=["api"],
                    )
                ],
                root,
            )

            self.assertEqual(steps[0].dynamic_ports, ["api"])
            self.assertEqual(steps[0].cpu_slots, 2)
            self.assertEqual(steps[0].memory_mb, 1024)
            self.assertEqual(steps[0].memory_reserve_mb, 256)
            self.assertEqual(steps[0].memory_guard, "advisory")

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

    def test_verification_step_serializes_cadence_and_cache_scope(self) -> None:
        payload = VerificationStep(
            runner="pytest",
            targets=["tests/test_demo.py"],
            cadence="final_only",
            cache_scope="source",
            cpu_slots=3,
            memory_mb=4096,
            memory_reserve_mb=1024,
            memory_guard="required",
            dynamic_ports=["api"],
        ).to_dict()

        self.assertEqual(payload["cadence"], "final_only")
        self.assertEqual(payload["cache_scope"], "source")
        self.assertEqual(payload["cpu_slots"], 3)
        self.assertEqual(payload["memory_mb"], 4096)
        self.assertEqual(payload["memory_reserve_mb"], 1024)
        self.assertEqual(payload["memory_guard"], "required")
        self.assertEqual(payload["dynamic_ports"], ["api"])
        restored = VerificationStep.from_dict(payload)
        self.assertEqual(restored.cpu_slots, 3)
        self.assertEqual(restored.memory_mb, 4096)
        self.assertEqual(restored.memory_reserve_mb, 1024)
        self.assertEqual(restored.memory_guard, "required")

    def test_gate_plan_filters_final_only_and_deduplicates_conservatively(self) -> None:
        duplicate_target = ["tests/test_demo.py"]
        steps = [
            VerificationStep(
                runner="pytest",
                targets=duplicate_target,
                parallel_safe=True,
                cache_scope="source",
                cpu_slots=2,
                memory_mb=4096,
                memory_reserve_mb=512,
                memory_guard="advisory",
                dynamic_ports=["api"],
            ),
            VerificationStep(
                runner="pytest",
                targets=duplicate_target,
                parallel_safe=False,
                cache_scope="run_context",
                cpu_slots=3,
                memory_mb=2048,
                memory_reserve_mb=1024,
                memory_guard="required",
                dynamic_ports=["frontend", "api"],
            ),
            VerificationStep(
                runner="pytest",
                targets=["tests/test_full.py"],
                cadence="final_only",
                cache_scope="source",
            ),
        ]

        implement = resolve_gate_plan_from_verification_steps(
            steps, Path("/tmp/demo"), phase="implement"
        )
        final = resolve_gate_plan_from_verification_steps(
            steps, Path("/tmp/demo"), phase="final"
        )

        self.assertEqual(implement.raw_command_count, 2)
        self.assertEqual(implement.unique_command_count, 1)
        self.assertEqual(implement.duplicates_removed, 1)
        self.assertEqual(len(implement.commands), 1)
        self.assertEqual(implement.parallel_groups, [])
        self.assertEqual(
            implement.cache_scopes[implement.commands[0]], "run_context"
        )
        self.assertEqual(
            implement.metadata[implement.commands[0]].dynamic_ports,
            ["api", "frontend"],
        )
        self.assertEqual(
            implement.metadata[implement.commands[0]].cpu_slots, 3
        )
        self.assertEqual(
            implement.metadata[implement.commands[0]].memory_mb, 4096
        )
        self.assertEqual(
            implement.metadata[implement.commands[0]].memory_reserve_mb, 1024
        )
        self.assertEqual(
            implement.metadata[implement.commands[0]].memory_guard, "required"
        )
        self.assertEqual(final.unique_command_count, 2)

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

    def test_parallel_timeout_cancels_peers_and_skips_later_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "later-group-ran"
            result = run_gate_plan(
                [],
                [
                    GateParallelGroup(
                        name="hanging",
                        commands=[
                            "python3 -c \"import time; time.sleep(30)\"",
                            "python3 -c \"import time; time.sleep(30)\"",
                        ],
                    ),
                    GateParallelGroup(
                        name="later",
                        commands=[
                            f"python3 -c \"from pathlib import Path; Path({str(marker)!r}).touch()\""
                        ],
                    ),
                ],
                Path(tmp),
                collect_all=True,
                command_timeout_seconds=0.2,
            )

            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 2)
            self.assertTrue(
                any(item.termination_reason == "timeout" for item in result.commands)
            )
            self.assertFalse(marker.exists())

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
