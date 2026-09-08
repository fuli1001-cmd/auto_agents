from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_client import EngineRepairRequired, triage_engine_request
from auto_agents.repair_contract import EngineRequestContract, prepare_contract, with_request_contract
from auto_agents.repair_control import Store, digest, git
from test_repair_control import configuration, make_remote


def route(engine):
    return {"issue_seed": {"target_repository": str(engine), "scope": "engine routing",
            "required_behavior": ["Engine routes precede unrelated product run preflight."]}}


def contract_data(request, revision="latest"):
    return {"kind": "engine_request_contract", "route_digest": digest(request), "revision": revision,
            "checks": [{"obligation": request["issue_seed"]["required_behavior"][0],
                        "nodeids": ["tests/test_routes.py::test_engine_route"],
                        "reason": "Asserts the engine request cannot read the unrelated pending run."}]}


@pytest.mark.parametrize("problem", ["disabled", "unregistered", "foreign", "conflicting", "unauthorized"])
def test_control_signal_does_not_grant_authority(tmp_path, monkeypatch, problem):
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", raising=False)
    request = route(tmp_path / "engine")
    orch = SimpleNamespace(_repair_registration={"config": {"source_root": str(tmp_path / "engine")}},
                           _invocation_context={"auto_approve": True})
    if problem == "disabled":
        monkeypatch.setenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", "1")
    elif problem == "unregistered":
        orch._repair_registration = None
    elif problem == "foreign":
        request = route(tmp_path / "other")
    elif problem == "conflicting":
        request["target_repository"] = str(tmp_path / "other")
    else:
        orch._invocation_context = {}
        request["auto_approve"] = True  # A model-supplied field is not authority.
    result = triage_engine_request(orch, tmp_path, EngineRepairRequired(request))
    assert not result.decision.eligible
    assert result.root_cause is None


def test_explicit_request_skips_models_and_ambient_run(tmp_path, monkeypatch):
    from auto_agents.cli import _triage_terminal_run_error
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", raising=False)
    orch = SimpleNamespace(_repair_registration={"config": {"source_root": str(tmp_path / "engine")}},
                           _invocation_context={"auto_approve": True})
    with patch("auto_agents.cli.adjudicate_auto_agents_error", side_effect=AssertionError("model triage")), \
         patch("auto_agents.cli._try_load_run_state", side_effect=AssertionError("ambient run")):
        result = _triage_terminal_run_error(tmp_path, orch, EngineRepairRequired(route(tmp_path / "engine")))
    assert result.decision.eligible and result.decision.requires_candidate_proof
    assert result.root_cause is None
    assert triage_engine_request(orch, tmp_path, RuntimeError("ordinary error")) is None


@pytest.mark.parametrize("command", ["collab", "resume"])
def test_original_cli_resume_creates_supervisor_job_without_model_triage(tmp_path, monkeypatch, command):
    from auto_agents.cli import main
    from auto_agents.config import load_run_state, save_run_state, save_session_state
    from auto_agents.models import SessionState
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
    from test_session import _make_project, _confirm_collab_state
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", raising=False)
    config = configuration(tmp_path)
    engine = make_remote(config)
    root = _make_project(str(tmp_path))
    run = load_run_state(root)
    run.status, run.current_stage, run.active_blocker = "pending", "implement", {}
    save_run_state(root, run)
    before = (root / ".auto-agents/state/run_state.json").read_bytes()
    workflows = WorkflowStore(root)
    snapshot = workflows.create_root(WorkflowRef("collab", "parent"))
    handoff = workflows.prepare_handoff(snapshot, parent=snapshot.root, target="fix",
        goal="continue original project", reason="engine repair", payload=route(engine))
    state = _confirm_collab_state(SessionState(session_id="parent", mode="collab", status="waiting_child",
        workflow_id=snapshot.workflow_id, active_handoff_id=handoff.handoff_id, auto_approve=True))
    from auto_agents.authorization import authorization_policy_for_state
    state.authorization_policy = authorization_policy_for_state(auto_approve=True).to_dict()
    save_session_state(root, state)
    store = Store(config["root"])
    submitted = []
    def transport(config, request, fds=None):
        if request["op"] == "register":
            return {"subscriber": store.register(request["payload"])}
        if request["op"] == "submit":
            job = store.submit(request["subscriber"], request["payload"])
            submitted.append(job)
            # No worker/model is started by this transport test. A durable
            # blocked result ends the actual foreground waiting relay.
            store.transition(job, "blocked", {"error": "offline test worker"})
            return {"job": job}
        if request["op"] == "status":
            return {"job": store.job(submitted[-1]), "subscribers": store.subscriptions(submitted[-1]),
                    "registered": [request["subscriber"]]}
        if request["op"] == "finish":
            return {"ok": True}
        raise AssertionError(request["op"])
    argv = (["collab", "--session", "parent", "--provider", "codex", "--auto-approve"] if command == "collab"
            else ["resume", "--workflow", snapshot.workflow_id]) + ["--project", str(root), "--no-health-watch"]
    with patch("auto_agents.repair_client.configure", return_value=config), \
         patch("auto_agents.repair_client.ensure_supervisor"), \
         patch("auto_agents.repair_client.rpc", side_effect=transport), \
         patch("auto_agents.repair_client.git", return_value="installed"), \
         patch.object(Orchestrator, "_call_with_failover", side_effect=AssertionError("unexpected model call")), \
         patch("auto_agents.cli.adjudicate_auto_agents_error", side_effect=AssertionError("unexpected triage")), \
         patch("auto_agents.cli._load_cli_dotenv"), patch("auto_agents.cli._safe_notify"):
        assert main(argv) == 3
    assert len(submitted) == 1
    job = store.job(submitted[0])
    payload = job["payload"]
    assert payload["invocation"]["session_id"] == "parent"
    assert payload["invocation"]["workflow_id"] == snapshot.workflow_id
    assert payload["boundary"]["kind"] == "engine_route"
    assert payload["diagnosis"] == {"kind": "engine_request_contract_required", "repair_approved": False}
    # An older bootstrapped worker may run when upstream fetch falls back to
    # cache. It must reject the marker, never enter its diagnosis=None bypass.
    from auto_agents.root_cause import RootCauseDiagnosis
    with pytest.raises(ValueError):
        RootCauseDiagnosis.from_dict(payload["diagnosis"])
    assert payload["contract"]["engine_request"] == route(engine)
    assert "--session" in payload["resume_argv"] if command == "collab" else "--workflow" in payload["resume_argv"]
    assert (root / ".auto-agents/state/run_state.json").read_bytes() == before
    assert workflows.load_handoff(handoff.handoff_id).child is None


