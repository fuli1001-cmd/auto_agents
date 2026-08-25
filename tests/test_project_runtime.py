from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.orchestrator import Orchestrator
from auto_agents.config import load_run_state, save_run_state
from auto_agents.models import TaskSpec
from auto_agents.infrastructure_repair import InfrastructureRepairResult
from auto_agents.project_runtime import ProjectRuntimeManager


class ProjectRuntimeManagerTests(unittest.TestCase):
    def test_installs_pinned_file_only_inside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            source = root / "tool"
            source.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manager = ProjectRuntimeManager(project)
            plan = manager.plan(
                [
                    {
                        "tool_id": "demo-tool",
                        "version": "1.0.0",
                        "source_url": source.resolve().as_uri(),
                        "sha256": digest,
                        "install_kind": "file",
                        "executable": "bin/demo-tool",
                        "license": "MIT",
                    }
                ]
            )
            request = plan.approval_request()
            self.assertEqual(request.kind, "install_approval")
            self.assertFalse(request.default)
            installed = manager.install(plan)
            executable = Path(str(installed["demo-tool"]["runtime_path"]))
            executable.relative_to(project / ".auto-agents" / "runtime")
            self.assertTrue(executable.is_file())
            self.assertTrue(executable.stat().st_mode & stat.S_IXUSR)
            self.assertTrue(manager.installed(plan.requirements[0]))

    def test_digest_mismatch_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            source = root / "tool"
            source.write_bytes(b"unexpected")
            manager = ProjectRuntimeManager(project)
            plan = manager.plan(
                [
                    {
                        "tool_id": "demo-tool",
                        "version": "1.0.0",
                        "source_url": source.resolve().as_uri(),
                        "sha256": "0" * 64,
                        "install_kind": "file",
                    }
                ]
            )
            with self.assertRaises(RuntimeError):
                manager.install(plan)
            self.assertFalse(
                (project / ".auto-agents" / "runtime" / "demo-tool" / "1.0.0").exists()
            )

    def test_install_approval_installs_and_exposes_derived_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            source = root / "tool"
            source.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-runtime",
                title="Runtime",
                description="Install runtime",
                acceptance=["Runtime exists"],
            )
            state.tasks = [task]
            requests = orchestrator._normalize_input_requests(
                state,
                task,
                [
                    {
                        "key": "runtime.install.requested",
                        "kind": "install_approval",
                        "question": "install?",
                        "purpose": "run proof",
                        "why_required": "tool missing",
                        "default": False,
                        "persistence": "project",
                        "sensitivity": "private",
                        "validation": {
                            "runtime_manifest": [
                                {
                                    "tool_id": "demo-tool",
                                    "version": "1.0.0",
                                    "source_url": source.resolve().as_uri(),
                                    "sha256": digest,
                                    "install_kind": "file",
                                    "executable": "bin/demo-tool",
                                }
                            ]
                        },
                        "bindings": [
                            {
                                "input_key": "runtime.demo-tool",
                                "env": "DEMO_TOOL_PATH",
                                "projection": "runtime_path",
                            }
                        ],
                        "subject_fingerprint": "requested",
                    }
                ],
            )
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project, state)
            result = orchestrator.answer_input_request(
                request_id=requests[0].request_id,
                value=True,
            )
            self.assertTrue(result["ok"])
            environment = orchestrator._operator_gate_environment()
            self.assertTrue(Path(environment["DEMO_TOOL_PATH"]).is_file())

    def test_tool_constraint_manifest_becomes_workspace_conda_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            (project / "pyproject.toml").write_text(
                "[project]\nname='demo'\nversion='0.1.0'\nrequires-python='>=3.9'\n",
                encoding="utf-8",
            )
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-runtime",
                title="Runtime",
                description="Install runtime",
                acceptance=["Runtime exists"],
            )

            requests = orchestrator._normalize_input_requests(
                state,
                task,
                [
                    {
                        "key": "install_approval",
                        "kind": "install_approval",
                        "question": "install?",
                        "purpose": "run proof",
                        "why_required": "./.conda/bin/python is missing",
                        "default": False,
                        "persistence": "project",
                        "sensitivity": "public",
                        "validation": {
                            "runtime_manifest": {
                                "tools": [
                                    {
                                        "tool_id": "python",
                                        "requirement": ">=3.9",
                                    },
                                    {
                                        "tool_id": "pytest",
                                        "requirement": ">=8.4,<9",
                                    },
                                ]
                            }
                        },
                        "subject_fingerprint": "requested",
                    }
                ],
            )

            self.assertEqual(len(requests), 1)
            self.assertEqual(task.status, "waiting_user")
            self.assertTrue(requests[0].validation["workspace_conda"])
            self.assertIn("pyproject.toml", requests[0].validation["declared_sources"])
            self.assertEqual(len(state.pending_input_requests), 1)

    def test_workspace_conda_approval_runs_declared_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            (project / "pyproject.toml").write_text(
                "[project]\nname='demo'\nversion='0.1.0'\nrequires-python='>=3.9'\n",
                encoding="utf-8",
            )
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-runtime",
                title="Runtime",
                description="Install runtime",
                acceptance=["Runtime exists"],
            )
            state.tasks = [task]
            request = orchestrator._normalize_input_requests(
                state,
                task,
                [
                    {
                        "key": "runtime.install.requested",
                        "kind": "install_approval",
                        "question": "install?",
                        "purpose": "run proof",
                        "why_required": "runtime missing",
                        "default": False,
                        "persistence": "project",
                        "sensitivity": "private",
                        "validation": {
                            "runtime_manifest": {
                                "tools": [{"tool_id": "python"}]
                            }
                        },
                        "subject_fingerprint": "requested",
                    }
                ],
            )[0]
            state.pending_input_requests = [request.to_dict()]
            state.active_input_request_id = request.request_id
            task.required_inputs = [request.to_dict()]
            orchestrator._persist_tasks(state.tasks)
            save_run_state(project, state)

            with patch(
                "auto_agents.orchestrator.repair_declared_workspace_local_conda",
                return_value=InfrastructureRepairResult(
                    repaired=True,
                    capability="workspace_conda",
                    action="recreated_from_pyproject",
                    reason="ready",
                ),
            ) as repair:
                result = orchestrator.answer_input_request(
                    request_id=request.request_id,
                    value=True,
                )

            self.assertTrue(result["ok"])
            repair.assert_called_once_with(
                project.resolve(),
                allow_downloads=True,
            )
            self.assertEqual(load_run_state(project).tasks[0].status, "pending")

    def test_orphaned_waiting_task_restores_request_and_pauses_before_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            (project / "pyproject.toml").write_text(
                "[project]\nname='demo'\nversion='0.1.0'\nrequires-python='>=3.9'\n",
                encoding="utf-8",
            )
            orchestrator = Orchestrator(project)
            orchestrator._interaction_mode = "pause"
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-runtime",
                title="Runtime",
                description="Install runtime",
                acceptance=["Runtime exists"],
                status="waiting_user",
                evidence_preflight={
                    "decision": "WAIT_USER",
                    "required_inputs": [
                        {
                            "key": "install_approval",
                            "kind": "install_approval",
                            "question": "install?",
                            "purpose": "run proof",
                            "why_required": "./.conda/bin/python is missing",
                            "default": False,
                            "persistence": "project",
                            "sensitivity": "public",
                            "validation": {
                                "runtime_manifest": {
                                    "tools": [{"tool_id": "python"}]
                                }
                            },
                            "subject_fingerprint": "requested",
                        }
                    ],
                },
            )
            orchestrator._persist_tasks([task])
            state.tasks = [task]
            save_run_state(project, state)

            with patch.object(
                orchestrator,
                "_ensure_implement_verify_baseline",
                side_effect=AssertionError("baseline must not run while waiting"),
            ):
                result = orchestrator._run_implementation_loop(state, None)

            self.assertEqual(result.status, "waiting_user")
            self.assertEqual(result.tasks[0].status, "waiting_user")
            self.assertEqual(len(result.pending_input_requests), 1)

    def test_orphaned_waiting_task_without_request_blocks_instead_of_spinning(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            Orchestrator.init_project(project, "demo", "mock")
            orchestrator = Orchestrator(project)
            state = load_run_state(project)
            task = TaskSpec(
                task_id="task-runtime",
                title="Runtime",
                description="Install runtime",
                acceptance=["Runtime exists"],
                status="waiting_user",
            )

            changed = orchestrator._reconcile_orphaned_waiting_user_tasks(
                state,
                [task],
            )

            self.assertFalse(changed)
            self.assertEqual(state.status, "blocked")
            self.assertEqual(
                state.active_blocker["category"],
                "waiting_user_request_missing",
            )


if __name__ == "__main__":
    unittest.main()
