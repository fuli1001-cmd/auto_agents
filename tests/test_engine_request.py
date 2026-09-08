from __future__ import annotations

import json
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


def test_acceptance_planning_is_bounded_read_only_and_cached(tmp_path):
    request = route(tmp_path / "engine")
    payload = {"invocation": {"engine_route": request}, "provider": "codex"}
    orch = SimpleNamespace(config=SimpleNamespace(efforts={}), _set_active_provider=lambda p: None)
    calls = []
    def plan(agent):
        calls.append(agent)
        assert agent.sandbox_mode == "read-only" and agent.timeout_seconds == 180
        assert not agent.progress_managed_timeout
        assert not agent.record_execution_incidents
        return SimpleNamespace(ok=True, summary=json.dumps({"checks": contract_data(request)["checks"]}))
    orch._call_with_failover = plan
    with patch("auto_agents.orchestrator.Orchestrator", return_value=orch):
        first = prepare_contract(payload, "sha", tmp_path, tmp_path, tmp_path)
        second = prepare_contract(payload, "sha", tmp_path, tmp_path, tmp_path)
    assert first.to_dict() == second.to_dict()
    assert len(calls) == 1


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


@pytest.mark.parametrize("problem", ["disabled", "unregistered", "disconnected"])
def test_unavailable_engine_channel_blocks_repeated_requests_without_children_or_providers(tmp_path, monkeypatch, problem):
    from auto_agents.config import load_run_state, save_run_state
    from auto_agents.models import SessionState
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.workflow_runtime import WorkflowCoordinator
    from test_session import _make_project
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", raising=False)
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_ROUTE_PROBE", raising=False)
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_SUBSCRIBER", "resuming-subscriber")
    project = _make_project(str(tmp_path))
    run = load_run_state(project)
    run.status, run.current_stage, run.active_blocker = "pending", "implement", {}
    save_run_state(project, run)
    before = (project / ".auto-agents/state/run_state.json").read_bytes()
    orch = Orchestrator(project)
    orch._repair_registration = {"config": {"source_root": str(tmp_path / "engine")}}
    if problem == "disabled":
        monkeypatch.setenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", "1")
    elif problem == "unregistered":
        orch._repair_registration = None
    coordinator = WorkflowCoordinator(orch)
    session = Session(orch, mode="collab", coordinator=coordinator)
    state = SessionState(session_id="parent", mode="collab", status="executing")
    with patch("auto_agents.repair_client.rpc", side_effect=OSError("channel closed")) as transport, \
         patch.object(coordinator, "prepare_run_route", side_effect=AssertionError("ambient run preflight")), \
         patch.object(coordinator, "start_seeded_session", side_effect=AssertionError("child")), \
         patch.object(orch, "_call_with_failover", side_effect=AssertionError("provider")):
        for _ in range(3):
            result = session._prepare_workflow_handoff(state, target="run", reason="engine", payload=route(tmp_path / "engine"))
            assert result.status == "blocked" and result.resolution == "execution_binding_mismatch"
            assert result.execution_log[-1]["retry_fix"] is False
            assert not result.active_handoff_id
        assert transport.call_count == (1 if problem == "disconnected" else 0)
    assert (project / ".auto-agents/state/run_state.json").read_bytes() == before


def test_conflicting_engine_targets_cannot_consume_a_receipt(tmp_path, monkeypatch):
    from auto_agents.repair_client import engine_route
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", raising=False)
    monkeypatch.setenv("AUTO_AGENTS_REPAIR_SUBSCRIBER", "subscriber")
    orch = SimpleNamespace(_repair_registration={"config": {"source_root": str(tmp_path / "engine")}})
    request = {**route(tmp_path / "engine"), "target_repository": str(tmp_path / "foreign")}
    with patch("auto_agents.repair_client.rpc", side_effect=AssertionError("must validate before receipt")):
        assert engine_route(orch, request) is False


