"""Unverified acceptance plans for explicitly authorized engine work.

These are not root-cause diagnoses. They adapt to the runner's proof interface
without fabricating investigator/reviewer approval or enabling legacy bypasses.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex

from .repair_control import atomic_json, digest
from .root_cause import RootCauseReport


def obligations(route):
    result = []
    for seed in (route, route.get("issue_seed", {}), route.get("spec_seed", {})):
        if not isinstance(seed, dict):
            continue
        for name in ("required_behavior", "requirements", "acceptance", "verification"):
            values = seed.get(name, [])
            if isinstance(values, list):
                result.extend(value.strip() for value in values if isinstance(value, str) and value.strip())
    if not result:
        raise ValueError("engine request has no explicit acceptance obligations")
    return list(dict.fromkeys(result))


@dataclass
class EngineRequestContract:
    route: dict
    revision: str
    checks: list

    @property
    def final(self):
        nodes = list(dict.fromkeys(node for check in self.checks for node in check["nodeids"]))
        return RootCauseReport(
            role="request", verdict="UNVERIFIED", owner="auto_agents", confidence=0.0,
            category="explicit_engine_request", generic=False, safe_to_repair=False,
            safe_to_attempt=False, causal_chain=["Authorized work request; no defect or successful repair is presumed."],
            evidence=[], expected_postconditions=obligations(self.route), failure_scope="session",
            proposed_fix_scope=[str(seed.get("scope", seed.get("summary", "")))
                                for seed in (self.route.get("issue_seed", {}), self.route.get("spec_seed", {}))
                                if isinstance(seed, dict)],
            verification_commands=[shlex.join(["python", "-m", "pytest", "-q", *nodes])],
            reproduction_outcome="Unverified acceptance plan, not a root-cause verdict.",
            resume_strategy="verify_before_resume")

    def to_dict(self):
        return {"kind": "engine_request_contract", "route_digest": digest(self.route),
                "revision": self.revision, "checks": self.checks, "repair_approved": False,
                "final": self.final.to_dict()}

    @classmethod
    def from_dict(cls, data, route):
        if data.get("kind") != "engine_request_contract" or data.get("route_digest") != digest(route):
            raise ValueError("engine acceptance contract does not match the requested route")
        required = obligations(route)
        checks = data.get("checks")
        if not isinstance(checks, list) or len(checks) != len(required):
            raise ValueError("every engine request obligation needs a verification mapping")
        for index, check in enumerate(checks):
            if not isinstance(check, dict) or check.get("obligation") != required[index]:
                raise ValueError("engine acceptance obligations cannot be omitted or rewritten")
            nodes = check.get("nodeids")
            if not isinstance(nodes, list) or not nodes or not isinstance(check.get("reason"), str) or not check["reason"].strip():
                raise ValueError("engine acceptance check needs test node IDs and a coverage explanation")
            for node in nodes:
                # No shell, pytest options, external paths, or diagnostic-only
                # commands. New tests may be planned, but cannot prove reuse.
                if (not isinstance(node, str) or not re.fullmatch(
                        r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py::[A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)?", node)):
                    raise ValueError("engine acceptance requires repository-local pytest node IDs")
        return cls(route, str(data.get("revision", "")), checks)


def prepare_contract(payload, revision, checkout, evidence, directory):
    """One bounded read-only planning call after fetch, cached by route + SHA."""
    from .models import AgentRequest
    from .orchestrator import Orchestrator
    route = payload["invocation"]["engine_route"]
    required = obligations(route)
    key = digest([route, revision])
    receipt = directory / ("request-contract-" + key[:24] + ".json")
    if receipt.exists():
        return EngineRequestContract.from_dict(json.loads(receipt.read_text()), route)
    orchestrator = Orchestrator(evidence)
    if payload.get("provider"):
        orchestrator._set_active_provider(payload["provider"])
    output = receipt.with_suffix(".output.json")
    prompt = (
        "Plan acceptance checks for an ALREADY AUTHORIZED engine work request. "
        "This is NOT terminal-error attribution: EngineRepairRequired is an intentional handoff signal. "
        "Do not decide whether that signal proves a bug, and do not claim the request is fixed. "
        "Inspect the selected upstream checkout read-only. Do not edit files, run tests, invoke providers, "
        "or change project state. Use at most 8 focused inspection commands, no broad /tmp searches. "
        "For EVERY exact obligation below select existing repository-local pytest node IDs whose assertions "
        "actually establish it. If coverage is missing, name the new test that must be implemented; "
        "a missing test will prevent upstream reuse. Explain the coverage for each mapping. "
        "Do not substitute a general passing suite or a synthetic route receipt for requested behavior. "
        "Return JSON only: {\"checks\":[{\"obligation\":\"exact input text\","
        "\"nodeids\":[\"tests/test_example.py::test_behavior\"],\"reason\":\"assertions or missing coverage\"}]}.\n"
        + json.dumps({"upstream": revision, "request": route, "obligations": required,
                      "frozen_evidence": str(evidence)}, ensure_ascii=False)
    )
    result = orchestrator._call_with_failover(AgentRequest(
        stage="self_repair_contract", purpose="diagnosis",
        effort=orchestrator.config.efforts.get("self_repair", "deep"),
        prompt=prompt, cwd=checkout, output_path=output, sandbox_mode="read-only",
        timeout_seconds=180, progress_managed_timeout=False, record_execution_incidents=False))
    if not result.ok:
        raise RuntimeError("engine acceptance planning failed; request retained for explicit resume")
    raw = (result.summary or result.stdout or (output.read_text() if output.exists() else "")).strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    data = json.loads(raw)
    data.update(kind="engine_request_contract", route_digest=digest(route), revision=revision)
    contract = EngineRequestContract.from_dict(data, route)
    atomic_json(receipt, contract.to_dict())
    return contract


def with_request_contract(payload, result):
    """Carry the same frozen contract through subscriber and publication proof."""
    if not payload.get("invocation", {}).get("engine_route"):
        return payload
    data = result.get("request_contract") or payload.get("request_contract")
    if not data:
        raise ValueError("engine request has no frozen acceptance contract")
    EngineRequestContract.from_dict(data, payload["invocation"]["engine_route"])
    return {**payload, "request_contract": data}