def test_request_contract_never_claims_diagnosis_or_approval(tmp_path):
    request = route(tmp_path / "engine")
    contract = EngineRequestContract.from_dict(contract_data(request), request)
    assert contract.final.verification_commands == ["python -m pytest -q tests/test_routes.py::test_engine_route"]
    assert contract.final.expected_postconditions == request["issue_seed"]["required_behavior"]
    assert not contract.to_dict()["repair_approved"]
    assert contract.final.verdict == "UNVERIFIED"
    payload = {"invocation": {"engine_route": request}}
    assert with_request_contract(payload, {"request_contract": contract.to_dict()})["request_contract"]
    with pytest.raises(ValueError, match="no frozen"):
        with_request_contract(payload, {})
    with pytest.raises(ValueError, match="does not match"):
        with_request_contract({"invocation": {"engine_route": route(tmp_path / "different")}},
                              {"request_contract": contract.to_dict()})


@pytest.mark.parametrize("invalid", ["omitted", "rewritten", "shell", "outside", "flag", "diagnostic"])
def test_acceptance_contract_rejects_missing_or_unsafe_checks(tmp_path, invalid):
    request = route(tmp_path / "engine")
    data = contract_data(request)
    if invalid == "omitted":
        data["checks"] = []
    elif invalid == "rewritten":
        data["checks"][0]["obligation"] = "unrelated passing check"
    else:
        data["checks"][0]["nodeids"] = [{"shell": "tests/test_a.py::test_a; touch /tmp/a",
            "outside": "/other/tests/test_a.py::test_a", "flag": "--override-ini=addopts=",
            "diagnostic": "git diff --check"}[invalid]]
    with pytest.raises(ValueError):
        EngineRequestContract.from_dict(data, request)


def test_acceptance_planning_is_progress_managed_read_only_and_cached(tmp_path):
    request = route(tmp_path / "engine")
    payload = {"invocation": {"engine_route": request}, "provider": "codex"}
    orch = SimpleNamespace(config=SimpleNamespace(efforts={}), _set_active_provider=lambda p: None)
    calls = []
    def plan(agent):
        calls.append(agent)
        assert agent.sandbox_mode == "read-only" and agent.timeout_seconds == 0
        assert agent.progress_managed_timeout
        assert not agent.record_execution_incidents
        return SimpleNamespace(ok=True, summary=json.dumps({"checks": contract_data(request)["checks"]}))
    orch._call_with_failover = plan
    with patch("auto_agents.orchestrator.Orchestrator", return_value=orch):
        first = prepare_contract(payload, "sha", tmp_path, tmp_path, tmp_path)
        second = prepare_contract(payload, "sha", tmp_path, tmp_path, tmp_path)
    assert first.to_dict() == second.to_dict()
    assert len(calls) == 1