def test_nested_engine_target_uses_the_same_admission_and_execution_owner(tmp_path, monkeypatch):
    from auto_agents.repair_client import engine_route
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", raising=False)
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_ROUTE_PROBE", raising=False)
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_SUBSCRIBER", raising=False)
    engine = tmp_path / "engine"
    orch = SimpleNamespace(_repair_registration={"config": {"source_root": str(engine)}},
                           _invocation_context={"auto_approve": True})
    request = {"fix_disposition": route(engine)}
    with pytest.raises(EngineRepairRequired) as raised:
        engine_route(orch, request)
    assert triage_engine_request(orch, tmp_path, raised.value).decision.eligible


def test_engine_route_reviews_and_consumes_retained_spec_cache_options_and_conda_candidate_once(tmp_path):
    from auto_agents.repair_contract import retained_route_inputs
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision
    evidence = tmp_path / "frozen"
    evidence.mkdir()
    retained = {"spec.md": "Engine routing spec", "cache.json": '{"vitest": "--no-cache"}',
                "environment.json": '{"mode": "conda", "candidate": "existing-prefix"}'}
    for name, content in retained.items():
        (evidence / name).write_text(content)
    request = route(tmp_path / "engine")
    request["issue_seed"]["evidence_refs"] = list(retained)
    payload = {"invocation": {"engine_route": request}, "project": str(tmp_path / "live")}
    calls = []
    def review(agent):
        context = json.loads(agent.prompt.splitlines()[-1])
        reviewed = {item["path"]: (Path(context["frozen_evidence"]) / item["path"]).read_text()
                    for item in context["retained_inputs"]}
        assert reviewed == retained
        calls.append(reviewed)
        return SimpleNamespace(ok=True, summary=json.dumps({"checks": contract_data(request)["checks"]}))
    orch = SimpleNamespace(config=SimpleNamespace(efforts={}), _call_with_failover=review,
                           _invocation_context=payload["invocation"])
    with patch("auto_agents.orchestrator.Orchestrator", return_value=orch):
        first = prepare_contract(payload, "base", tmp_path, evidence, tmp_path)
        second = prepare_contract(payload, "base", tmp_path, evidence, tmp_path)
        assert first.to_dict() == second.to_dict()
        assert len(calls) == 1
        # A modified retained candidate must be reviewed again, not hidden by
        # a cached route/SHA pair. No live project input is read or generated.
        retained["environment.json"] = '{"mode": "conda", "candidate": "updated-prefix"}'
        (evidence / "environment.json").write_text(retained["environment.json"])
        prepare_contract(payload, "base", tmp_path, evidence, tmp_path)
        assert len(calls) == 2
    runner = AutoAgentsSelfRepairRunner(orch, target_project_root=evidence,
        error=EngineRepairRequired(request), decision=SelfRepairDecision(True), diagnosis=first)
    prompt = runner._build_prompt(tmp_path / "candidate", evidence)
    for item in retained_route_inputs(request, evidence):
        assert item["sha256"] in prompt
    assert {name: (evidence / name).read_text() for name in retained} == retained


