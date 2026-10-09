"""Every retained public behavior has a concrete, executable regression mapping."""

import ast
import json
from pathlib import Path


def test_behavior_contract_inventory_has_live_public_regressions():
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads((root / "schemas/behavior_contracts.json").read_text())
    assert inventory["version"] == 1
    identities = set()
    for contract in inventory["contracts"]:
        assert contract["id"] not in identities
        identities.add(contract["id"])
        assert contract["trigger"] and contract["expected"]
        assert contract["status"] in {"retained", "changed", "retired"}
        if contract["status"] != "retained":
            assert contract["rationale"]
        assert contract["tests"]
        for node in contract["tests"]:
            module, function = node.split("::")
            tree = ast.parse((root / module).read_text())
            assert function in {
                n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
            }, node
