import json
from dataclasses import replace
from unittest.mock import patch

from auto_agents.adapters.codex import CodexAdapter
from auto_agents.adapters.shell import ShellAdapter
from auto_agents.cli import build_parser
from auto_agents.config import load_project_config, save_project_config
from auto_agents.models import AgentRequest, ProjectConfig, ProviderConfig, SMART_TIMEOUT_PROGRESS_PROTOCOL
from auto_agents.orchestrator import Orchestrator
from auto_agents.prompting.core import PromptBlock, PromptSpec, prepare_request
from auto_agents.prompting.runtime import resolve_runtime
from auto_agents.provider_environment import effective_environment
from auto_agents.repair_v2.providers import AgentSandbox
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.validation import validate_project_config_payload


def _request(root):
    return AgentRequest(stage="implement", effort="deep", prompt="task", cwd=root,
                        output_path=root / "answer.txt",
                        prompt_spec=PromptSpec(purpose="implement", blocks=(PromptBlock("task"),)))


def test_two_codex_accounts_have_distinct_runtime_and_resume_identity(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for home, model, guide in ((first, "model-first", "first guide"),
                               (second, "model-second", "second guide")):
        home.mkdir()
        (home / "config.toml").write_text(f'model = "{model}"\n')
        (home / "AGENTS.md").write_text(guide)
    providers = {
        name: ProviderConfig(kind="codex", profile_map={"deep": ""},
                             environment={"CODEX_HOME": str(home), "OPENAI_API_KEY": None},
                             provider_name=name)
        for name, home in (("codex-first", first), ("codex-second", second))
    }
    request = _request(tmp_path)
    with patch.dict("os.environ", {"OPENAI_API_KEY": "host-secret"}):
        runtimes = [resolve_runtime(provider, request, env=effective_environment(provider), probe=False)
                    for provider in providers.values()]
        assert all("OPENAI_API_KEY" not in effective_environment(provider)
                   for provider in providers.values())
    assert [runtime.resolved_model for runtime in runtimes] == ["model-first", "model-second"]
    assert runtimes[0].settings_fingerprint != runtimes[1].settings_fingerprint
    assert runtimes[0].instructions_hash != runtimes[1].instructions_hash
    first_request = prepare_request(request, runtimes[0])
    continuation = replace(request, resume_session_id="first-thread",
                           resume_prompt_hash=first_request.prompt_metadata["compatibility_hash"])
    assert prepare_request(continuation, runtimes[0]).resume_session_id == "first-thread"
    assert prepare_request(continuation, runtimes[1]).resume_session_id == ""
    assert "host-secret" not in json.dumps(first_request.prompt_metadata)


def test_provider_environment_reaches_cli_and_preserves_host(tmp_path):
    config = ProviderConfig(kind="codex", profile_map={"deep": ""},
                            environment={"CODEX_HOME": str(tmp_path / "account"),
                                         "OPENAI_API_KEY": None, "CUSTOM_VALUE": "account-one"})
    adapter = CodexAdapter(config)
    with patch.dict("os.environ", {"OPENAI_API_KEY": "host-secret", "CUSTOM_VALUE": "host"}):
        with patch("auto_agents.adapters.codex.run_subprocess_with_optional_streaming",
                   return_value=("", "", 0, False, False)) as run:
            with patch("auto_agents.prompting.runtime.cli_capabilities", return_value=("", ())):
                adapter.run(_request(tmp_path))
        env = run.call_args.args[2]
        assert env["CODEX_HOME"] == str(tmp_path / "account")
        assert env["CUSTOM_VALUE"] == "account-one"
        assert "OPENAI_API_KEY" not in env
        assert env["AUTO_AGENTS_STAGE"] == "implement"
        assert env["AUTO_AGENTS_EFFORT"] == "deep"
        assert __import__("os").environ["OPENAI_API_KEY"] == "host-secret"


def test_generic_shell_provider_uses_the_same_environment_binding(tmp_path):
    config = ProviderConfig(kind="shell", binary="sh", profile_map={},
                            progress_protocol=SMART_TIMEOUT_PROGRESS_PROTOCOL,
                            environment={"CUSTOM_ACCOUNT": "second", "INHERITED_ACCOUNT": None},
                            provider_name="shell-second")
    adapter = ShellAdapter(config)
    with patch.dict("os.environ", {"INHERITED_ACCOUNT": "first"}):
        with patch("auto_agents.adapters.shell.run_subprocess_with_optional_streaming",
                   return_value=("ok", "", 0, False, False)) as run:
            result = adapter.run(_request(tmp_path))
    env = run.call_args.args[2]
    assert result.ok
    assert env["CUSTOM_ACCOUNT"] == "second"
    assert "INHERITED_ACCOUNT" not in env
    assert result.prompt_metadata["settings_fingerprint"] == resolve_runtime(
        config, _request(tmp_path), probe=False).settings_fingerprint


def test_environment_config_round_trip_and_validation():
    config = ProviderConfig(environment={"CODEX_HOME": "/account", "OPENAI_API_KEY": None},
                            provider_name="codex-a")
    assert ProviderConfig.from_dict(config.to_dict()).environment == config.environment
    assert "provider_name" not in config.to_dict()
    payload = {"project_name": "test", "providers": {"codex-a": config.to_dict()},
               "active_provider": "codex-a", "efforts": {}, "gates": {}, "git": {},
               "approvals": {}, "retries": {}}
    errors = validate_project_config_payload(payload)
    assert not any("providers.codex-a" in error for error in errors)
    loaded = ProjectConfig.from_dict(payload)
    assert loaded.providers["codex-a"].provider_name == "codex-a"
    payload["providers"]["codex-a"]["environment"]["AUTO_AGENTS_STAGE"] = "wrong"
    assert any("environment" in error for error in validate_project_config_payload(payload))


def test_cli_selects_configured_account_alias_and_rejects_unknown_alias(tmp_path):
    for command in ("run", "fix", "collab", "provider-resolve"):
        parsed = build_parser().parse_args([command, "--project", str(tmp_path),
                                            "--provider", "codex-second"])
        assert parsed.provider == "codex-second"
    Orchestrator.init_project(tmp_path / "project", "project")
    config = load_project_config(tmp_path / "project")
    config.providers["codex-second"] = ProviderConfig(
        kind="codex", environment={"CODEX_HOME": "/account/second"})
    save_project_config(tmp_path / "project", config)
    orchestrator = Orchestrator(tmp_path / "project")
    orchestrator._set_active_provider("codex-second")
    selected = load_project_config(tmp_path / "project")
    assert selected.active_provider == "codex-second"
    assert selected.provider.environment["CODEX_HOME"] == "/account/second"
    try:
        orchestrator._set_active_provider("unconfigured-account")
    except ValueError as error:
        assert "Configured providers" in str(error)
    else:
        raise AssertionError("an unknown provider alias was silently created")


def test_repair_sandbox_copies_selected_account_and_rejects_account_switch(tmp_path):
    first, second = tmp_path / "account-a", tmp_path / "account-b"
    for home, account in ((first, "first"), (second, "second")):
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"account": account}))
        (home / "config.toml").write_text(f'model = "{account}"\n')
        (home / "deep.config.toml").write_text(f'model = "{account}-deep"\n')
    root = tmp_path / "repair-state"
    sandbox = AgentSandbox(root, "image", kind="codex", provider_name="codex-a",
                           environment={"HOME": str(tmp_path), "CODEX_HOME": str(first)},
                           binding="account-a")
    with patch("auto_agents.repair_v2.storage.require_space"):
        private = sandbox.home("implement")
    assert json.loads((private / ".codex/auth.json").read_text())["account"] == "first"
    assert "first-deep" in (private / ".codex/deep.config.toml").read_text()
    switched = AgentSandbox(root, "image", kind="codex", provider_name="codex-b",
                            environment={"HOME": str(tmp_path), "CODEX_HOME": str(second)},
                            binding="account-b")
    try:
        switched.home("implement")
    except RepairBlocked as error:
        assert error.code == "provider_configuration"
    else:
        raise AssertionError("repair sandbox reused another account")


def test_repair_container_remaps_native_home_and_respects_unset_variables(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    sandbox = AgentSandbox(tmp_path / "repair-state", "image", kind="codex",
                           environment={"HOME": "/host/home", "CODEX_HOME": "/host/account",
                                        "OPENAI_API_KEY": "selected", "ANTHROPIC_API_KEY": "other"})
    with patch.object(sandbox, "home", return_value=tmp_path / "private"), \
         patch("auto_agents.repair_v2.docker.run", return_value=(0, "")):
        with sandbox.command("plan", root, ["/usr/bin/codex"]) as command:
            variables = [command[index + 1] for index, item in enumerate(command) if item == "-e"]
    assert variables.count("CODEX_HOME=/agent-home/.codex") == 1
    assert "OPENAI_API_KEY=selected" in variables
    assert not any(value.startswith("ANTHROPIC_API_KEY=") for value in variables)
    assert not any("/host/account" in value for value in variables)
