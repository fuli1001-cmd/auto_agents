"""Prompt contracts, provider resolution, and continuation compatibility."""
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.agent_instructions import (
    COMPLETE_RULES_PATH, MANAGED_BEGIN, ensure_agent_instructions_synced,
    sync_agent_instructions,
)
from auto_agents.models import AgentRequest, ProviderConfig, ProjectConfig, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.prompting import (
    ContextBlock, PromptBlock, PromptSpec, ProviderRuntime, PromptingConfig,
    append_context, compose_prompt, prepare_request, render_prompt,
)
from auto_agents.prompting.runtime import resolve_runtime, last_option


def request(root, prompt=None, **kwargs):
    return AgentRequest(stage="implement", effort="deep", cwd=root,
                        prompt=prompt or compose_prompt(["Owned task", PromptBlock("Return DECISION only", kind="output")], purpose="review"),
                        output_path=root / "answer.md", **kwargs)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_default_config_and_generic_escape_hatch():
    config = ProjectConfig.from_dict({"providers": {"codex": {}}, "active_provider": "codex"})
    assert config.prompting.model_adaptation == "auto"
    assert ProjectConfig.from_dict(config.to_dict()).prompting == config.prompting
    assert PromptingConfig.from_dict({"model_adaptation": "generic"}).model_adaptation == "generic"
    with pytest.raises(ValueError):
        PromptingConfig.from_dict({"model_adaptation": "guess"})


def test_real_role_is_independent_of_effort_stage(tmp_path):
    req = request(tmp_path)
    prepared = prepare_request(req, ProviderRuntime("codex", resolved_model="gpt-6-astra"))
    assert prepared.stage == "implement"
    assert prepared.purpose == "review"
    assert prepared.sandbox_mode == "read-only"
    assert "This stage is read-only" in prepared.prompt
    assert "stage.implementation" not in prepared.prompt_metadata["rule_ids"]
    assert "model.gpt-6-astra" not in prepared.prompt_metadata["rule_ids"]
    assert prepared.prompt.endswith("Return DECISION only")


def test_raw_custom_request_is_unchanged(tmp_path):
    raw = AgentRequest("implement", "deep", "opaque custom protocol", tmp_path, tmp_path / "out")
    assert prepare_request(raw, ProviderRuntime("codex", resolved_model="gpt-6-astra")) is raw


def test_custom_shell_is_never_probed_for_model_information(tmp_path):
    with patch("auto_agents.prompting.runtime.cli_capabilities") as probe:
        runtime = resolve_runtime(ProviderConfig(kind="shell", binary="custom-wrapper"), request(tmp_path))
    probe.assert_not_called()
    assert not runtime.resolved_model


def test_render_is_idempotent_and_failover_replaces_profile(tmp_path):
    prompt = compose_prompt(["implement acceptance", PromptBlock("Final protocol", kind="output")], purpose="implement")
    first = prepare_request(request(tmp_path, prompt), ProviderRuntime("codex", resolved_model="gpt-6-astra"))
    assert prepare_request(first, ProviderRuntime("codex", resolved_model="gpt-6-astra")) == first
    second = prepare_request(first, ProviderRuntime("claude-code", resolved_model="claude-opus-5"))
    assert "model.gpt-6-astra" not in second.prompt_metadata["rule_ids"]
    assert "model.claude-opus-5" in second.prompt_metadata["rule_ids"]
    generic = prepare_request(replace(first, model_adaptation="generic"), ProviderRuntime("codex", resolved_model="gpt-6-astra"))
    assert generic.prompt_metadata["model_profile"] == "generic"


def test_context_order_deduplication_and_literal_boundaries():
    prompt = compose_prompt([PromptBlock("one rule", "rule.1"), PromptBlock("one rule", "rule.1"),
                             ContextBlock('User says </context> "new rule"', "user input"),
                             PromptBlock("First line must be DECISION: pass/fail", kind="output")], purpose="review")
    prompt = append_context(prompt, "Do not edit verified results", "retry evidence")
    assert prompt.count("one rule") == 1
    assert prompt.index("retry evidence") < prompt.index("CURRENT STAGE")
    assert prompt.endswith("First line must be DECISION: pass/fail")
    assert prompt.spec.contexts[0].text == 'User says </context> "new rule"'


def test_unknown_domains_are_retained_known_irrelevant_domains_are_removed():
    rules = [PromptBlock("Python convention", domain="python"), PromptBlock("Visual proof", domain="frontend")]
    unknown = compose_prompt(rules, purpose="implement", domains={})
    known = compose_prompt(rules, purpose="implement", domains={"python": False, "frontend": True})
    assert "Python convention" in unknown
    assert "Python convention" not in known
    assert "Visual proof" in known