def _exercise_engine_candidate(tmp_path, monkeypatch, *, dirty):
    from contextlib import ExitStack
    from auto_agents.config import load_run_state, save_run_state
    from auto_agents.models import AgentResult, SessionState
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.repair_control import Repository
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner
    from auto_agents.session import Session
    from auto_agents.workflow_runtime import WorkflowCoordinator
    from test_session import _make_project, _configure_git_identity
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", raising=False)
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_ROUTE_PROBE", raising=False)
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_SUBSCRIBER", raising=False)
    config = configuration(tmp_path)
    engine = make_remote(config)
    project = _make_project(str(tmp_path))
    _configure_git_identity(project)
    run = load_run_state(project)
    run.status, run.current_stage, run.active_blocker = "pending", "implement", {}
    save_run_state(project, run)
    git(project, "add", "-A")
    git(project, "commit", "-m", "product fixture")
    if dirty:
        for root in (engine, project):
            (root / "developer.txt").write_text("staged")
            git(root, "add", "developer.txt")
            (root / "developer.txt").write_text("unstaged")
            (root / "untracked.txt").write_text("untracked")
    def snapshot(root):
        return (git(root, "rev-parse", "HEAD"), git(root, "diff", "--binary"),
                git(root, "diff", "--cached", "--binary"), git(root, "status", "--porcelain"),
                (root / "untracked.txt").read_bytes() if dirty else b"")
    before = {root: snapshot(root) for root in (engine, project)}
    run_before = (project / ".auto-agents/state/run_state.json").read_bytes()
    orch = Orchestrator(project)
    orch._repair_registration = {"config": config}
    orch._invocation_context = {"auto_approve": True}
    coordinator = WorkflowCoordinator(orch)
    session = Session(orch, mode="collab", coordinator=coordinator)
    with patch.object(coordinator, "prepare_run_route", side_effect=AssertionError("product preflight")):
        with pytest.raises(EngineRepairRequired) as raised:
            session._prepare_workflow_handoff(SessionState(session_id="parent", mode="collab"),
                target="run", reason="engine", payload=route(engine))
    admission = triage_engine_request(orch, project, raised.value)
    assert admission.decision.eligible
    repository = Repository(config)
    base, _ = repository.fetch()
    runtime = repository.worktree(base, "runtime")
    frozen = tmp_path / "frozen"
    from auto_agents.root_cause import RootCauseCoordinator
    RootCauseCoordinator._copy_diagnostic_tree(project, frozen)
    orch._invocation_context["engine_route"] = raised.value.route_payload
    runner = AutoAgentsSelfRepairRunner(orch, target_project_root=frozen,
        error=raised.value, decision=admission.decision)
    runner.repo_root = runtime
    runner._real_project_root = project
    runner._engine_source_root = engine
    runner._verification_python_cache = sys.executable
    called = []
    def implement(request):
        assert request.sandbox_mode == "workspace-write"
        assert request.cwd not in (engine, project, runtime)
        assert git(request.cwd, "rev-parse", "HEAD") == base
        code = "from pathlib import Path\nPath('bug.py').write_text(\"value = 'candidate'\\n\")\n"
        for root in (engine, project):
            code += (f"try:\n Path({str(root / 'bug.py')!r}).write_text('forbidden')\n"
                     "except PermissionError:\n pass\nelse:\n raise AssertionError('foreign write permitted')\n")
        with runner._verification_argv([runner._verification_python(), "-c", code], request.cwd,
                                       read_roots=(engine,)) as command:
            result = subprocess.run(command, cwd=request.cwd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert (request.cwd / "bug.py").read_text() == "value = 'candidate'\n"
        called.append(request.cwd)
        # Fail the candidate deliberately to exercise real worktree cleanup.
        return AgentResult(False, [], request.output_path, summary="candidate rejected")
    with ExitStack() as stack:
        stack.enter_context(patch.object(orch, "_call_with_failover", side_effect=implement))
        stack.enter_context(patch.object(runner, "_artifact_paths", return_value=(tmp_path / "prompt", tmp_path / "output")))
        stack.enter_context(patch.object(runner, "_resume_interrupted_candidate", return_value=""))
        stack.enter_context(patch.object(runner, "_preserve_interrupted_candidate", return_value=True))
        stack.enter_context(patch.object(runner, "_provider_continuation", return_value={}))
        result = runner._run_candidate(experiment_id="engine", attempt=1, deadline=None,
                                       prior_failures=[], seen_fingerprints=set())
    assert result.status == "candidate_failed"
    assert len(called) == 1 and not called[0].exists()
    assert {root: snapshot(root) for root in (engine, project)} == before
    assert (project / ".auto-agents/state/run_state.json").read_bytes() == run_before


def test_authorized_engine_route_executes_in_permissioned_engine_workspace(tmp_path, monkeypatch):
    _exercise_engine_candidate(tmp_path, monkeypatch, dirty=False)


def test_engine_candidate_verification_and_rollback_preserve_both_dirty_repositories(tmp_path, monkeypatch):
    _exercise_engine_candidate(tmp_path, monkeypatch, dirty=True)
