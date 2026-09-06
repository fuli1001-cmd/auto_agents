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
    first = RuntimeAdapter("codex", [replace(_make_result(False, stderr="rate limit"), cleanup_incomplete=True)])
    second = RuntimeAdapter("claude-code", [_make_result()])
    orch = _stub_orchestrator({"codex": {}, "claude-code": {}}, "codex", {"codex": first, "claude-code": second})
    assert not orch._call_with_failover(request(tmp_path)).ok
    assert not second.requests


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
