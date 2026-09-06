"""Offline invariants for full task reconstruction and token optimizations."""
from dataclasses import replace

import pytest

from auto_agents.models import AgentRequest, AgentResult, ProviderConfig
from auto_agents.orchestrator import Orchestrator
from auto_agents.prompting import ContextBlock, PromptBlock, ProviderRuntime, compose_prompt, prepare_request
from auto_agents.prompting.core import fresh_request
from auto_agents.prompting.runtime import resolve_runtime
from test_failover import _stub_orchestrator, _SequenceAdapter, _make_result


def request(root):
    return AgentRequest(
        "implement", "deep",
        compose_prompt([
            "Preserve every existing database row.",
            ContextBlock("User correction: preserve fractional totals.", "user", "message:0"),
            PromptBlock("Return IMPLEMENTED and proof references.", kind="output"),
        ], purpose="implement"), root, root / "result.md",
    )


@pytest.mark.parametrize("reason", ["missing_id", "provider_switch", "effort_change", "settings_change"])
def test_incompatible_continuation_rebuilds_complete_task(tmp_path, reason):
    runtime = ProviderRuntime("codex", resolved_model="gpt-6-astra", settings_fingerprint="original")
    initial = prepare_request(request(tmp_path), runtime)
    continued = Orchestrator._prompt_handoff(initial, "Partial patch exists; test still fails.", "old-session")
    if reason == "missing_id":
        continued = replace(continued, resume_session_id="")
    elif reason == "provider_switch":
        continued = fresh_request(continued, "provider-switch")
        runtime = ProviderRuntime("claude-code", resolved_model="claude-opus-5")
    elif reason == "effort_change":
        continued = replace(continued, effort="max")
    else:
        runtime = replace(runtime, settings_fingerprint="changed")
    rendered = prepare_request(continued, runtime)
    assert rendered.resume_session_id == ""
    assert not rendered.prompt_is_continuation
    assert "Preserve every existing database row." in rendered.prompt
    assert "preserve fractional totals" in rendered.prompt
    assert "Partial patch exists" in rendered.prompt
    assert rendered.prompt.endswith("Return IMPLEMENTED and proof references.")
    assert prepare_request(rendered, runtime).prompt == rendered.prompt


class RuntimeAdapter(_SequenceAdapter):
    def __init__(self, provider, results, mutate=None):
        super().__init__(results)
        self.provider = provider
        self.mutate = mutate

    def describe_runtime(self, request):
        return ProviderRuntime(self.provider, resolved_model="gpt-6-astra" if self.provider == "codex" else "claude-opus-5")

    def run(self, request):
        if self.mutate:
            self.mutate(request)
        return super().run(request)


def test_delta_failover_transfers_contract_and_partial_work(tmp_path):
    patch = tmp_path / "partial.py"
    first = RuntimeAdapter("codex", [_make_result(False, stderr="rate limit", summary="Patch ready; not verified")],
                           lambda req: patch.write_text("value = 42\n"))
    second = RuntimeAdapter("claude-code", [_make_result()])
    orch = _stub_orchestrator({"codex": {}, "claude-code": {}}, "codex", {"codex": first, "claude-code": second})
    initial = prepare_request(request(tmp_path), first.describe_runtime(None))
    continued = replace(Orchestrator._prompt_handoff(initial, "Retry the failing test.", "native-a"), resume_provider="codex")
    assert orch._call_with_failover(continued).ok
    sent = second.requests[0]
    assert not sent.resume_session_id and not sent.prompt_is_continuation
    assert "Preserve every existing database row." in sent.prompt
    assert "preserve fractional totals" in sent.prompt
    assert "Patch ready; not verified" in sent.prompt
    assert "previous_claims_unverified" in sent.prompt
    assert patch.read_text() == "value = 42\n"
    assert sent.prompt_metadata["provider"] == "claude-code"


def test_live_process_prevents_failover(tmp_path):
    from auto_agents.models import ProviderCleanupIncompleteError
    first = RuntimeAdapter("codex", [replace(_make_result(False, stderr="rate limit"), cleanup_incomplete=True)])
    second = RuntimeAdapter("claude-code", [_make_result()])
    orch = _stub_orchestrator({"codex": {}, "claude-code": {}}, "codex", {"codex": first, "claude-code": second})
    with pytest.raises(ProviderCleanupIncompleteError):
        orch._call_with_failover(request(tmp_path))
    assert not second.requests
    with pytest.raises(ProviderCleanupIncompleteError):
        orch._call_with_failover(request(tmp_path))
    assert len(first.requests) == 1