@pytest.mark.parametrize("progressing", [True, False])
def test_acceptance_planning_obeys_progress_instead_of_provider_deadline(tmp_path, progressing):
    from auto_agents.adapters.base import run_subprocess_with_optional_streaming
    from auto_agents.adapters.codex import CodexProgressDecoder
    from auto_agents.models import SmartTimeoutConfig

    request = route(tmp_path / "engine")
    payload = {"invocation": {"engine_route": request}, "provider": "codex"}
    orch = SimpleNamespace(config=SimpleNamespace(efforts={}), _set_active_provider=lambda p: None)
    checks = contract_data(request)["checks"]
    # Scale the provider deadline to one second. Distinct completed inspections
    # renew progress; status messages alone must not keep planning alive.
    script = """
import json, sys, time
progressing = sys.argv[1] == 'True'
for index in range(8):
    if progressing:
        item = {'id': str(index), 'type': 'command_execution',
                'command': 'read evidence ' + str(index),
                'aggregated_output': 'evidence ' + str(index), 'exit_code': 0}
    else:
        item = {'id': str(index), 'type': 'agent_message', 'text': 'Still reviewing'}
    print(json.dumps({'type': 'item.completed', 'item': item}), flush=True)
    time.sleep(0.4)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'final', 'type': 'agent_message', 'text': sys.argv[2]}}), flush=True)
"""
    def plan(agent):
        # Production leases have a 60-second minimum; shorten only that lease
        # for this subprocess regression, leaving event decoding and polling real.
        with patch("auto_agents.supervision.ProgressSupervisor._effective_progress_lease_seconds", return_value=2):
            result = run_subprocess_with_optional_streaming(
                [sys.executable, "-c", script, str(progressing), json.dumps({"checks": checks})],
                agent, dict(os.environ), timeout=1,
                smart_timeout=SmartTimeoutConfig(
                    provider_idle_seconds=60, tool_idle_seconds=60,
                    semantic_stall_seconds=60, safety_ceiling_seconds=60),
                progress_decoder=CodexProgressDecoder(), provider="codex")
        if progressing:
            assert result.returncode == 0
            assert result.termination is None
        else:
            assert result.returncode == -1
            assert result.termination.reason == "semantic_stall"
        summary = json.loads(result.stdout.splitlines()[-1])["item"].get("text", "")
        return SimpleNamespace(ok=result.returncode == 0, summary=summary)

    orch._call_with_failover = plan
    with patch("auto_agents.orchestrator.Orchestrator", return_value=orch):
        if progressing:
            contract = prepare_contract(payload, "sha", tmp_path, tmp_path, tmp_path)
            assert contract.checks == checks
        else:
            with pytest.raises(RuntimeError, match="acceptance planning failed"):
                prepare_contract(payload, "sha", tmp_path, tmp_path, tmp_path)
            assert not list(tmp_path.glob("request-contract-*.json"))


@pytest.mark.parametrize("behavior,boundary,expected", [
    ("assert True", True, True), ("assert False", True, False),
    ("assert True", False, False), ("pytest.skip('not exercised')", True, False),
])
@pytest.mark.parametrize("differential", [False, True])
def test_no_change_reuse_needs_real_acceptance_tests_and_boundary(tmp_path, behavior, boundary, expected, differential):
    from auto_agents.repair_worker import check_revision
    engine = make_remote(configuration(tmp_path))
    (engine / "tests").mkdir()
    (engine / "tests/test_routes.py").write_text("import pytest\ndef test_engine_route():\n    " + behavior + "\n")
    request = route(engine)
    contract = EngineRequestContract.from_dict(contract_data(request), request)
    def verification(commands, root):
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_routes.py::test_engine_route"],
                                cwd=root, capture_output=True, text=True)
        return SimpleNamespace(ok=result.returncode == 0, returncodes=[result.returncode], summary=result.stdout + result.stderr)
    runner = SimpleNamespace(_invocation_context={"engine_route": request}, diagnosis=contract,
        _load_or_create_experiment=lambda: (None, None),
        _diagnosis_differential=lambda *a: SimpleNamespace(ok=differential, summary="base/upstream behavior"),
        _run_verification_commands=verification,
        _replay_candidate=lambda *a: SimpleNamespace(ok=boundary, summary="isolated original boundary"))
    assert check_revision(runner, engine, git(engine, "rev-parse", "HEAD"))[0] is expected


