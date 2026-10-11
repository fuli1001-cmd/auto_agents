"""Snapshot evidence retains admitted provenance and exact browser inputs."""

import copy
import json
from pathlib import Path

import pytest

from auto_agents.control import ControlError
from auto_agents.control import prototype_scope as scope
from auto_agents.control.workspace import git
from test_control_prototype_contract import HTML, Iteration, plan, setup, write_package


@pytest.fixture
def admission(tmp_path):
    return {
        "has_frontend": True,
        "original": scope.encoded({"home.html": HTML.encode(), "manifest.json": b"{}"}),
        "history": {},
        "active_requirements": ["REQ-001"],
        "permissions": "Add retry only.",
    }, {".auto-agents/docs/requirements_trace.json": "frozen"}, tmp_path / "source-id/original"


@pytest.mark.parametrize("target", ["repair", "change", "flow"])
def test_exact_snapshot_files_are_admitted_evidence_without_rewriting_receipts(admission, target):
    baseline, inputs, original = admission
    value = plan()
    if target == "repair":
        row = {"id": "restore", "flow_ids": ["smart-duration"], "reason": "Restore existing duration"}
        value["repairs"].append(row)
    elif target == "change":
        row = value["changes"][0]
    else:
        row = value["flows"][0]
        row["requirement_ids"] = []
    row["evidence"] = [str(original / "home.html")]
    untouched = copy.deepcopy(value)
    assert scope.check_schema(value, baseline, inputs, original_path=original) == untouched
    assert value == untouched


@pytest.mark.parametrize("ref", [
    "/tmp/other-source/original/home.html", "../original/home.html",
    "{root}/missing.html", "{root}/../original/home.html",
    "{root}/home.html?state=ready", "{root}/home.html#source",
    "{root}", "{root}/../../source.py", "{root}/../../repaired/home.html",
    {"path": "home.html"},
])
def test_snapshot_evidence_cannot_admit_other_paths_or_malformed_refs(admission, ref):
    baseline, inputs, original = admission
    if isinstance(ref, str):
        ref = ref.format(root=original)
    value = plan()
    value["flows"][0]["evidence"] = [ref]
    with pytest.raises(ControlError, match="Scope evidence must identify") as caught:
        scope.check_schema(value, baseline, inputs, original_path=original)
    assert caught.value.details["record"] == "smart-duration"
    assert caught.value.details["invalid_evidence"] == [ref]


def test_snapshot_frontend_evidence_still_requires_preservation_checks(admission):
    baseline, inputs, original = admission
    value = plan()
    value["flows"][0]["evidence"] = [str(original / "home.html")]
    value["checks"][0]["kind"] = "new"
    with pytest.raises(ControlError, match="Existing flows need preservation checks"):
        scope.check_schema(value, baseline, inputs, original_path=original)


def test_an_absolute_path_needs_the_controller_supplied_snapshot_binding(admission):
    baseline, inputs, original = admission
    value = plan()
    value["flows"][0]["evidence"] = [str(original / "home.html")]
    with pytest.raises(ControlError, match="Scope evidence must identify"):
        scope.check_schema(value, baseline, inputs)


def test_missing_checkbox_input_identifies_the_exact_check_and_step(admission):
    baseline, inputs, _ = admission
    value = plan()
    value["checks"][0]["steps"] = [{"action": "check", "selector": "#manual"}]
    with pytest.raises(ControlError, match="Browser action needs its frozen input") as caught:
        scope.check_schema(value, baseline, inputs)
    assert caught.value.details == {
        "check": "smart-duration", "step": 1, "action": "check", "selector": "#manual",
    }


@pytest.mark.parametrize("broken", [False, True])
def test_structural_repairs_use_snapshot_evidence_and_actual_manifest_validation(tmp_path, monkeypatch, broken):
    class SnapshotEvidence(Iteration):
        def run(self, ctx, phase, prompt, *args, **kwargs):
            result = super().run(ctx, phase, prompt, *args, **kwargs)
            if phase == "prototype_prepare":
                owned, _ = json.JSONDecoder().raw_decode(prompt.split("Owned inputs:\n", 1)[1])
                self.admitted_errors = owned["admitted_baseline"]["original_manifest_errors"]
                original = Path(owned["original_path"])
                write_package(ctx.workspace_root, scope.PREPARED)
                reply = json.loads(result["text"])
                reply["scope"]["repairs"] = [{
                    "id": "repair-package-structure", "flow_ids": ["package-structure"],
                    "evidence": [str(original / "manifest.json")],
                    "reason": "Repair admitted invalid manifest refs", "files": ["manifest.json"],
                }]
                reply["scope"]["flows"][0].update(
                    requirement_ids=[], evidence=[str(original / "home.html")],
                )
                result["text"] = json.dumps(reply)
            return result

    worker = SnapshotEvidence()
    root, engine = setup(tmp_path, worker)
    if broken:
        path = root / scope.CANONICAL / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["index_ref"] = scope.CANONICAL + "/missing.html"
        path.write_text(json.dumps(manifest))
        git(root, "add", "-f", scope.CANONICAL)
        git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Invalid admitted manifest")
    if broken:
        check_schema = scope.check_schema
        with monkeypatch.context() as previous_validator:
            previous_validator.setattr(
                scope, "check_schema",
                lambda value, baseline, inputs, **kwargs: check_schema(value, baseline, inputs),
            )
            blocked = engine.start("run", "Add retry")
        assert blocked["status"] == "BLOCKED" and "Scope evidence must identify" in blocked["failure"]["message"]
        prepare_calls = len([phase for _, phase in worker.calls if phase == "prototype_prepare"])
        retained = next(op for op in engine.store.operations(blocked["id"]) if not op["consumed"])
        native_payload = copy.deepcopy(engine.store.outcome(retained["id"])["payload"])
        work = engine.resume(blocked["id"])
        assert len([phase for _, phase in worker.calls if phase == "prototype_prepare"]) == prepare_calls
        assert engine.store.outcome(retained["id"])["payload"] == native_payload
        assert next(op for op in engine.store.operations(work["id"]) if op["id"] == retained["id"])["consumed"]
        assert work["status"] == "WAITING" and work["context"]["approval"] == "prototype", work["failure"]
        frozen = scope.get(engine.store, "scope", work["context"]["prototype_scope"])
        evidence = frozen["scope"]["repairs"][0]["evidence"][0]
        assert evidence.endswith("/original/manifest.json") and Path(evidence).is_absolute()
        scope.require_certificate(engine, work, engine.context(work), scope.package(engine.context(work).workspace_root, scope.CANONICAL))
    else:
        work = engine.start("run", "Add retry")
        assert work["status"] == "BLOCKED" and work["phase"] == "prototype_prepare"
        assert work["failure"]["message"] == "Package-structure repair requires an invalid admitted manifest"
        assert work["failure"]["details"]["record"] == "repair-package-structure"
        assert all(op["model"] for op in engine.store.operations(work["id"]))
    assert bool(worker.admitted_errors) == broken