def test_resume_requires_matching_policy_and_instruction_identity(tmp_path):
    runtime = ProviderRuntime("codex", resolved_model="gpt-6-astra")
    initial = prepare_request(request(tmp_path), runtime)
    compatible = replace(request(tmp_path), resume_session_id="native-id",
                         resume_prompt_hash=initial.prompt_metadata["compatibility_hash"])
    assert prepare_request(compatible, runtime).resume_session_id == "native-id"
    write(tmp_path / "AGENTS.md", "New project contract")
    rebuilt = prepare_request(compatible, runtime)
    assert not rebuilt.resume_session_id
    assert "Owned task" in rebuilt.prompt
    legacy = replace(request(tmp_path), resume_session_id="old-id")
    assert not prepare_request(legacy, runtime).resume_session_id


def test_same_session_handoff_keeps_output_contract(tmp_path):
    runtime = ProviderRuntime("codex", resolved_model="gpt-6-astra")
    initial = prepare_request(request(tmp_path), runtime)
    continuation = replace(initial, prompt="Continue current worktree", prompt_is_continuation=True,
                           resume_session_id="native-id", resume_prompt_hash=initial.prompt_metadata["compatibility_hash"])
    rendered = prepare_request(continuation, runtime)
    assert rendered.resume_session_id == "native-id"
    assert "Continue current worktree" in rendered.prompt
    assert rendered.prompt.endswith("Return DECISION only")
    assert prepare_request(rendered, runtime).prompt == rendered.prompt


def test_changed_stage_contract_invalidates_native_session(tmp_path):
    runtime = ProviderRuntime("codex", resolved_model="gpt-6-astra")
    initial = prepare_request(request(tmp_path), runtime)
    changed = replace(initial, resume_session_id="old-session",
                      resume_prompt_hash=initial.prompt_metadata["compatibility_hash"],
                      prompt_spec=replace(initial.prompt_spec, output_contract=("Return a different schema",)))
    rebuilt = prepare_request(changed, runtime)
    assert not rebuilt.resume_session_id
    assert rebuilt.prompt.endswith("Return a different schema")


def test_attachment_context_precedes_output_contract(tmp_path):
    req = request(tmp_path, attachments=[tmp_path / "screen.png"])
    output = prepare_request(req, ProviderRuntime("claude-code", resolved_model="claude-opus-5"))
    assert "Read tool" in output.prompt
    assert output.prompt.endswith("Return DECISION only")
    assert prepare_request(output, ProviderRuntime("claude-code", resolved_model="claude-opus-5")).prompt.count("screen.png") == 1


@pytest.mark.parametrize("args,expected", [
    (["--model=a", "--model", "b"], "b"), (["-m", "gpt-6-astra"], "gpt-6-astra"),
    (["--model", "a", "--model=b"], "b"), ([], ""),
])
def test_model_argument_forms(args, expected):
    assert last_option(args, "--model", "-m") == expected


def test_codex_profile_files_and_trusted_project_precedence(tmp_path):
    home, project = tmp_path / "codex-home", tmp_path / "project"
    project.mkdir()
    write(home / "config.toml", 'model="gpt-5.6-sol"\n[projects."' + str(project) + '"]\ntrust_level="trusted"\n')
    write(home / "deep.config.toml", 'model="gpt-6-astra"\n')
    cfg = ProviderConfig(kind="codex")
    with patch("auto_agents.prompting.runtime.cli_capabilities", return_value=("1.0", ("profile-files",))):
        runtime = resolve_runtime(cfg, request(project), env={"CODEX_HOME": str(home)})
        assert runtime.resolved_model == "gpt-6-astra"
        write(project / ".codex/config.toml", 'model="gpt-5.6-sol"\n')
        assert resolve_runtime(cfg, request(project), env={"CODEX_HOME": str(home)}).resolved_model == "gpt-5.6-sol"
        cfg.extra_args = ["-c", 'model="gpt-6-astra"']
        assert resolve_runtime(cfg, request(project), env={"CODEX_HOME": str(home)}).resolved_model == "gpt-6-astra"