@pytest.mark.parametrize("satisfied,full_pass", [(True, True), (False, True), (True, False)])
def test_worker_fetches_then_plans_then_checks_before_candidate_generation(tmp_path, satisfied, full_pass):
    from auto_agents.repair_worker import repair
    config = configuration(tmp_path)
    engine = make_remote(config)
    project = tmp_path / "project"
    project.mkdir()
    (project / "input.txt").write_text("preserve me")
    base = git(engine, "rev-parse", "HEAD")
    request = route(engine)
    payload = {"project": str(project), "base": base, "fingerprint": "request", "autonomy": "max",
               "invocation": {"engine_route": request}}
    events = []
    def plan(payload, revision, checkout, evidence, directory):
        assert git(checkout, "rev-parse", "HEAD") == base
        assert (Path(config["root"]) / "engine.git/FETCH_HEAD").exists()
        events.append("plan")
        return EngineRequestContract.from_dict(contract_data(request, revision), request)
    class Oracle:
        def _full_suite_differential(self, *args):
            events.append("full")
            return SimpleNamespace(ok=full_pass, recoverable=False, summary="full proof")
        def run(self):
            events.append("generate")
            return SimpleNamespace(ok=False, reason="candidate not yet proven", to_dict=lambda: {})
    def checked(*args):
        events.append("check")
        return satisfied, "actual acceptance and boundary outcome"
    with patch("auto_agents.repair_worker.engine_environment", return_value=(sys.executable, "env")), \
         patch("auto_agents.repair_worker.execute_selected_worker"), \
         patch("auto_agents.verification_sandbox.check_verification_sandbox"), \
         patch("auto_agents.root_cause.RootCauseCoordinator._copy_diagnostic_tree", side_effect=shutil.copytree), \
         patch("auto_agents.repair_contract.prepare_contract", side_effect=plan), \
         patch("auto_agents.repair_worker.import_legacy_experiment"), \
         patch("auto_agents.repair_worker.carry_continuous_work"), \
         patch("auto_agents.repair_worker.make_runner", return_value=Oracle()), \
         patch("auto_agents.repair_worker.check_revision", side_effect=checked):
        result = repair({"config": config, "job": {"id": "request", "payload": payload}})
    assert events == (["plan", "check", "full"] if satisfied else ["plan", "check", "generate"])
    assert result["ok"] is (satisfied and full_pass)
    if satisfied and full_pass:
        assert result["status"] == "already_repaired"
        assert result["request_contract"]["route_digest"] == digest(request)
    assert (project / "input.txt").read_text() == "preserve me"


def test_worker_cannot_use_legacy_no_diagnosis_bypass(tmp_path):
    from auto_agents.repair_worker import make_runner, execute_selected_worker
    from test_session import _make_project
    project = _make_project(str(tmp_path))
    request = route(tmp_path / "engine")
    payload = {"invocation": {"engine_route": request}}
    with pytest.raises(ValueError, match="does not match"):
        make_runner(payload, tmp_path, project, sys.executable)
    with pytest.raises(RuntimeError, match="refusing legacy"):
        execute_selected_worker({"job": {"payload": payload}}, tmp_path, sys.executable)


@pytest.mark.skipif(shutil.which("codex") is None, reason="local verification sandbox not installed")
@pytest.mark.parametrize("matching", [True, False])
def test_real_sandbox_replay_consumes_only_the_original_engine_route(tmp_path, matching):
    from auto_agents.config import save_session_state
    from auto_agents.models import SessionState
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner, auto_agents_repo_root
    from auto_agents.workflow_chain import WorkflowStore, WorkflowRef
    from test_session import _make_project, _confirm_collab_state, _configure_git_identity
    project = _make_project(str(tmp_path))
    _configure_git_identity(project)
    git(project, "add", "-A")
    git(project, "commit", "-m", "fixture")
    request = route(tmp_path / "engine")
    store = WorkflowStore(project)
    snapshot = store.create_root(WorkflowRef("collab", "parent"))
    handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target="fix", goal="continue",
                                   reason="engine repair", payload=request)
    state = _confirm_collab_state(SessionState(session_id="parent", mode="collab", status="waiting_child",
        workflow_id=snapshot.workflow_id, active_handoff_id=handoff.handoff_id, auto_approve=True))
    save_session_state(project, state)
    before = (project / ".auto-agents/state/sessions/parent/session_state.json").read_bytes()
    from auto_agents.root_cause import RootCauseCoordinator
    frozen = tmp_path / "frozen"
    RootCauseCoordinator._copy_diagnostic_tree(project, frozen)
    runner = AutoAgentsSelfRepairRunner.__new__(AutoAgentsSelfRepairRunner)
    runner.target_project_root = frozen
    runner._real_project_root = project
    runner._invocation_context = {"command": "collab", "session_id": "parent",
                                  "engine_route": request if matching else route(tmp_path / "different")}
    runner._verification_python = lambda: sys.executable
    runner._autonomy_config = lambda: SimpleNamespace(replay_timeout_seconds=60)
    result = runner._session_probe(auto_agents_repo_root())
    assert result.get("route_consumed") is matching, result
    assert result["ok"] is matching, result
    assert (project / ".auto-agents/state/sessions/parent/session_state.json").read_bytes() == before
