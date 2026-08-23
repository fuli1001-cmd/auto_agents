from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gates import (
    build_failure_identity_diagnostic_command,
    classify_reported_infrastructure_failure,
    command_from_verification_step,
    extract_failure_ids,
    extract_failure_info,
    expand_pytest_directory_steps,
    expand_verification_directory_steps,
    expand_vitest_directory_steps,
    remap_expanded_proof_dependencies,
    remap_expanded_proof_ids,
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


class _RecordingGateExecutor:
    def __init__(
        self,
        durations: dict[str, float],
        *,
        failures: set[str] | None = None,
        slots: dict[str, int] | None = None,
        capacity: int = 2,
        estimates: dict[str, float] | None = None,
        exclusive: set[str] | None = None,
    ) -> None:
        self.durations = durations
        self.failures = failures or set()
        self.slots = slots or {}
        self._capacity = capacity
        self.estimates = estimates or {}
        self.exclusive_commands = exclusive or set()
        self.events: list[tuple[str, str, str, float]] = []
        self.running_slots = 0
        self.max_running_slots = 0
        self._lock = threading.Lock()

    def capacity(self) -> int:
        return self._capacity

    def required_slots(self, command: str) -> int:
        return self.slots.get(command, 1)

    def exclusive(self, command: str) -> bool:
        return command in self.exclusive_commands

    def estimated_duration(self, command: str) -> float | None:
        return self.estimates.get(command)

    def priority(self, command: str) -> tuple[object, ...]:
        estimate = self.estimated_duration(command)
        return (
            0 if estimate is not None else 1,
            -(estimate or 0.0),
        )

    def run(
        self,
        command: str,
        *,
        lane: str = "",
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event=None,
        progress=None,
    ) -> CommandResult:
        del timeout_seconds, adaptive_timeout_enabled, idle_timeout_seconds
        slots = self.required_slots(command)
        with self._lock:
            self.running_slots += slots
            self.max_running_slots = max(
                self.max_running_slots, self.running_slots
            )
            self.events.append(("start", command, lane, time.monotonic()))
        time.sleep(self.durations[command])
        with self._lock:
            self.events.append(("finish", command, lane, time.monotonic()))
            self.running_slots -= slots
        failed = command in self.failures
        return CommandResult(
            command=command,
            ok=not failed,
            returncode=1 if failed else 0,
            stdout=command,
            duration_seconds=self.durations[command],
        )


class _ConflictOnceGateExecutor(_RecordingGateExecutor):
    def __init__(self) -> None:
        super().__init__({"flaky-browser": 0.0}, capacity=1)
        self.calls = 0

    def run(self, command: str, **kwargs) -> CommandResult:
        self.calls += 1
        if self.calls == 1:
            return CommandResult(
                command=command,
                ok=False,
                returncode=1,
                stderr=(
                    "BrowserArtifactPublicationConflictError: "
                    "browser_artifact_publication_conflict: screenshot.png"
                ),
            )
        return CommandResult(command=command, ok=True, returncode=0, stdout="confirmed")


class GateTests(unittest.TestCase):
    def test_run_commands_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(["python3 -c \"print('ok')\""], Path(tmp))
            self.assertTrue(result.ok)
            self.assertEqual(len(result.commands), 1)
            self.assertEqual(result.commands[0].stdout, "ok")

    def test_browser_artifact_publication_conflict_gets_one_confirmation_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "attempt.txt"
            script = (
                "from pathlib import Path; import sys; "
                f"p=Path({str(marker)!r}); first=not p.exists(); "
                "p.write_text('seen', encoding='utf-8'); "
                "print('browser_artifact_publication_conflict: screenshot.png' "
                "if first else 'confirmed'); sys.exit(1 if first else 0)"
            )
            result = run_commands(
                [f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"],
                Path(tmp),
            )

        self.assertTrue(result.ok)
        self.assertEqual(
            result.commands[0].process_snapshot[
                "browser_artifact_publication_confirmation"
            ],
            {
                "attempts": 2,
                "first_returncode": 1,
                "confirmed_ok": True,
                "confirmed_returncode": 0,
            },
        )

    def test_persistent_browser_artifact_conflict_is_retried_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "attempts.txt"
            script = (
                "from pathlib import Path; import sys; "
                f"p=Path({str(marker)!r}); "
                "count=int(p.read_text() or '0') if p.exists() else 0; "
                "p.write_text(str(count + 1), encoding='utf-8'); "
                "print('BrowserArtifactPublicationConflictError'); sys.exit(1)"
            )
            result = run_commands(
                [f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"],
                Path(tmp),
            )
            attempts = marker.read_text(encoding="utf-8")

        self.assertFalse(result.ok)
        self.assertEqual(attempts, "2")

    def test_gate_executor_conflict_gets_confirmation_retry(self) -> None:
        executor = _ConflictOnceGateExecutor()

        result = run_gate_plan(
            ["flaky-browser"],
            [],
            Path("/tmp"),
            collect_all=False,
            parallel_workers=1,
            gate_executor=executor,
        )

        self.assertTrue(result.ok)
        self.assertEqual(executor.calls, 2)
        self.assertEqual(result.commands[0].stdout, "confirmed")

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
        self.assertEqual(
            result.process_snapshot["reported_infrastructure_marker"]["source"],
            "builtin",
        )

    def test_marker_literal_in_pytest_source_is_not_infrastructure_failure(self) -> None:
        result = CommandResult(
            command="python -m pytest -q tests/test_audit.py",
            ok=False,
            returncode=1,
            stdout=(
                "________________ test_audit __________________\n"
                "    def test_audit():\n"
                '        assert "browser_verification_infrastructure_failed" in source\n'
                "E       AssertionError: missing current-run evidence\n"
                "FAILED tests/test_audit.py::test_audit - AssertionError\n"
            ),
        )

        classify_reported_infrastructure_failure(result)
        info = extract_failure_info(GateResult(ok=False, commands=[result]))

        self.assertFalse(result.infrastructure_error)
        self.assertTrue(info.comparable)
        self.assertEqual(info.failure_ids, ["tests/test_audit.py::test_audit"])

    def test_marker_literal_in_file_not_found_traceback_is_not_infrastructure(self) -> None:
        result = CommandResult(
            command="python -m pytest -q tests/test_audit.py",
            ok=False,
            returncode=1,
            stderr=(
                'marker = "BrowserVerificationInfrastructureError"\n'
                "E   FileNotFoundError: current browser evidence receipt is missing\n"
                "FAILED tests/test_audit.py::test_current_receipt - FileNotFoundError\n"
            ),
        )

        classify_reported_infrastructure_failure(result)

        self.assertFalse(result.infrastructure_error)

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

    def test_verification_step_preserves_multiword_argument_boundary(self) -> None:
        command = command_from_verification_step(
            VerificationStep(
                kind="test",
                runner="pytest",
                targets=["tests"],
                args=["-m", "not storage_real_smoke and not real_provider_smoke"],
            )
        )

        self.assertEqual(
            shlex.split(command),
            [
                "conda",
                "run",
                "-p",
                "./.conda",
                "python",
                "-m",
                "pytest",
                "-q",
                "-m",
                "not storage_real_smoke and not real_provider_smoke",
                "tests",
            ],
        )

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

    def test_expand_pytest_directory_steps_honors_ignore_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_fast.py").write_text("", encoding="utf-8")
            (tests / "test_serial.py").write_text("", encoding="utf-8")

            steps = expand_pytest_directory_steps(
                [
                    VerificationStep(
                        kind="test",
                        runner="pytest",
                        targets=["tests"],
                        args=[
                            "--ignore=tests/test_serial.py",
                            "--maxfail=1",
                        ],
                    )
                ],
                root,
            )

            self.assertEqual([step.targets for step in steps], [["tests/test_fast.py"]])
            self.assertEqual(steps[0].args, ["--maxfail=1"])

    def test_expand_pytest_directory_steps_bounds_and_balances_process_fanout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            for index in range(10):
                (tests / f"test_{index}.py").write_text(
                    "x" * (index + 1),
                    encoding="utf-8",
                )

            steps = expand_pytest_directory_steps(
                [
                    VerificationStep(
                        kind="test",
                        runner="pytest",
                        targets=["tests"],
                        max_batches=2,
                    )
                ],
                root,
                max_batches_per_step=8,
            )

            self.assertEqual(len(steps), 2)
            self.assertEqual(
                sorted(target for step in steps for target in step.targets),
                [f"tests/test_{index}.py" for index in range(10)],
            )
            self.assertTrue(all(len(step.targets) > 1 for step in steps))

    def test_max_batches_one_preserves_runner_directory_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_fast.py").write_text("", encoding="utf-8")

            pytest_step = VerificationStep(
                kind="test",
                runner="pytest",
                targets=["tests"],
                args=["--ignore=tests/test_generated.py"],
                max_batches=1,
            )
            vitest_step = VerificationStep(
                kind="test",
                runner="vitest",
                targets=["src"],
                args=["--exclude=src/e2e/**"],
                max_batches=1,
            )

            self.assertEqual(
                expand_pytest_directory_steps([pytest_step], root),
                [pytest_step],
            )
            self.assertEqual(
                expand_vitest_directory_steps([vitest_step], root),
                [vitest_step],
            )

    def test_policy_v3_stable_shards_override_legacy_single_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            for index in range(8):
                (tests / f"test_{index}.py").write_text("", encoding="utf-8")
            step = VerificationStep(
                kind="test",
                runner="pytest",
                targets=["tests"],
                max_batches=1,
                result_cache_scope="auto",
            )

            first = expand_pytest_directory_steps(
                [step], root, max_batches_per_step=4, stable_shards=True
            )
            second = expand_pytest_directory_steps(
                [step], root, max_batches_per_step=4, stable_shards=True
            )

            self.assertGreater(len(first), 1)
            self.assertEqual(
                [item.targets for item in first],
                [item.targets for item in second],
            )
            self.assertEqual(
                sorted(target for item in first for target in item.targets),
                [f"tests/test_{index}.py" for index in range(8)],
            )

    def test_artifact_producing_steps_remain_single_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            for index in range(4):
                (tests / f"test_{index}.py").write_text("", encoding="utf-8")
            pytest_step = VerificationStep(
                proof_id="evidence.pytest",
                kind="test",
                runner="pytest",
                targets=["tests"],
                artifact_globs=[".tmp-tests/evidence/pytest-*.json"],
                result_cache_scope="auto",
            )
            vitest_step = VerificationStep(
                proof_id="evidence.vitest",
                kind="test",
                runner="vitest",
                targets=["tests/test_0.py", "tests/test_1.py"],
                artifact_globs=[".tmp-tests/evidence/vitest-*.json"],
                result_cache_scope="auto",
            )

            self.assertEqual(
                expand_pytest_directory_steps(
                    [pytest_step], root, max_batches_per_step=4, stable_shards=True
                ),
                [pytest_step],
            )
            self.assertEqual(
                expand_vitest_directory_steps(
                    [vitest_step], root, max_batches_per_step=4, stable_shards=True
                ),
                [vitest_step],
            )

    def test_expanded_proof_references_follow_every_stable_shard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema_tests = root / "tests" / "schema"
            schema_tests.mkdir(parents=True)
            for index in range(8):
                (schema_tests / f"test_{index}.py").write_text("", encoding="utf-8")
            source = [
                VerificationStep(
                    proof_id="affected.schema",
                    runner="pytest",
                    targets=["tests/schema"],
                    levels=["affected"],
                    result_cache_scope="auto",
                ),
                VerificationStep(
                    proof_id="affected.api",
                    runner="pytest",
                    targets=["tests/test_api.py::test_contract"],
                    levels=["affected"],
                    depends_on_proofs=["affected.schema"],
                    result_cache_scope="auto",
                ),
            ]

            expanded = expand_verification_directory_steps(
                source,
                root,
                max_batches_per_step=4,
                stable_shards=True,
            )
            remapped = remap_expanded_proof_dependencies(source, expanded)
            schema_proofs = [
                step.proof_id
                for step in remapped
                if step.proof_id.startswith("affected.schema.shard-")
            ]
            api = next(step for step in remapped if step.proof_id == "affected.api")

            self.assertGreater(len(schema_proofs), 1)
            self.assertEqual(api.depends_on_proofs, schema_proofs)
            fallback, unknown = remap_expanded_proof_ids(
                source,
                remapped,
                ["affected.schema", "legacy.proof"],
                preserve_unknown=False,
            )
            self.assertEqual(fallback, schema_proofs)
            self.assertEqual(unknown, ["legacy.proof"])

    def test_expand_vitest_directory_steps_splits_files_and_honors_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "src"
            tests.mkdir()
            (tests / "alpha.test.ts").write_text("", encoding="utf-8")
            (tests / "beta.spec.tsx").write_text("", encoding="utf-8")
            (tests / "helper.ts").write_text("", encoding="utf-8")

            steps = expand_vitest_directory_steps(
                [
                    VerificationStep(
                        kind="test",
                        runner="vitest",
                        targets=["src"],
                        args=["--exclude", "src/beta.spec.tsx", "--reporter=dot"],
                        parallel_safe=True,
                        result_cache_scope="candidate",
                    )
                ],
                root,
            )

            self.assertEqual([step.targets for step in steps], [["src/alpha.test.ts"]])
            self.assertEqual(steps[0].args, ["--reporter=dot"])
            self.assertTrue(steps[0].parallel_safe)
            self.assertEqual(steps[0].result_cache_scope, "candidate")

    def test_expand_vitest_directory_steps_uses_config_aware_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_root = root / "workbench"
            tests = package_root / "src"
            binary = package_root / "node_modules" / ".bin" / "vitest"
            tests.mkdir(parents=True)
            binary.parent.mkdir(parents=True)
            binary.write_text("", encoding="utf-8")
            (package_root / "package.json").write_text(
                '{"devDependencies":{"vitest":"3.1.1"}}\n',
                encoding="utf-8",
            )
            (package_root / "vitest.config.ts").write_text(
                'export default { test: { exclude: ["src/retired.test.ts"] } };\n',
                encoding="utf-8",
            )
            included = tests / "included.test.ts"
            retired = tests / "retired.test.ts"
            included.write_text("", encoding="utf-8")
            retired.write_text("", encoding="utf-8")
            listed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps([{"file": str(included)}]),
                stderr="",
            )
            step = VerificationStep(
                kind="test",
                runner="vitest",
                targets=["workbench/src"],
                max_batches=1,
                result_cache_scope="auto",
            )

            with patch("auto_agents.gates.subprocess.run", return_value=listed) as run:
                expanded = expand_vitest_directory_steps(
                    [step],
                    root,
                    stable_shards=True,
                )

            self.assertEqual(
                [target for item in expanded for target in item.targets],
                ["workbench/src/included.test.ts"],
            )
            command = run.call_args.args[0]
            self.assertEqual(
                command[1:],
                ["list", "src", "--filesOnly", "--json"],
            )
            self.assertEqual(run.call_args.kwargs["cwd"], package_root)

    def test_expand_vitest_directory_preserves_target_without_local_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_root = root / "workbench"
            tests = package_root / "src"
            tests.mkdir(parents=True)
            (package_root / "vitest.config.ts").write_text(
                'export default { test: { exclude: ["src/retired.test.ts"] } };\n',
                encoding="utf-8",
            )
            (tests / "included.test.ts").write_text("", encoding="utf-8")
            (tests / "retired.test.ts").write_text("", encoding="utf-8")
            step = VerificationStep(
                kind="test",
                runner="vitest",
                targets=["workbench/src"],
                max_batches=1,
                result_cache_scope="auto",
            )

            expanded = expand_vitest_directory_steps(
                [step],
                root,
                stable_shards=True,
            )

            self.assertEqual(expanded, [step])

    def test_resolved_plan_preserves_v2_serial_and_result_cache_metadata(self) -> None:
        step = VerificationStep(
            kind="test",
            runner="pytest",
            targets=["tests/test_demo.py"],
            parallel_safe=False,
            serial_reason="shared_mutable_state",
            cache_scope="source",
            result_cache_scope="off",
        )

        plan = resolve_gate_plan_from_verification_steps([step], phase="final")
        command = plan.commands[0]

        self.assertEqual(plan.metadata[command].serial_reason, "shared_mutable_state")
        self.assertEqual(plan.result_cache_scopes[command], "off")

    def test_resolved_plan_preserves_exclusive_resource_class(self) -> None:
        step = VerificationStep(
            kind="test",
            runner="pytest",
            targets=["tests/test_timing_sensitive.py"],
            parallel_safe=False,
            serial_reason="shared_mutable_state",
            resource_class="exclusive",
        )

        plan = resolve_gate_plan_from_verification_steps([step], phase="final")
        command = plan.commands[0]

        self.assertEqual(plan.metadata[command].resource_class, "exclusive")

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
            max_batches=1,
            cpu_slots=3,
            memory_mb=4096,
            memory_reserve_mb=1024,
            memory_guard="required",
            dynamic_ports=["api"],
        ).to_dict()

        self.assertEqual(payload["cadence"], "final_only")
        self.assertEqual(payload["cache_scope"], "source")
        self.assertEqual(payload["max_batches"], 1)
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
        self.assertEqual(restored.max_batches, 1)

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

    def test_isolated_gate_plan_overlaps_serial_lane_and_parallel_group(self) -> None:
        executor = _RecordingGateExecutor(
            {
                "serial-one": 0.12,
                "serial-two": 0.12,
                "parallel-one": 0.18,
            },
            capacity=2,
        )

        result = run_gate_plan(
            ["serial-one", "serial-two"],
            [GateParallelGroup(name="checks", commands=["parallel-one"])],
            Path("/tmp"),
            collect_all=True,
            parallel_workers=2,
            gate_executor=executor,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [item.command for item in result.commands],
            ["serial-one", "serial-two", "parallel-one"],
        )
        event_times = {
            (event, command): timestamp
            for event, command, _lane, timestamp in executor.events
        }
        self.assertLess(
            event_times[("start", "parallel-one")],
            event_times[("finish", "serial-one")],
        )
        self.assertLess(
            event_times[("finish", "parallel-one")],
            event_times[("finish", "serial-two")],
        )

    def test_isolated_gate_plan_runs_exclusive_command_without_overlap(self) -> None:
        executor = _RecordingGateExecutor(
            {
                "serial-exclusive": 0.04,
                "parallel-one": 0.04,
                "parallel-two": 0.04,
            },
            capacity=2,
            exclusive={"serial-exclusive"},
        )

        result = run_gate_plan(
            ["serial-exclusive"],
            [
                GateParallelGroup(
                    name="checks",
                    commands=["parallel-one", "parallel-two"],
                )
            ],
            Path("/tmp"),
            collect_all=True,
            parallel_workers=2,
            gate_executor=executor,
        )

        self.assertTrue(result.ok)
        event_times = {
            (event, command): timestamp
            for event, command, _lane, timestamp in executor.events
        }
        self.assertGreaterEqual(
            event_times[("start", "parallel-one")],
            event_times[("finish", "serial-exclusive")],
        )
        self.assertEqual(executor.max_running_slots, 2)

    def test_isolated_gate_plan_preserves_group_barriers_during_overlap(self) -> None:
        executor = _RecordingGateExecutor(
            {
                "serial": 0.3,
                "group-one": 0.08,
                "group-two": 0.08,
            },
            capacity=2,
        )

        result = run_gate_plan(
            ["serial"],
            [
                GateParallelGroup(name="one", commands=["group-one"]),
                GateParallelGroup(name="two", commands=["group-two"]),
            ],
            Path("/tmp"),
            collect_all=True,
            parallel_workers=2,
            gate_executor=executor,
        )

        self.assertTrue(result.ok)
        event_times = {
            (event, command): timestamp
            for event, command, _lane, timestamp in executor.events
        }
        self.assertGreaterEqual(
            event_times[("start", "group-two")],
            event_times[("finish", "group-one")],
        )
        self.assertLess(
            event_times[("start", "group-two")],
            event_times[("finish", "serial")],
        )

    def test_isolated_gate_plan_backfills_behind_slot_blocked_group_frontier(
        self,
    ) -> None:
        executor = _RecordingGateExecutor(
            {
                "group-one-light": 0.12,
                "group-one-heavy": 0.04,
                "group-two-light": 0.04,
            },
            slots={"group-one-heavy": 2},
            capacity=2,
            estimates={
                "group-one-light": 10.0,
                "group-one-heavy": 1.0,
                "group-two-light": 1.0,
            },
        )

        result = run_gate_plan(
            [],
            [
                GateParallelGroup(
                    name="one",
                    commands=["group-one-light", "group-one-heavy"],
                ),
                GateParallelGroup(
                    name="two",
                    commands=["group-two-light"],
                ),
            ],
            Path("/tmp"),
            collect_all=True,
            parallel_workers=2,
            gate_executor=executor,
        )

        self.assertTrue(result.ok)
        event_times = {
            (event, command): timestamp
            for event, command, _lane, timestamp in executor.events
        }
        self.assertLess(
            event_times[("start", "group-two-light")],
            event_times[("finish", "group-one-light")],
        )
        self.assertLessEqual(executor.max_running_slots, 2)

    def test_isolated_gate_plan_stops_dispatch_and_drains_inflight(self) -> None:
        executor = _RecordingGateExecutor(
            {
                "fails": 0.04,
                "serial-later": 0.01,
                "inflight": 0.16,
                "parallel-later": 0.01,
            },
            failures={"fails"},
            capacity=2,
        )
        started = time.monotonic()

        result = run_gate_plan(
            ["fails", "serial-later"],
            [
                GateParallelGroup(
                    name="checks",
                    commands=["inflight", "parallel-later"],
                )
            ],
            Path("/tmp"),
            collect_all=False,
            parallel_workers=2,
            gate_executor=executor,
        )

        self.assertFalse(result.ok)
        self.assertGreaterEqual(time.monotonic() - started, 0.14)
        self.assertEqual(
            [item.command for item in result.commands],
            ["fails", "inflight"],
        )
        started_commands = {
            command
            for event, command, _lane, _timestamp in executor.events
            if event == "start"
        }
        self.assertNotIn("serial-later", started_commands)
        self.assertNotIn("parallel-later", started_commands)

    def test_isolated_gate_plan_uses_lpt_and_weighted_capacity(self) -> None:
        executor = _RecordingGateExecutor(
            {
                "short": 0.03,
                "long": 0.08,
                "normal": 0.03,
            },
            slots={"long": 2},
            capacity=3,
            estimates={"short": 1.0, "long": 10.0, "normal": 2.0},
        )

        result = run_gate_plan(
            [],
            [
                GateParallelGroup(
                    name="checks",
                    commands=["short", "long", "normal"],
                )
            ],
            Path("/tmp"),
            collect_all=True,
            parallel_workers=3,
            gate_executor=executor,
        )

        self.assertTrue(result.ok)
        starts = [
            command
            for event, command, _lane, _timestamp in executor.events
            if event == "start"
        ]
        self.assertEqual(set(starts[:2]), {"long", "normal"})
        self.assertEqual(starts[-1], "short")
        self.assertLessEqual(executor.max_running_slots, 3)

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
