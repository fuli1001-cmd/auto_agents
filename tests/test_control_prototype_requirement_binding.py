"""Existing behavior keeps its provenance without authorizing new requirements."""

import copy
import json
from types import SimpleNamespace

import pytest

from auto_agents.control import ControlError
from auto_agents.control import prototype_scope as scope
from auto_agents.control.workspace import git
from test_control_prototype_contract import HTML, Iteration, plan, setup, write_package


@pytest.fixture
def admission():
    history = ".auto-agents/docs/frontend_prototype_variants/old/frontend_prototype/home.html"
    return {
        "has_frontend": True,
        "original": scope.encoded({"home.html": HTML.encode()}),
        "history": {history: "frozen"},
        "active_requirements": ["REQ-001"],
        "historical_requirements": ["REQ-OLD"],
        "permissions": "Add retry only.",
    }, {
        ".auto-agents/docs/requirements_trace.json": "frozen",
        ".auto-agents/docs/requirements.md": "frozen",
        "app/recovery.py": "frozen",
        "workbench/src/app/page.tsx": "frozen",
    }


@pytest.mark.parametrize("identities", [[], ["REQ-OLD"], ["REQ-001", "REQ-OLD"]])
def test_existing_flow_accepts_grounded_history_without_a_new_requirement(admission, identities):
    baseline, inputs = admission
    value = plan()
    value["flows"][0]["requirement_ids"] = identities
    value["flows"][0]["evidence"] = [next(iter(baseline["history"]))]
    assert scope.check_schema(value, baseline, inputs) == value


def test_unknown_historical_requirement_cannot_be_invented(admission):
    baseline, inputs = admission
    value = plan()
    value["flows"][0]["requirement_ids"] = ["REQ-FORGED"]
    with pytest.raises(ControlError, match="requirement identities"):
        scope.check_schema(value, baseline, inputs)


@pytest.mark.parametrize("identities", [[], ["REQ-OLD"]])
@pytest.mark.parametrize("target", ["change", "new_flow", "mixed_flow"])
def test_changes_and_new_behavior_always_need_active_requirements(admission, identities, target):
    baseline, inputs = admission
    value = plan()
    if target == "change":
        value["changes"][0]["requirement_ids"] = identities
    elif target == "new_flow":
        value["flows"][-1]["requirement_ids"] = identities
    else:
        value["flows"][0]["requirement_ids"] = identities
        check = {**copy.deepcopy(value["checks"][0]), "id": "new-mixed", "kind": "new"}
        value["checks"].append(check)
        value["flows"][0]["checks"].append(check["id"])
    with pytest.raises(ControlError, match="active requirement identities"):
        scope.check_schema(value, baseline, inputs)


@pytest.mark.parametrize("evidence", [["goal"], [".auto-agents/docs/requirements.md"]])
def test_unmapped_preservation_needs_concrete_existing_behavior_evidence(admission, evidence):
    baseline, inputs = admission
    value = plan()
    value["flows"][0].update(requirement_ids=[], evidence=evidence)
    with pytest.raises(ControlError, match="existing product evidence"):
        scope.check_schema(value, baseline, inputs)


def test_new_ui_flow_can_cite_requirements_and_backend_evidence(admission):
    baseline, inputs = admission
    value = plan()
    value["flows"][-1]["evidence"] = ["goal", ".auto-agents/docs/requirements.md", "app/recovery.py"]
    assert scope.check_schema(value, baseline, inputs) == value


@pytest.mark.parametrize("evidence", [scope.CANONICAL + "/home.html", "workbench/src/app/page.tsx"])
def test_existing_frontend_evidence_still_requires_preservation_checks(admission, evidence):
    baseline, inputs = admission
    value = plan()
    value["flows"][0]["evidence"] = [evidence]
    value["checks"][0]["kind"] = "new"
    with pytest.raises(ControlError, match="preservation checks"):
        scope.check_schema(value, baseline, inputs)


def test_all_missing_new_requirement_checks_are_identified(admission):
    baseline, inputs = admission
    baseline["active_requirements"] += ["REQ-002", "REQ-003"]
    value = plan()
    value["changes"][0]["requirement_ids"].append("REQ-002")
    value["changes"].append({**copy.deepcopy(value["changes"][0]), "id": "second", "requirement_ids": ["REQ-003"]})
    with pytest.raises(ControlError, match="New requirements need new behavior checks") as caught:
        scope.check_schema(value, baseline, inputs)
    assert caught.value.details["uncovered"] == [
        {"change": "recovery", "requirement_ids": ["REQ-002"]},
        {"change": "second", "requirement_ids": ["REQ-003"]},
    ]


def test_historical_ids_come_from_frozen_manifests_and_exclude_inactive_requirements(tmp_path):
    root, _ = setup(tmp_path)
    prefix = ".auto-agents/docs/frontend_prototype_variants/old/frontend_prototype"
    write_package(root, prefix)
    manifest = root / prefix / "manifest.json"
    value = json.loads(manifest.read_text())
    value["pages"][0]["requirement_ids"] = ["REQ-OLD", "REQ-RETIRED"]
    manifest.write_text(json.dumps(value))
    git(root, "add", "-f", prefix)
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Historical provenance")
    baseline = {"source": git(root, "rev-parse", "HEAD"), "original": {}, "history": {prefix + "/manifest.json": "frozen"}}
    value["pages"][0]["requirement_ids"] = ["REQ-FORGED"]
    manifest.write_text(json.dumps(value))
    trace = {"requirements": [{"id": "REQ-RETIRED", "status": "superseded"}]}
    assert scope.historical_requirement_ids(SimpleNamespace(workspace_root=root), baseline, trace) == ["REQ-OLD"]


def test_historical_preservation_reaches_review_with_current_requirement_trace(tmp_path):
    class Preserve(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare":
                reply = json.loads(result["text"])
                reply["scope"]["flows"][0]["requirement_ids"] = ["REQ-OLD"]
                reply["scope"]["flows"][1]["requirement_ids"] = []
                result["text"] = json.dumps(reply)
            return result

    root, engine = setup(tmp_path, Preserve())
    prefix = ".auto-agents/docs/frontend_prototype_variants/old/frontend_prototype"
    write_package(root, prefix)
    path = root / prefix / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["pages"][0]["requirement_ids"] = ["REQ-OLD"]
    path.write_text(json.dumps(manifest))
    git(root, "add", "-f", prefix)
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Historical frontend")
    work = engine.start("run", "Add retry")
    assert work["status"] == "WAITING" and work["context"]["approval"] == "prototype", work["failure"]
    operation = next(o for o in engine.store.operations(work["id"]) if o["kind"] == "model:prototype_prepare")
    baseline = operation["inputs"]["inputs"]["admitted_baseline"]
    assert "REQ-OLD" in baseline["historical_requirements"] and "REQ-OLD" not in baseline["active_requirements"]
