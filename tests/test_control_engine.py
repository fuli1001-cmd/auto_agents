import json
from pathlib import Path
import pytest
from auto_agents.control import Store, VerificationSpec
from auto_agents.control.engine import Engine
from auto_agents.models import ProjectConfig
from auto_agents.control.workspace import git


class Worker:
    def __init__(self):
        self.calls = []

    def run(self, ctx, phase, prompt, output, **kwargs):
        self.calls.append((ctx.work_id, phase))
        root = ctx.workspace_root

        def artifact(name, text):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        if phase == "classify":
            data = {
                "decision": "fix",
                "checks": [{"command": "python -m pytest -q tests/test_value.py"}],
                "paths": ["value.py", "tests"],
            }
        elif phase == "implement":
            artifact("value.py", "VALUE = 1\n")
            artifact(
                "tests/test_value.py",
                "from value import VALUE\ndef test_value(): assert VALUE == 1\n",
            )
            data = {"summary": "Fixed value"}
        elif phase == "review":
            data = {
                "decision": "pass",
                "test_changes_valid": True,
                "reason": "Check covers the required value",
            }
        elif phase == "clarify":
            for name in ("project_brief", "requirements"):
                artifact(
                    ".auto-agents/docs/" + name + ".md",
                    "# Required value\nSet value to one.\n",
                )
            trace = {
                "version": 1,
                "requirements": [
                    {
                        "id": "REQ-001",
                        "text": ctx.contract.goal,
                        "source": "spec",
                        "status": "active",
                        "priority": "mandatory",
                        "acceptance_oracles": ["Observe requested value"],
                        "oracle_type": "mixed",
                        "oracle_strength": "behavioral",
                        "evidence_boundary": "system_boundary",
                        "forbidden_proxy_oracles": [],
                        "forbidden_patterns": [],
                        "notes": "",
                    }
                ],
            }
            artifact(".auto-agents/docs/requirements_trace.json", json.dumps(trace))
            data = {
                "artifacts": [
                    ".auto-agents/docs/project_brief.md",
                    ".auto-agents/docs/requirements.md",
                    ".auto-agents/docs/requirements_trace.json",
                ],
                "frontend": False,
                "questions": [],
            }
        elif phase == "design":
            artifact(
                ".auto-agents/docs/architecture.md", "# Architecture\nOne module.\n"
            )
            artifact("DESIGN.md", "# Design\nA value.\n")
            data = {"artifacts": [".auto-agents/docs/architecture.md", "DESIGN.md"]}
        elif phase == "plan":
            data = {
                "tasks": [
                    {
                        "task_id": "T1",
                        "goal": "Set value to one",
                        "paths": ["value.py", "tests"],
                        "depends_on": [],
                        "requirement_ids": ["REQ-001"],
                        "checks": [
                            {"command": "python -m pytest -q tests/test_value.py"}
                        ],
                    }
                ],
                "checks": [{"command": "python -m pytest -q tests/test_value.py"}],
            }
        elif phase == "provider_research":
            data = {
                "not_required": True,
                "reason": "No external content provider in this project",
            }
        elif phase == "readme":
            artifact("README.md", "# Value\nVerified value equals one.\n")
            data = {"artifacts": ["README.md"]}
        elif phase == "diagnose":
            data = (
                {"action": "acceptance"}
                if ctx.contract.inputs.get("accept_ready")
                or (root / "value.py").read_text() == "VALUE = 1\n"
                else {"action": "fix", "issue": {"summary": "Set value to one"}}
            )
        elif phase == "acceptance":
            artifact(
                ".auto-agents/docs/acceptance/result.txt", "Observed value equals one"
            )
            data = {
                "accepted": True,
                "evidence_refs": [".auto-agents/docs/acceptance/result.txt"],
                "checks": [{"command": "python -m pytest -q tests/test_value.py"}],
            }
        elif phase == "acceptance_review":
            data = {
                "decision": "pass",
                "evidence_refs": [".auto-agents/docs/acceptance/result.txt"],
                "reason": "Checked observed value",
            }
        else:
            raise AssertionError(phase)
        output.write_text(json.dumps(data))
        return {"text": json.dumps(data), "provider": "fixture"}


def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init")
    (root / "value.py").write_text("VALUE = 0\n")
    git(root, "add", "value.py")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "baseline",
    )
    return root


@pytest.mark.parametrize("mode", ["fix", "run", "collab"])
def test_every_mode_uses_one_controller_and_real_verification(tmp_path, mode):
    root = project(tmp_path)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    result = engine.start(mode, "Set the value to one")
    assert result["status"] == "COMPLETED", result["failure"]
    assert (root / "value.py").read_text() == "VALUE = 1\n"
    calls = list(worker.calls)
    assert engine.resume(result["id"])["status"] == "COMPLETED"
    assert worker.calls == calls
    assert all(x["state"] != "UNKNOWN" for x in store.operations())
    assert not (root / ".auto-agents/state/owned/workspaces" / result["id"]).exists()


def test_operator_approval_and_cancel_do_not_invoke_provider_again(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        auto_approve=False,
        print_fn=lambda *a: None,
    )
    result = engine.start("run", "Set value")
    assert (
        result["status"] == "WAITING" and result["context"]["waiting_for"] == "approval"
    )
    before = len(worker.calls)
    store.transition(result, "CANCELLED")
    assert (
        engine.resume(result["id"])["status"] == "CANCELLED"
        and len(worker.calls) == before
    )
