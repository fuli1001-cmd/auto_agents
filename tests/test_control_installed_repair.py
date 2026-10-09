"""An operator-installed patch stays subject to real owned verification and review."""

import hashlib
import sys

import pytest

from auto_agents.control import ControlError, Store
from auto_agents.control.effects import compile_checks
from auto_agents.control.engine import Engine
from auto_agents.control.workspace import Workspaces, git
from auto_agents.models import ProjectConfig
from test_control_engine import Worker, project


def prepared(tmp_path, *, omit_new_file=False, operator_supplied=False, declared_output=False):
    root = project(tmp_path)
    store = Store(root)
    workspaces = Workspaces(root, store)
    source = workspaces.snapshot()
    parent = store.create_workflow("collab", "Inspect existing value", source)
    data = {
        "value.py": b"VALUE = 1\n",
        "tests/test_value.py": b"from value import VALUE\ndef test_value(): assert VALUE == 1\n",
    }
    identities = {name: {"sha256": hashlib.sha256(value).hexdigest(), "executable": False}
                  for name, value in data.items() if not omit_new_file or name == "value.py"}
    command = sys.executable + " -m pytest -q tests/test_value.py"
    outputs = []
    if declared_output:
        outputs = [".auto-agents/docs/acceptance/fix-junit.xml"]
        command += " --junitxml=" + outputs[0]
    child = store.child(parent, "fix", "Set value to one", source,
        scope=tuple(data), checks=compile_checks([{"command": command, "outputs": outputs}], root),
        inputs={"operator_installed_patch": {"files": identities}})
    engine = Engine(root, store, ProjectConfig("fixture"), transport=Worker(),
                    auto_approve=True, print_fn=lambda *args: None)
    if operator_supplied:
        child = store.transition(child, "RUNNING")
        context = engine.context(child)

        def apply_patch(identity):
            for name, value in data.items():
                path = context.workspace_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            return {"files": identities, "origin": "operator"}

        receipt, operation = engine.effect(child, "operator_patch", {"files": identities}, apply_patch)
        candidate = workspaces.candidate(child, context.workspace_root, source, tuple(data))
        child = store.transition(child, "READY", phase="verify", operation=operation,
            context={"candidate": candidate, "implementation": receipt,
                     "classification": {"decision": "fix", "origin": "operator"}})
    else:
        child = store.transition(child, "RUNNING", phase="implement")
        child = store.transition(child, "READY")
    result = engine.drive(child["id"])
    assert result["status"] == "COMPLETED", result["failure"]
    for name, value in data.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    parent = store.work(parent["id"])
    parent = store.transition(parent, "WAITING", context={**parent["context"], "installed_repairs": [child["id"]]})
    return root, store, workspaces, parent, result


def test_operator_patch_is_verified_reviewed_and_adopted_with_original_contract(tmp_path):
    root, store, workspaces, parent, child = prepared(tmp_path, operator_supplied=True)
    original_contract = parent["contract"]
    original_source = store.contract(original_contract).source
    workspaces.adopt(parent, child["result"]["candidate"])
    state = {**parent["context"], "candidate": workspaces.candidate(
        parent, workspaces.ensure(parent, original_source), original_source)}
    state.pop("child", None)
    state.pop("waiting_for", None)
    resumed = store.transition(parent, "READY", phase="acceptance", context=state)
    assert resumed["contract"] == original_contract
    assert store.contract(original_contract).source == original_source
    assert resumed["calls"] == 0 and store.workflow(parent["workflow"])["calls"] == 1
    assert (workspaces.ensure(resumed, original_source) / "value.py").read_text() == "VALUE = 1\n"
    assert {operation["kind"] for operation in store.operations(child["id"])} == {
        "operator_patch", "verify", "model:review",
    }
    assert all(operation["consumed"] for operation in store.operations(child["id"]))


def test_managed_junit_cache_does_not_become_a_protected_worker_write(tmp_path):
    root, store, workspaces, parent, child = prepared(
        tmp_path, operator_supplied=True, declared_output=True,
    )
    report = child["result"]["verification"]
    assert report["ok"] and report["checks"][0]["executed"] == 1
    assert ".auto-agents/docs/acceptance/fix-junit.xml" in child["result"]["candidate"]["paths"]


def test_unregistered_gate_artifact_remains_a_protected_write(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    workspaces = Workspaces(root, store)
    source = workspaces.snapshot()
    work = store.create_workflow("fix", "Fix existing value", source)
    path = workspaces.ensure(work, source)
    forged = path / ".auto-agents/runs/forged/gate-artifacts/forged/result.xml"
    forged.parent.mkdir(parents=True)
    forged.write_text("untrusted output")
    with pytest.raises(ControlError, match="protected paths"):
        workspaces.candidate(work, path, source)


def test_reviewed_exact_installed_patch_delivers_without_sweeping_user_index(tmp_path):
    root, store, workspaces, parent, child = prepared(tmp_path)
    (root / "notes.txt").write_text("User notes\n")
    git(root, "add", "notes.txt")
    calls = store.workflow(parent["workflow"])["calls"]
    result = workspaces.deliver(parent, child["result"]["candidate"])
    assert result["revision"] == git(root, "rev-parse", "HEAD")
    assert git(root, "show", "HEAD:value.py") == "VALUE = 1"
    assert git(root, "diff", "--cached", "--name-only") == "notes.txt"
    assert git(root, "diff", "--name-only") == ""
    assert store.workflow(parent["workflow"])["calls"] == calls


@pytest.mark.parametrize("case", ["not_registered", "changed_bytes", "staged_target", "forged_review", "foreign_child", "advanced_head", "undeclared_new_file"])
def test_installed_patch_never_bypasses_source_or_receipt_guards(tmp_path, case):
    root, store, workspaces, parent, child = prepared(tmp_path, omit_new_file=case == "undeclared_new_file")
    if case == "not_registered":
        parent = store.transition(parent, "WAITING", context={**parent["context"], "installed_repairs": []})
    elif case == "changed_bytes":
        (root / "value.py").write_text("VALUE = 99\n")
    elif case == "staged_target":
        git(root, "add", "value.py")
    elif case == "forged_review":
        store.transition(child, "COMPLETED", result={**child["result"], "review": {
            "decision": "pass", "test_changes_valid": True, "reason": "forged result",
        }})
    elif case == "foreign_child":
        parent = store.create_workflow("collab", "Another original goal", store.contract(parent["contract"]).source)
        parent = store.transition(parent, "READY", context={"installed_repairs": [child["id"]]})
    elif case == "advanced_head":
        (root / "notes.txt").write_text("User notes\n")
        git(root, "add", "notes.txt")
        git(root, "-c", "user.name=User", "-c", "user.email=user@example.test", "commit", "-m", "User change")
    before = git(root, "rev-parse", "HEAD")
    with pytest.raises(ControlError):
        workspaces.deliver(parent, child["result"]["candidate"])
    assert git(root, "rev-parse", "HEAD") == before