def test_native_effort_edit_changes_runtime_identity(tmp_path):
    home = tmp_path / "native"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
    config = ProviderConfig(kind="codex", profile_map={"deep": ""})
    first = resolve_runtime(config, request(tmp_path), env={"CODEX_HOME": str(home)}, probe=False)
    (home / "config.toml").write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n')
    second = resolve_runtime(config, request(tmp_path), env={"CODEX_HOME": str(home)}, probe=False)
    assert first.resolved_model == second.resolved_model == "gpt-6-astra"
    assert first.settings_fingerprint != second.settings_fingerprint


@pytest.fixture
def session_case(tmp_path):
    from auto_agents.session import Session
    from auto_agents.models import SessionState
    Orchestrator.init_project(tmp_path, "token-test", "mock")
    orch = Orchestrator(tmp_path)
    session = Session(orch, mode="collab")
    goal = "Preserve all existing behavior. " * 100
    state = SessionState(session_id="token-test", mode="collab", status="executing", goal=goal,
                         conversation=[{"role": "user", "content": goal}])
    requests = []
    runtime = ProviderRuntime("mock", resolved_model="fixed-test-model")

    def call(req):
        prepared = prepare_request(req, runtime)
        requests.append(prepared)
        return AgentResult(True, [], req.output_path, summary="Recorded the requirement.",
                           provider_session_id="native-session", prompt_metadata=prepared.prompt_metadata)

    orch._call_with_failover = call
    return session, state, requests


def session_turn(session, state, label):
    return session._call_agent(state, label, session._build_collab_prompt(state, ""))


def test_session_sends_only_new_context_and_skips_own_answer(session_case):
    from auto_agents.models import SessionState
    session, state, requests = session_case
    reply = session_turn(session, state, "collab-1")
    state.conversation.extend([{"role": "agent", "content": reply},
                               {"role": "user", "content": "Keep fractional totals exactly."}])
    # Exercise the durable JSON round-trip, not only an in-memory checkpoint.
    state = SessionState.from_dict(state.to_dict())
    session_turn(session, state, "collab-2")
    sent = requests[-1]
    assert sent.prompt_is_continuation and sent.resume_session_id
    assert "Keep fractional totals exactly." in sent.prompt
    assert "Preserve all existing behavior." not in sent.prompt
    assert reply not in sent.prompt
    assert len(sent.prompt) < len(requests[0].prompt)
    assert any("Preserve all existing behavior." in c.text for c in sent.prompt_spec.contexts)
    assert state.provider_continuations["collab"]["policy_version"] == 4


@pytest.mark.parametrize("change", ["history", "execution", "workspace", "legacy", "no_session"])
def test_session_falls_back_when_sync_cannot_be_proven(session_case, change):
    session, state, requests = session_case
    session_turn(session, state, "collab-1")
    if change == "history":
        state.conversation[0]["content"] = "Changed historical request."
    elif change == "execution":
        state.provider_continuations["collab"]["input_checkpoint"]["execution_count"] = 99
    elif change == "workspace":
        (session.project_root / "external.py").write_text("changed = True\n")
    elif change == "legacy":
        state.provider_continuations["collab"]["policy_version"] = 3
    else:
        state.provider_continuations["collab"]["provider_session_id"] = ""
    session_turn(session, state, "collab-2")
    assert not requests[-1].resume_session_id
    assert not requests[-1].prompt_is_continuation
    assert "Preserve all existing behavior." in requests[-1].prompt


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_acceleration_observation_does_not_send_delta(session_case, mode):
    session, state, requests = session_case
    session_turn(session, state, "collab-1")
    session.config.execution.acceleration.mode = mode
    state.conversation.append({"role": "user", "content": "Preserve the public API."})
    session_turn(session, state, "collab-2")
    assert not requests[-1].resume_session_id
    assert not requests[-1].prompt_is_continuation
    assert "Preserve all existing behavior." in requests[-1].prompt
    if mode == "observe":
        assert requests[-1].prompt_metadata["delta_candidate_bytes"] > 0


def test_failed_call_does_not_leave_a_reusable_cursor(session_case):
    session, state, requests = session_case
    session_turn(session, state, "collab-1")
    def fail(req):
        raise RuntimeError("All providers exhausted")
    session.orch._call_with_failover = fail
    with pytest.raises(RuntimeError):
        session_turn(session, state, "collab-2")
    assert "collab" not in state.provider_continuations


