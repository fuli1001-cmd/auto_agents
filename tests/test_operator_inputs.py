from __future__ import annotations

import json
import io
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import load_run_state, save_run_state
from auto_agents.cli import build_parser, main
from auto_agents.io_utils import write_text
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.models import AgentResult, GateConfig, TaskSpec
from auto_agents.operator_inputs import (
    OperatorInputStore,
    UserInputRequest,
    prompt_for_request,
)
from auto_agents.orchestrator import Orchestrator


def _request(**updates):
    payload = {
        "key": "youtube.authorization",
        "kind": "attestation",
        "question": (
            "你是否确认：你或项目团队拥有该视频，或已获得权利人的明确授权，"
            "可以下载、处理、生成衍生内容，并长期用于自动化测试？"
        ),
        "purpose": "证明真实视频测试已获得操作者授权。",
        "why_required": "真实系统边界不能使用未经确认的第三方素材。",
        "how_to_obtain": ["只有确实拥有权利或明确许可时选择 y。"],
        "recommended_answer": "不确定时选择 n。",
        "default": False,
        "persistence": "project",
        "sensitivity": "private",
        "subject_fingerprint": "video-abc",
        "question_version": 1,
        "validation": {
            "claims": ["download", "processing", "derivative_creation", "automated_testing"],
            "subject": {"source_url_input_key": "youtube.source_url"},
            "stable_test_use": True,
        },
        "bindings": [
            {
                "env": "SDGLOBAL_TEST_YOUTUBE_AUTHORIZATION_EVIDENCE",
                "projection": "artifact_path",
            }
        ],
    }
    payload.update(updates)
    return UserInputRequest.from_dict(payload)


