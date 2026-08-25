from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.orchestrator import Orchestrator
from auto_agents.config import load_run_state, save_run_state
from auto_agents.models import TaskSpec
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


if __name__ == "__main__":
    unittest.main()
