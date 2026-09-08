from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_contract import EngineRequestContract, prepare_contract
from auto_agents.repair_control import digest


EXISTING = "tests/test_existing.py::test_existing"
PLANNED = "tests/test_planned.py::TestCoverage::test_missing"


def plan(nodes, reason):
    route = {"issue_seed": {"required_behavior": ["complete coverage"]}}
    data = {"kind": "engine_request_contract", "route_digest": digest(route), "revision": "revision",
            "checks": [{"obligation": "complete coverage", "nodeids": nodes, "reason": reason}]}
    return route, data


@pytest.mark.parametrize("existing", [[], [EXISTING]])
def test_explicit_planned_nodes_are_required_even_with_partial_existing_coverage(existing):
    route, data = plan(existing, f"Missing coverage requires `{PLANNED}`. Implement {PLANNED} before reuse.")
    original = copy.deepcopy(data)
    contract = EngineRequestContract.from_dict(data, route)
    assert contract.checks[0]["nodeids"] == [*existing, PLANNED]
    assert contract.checks[0]["reason"] == data["checks"][0]["reason"]
    assert data == original
    assert EngineRequestContract.from_dict(contract.to_dict(), route).to_dict() == contract.to_dict()
    assert contract.final.verification_commands == [shlex.join(["python", "-m", "pytest", "-q", *existing, PLANNED])]
    assert contract.final.verdict == "UNVERIFIED" and not contract.to_dict()["repair_approved"]


@pytest.mark.parametrize("reference", ["add a missing test", "/outside/" + PLANNED,
    "../" + PLANNED, PLANNED + "[case]", PLANNED + "::extra", PLANNED + ".extra"])
def test_explanations_cannot_invent_nodes_or_turn_external_or_partial_paths_into_checks(reference):
    route, data = plan([], "Missing coverage: " + reference)
    with pytest.raises(ValueError, match="needs test node IDs"):
        EngineRequestContract.from_dict(data, route)


def test_unsafe_structured_nodes_are_not_rescued_by_valid_explanation():
    route, data = plan(["tests/test_existing.py::test_existing; touch /tmp/unwanted"], "Add " + PLANNED)
    with pytest.raises(ValueError, match="repository-local"):
        EngineRequestContract.from_dict(data, route)


def test_normalization_preserves_raw_output_and_cache_without_another_model_call(tmp_path):
    route, data = plan([], "Implement " + PLANNED + " for complete coverage.")
    raw = json.dumps({"checks": data["checks"]})
    calls = []

    def respond(request):
        calls.append(request)
        request.output_path.write_text(raw)
        return SimpleNamespace(ok=True, summary=raw)

    orchestrator = SimpleNamespace(config=SimpleNamespace(efforts={}), _call_with_failover=respond)
    with patch("auto_agents.orchestrator.Orchestrator", return_value=orchestrator):
        first = prepare_contract({"invocation": {"engine_route": route}}, "revision", tmp_path, tmp_path, tmp_path)
        second = prepare_contract({"invocation": {"engine_route": route}}, "revision", tmp_path, tmp_path, tmp_path)
    assert len(calls) == 1
    assert calls[0].output_path.read_text() == raw
    assert first.to_dict() == second.to_dict()
    assert first.checks[0]["nodeids"] == [PLANNED]


def test_missing_planned_node_still_prevents_successful_verification(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_existing.py").write_text("def test_existing():\n    assert True\n")
    route, data = plan([EXISTING], "The existing test is partial; implement " + PLANNED + ".")
    contract = EngineRequestContract.from_dict(data, route)
    command = shlex.split(contract.final.verification_commands[0])
    result = subprocess.run([sys.executable, *command[1:]], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "test_planned.py" in result.stderr
    assert not contract.to_dict()["repair_approved"]