class OperatorInputStoreTests(unittest.TestCase):
    def test_attestation_is_one_safe_default_no_question(self):
        request = _request()
        self.assertEqual(request.kind, "attestation")
        self.assertFalse(request.default)
        self.assertIn("不确定时选择 n", request.render())

    def test_project_values_and_secrets_are_persisted_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            store = OperatorInputStore(project)
            url_request = _request(
                key="youtube.source_url",
                kind="url",
                question="请输入公开视频 URL",
                validation={"https_only": True},
            )
            secret_request = _request(
                key="provider.api_token",
                kind="secret",
                question="请输入 Provider token",
                sensitivity="secret",
                validation={},
            )

            store.save_answer(url_request, "https://www.youtube.com/watch?v=abcdefghijk")
            store.save_answer(secret_request, "top-secret-value")

            inputs_text = store.inputs_path.read_text(encoding="utf-8")
            secrets_text = store.secrets_path.read_text(encoding="utf-8")
            self.assertNotIn("top-secret-value", inputs_text)
            self.assertIn("top-secret-value", secrets_text)
            self.assertEqual(stat.S_IMODE(store.inputs_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.secrets_path.stat().st_mode), 0o600)
            environment, missing = store.environment(
                [
                    {
                        "input_key": "provider.api_token",
                        "env": "PROVIDER_API_TOKEN",
                        "projection": "value",
                    }
                ]
            )
            self.assertEqual(missing, [])
            self.assertEqual(environment["PROVIDER_API_TOKEN"], "top-secret-value")

    def test_attestation_resolves_subject_from_prior_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            store = OperatorInputStore(project)
            source = _request(
                key="youtube.source_url",
                kind="url",
                question="请输入公开视频 URL",
                validation={"https_only": True},
            )
            url = "https://www.youtube.com/watch?v=abcdefghijk"
            store.save_answer(source, url)
            record = store.save_answer(_request(), "y")
            artifact = json.loads(
                Path(str(record["artifact_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["subject"]["source_url"], url)
            self.assertEqual(artifact["source_url"], url)
            self.assertEqual(
                artifact["rights_basis"],
                "ownership_or_explicit_permission_attested",
            )

    def test_question_or_subject_change_invalidates_saved_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            store = OperatorInputStore(project)
            request = _request()
            store.save_answer(request, True)
            self.assertTrue(store.is_valid(request)[0])
            self.assertFalse(store.is_valid(_request(question_version=2))[0])
            self.assertFalse(
                store.is_valid(_request(subject_fingerprint="video-other"))[0]
            )

    def test_operator_answer_invalidates_preflight_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            task = orchestrator._load_tasks_from_plan()[0]
            task.required_inputs = [_request().to_dict()]
            before = orchestrator._evidence_preflight_fingerprint(task)
            orchestrator._operator_inputs.save_answer(_request(), True)
            after = orchestrator._evidence_preflight_fingerprint(task)
            self.assertNotEqual(before, after)

    def test_echo_mode_is_selectable(self):
        request = _request(
            key="provider.api_token",
            kind="secret",
            question="请输入 token",
            sensitivity="secret",
            validation={},
        )
        calls = []
        visible = prompt_for_request(
            request,
            echo_mode="visible",
            input_fn=lambda prompt: calls.append("visible") or "a",
            secret_input_fn=lambda prompt: calls.append("hidden") or "b",
        )
        hidden = prompt_for_request(
            request,
            echo_mode="hidden",
            input_fn=lambda prompt: calls.append("visible") or "a",
            secret_input_fn=lambda prompt: calls.append("hidden") or "b",
        )
        self.assertEqual((visible, hidden), ("a", "b"))
        self.assertEqual(calls, ["visible", "hidden"])

    def test_waiting_request_is_answered_and_task_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need input",
                acceptance=["Input exists"],
                status="pending",
            )
            state.tasks = [task]
            requests = orchestrator._normalize_input_requests(
                state, task, [_request().to_dict()]
            )
            self.assertEqual(task.status, "waiting_user")
            state.status = "waiting_user"
            save_run_state(project, state)
            orchestrator._persist_tasks(state.tasks)

            result = orchestrator.answer_input_request(
                request_id=requests[0].request_id,
                value=True,
            )
            resumed = load_run_state(project)
            self.assertTrue(result["resume"])
            self.assertEqual(resumed.status, "pending")
            self.assertEqual(resumed.tasks[0].status, "pending")
            self.assertEqual(resumed.pending_input_requests, [])

    def test_interactive_input_answers_entire_batch_before_resuming(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            answers = iter(
                ["https://www.youtube.com/watch?v=abcdefghijk", "y"]
            )
            prompts = []

            def answer(prompt):
                prompts.append(prompt)
                return next(answers)

            orchestrator = Orchestrator(project, user_input_fn=answer)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need an authorized source",
                acceptance=["Authorized input exists"],
            )
            state.tasks = [task]
            source_request = _request(
                key="youtube.source_url",
                kind="url",
                question="请输入公开视频 URL",
                default="",
                validation={"https_only": True},
                bindings=[],
                subject_fingerprint="video-url",
            )
            orchestrator._normalize_input_requests(
                state,
                task,
                [source_request.to_dict(), _request().to_dict()],
            )
            state.status = "waiting_user"
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project, state)

            resumed = orchestrator._process_pending_user_input(
                state, state.tasks
            )

            self.assertTrue(resumed)
            self.assertEqual(len(prompts), 2)
            self.assertIn("公开视频 URL", prompts[0])
            self.assertIn("明确授权", prompts[1])
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.pending_input_requests, [])
            self.assertEqual(state.tasks[0].status, "pending")

    def test_input_batch_validation_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need an authorized source and runtime.",
                acceptance=["The real proof passes."],
            )
            malformed_install = _request(
                key="runtime.install",
                kind="install_approval",
                question="Install yt-dlp?",
                purpose="Run the real proof.",
                why_required="yt-dlp is required.",
                validation={
                    "runtime_manifest": [
                        {"tool_id": "yt-dlp", "version": "2026.07.04"}
                    ]
                },
                bindings=[],
            )

            with self.assertRaisesRegex(ValueError, "pinned source_url"):
                orchestrator._normalize_input_requests(
                    state,
                    task,
                    [_request().to_dict(), malformed_install.to_dict()],
                )

            self.assertEqual(state.pending_input_requests, [])
            self.assertEqual(state.active_input_request_id, "")
            self.assertEqual(task.operator_input_bindings, [])
            self.assertEqual(task.required_inputs, [])
            self.assertEqual(task.status, "pending")

    def test_malformed_preflight_input_batch_retries_without_partial_state(self):
        valid_source = _request(
            key="youtube.source_url",
            kind="url",
            question="请输入获得授权的公开视频 URL",
            validation={"https_only": True},
        )
        malformed_install = _request(
            key="runtime.install",
            kind="install_approval",
            question="Install yt-dlp?",
            purpose="Run the real proof.",
            why_required="yt-dlp is required.",
            validation={
                "runtime_manifest": [
                    {"tool_id": "yt-dlp", "version": "2026.07.04"}
                ]
            },
            bindings=[],
        )

        class Adapter:
            def __init__(self):
                self.calls = 0

            def run(self, request):
                self.calls += 1
                inputs = (
                    [_request().to_dict(), malformed_install.to_dict()]
                    if self.calls == 1
                    else [valid_source.to_dict()]
                )
                payload = {
                    "decision": "WAIT_USER",
                    "target_stage": "",
                    "reason": "operator prerequisites are required",
                    "checklist": ["collect the authorized source"],
                    "required_inputs": inputs,
                    "required_mutations": [],
                }
                summary = "EVIDENCE_PREFLIGHT: " + json.dumps(
                    payload, ensure_ascii=False
                )
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary,
                )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            adapter = Adapter()
            orchestrator.adapter = adapter
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need an authorized source and runtime.",
                acceptance=["The real proof passes."],
            )
            state.tasks = [task]

            with mock.patch.object(
                orchestrator, "_task_needs_evidence_preflight", return_value=True
            ):
                result = orchestrator._ensure_evidence_preflight(state, task)

            self.assertEqual(adapter.calls, 2)
            self.assertEqual(result["decision"], "WAIT_USER")
            self.assertEqual(
                [item["key"] for item in state.pending_input_requests],
                ["youtube.source_url"],
            )
            self.assertEqual(task.status, "waiting_user")

    def test_exhausted_malformed_preflight_blocks_as_auto_agents_owned(self):
        malformed_install = _request(
            key="runtime.install",
            kind="install_approval",
            question="Install yt-dlp?",
            purpose="Run the real proof.",
            why_required="yt-dlp is required.",
            validation={
                "runtime_manifest": [
                    {"tool_id": "yt-dlp", "version": "2026.07.04"}
                ]
            },
            bindings=[],
        )

        class Adapter:
            def run(self, request):
                payload = {
                    "decision": "WAIT_USER",
                    "target_stage": "",
                    "reason": "operator prerequisites are required",
                    "checklist": ["collect prerequisites"],
                    "required_inputs": [malformed_install.to_dict()],
                    "required_mutations": [],
                }
                summary = "EVIDENCE_PREFLIGHT: " + json.dumps(payload)
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary,
                )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            orchestrator.adapter = Adapter()
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need a valid runtime request.",
                acceptance=["The proof passes."],
            )
            state.tasks = [task]

            with mock.patch.object(
                orchestrator, "_task_needs_evidence_preflight", return_value=True
            ):
                result = orchestrator._ensure_evidence_preflight(state, task)
            routed = orchestrator._route_evidence_preflight(
                state, [task], task, result
            )

            self.assertEqual(state.pending_input_requests, [])
            self.assertEqual(task.operator_input_bindings, [])
            self.assertEqual(routed.status, "blocked")
            self.assertEqual(routed.active_blocker["owner"], "auto_agents")
            self.assertEqual(
                routed.active_blocker["category"],
                "evidence_preflight_protocol_invalid",
            )

    def test_recovery_stop_routes_persisted_lineage_input_to_wait_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            owner = TaskSpec(
                task_id="task-owner",
                title="Owner",
                description="Own the real proof.",
                acceptance=["The proof passes."],
            )
            repair = TaskSpec(
                task_id="repair-owner-r1-1",
                title="Repair proof",
                description="Repair the owner's proof.",
                acceptance=["The proof passes."],
                status="in_progress",
                parent_task_id=owner.task_id,
                task_origin="evidence_repair",
            )
            request = _request(task_id=repair.task_id)
            state.tasks = [owner, repair]
            state.pending_input_requests = [request.to_dict()]

            stopped = orchestrator._block_for_recovery_stop(
                state,
                state.tasks,
                repair,
                owner,
                reason="authorized source is required",
                signature="same-failure",
                blocker_owner="user_input",
                prerequisite_keys=[request.key],
                evidence_refs=[f"pending-input:{request.request_id}"],
            )

            self.assertTrue(stopped)
            self.assertEqual(state.status, "waiting_user")
            self.assertEqual(state.active_blocker, {})
            self.assertEqual(repair.status, "waiting_user")
            self.assertEqual(repair.required_inputs[0]["key"], request.key)
            self.assertEqual(state.active_input_request_id, request.request_id)

    def test_answered_bound_input_rejects_provider_stop_and_requeues(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            request = _request(
                key="fixture.source",
                kind="url",
                question="Provide the authorized fixture URL",
                validation={"https_only": True},
                bindings=[
                    {
                        "env": "TEST_FIXTURE_URL",
                        "projection": "value",
                    }
                ],
                task_id="bounded-repair",
            )
            task = TaskSpec(
                task_id="bounded-repair",
                title="Repair the bounded failure",
                description="Address the latest implementation failure.",
                acceptance=["The boundary proof passes."],
                status="in_progress",
                task_origin="scope_split",
                operator_input_bindings=[
                    {
                        "input_key": request.key,
                        "env": "TEST_FIXTURE_URL",
                        "projection": "value",
                    }
                ],
                verify_history=[
                    {
                        "attempt": 1,
                        "decision": "fail",
                        "summary": "The worker reached a later bounded failure.",
                        "failure_ids": ["tests/test_boundary.py::test_import"],
                    }
                ],
            )
            state = load_run_state(project)
            state.tasks = [task]
            before = orchestrator._recovery_evidence_fingerprint(
                task,
                state=state,
                tasks=state.tasks,
            )
            answer = "https://example.test/authorized-fixture"
            orchestrator._operator_inputs.save_answer(request, answer)
            secret_request = _request(
                key="provider.test_token",
                kind="secret",
                question="Provide the test provider token",
                sensitivity="secret",
                validation={},
                bindings=[],
            )
            secret_answer = "never-render-this-token"
            orchestrator._operator_inputs.save_answer(
                secret_request,
                secret_answer,
            )
            after = orchestrator._recovery_evidence_fingerprint(
                task,
                state=state,
                tasks=state.tasks,
            )
            review = "The latest verification reached an implementation failure."
            evidence = orchestrator._recovery_judge_evidence(
                state,
                task,
                task,
                review,
                1,
            )

            self.assertNotEqual(before, after)
            serialized_evidence = json.dumps(evidence, ensure_ascii=False)
            self.assertNotIn(answer, serialized_evidence)
            self.assertNotIn(secret_answer, serialized_evidence)
            record = next(
                item
                for item in evidence["operator_inputs"]["records"]
                if item["key"] == request.key
            )
            binding = evidence["operator_inputs"]["bindings"][0]
            self.assertTrue(record["present"])
            self.assertTrue(binding["available"])

            with mock.patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "STOP",
                    "reason": "The fixture input is still missing.",
                    "actionable_items": [],
                    "split_axis": [],
                    "owner": "user_input",
                    "prerequisite_keys": [request.key],
                    "evidence_refs": [
                        "latest-review",
                        f"operator-input:{request.key}",
                    ],
                    "source": "provider",
                },
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": review,
                        "failure_ids": ["tests/test_boundary.py::test_import"],
                    },
                )

            self.assertTrue(scheduled)
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.recovery_history[-1]["result"], "requeued")
            self.assertIn("rejected_stop", task.recovery_history[-1])
            self.assertEqual(state.last_recovery_route["outcome"], "requeued")
            self.assertEqual(
                state.last_recovery_route["judge_source"],
                "reconciled_fallback",
            )
            self.assertEqual(state.active_blocker, {})

    def test_provider_stop_for_outstanding_input_routes_wait_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            task = TaskSpec(
                task_id="bounded-repair",
                title="Repair the bounded failure",
                description="Run the authorized boundary proof.",
                acceptance=["The boundary proof passes."],
                status="in_progress",
                task_origin="scope_split",
            )
            request = _request(task_id=task.task_id)
            state = load_run_state(project)
            state.tasks = [task]
            state.pending_input_requests = [request.to_dict()]

            with mock.patch.object(
                orchestrator,
                "_run_recovery_judge",
                return_value={
                    "decision": "STOP",
                    "reason": "The authorization is genuinely outstanding.",
                    "actionable_items": [],
                    "split_axis": [],
                    "owner": "user_input",
                    "prerequisite_keys": [request.key],
                    "evidence_refs": [
                        f"pending-input:{request.request_id}",
                    ],
                    "source": "provider",
                },
            ):
                scheduled = orchestrator._schedule_repair_tasks_for_failure(
                    state,
                    state.tasks,
                    task,
                    {
                        "reason": "review rejected the task",
                        "review": "Authorization must exist before the proof runs.",
                    },
                )

            self.assertTrue(scheduled)
            self.assertEqual(state.status, "waiting_user")
            self.assertEqual(task.status, "waiting_user")
            self.assertEqual(state.active_blocker, {})
            self.assertEqual(state.active_input_request_id, request.request_id)
            self.assertEqual(
                state.last_recovery_route["stop_owner"],
                "user_input",
            )

    def test_pending_task_with_persisted_input_is_reconciled_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="repair-owner-r1-1",
                title="Repair proof",
                description="Repair the proof.",
                acceptance=["The proof passes."],
                status="pending",
                task_origin="evidence_repair",
            )
            request = _request(task_id=task.task_id)
            state.tasks = [task]
            state.pending_input_requests = [request.to_dict()]

            changed = orchestrator._reconcile_orphaned_waiting_user_tasks(
                state, state.tasks
            )

            self.assertTrue(changed)
            self.assertEqual(task.status, "waiting_user")
            self.assertEqual(task.required_inputs[0]["key"], request.key)
            self.assertEqual(state.active_input_request_id, request.request_id)

    def test_stale_auto_agents_blocker_resumes_through_persisted_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="repair-owner-r1-1",
                title="Repair proof",
                description="Repair the proof.",
                acceptance=["The proof passes."],
                status="pending",
                task_origin="evidence_repair",
            )
            request = _request(task_id=task.task_id)
            state.current_stage = "implement"
            state.status = "blocked"
            state.last_error = "automatic auto_agents self-repair failed"
            state.active_blocker = {
                "owner": "auto_agents",
                "category": "evidence_preflight_runtime_source_divergence",
                "reason": state.last_error,
                "status": "blocked",
            }
            state.tasks = [task]
            state.pending_input_requests = [request.to_dict()]

            changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "waiting_user")
            self.assertEqual(state.active_blocker, {})
            self.assertEqual(state.last_error, "")
            self.assertEqual(task.status, "waiting_user")
            self.assertEqual(state.active_input_request_id, request.request_id)

    def test_runtime_identity_records_loaded_source_and_repository_revision(self):
        identity = Orchestrator._auto_agents_runtime_identity()

        self.assertTrue(
            identity["orchestrator_module"].endswith(
                "/src/auto_agents/orchestrator.py"
            )
        )
        self.assertEqual(len(identity["orchestrator_sha256"]), 64)
        self.assertEqual(
            Path(identity["repository_root"]),
            Path(__file__).resolve().parents[1],
        )
        self.assertTrue(identity["repository_head"])
        self.assertTrue(identity["python_executable"])

    def test_declined_attestation_routes_to_clarify_without_silent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need authorization",
                acceptance=["Authorized input exists"],
            )
            state.tasks = [task]
            request = orchestrator._normalize_input_requests(
                state, task, [_request().to_dict()]
            )[0]
            state.status = "waiting_user"
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project, state)
            orchestrator.answer_input_request(
                request_id=request.request_id,
                value=False,
            )
            routed = load_run_state(project)
            self.assertEqual(routed.rejected_stage, "clarify")
            self.assertIn("Do not weaken", routed.rejection_reason)
            self.assertEqual(routed.active_blocker, {})

    def test_preflight_parser_accepts_wait_user(self):
        payload = {
            "decision": "WAIT_USER",
            "target_stage": "",
            "reason": "authorized fixture required",
            "checklist": ["collect one answer"],
            "required_inputs": [_request().to_dict()],
            "required_mutations": [],
        }
        parsed = Orchestrator._parse_evidence_preflight(
            "EVIDENCE_PREFLIGHT: " + json.dumps(payload, ensure_ascii=False)
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["decision"], "WAIT_USER")

    def test_semantic_target_approval_requires_structured_input(self):
        issue = Orchestrator._evidence_preflight_protocol_issue(
            {
                "decision": "ROUTE",
                "target_stage": "clarify",
                "reason": "持久化边界尚未确定。",
                "required_inputs": [],
                "required_mutations": [
                    {
                        "path": ".auto-agents/state/requirements_trace.json",
                        "reason": "记录结构扩展所需的用户批准持久化决策。",
                        "owner": "target_project",
                    },
                    {
                        "path": ".auto-agents/state/task_plan.json",
                        "reason": "重绑后续验证任务。",
                        "owner": "plan",
                    },
                ],
            }
        )

        self.assertIn("structured required_inputs", issue)
        self.assertIn("requirements_trace.json", issue)
        self.assertNotIn("task_plan.json", issue)

    def test_satisfied_preflight_input_advances_to_next_owner_partition(self):
        approval = _request(
            key="persistence.choice",
            kind="boolean",
            question="Has the persistence choice been approved?",
            purpose="Bind the approved persistence boundary.",
            why_required="Planning depends on the approved boundary.",
            how_to_obtain=["Confirm the recorded project decision."],
            recommended_answer="Answer no until the decision is recorded.",
            validation={},
            bindings=[],
            subject_fingerprint="persistence-boundary",
        )

        class Adapter:
            def run(self, request):
                payload = {
                    "decision": "WAIT_USER",
                    "target_stage": "",
                    "reason": "The operator choice is available; update the plan.",
                    "checklist": ["publish the verification task"],
                    "required_inputs": [approval.to_dict()],
                    "required_mutations": [
                        {
                            "path": ".auto-agents/state/requirements_trace.json",
                            "reason": "record the approved persistence boundary",
                            "owner": "target_project",
                        },
                        {
                            "path": ".auto-agents/state/task_plan.json",
                            "reason": "bind the approved persistence boundary",
                            "owner": "plan",
                        }
                    ],
                }
                summary = "EVIDENCE_PREFLIGHT: " + json.dumps(payload)
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary,
                )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            orchestrator.adapter = Adapter()
            orchestrator._operator_inputs.save_answer(approval, True)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Persistence boundary",
                description="Bind an approved persistence choice into the plan.",
                acceptance=["The plan publishes the real boundary proof."],
                required_inputs=[approval.to_dict()],
            )
            state.tasks = [task]

            with mock.patch.object(
                orchestrator,
                "_task_needs_evidence_preflight",
                return_value=True,
            ):
                result = orchestrator._ensure_evidence_preflight(state, task)

            self.assertEqual(result["decision"], "ROUTE")
            self.assertEqual(result["target_stage"], "clarify")
            self.assertEqual(
                result["actionable_paths"],
                [".auto-agents/state/requirements_trace.json"],
            )
            self.assertEqual(
                result["actionable_mutations_by_owner"]["plan"],
                [".auto-agents/state/task_plan.json"],
            )
            self.assertEqual(state.pending_input_requests, [])
            self.assertEqual(task.status, "pending")

    def test_preflight_snapshot_sees_untracked_candidate_and_queues_input(self):
        class Adapter:
            def run(self, request):
                if not (request.cwd / "candidate-untracked.txt").is_file():
                    raise AssertionError("candidate snapshot omitted untracked file")
                payload = {
                    "decision": "WAIT_USER",
                    "target_stage": "",
                    "reason": "authorization is required",
                    "checklist": ["collect authorization"],
                    "required_inputs": [_request().to_dict()],
                    "required_mutations": [],
                }
                summary = "EVIDENCE_PREFLIGHT: " + json.dumps(
                    payload, ensure_ascii=False
                )
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary,
                )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            (project / "candidate-untracked.txt").write_text(
                "candidate\n", encoding="utf-8"
            )
            orchestrator = Orchestrator(project)
            orchestrator.adapter = Adapter()
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need input",
                acceptance=["Input exists"],
            )
            state.tasks = [task]
            with mock.patch.object(
                orchestrator, "_task_needs_evidence_preflight", return_value=True
            ):
                result = orchestrator._ensure_evidence_preflight(state, task)
            self.assertEqual(result["decision"], "WAIT_USER")
            self.assertEqual(task.status, "waiting_user")
            self.assertEqual(len(state.pending_input_requests), 1)

    def test_preflight_retries_operator_mutation_and_queues_input(self):
        source_request = _request(
            key="youtube.source_url",
            kind="url",
            question="请输入获得授权的公开视频 URL",
            purpose="为真实 YouTube 导入测试提供授权视频。",
            why_required="真实系统边界测试需要操作者选择视频。",
            how_to_obtain=["提供项目拥有或已明确获授权的视频 URL。"],
            recommended_answer="使用专门用于自动化测试的视频。",
            default="",
            sensitivity="private",
            validation={"https_only": True},
            bindings=[
                {
                    "env": "SDGLOBAL_TEST_YOUTUBE_PUBLIC_VIDEO_URL",
                    "projection": "value",
                }
            ],
            subject_fingerprint="",
        )

        class Adapter:
            def __init__(self):
                self.prompts = []

            def run(self, request):
                self.prompts.append(request.prompt)
                if len(self.prompts) == 1:
                    payload = {
                        "decision": "ROUTE",
                        "target_stage": "provider_research",
                        "reason": (
                            "authorized fixture and pinned tool bindings are absent"
                        ),
                        "checklist": ["collect prerequisites"],
                        "required_inputs": [],
                        "required_mutations": [
                            {
                                "path": (
                                    ".auto-agents/docs/provider_references/yt-dlp.md"
                                ),
                                "reason": "resolve audited tool pins",
                                "owner": "provider_research",
                                "config_scope": "operator",
                            },
                            {
                                "path": ".auto-agents/config.json",
                                "reason": "bind the authorized fixture and runtime",
                                "owner": "target_project",
                                "config_scope": "operator",
                            },
                        ],
                    }
                else:
                    payload = {
                        "decision": "ROUTE",
                        "target_stage": "provider_research",
                        "reason": "collect operator input before verification",
                        "checklist": ["collect the authorized source URL"],
                        "required_inputs": [source_request.to_dict()],
                        "required_mutations": [
                            {
                                "path": (
                                    ".auto-agents/docs/provider_references/yt-dlp.md"
                                ),
                                "reason": "resolve audited tool pins",
                                "owner": "provider_research",
                            }
                        ],
                    }
                summary = "EVIDENCE_PREFLIGHT: " + json.dumps(
                    payload, ensure_ascii=False
                )
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary,
                )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            adapter = Adapter()
            orchestrator.adapter = adapter
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need an authorized fixture and pinned tools",
                acceptance=["Real boundary proof passes"],
            )
            state.tasks = [task]
            with mock.patch.object(
                orchestrator, "_task_needs_evidence_preflight", return_value=True
            ):
                result = orchestrator._ensure_evidence_preflight(state, task)

            self.assertEqual(len(adapter.prompts), 2)
            self.assertIn(
                "Operator-owned prerequisites cannot be represented only",
                adapter.prompts[1],
            )
            self.assertEqual(result["decision"], "WAIT_USER")
            self.assertEqual(task.status, "waiting_user")
            self.assertEqual(len(state.pending_input_requests), 1)
            self.assertEqual(
                state.pending_input_requests[0]["key"], "youtube.source_url"
            )

    def test_cached_semantic_target_mutation_is_invalidated_and_recollected(self):
        class Adapter:
            def __init__(self):
                self.calls = 0

            def run(self, request):
                self.calls += 1
                payload = {
                    "decision": "WAIT_USER",
                    "target_stage": "",
                    "reason": "operator approval is required",
                    "checklist": ["collect the approval"],
                    "required_inputs": [
                        _request(
                            key="boundary.approval",
                            question="Do you approve use of the authorized fixture?",
                            purpose="Authorize the real boundary proof.",
                            why_required="The proof performs an external operation.",
                            how_to_obtain=["Answer yes only when approved."],
                            recommended_answer="Answer no when uncertain.",
                            subject_fingerprint="fixture-approval",
                            validation={"claims": ["external_operation"]},
                            bindings=[],
                        ).to_dict()
                    ],
                    "required_mutations": [],
                }
                summary = "EVIDENCE_PREFLIGHT: " + json.dumps(payload)
                write_text(request.output_path, summary)
                return AgentResult(
                    ok=True,
                    command=["fake"],
                    output_path=request.output_path,
                    summary=summary,
                )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            adapter = Adapter()
            orchestrator.adapter = adapter
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="External boundary",
                description="Exercise an authorized external boundary.",
                acceptance=["The real boundary proof passes."],
            )
            state.tasks = [task]
            with mock.patch.object(
                orchestrator, "_task_needs_evidence_preflight", return_value=True
            ):
                task.evidence_preflight = {
                    "fingerprint": orchestrator._evidence_preflight_fingerprint(task),
                    "decision": "BLOCK",
                    "target_stage": "",
                    "reason": "operator prerequisites are absent",
                    "checklist": ["collect operator prerequisites"],
                    "required_inputs": [],
                    "required_mutations": [
                        {
                            "path": ".auto-agents/state/requirements_trace.json",
                            "reason": (
                                "Record the persistence choice that requires user "
                                "approval."
                            ),
                            "owner": "target_project",
                        }
                    ],
                }
                result = orchestrator._ensure_evidence_preflight(state, task)

            self.assertEqual(adapter.calls, 1)
            self.assertEqual(result["decision"], "WAIT_USER")
            self.assertEqual(task.status, "waiting_user")
            self.assertEqual(task.evidence_preflight["decision"], "WAIT_USER")
            self.assertEqual(len(state.pending_input_requests), 1)
            self.assertEqual(state.active_blocker, {})

    def test_restored_operator_mutation_cannot_route_to_terminal_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="External boundary",
                description="Exercise an authorized external boundary.",
                acceptance=["The real boundary proof passes."],
            )
            result = {
                "fingerprint": "legacy-fingerprint",
                "decision": "BLOCK",
                "target_stage": "",
                "reason": "operator prerequisites are absent",
                "checklist": ["collect operator prerequisites"],
                "required_inputs": [],
                "required_mutations": [
                    {
                        "path": ".auto-agents/config.json",
                        "reason": "bind the approved external fixture",
                        "owner": "target_project",
                        "config_scope": "operator",
                    }
                ],
            }
            task.evidence_preflight = dict(result)
            state.tasks = [task]
            state.resume_context["evidence_preflight_routes"] = {
                task.task_id: {"repeat": 1}
            }

            with mock.patch.object(
                orchestrator,
                "_block_run",
                side_effect=AssertionError("malformed result must not block"),
            ):
                routed = orchestrator._route_evidence_preflight(
                    state, [task], task, result
                )

            self.assertEqual(routed.status, "pending")
            self.assertEqual(routed.active_blocker, {})
            self.assertEqual(task.evidence_preflight, {})
            self.assertNotIn(
                task.task_id,
                routed.resume_context.get("evidence_preflight_routes", {}),
            )
            persisted = load_run_state(project)
            self.assertEqual(persisted.tasks[0].evidence_preflight, {})

    def test_pause_mode_persists_waiting_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            orchestrator._interaction_mode = "pause"
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need input",
                acceptance=["Input exists"],
            )
            state.tasks = [task]
            orchestrator._normalize_input_requests(
                state, task, [_request().to_dict()]
            )
            self.assertFalse(orchestrator._process_pending_user_input(state, [task]))
            persisted = load_run_state(project)
            self.assertEqual(persisted.status, "waiting_user")
            self.assertEqual(persisted.active_blocker, {})

    def test_cli_parser_exposes_interaction_and_answer_commands(self):
        parser = build_parser()
        run = parser.parse_args(
            [
                "run",
                "--project",
                "/tmp/demo",
                "--interaction-mode",
                "pause",
                "--secret-echo",
                "visible",
            ]
        )
        answer = parser.parse_args(
            ["answer", "--project", "/tmp/demo", "--yes"]
        )
        self.assertEqual(run.interaction_mode, "pause")
        self.assertEqual(run.secret_echo, "visible")
        self.assertTrue(answer.yes)

    def test_answer_cli_saves_without_resume_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need input",
                acceptance=["Input exists"],
            )
            state.tasks = [task]
            orchestrator._normalize_input_requests(
                state, task, [_request().to_dict()]
            )
            state.status = "waiting_user"
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project, state)
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                exit_code = main(
                    [
                        "answer",
                        "--project",
                        str(project),
                        "--yes",
                        "--no-resume",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(load_run_state(project).status, "pending")

    def test_answer_cli_collects_remaining_inputs_before_resuming(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-001",
                title="Input",
                description="Need an authorized source",
                acceptance=["Authorized input exists"],
            )
            state.tasks = [task]
            source_request = _request(
                key="youtube.source_url",
                kind="url",
                question="请输入公开视频 URL",
                default="",
                validation={"https_only": True},
                bindings=[],
                subject_fingerprint="video-url",
            )
            requests = orchestrator._normalize_input_requests(
                state,
                task,
                [source_request.to_dict(), _request().to_dict()],
            )
            state.status = "waiting_user"
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project, state)
            output = io.StringIO()
            resumed_state = mock.Mock()
            resumed_state.to_dict.return_value = {"status": "completed"}

            with mock.patch("sys.stdout", output):
                with mock.patch.object(
                    Orchestrator,
                    "_interactive_input_available",
                    return_value=True,
                ):
                    with mock.patch.object(
                        Orchestrator, "_prompt_user", return_value="y"
                    ) as prompt:
                        with mock.patch(
                            "auto_agents.cli._resume_run_after_answer",
                            return_value=resumed_state,
                        ) as resume:
                            exit_code = main(
                                [
                                    "answer",
                                    "--project",
                                    str(project),
                                    "--request-id",
                                    requests[0].request_id,
                                    "--value",
                                    "https://www.youtube.com/watch?v=abcdefghijk",
                                ]
                            )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["resume"])
            self.assertEqual(payload["remaining"], 0)
            self.assertEqual(payload["resumed_run"]["status"], "completed")
            self.assertEqual(prompt.call_count, 1)
            resume.assert_called_once()
            self.assertEqual(
                load_run_state(project).pending_input_requests, []
            )

    def test_sequential_scheduler_finishes_independent_task_before_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            orchestrator._interaction_mode = "pause"
            state = load_run_state(project)
            waiting = TaskSpec(
                task_id="task-waiting",
                title="Waiting",
                description="Need input",
                acceptance=["Input exists"],
            )
            independent = TaskSpec(
                task_id="task-independent",
                title="Independent",
                description="Can finish",
                acceptance=["Done"],
            )
            tasks = [waiting, independent]
            state.tasks = tasks
            orchestrator._normalize_input_requests(
                state, waiting, [_request(task_id=waiting.task_id).to_dict()]
            )

            def execute(_state, _tasks, task):
                task.status = "done"
                return None

            with mock.patch.object(
                orchestrator,
                "_execute_task_in_main_worktree",
                side_effect=execute,
            ):
                result = orchestrator._run_sequential_implementation_loop(
                    state, tasks, None
                )
            self.assertEqual(independent.status, "done")
            self.assertEqual(waiting.status, "waiting_user")
            self.assertEqual(result.status, "waiting_user")

    def test_declared_binding_is_injected_without_storing_value_in_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            store = OperatorInputStore(project)
            request = _request(
                key="provider.api_token",
                kind="secret",
                question="请输入 token",
                sensitivity="secret",
                validation={},
            )
            store.save_answer(request, "secret-for-gate")
            orchestrator = Orchestrator(project)
            task = orchestrator._load_tasks_from_plan()[0]
            task.operator_input_bindings = [
                {
                    "input_key": "provider.api_token",
                    "env": "PROVIDER_API_TOKEN",
                    "projection": "value",
                }
            ]
            orchestrator._persist_tasks([task])
            environment = orchestrator._operator_gate_environment()
            self.assertEqual(environment["PROVIDER_API_TOKEN"], "secret-for-gate")
            self.assertNotIn(
                "secret-for-gate",
                (project / ".auto-agents" / "state" / "task_plan.json").read_text(
                    encoding="utf-8"
                ),
            )

    def test_gate_output_redacts_operator_bound_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            command = "printf '%s' \"$PROVIDER_API_TOKEN\""
            with LocalGatePlanExecutor(
                project,
                GateConfig(),
                {command: {}},
                environment_overrides={"PROVIDER_API_TOKEN": "secret-for-gate"},
            ) as executor:
                result = executor.run(
                    command,
                    timeout_seconds=30,
                    adaptive_timeout_enabled=False,
                    idle_timeout_seconds=30,
                )
            self.assertTrue(result.ok)
            self.assertEqual(result.stdout, "[REDACTED]")
            self.assertNotIn("secret-for-gate", result.stderr)


if __name__ == "__main__":
    unittest.main()