def test_codex_untrusted_unknown_and_legacy_configuration(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    write(home / "config.toml", 'model="gpt-5.6-sol"\n[profiles.deep]\nmodel="gpt-6-astra"\n')
    cfg = ProviderConfig(kind="codex")
    runtime = resolve_runtime(cfg, request(project), env={"CODEX_HOME": str(home)}, probe=False)
    assert runtime.resolved_model == "gpt-6-astra"
    write(project / ".codex/config.toml", 'model="gpt-5.6-sol"\n')
    assert not resolve_runtime(cfg, request(project), env={"CODEX_HOME": str(home)}, probe=False).resolved_model
    with (home / "config.toml").open("a") as handle:
        handle.write('[projects."' + str(project) + '"]\ntrust_level="untrusted"\n')
    assert resolve_runtime(cfg, request(project), env={"CODEX_HOME": str(home)}, probe=False).resolved_model == "gpt-6-astra"


def test_invalid_config_diagnostics_do_not_expose_secret(tmp_path):
    home = tmp_path / "home"
    write(home / "config.toml", 'secret="DO_NOT_LOG_THIS\n')
    result = resolve_runtime(ProviderConfig(), request(tmp_path), env={"CODEX_HOME": str(home)}, probe=False)
    assert not result.resolved_model
    assert "DO_NOT_LOG_THIS" not in repr(result)


def test_explicit_codex_model_on_custom_provider_is_not_assumed_openai(tmp_path):
    config = ProviderConfig(kind="codex", profile_map={}, extra_args=["--model=gpt-6-astra", "-c", 'model_provider="gateway"'])
    result = resolve_runtime(config, request(tmp_path), env={"CODEX_HOME": str(tmp_path / "empty")}, probe=False)
    assert result.configured_model == "gpt-6-astra"
    assert not result.resolved_model


def test_claude_alias_is_not_guessed_and_override_is_respected(tmp_path):
    config = ProviderConfig(kind="claude-code", profile_map={"deep": "opus"})
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    assert not resolve_runtime(config, request(tmp_path), env=env, probe=False).resolved_model
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "claude-opus-5"
    assert resolve_runtime(config, request(tmp_path), env=env, probe=False).resolved_model == "claude-opus-5"
    env["ANTHROPIC_BASE_URL"] = "https://gateway.invalid"
    assert not resolve_runtime(config, request(tmp_path), env=env, probe=False).resolved_model


def test_copilot_modern_profile_and_auto_selection(tmp_path):
    profile = tmp_path / "profile"
    write(profile / "config.json", '{"model":"gpt-5.6-sol"}')
    write(profile / "settings.json", '{"model":"gpt-6-astra"}')
    config = ProviderConfig(kind="copilot-cli", profile_map={"deep": str(profile)})
    assert resolve_runtime(config, request(tmp_path), env={}, probe=False).resolved_model == "gpt-6-astra"
    config.extra_args = ["--model=auto"]
    assert not resolve_runtime(config, request(tmp_path), env={}, probe=False).resolved_model


def test_copilot_command_and_resolver_use_same_overridden_directory(tmp_path):
    from auto_agents.adapters.copilot_cli import CopilotCliAdapter
    write(tmp_path / "chosen/settings.json", '{"model":"gpt-6-astra"}')
    config = ProviderConfig(kind="copilot-cli", extra_args=["--config-dir=chosen"], profile_map={"deep": "ignored"})
    req = request(tmp_path)
    adapter = CopilotCliAdapter(config)
    command = adapter._build_command(req)
    assert last_option(command, "--model") == "gpt-6-astra"
    assert resolve_runtime(config, req, env={}, probe=False).resolved_model == "gpt-6-astra"


def test_antigravity_selection_is_only_confirmed_with_native_flag(tmp_path):
    config = ProviderConfig(kind="antigravity", profile_map={"deep": "Gemini 3.5 Flash (High)"})
    assert not resolve_runtime(config, request(tmp_path), env={}, probe=False).resolved_model
    with patch("auto_agents.prompting.runtime.cli_capabilities", return_value=("1.1", ("--model",))):
        assert resolve_runtime(config, request(tmp_path), env={}).resolved_model == "gemini-3.5-flash"


def test_complete_rules_not_truncated_and_manual_content_survives(tmp_path):
    write(tmp_path / "AGENTS.md", "Human-maintained preface.\n")
    write(tmp_path / ".auto-agents/project-rules.md", "- Follow the entire project contract.\n")
    rules = ["Must preserve identifier_" + str(i) + " " + "very precise constraint " * 25 for i in range(25)]
    sync_agent_instructions(tmp_path, normalized_rules={"hard_rules": rules})
    full = (tmp_path / COMPLETE_RULES_PATH).read_text()
    assert all(rule.rstrip() in full for rule in rules)
    assert (tmp_path / "AGENTS.md").read_text().startswith("Human-maintained preface.")
    assert (tmp_path / ".agents/rules/auto-agents.md").read_text().startswith("---\ntrigger: always_on\n---")
    assert (tmp_path / ".github/instructions/product-contract.instructions.md").read_text().startswith('---\napplyTo: "**"\n---')
    # Source unchanged and files unchanged -> no rewriting or extra normalization.
    assert not ensure_agent_instructions_synced(tmp_path).synced
    write(tmp_path / "AGENTS.md", (tmp_path / "AGENTS.md").read_text() + "Human-maintained suffix.\n")
    sync_agent_instructions(tmp_path, normalized_rules={"hard_rules": rules})
    actual = (tmp_path / "AGENTS.md").read_text()
    assert actual.count(MANAGED_BEGIN) == 1
    assert actual.endswith("Human-maintained suffix.\n")


def test_native_policy_update_or_missing_carrier_refreshes_lock(tmp_path):
    sync_agent_instructions(tmp_path)
    lock_path = tmp_path / ".auto-agents/state/agent_instructions.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["generator_policy_hash"] = "old-policy"
    lock_path.write_text(json.dumps(lock))
    assert ensure_agent_instructions_synced(tmp_path).synced
    lock = json.loads(lock_path.read_text())
    del lock["generated_sha256"][".agents/rules/auto-agents.md"]
    lock_path.write_text(json.dumps(lock))
    assert ensure_agent_instructions_synced(tmp_path).synced
    assert not ensure_agent_instructions_synced(tmp_path).synced


def test_task_builder_keeps_machine_proofs_outside_short_summary(tmp_path):
    root = tmp_path / "project"
    Orchestrator.init_project(root, "test", "mock")
    task = TaskSpec(task_id="t1", title="change", description="change", acceptance=["observable behavior"],
                    requirement_proofs=[{"requirement_id": "REQ-001", "oracle_index": 0}])
    prompt = Orchestrator(root)._build_task_prompt(task, "implement")
    assert prompt.spec.purpose == "implement"
    assert "ORACLE_PROOF_UPDATES" in "\n".join(prompt.spec.output_contract)
    assert "complete required" in "\n".join(prompt.spec.output_contract)
    assert "PERSISTENCE CONTRACT:" not in prompt
    assert "visual_evidence on that proof" not in prompt


def test_normalized_persistence_axes_keep_migration_contract(tmp_path):
    root = tmp_path / "project"
    Orchestrator.init_project(root, "test", "mock")
    task = TaskSpec(task_id="t1", title="migration", description="upgrade", acceptance=["preserve rows"],
                    persistence_change={"storage_transition": "migrate_in_place", "compatibility_policy": "migrate_all"})
    assert "PERSISTENCE CONTRACT:" in Orchestrator(root)._build_task_prompt(task, "implement")


def test_task_context_keeps_contract_but_not_duplicate_execution_history():
    from auto_agents.prompting.core import task_context
    task = TaskSpec(task_id="t1", title="fix", description="fix", acceptance=["exact behavior"],
                    review_history=[{"summary": "obsolete finding"}])
    result = task_context(task)
    assert result["acceptance"] == ["exact behavior"]
    assert "review_history" not in result
    assert "requirement_proofs" in result


def test_cached_review_needs_current_policy_and_native_instructions(tmp_path):
    from auto_agents.config import load_run_state
    root = tmp_path / "project"
    Orchestrator.init_project(root, "test", "mock")
    orch = Orchestrator(root)
    state = load_run_state(root)
    task = TaskSpec(task_id="t1", title="fix", description="fix", acceptance=["works"])
    state.task_review_cache[task.task_id] = {"fingerprint": "candidate", "decision": "pass", "summary": "old result"}
    assert orch._cached_review_result(state, task, "candidate") is None
    orch._store_task_review_cache(state, task, "candidate", "current result")
    assert orch._cached_review_result(state, task, "candidate")["ok"]
    write(root / COMPLETE_RULES_PATH, "Changed full project contract")
    assert orch._cached_review_result(state, task, "candidate") is None


def test_rebased_prompt_keeps_spec_and_output_boundary(tmp_path):
    prompt = compose_prompt([PromptBlock("Read /old/root/file", "input.path"),
                             ContextBlock("/old/root/data", "source"),
                             PromptBlock("Return only JSON", kind="output")], purpose="review")
    rebased = prompt.replace("/old/root", "/new/root") + "Additional constraint"
    assert rebased.spec.purpose == "review"
    assert "/old/root" not in rebased
    assert "/new/root/data" in rebased
    assert rebased.endswith("Return only JSON")


def test_observed_models_are_metadata_not_a_guessed_policy(tmp_path):
    from auto_agents.prompting.runtime import observed_model_metadata
    prepared = prepare_request(request(tmp_path), ProviderRuntime("claude-code", configured_model="opus"))
    result = observed_model_metadata(prepared, '\n'.join([
        json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5"}),
        json.dumps({"type": "tool_result", "model": "not-a-model-observation"}),
    ]))
    assert result["observed_models"] == ["claude-opus-5"]
    assert result["model_profile"] == "generic"


def test_new_and_legacy_native_resumption_end_to_end(tmp_path):
    from auto_agents.models import AgentResult, AgentTermination
    from auto_agents.config import load_project_config
    from auto_agents.prompting.core import digest
    root = tmp_path / "project"
    Orchestrator.init_project(root, "test", "mock")
    orch = Orchestrator(root)
    req = request(root, attempt_id="resumption-test")

    class Adapter:
        requests = []

        def describe_runtime(self, request):
            return ProviderRuntime("codex", resolved_model="gpt-6-astra")

        def run(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return AgentResult(False, [], request.output_path, provider_session_id="session", termination=AgentTermination(
                    reason="semantic_stall", elapsed_seconds=1, last_provider_activity_seconds=0,
                    last_semantic_progress_seconds=1))
            return AgentResult(True, [], request.output_path, summary="DECISION: pass", provider_session_id="session")

    adapter = Adapter()
    with patch.object(orch, "_record_provider_execution_incident", return_value=None), \
         patch.object(orch, "_resolve_provider_execution_incidents", create=True):
        result = orch._run_provider_with_smart_recovery(adapter, req, "codex")
    assert result.ok
    assert len(adapter.requests) == 2
    assert adapter.requests[1].resume_session_id == "session"
    assert adapter.requests[1].prompt.endswith("Return DECISION only")
    assert "AUTO-AGENTS TAKEOVER" in adapter.requests[1].prompt
    artifact = adapter.requests[1].progress_report_path.with_suffix(".prompt.json")
    saved = json.loads(artifact.read_text())
    assert saved["prompt_sha256"] == digest(adapter.requests[1].prompt)
    assert saved["purpose"] == "review"


def test_fresh_handoff_retains_failure_context(tmp_path):
    runtime = ProviderRuntime("codex", resolved_model="gpt-6-astra")
    req = prepare_request(request(tmp_path), runtime)
    handed = Orchestrator._prompt_handoff(req, "bounded progress from expired turn", "")
    rendered = prepare_request(handed, runtime)
    assert "Owned task" in rendered.prompt
    assert "bounded progress from expired turn" in rendered.prompt
    assert rendered.prompt.endswith("Return DECISION only")


def test_evaluation_corpus_and_offline_checks(tmp_path):
    from auto_agents.prompting.evaluate import check_result, fixture_fingerprint, seed
    corpus = json.loads((Path(__file__).parent / "fixtures/prompt_baseline.json").read_text())
    assert len(corpus["cases"]) == 8
    review = next(c for c in corpus["cases"] if c["id"] == "read-only-review")
    root = tmp_path / "review"
    prompt = seed(root, review)
    assert prompt.spec.purpose == "review"
    before = fixture_fingerprint(root)
    assert check_result(review, root, "DECISION: fail\nEmpty input is broken.", before)["accepted"]
    write(root / "solution.py", "unauthorized edit")
    assert not check_result(review, root, "DECISION: fail", before)["scope_ok"]
    small = next(c for c in corpus["cases"] if c["id"] == "small-change")
    root = tmp_path / "implement"
    seed(root, small)
    assert not check_result(small, root, "done", {})["accepted"]
    write(root / "solution.py", "def total(values):\n    return sum(values)\n")
    assert check_result(small, root, "done", {})["accepted"]


def test_fix_classification_keeps_wire_contract_after_context(tmp_path):
    from auto_agents.models import SessionState
    from auto_agents.session import Session
    root = tmp_path / "project"
    Orchestrator.init_project(root, "test", "mock")
    session = Session(Orchestrator(root), mode="fix")
    prompt = session._build_converse_prompt(SessionState(session_id="test", mode="fix", goal="Diagnose empty input"))
    assert prompt.spec.purpose == "fix_converse"
    assert any("FIX_DISPOSITION" in line for line in prompt.spec.output_contract)
    assert prompt.rfind("CURRENT STAGE:") < prompt.rfind("FIX_DISPOSITION")


def test_evaluation_is_available_through_normal_cli(tmp_path):
    from auto_agents.cli import main
    output = tmp_path / "capture.json"
    with patch("auto_agents.cli._load_cli_dotenv"), patch("auto_agents.prompting.evaluate.capture") as capture:
        assert main(["prompt-eval", "capture", "--output", str(output)]) == 0
    capture.assert_called_once_with(output)