@pytest.mark.parametrize("case", ["protocol", "semantic", "disabled"])
def test_review_only_resumes_explicit_protocol_corrections(session_case, case):
    session, state, requests = session_case
    orch = session.orch
    captured = []
    runtime = ProviderRuntime("mock", resolved_model="fixed-test-model")
    if case == "disabled":
        orch.config.execution.acceleration.session_continuation_enabled = False

    def call(req):
        prepared = prepare_request(req, runtime)
        captured.append(prepared)
        summary = "Looks correct." if case != "semantic" else "DECISION: fail\n.auto-agents/state/task_plan.json status in_progress"
        if len(captured) > 1:
            summary = "DECISION: pass\nBehavior verified."
        return AgentResult(True, [], req.output_path, summary=summary, provider_session_id="review-native",
                           prompt_metadata=prepared.prompt_metadata)

    orch._call_with_failover = call
    result = orch._run_agent_with_retries(
        None, "review", "review-protocol-test",
        compose_prompt(["Review all owned acceptance.", PromptBlock("Return DECISION: pass or DECISION: fail.", kind="output")], purpose="review"),
        validation_feedback=orch._review_validation_feedback,
        protocol_retry_eligible=lambda r: not orch._has_explicit_review_decision(r.summary),
    )
    assert result.ok and len(captured) == 2
    assert bool(captured[1].resume_session_id) == (case == "protocol")
    assert captured[1].prompt_is_continuation == (case == "protocol")
    assert "DECISION" in captured[1].prompt
    if case == "protocol":
        assert "Looks correct." not in captured[1].prompt
        rebuilt = prepare_request(fresh_request(captured[1], "provider-switch"), runtime)
        assert "Review all owned acceptance." in rebuilt.prompt
        assert "Looks correct." in rebuilt.prompt




@pytest.mark.parametrize("mutates", [False, True])
def test_missing_native_session_rebuilds_once_only_without_mutations(session_case, monkeypatch, mutates):
    session, _, _ = session_case
    orch = session.orch
    root = session.project_root
    adapter = RuntimeAdapter("codex", [_make_result(False, stderr="Error: session old-native not found"), _make_result()])
    if mutates:
        adapter.mutate = lambda req: (root / "unexpected.py").write_text("changed = True\n")
    initial = prepare_request(request(root), adapter.describe_runtime(None))
    continued = Orchestrator._prompt_handoff(initial, "Continue the owned patch.", "old-native")
    monkeypatch.setattr(orch, "_record_provider_execution_incident", lambda *args: None)
    result = orch._run_provider_with_smart_recovery(adapter, continued, "codex")
    assert len(adapter.requests) == (1 if mutates else 2)
    if not mutates:
        assert result.ok
        assert not adapter.requests[1].resume_session_id
        assert "Preserve every existing database row." in adapter.requests[1].prompt


def test_switch_back_to_previous_provider_gets_latest_complete_context(tmp_path):
    first = RuntimeAdapter("codex", [_make_result(False, stderr="rate limit"), _make_result()])
    second = RuntimeAdapter("claude-code", [_make_result(provider_session_id="native-b"),
                                             _make_result(False, stderr="rate limit", summary="Partial change preserved")])
    orch = _stub_orchestrator({"codex": {}, "claude-code": {}}, "codex", {"codex": first, "claude-code": second})
    initial = request(tmp_path)
    assert orch._call_with_failover(initial).ok
    updated = replace(initial, prompt_spec=replace(initial.prompt_spec, contexts=(
        *initial.prompt_spec.contexts, ContextBlock("New constraint: keep the public API.", "user", "message:1"),
    )))
    prepared = prepare_request(updated, second.describe_runtime(None))
    continued = replace(Orchestrator._prompt_handoff(prepared, "Apply the new constraint.", "native-b"), resume_provider="claude-code")
    assert orch._call_with_failover(continued).ok
    returned = first.requests[-1]
    assert not returned.resume_session_id
    assert "Preserve every existing database row." in returned.prompt
    assert "New constraint: keep the public API." in returned.prompt
    assert "Partial change preserved" in returned.prompt


def test_outer_session_does_not_retry_cleanup_failure(session_case):
    from auto_agents.models import ProviderCleanupIncompleteError
    session, state, _ = session_case
    calls = []
    def fail(req):
        calls.append(req)
        raise ProviderCleanupIncompleteError("Old process still alive")
    session.orch._call_with_failover = fail
    with pytest.raises(ProviderCleanupIncompleteError):
        session._phase_converse(state)
    assert len(calls) == 1


def test_stage_retry_marks_partial_usage_as_a_known_subtotal(session_case):
    from auto_agents.models import AgentUsage
    session, _, _ = session_case
    orch = session.orch
    calls = []
    def call(req):
        calls.append(req)
        return AgentResult(True, [], req.output_path,
                           summary="DECISION: pass" if len(calls) == 2 else "Missing decision",
                           usage=AgentUsage(100, 20, 10) if len(calls) == 2 else None)
    orch._call_with_failover = call
    result = orch._run_agent_with_retries(
        None, "review", "review-usage-test", compose_prompt(["Review acceptance."], purpose="review"),
        validation_feedback=orch._review_validation_feedback,
    )
    assert result.ok
    assert result.prompt_metadata["stage_usage_complete"] is False
