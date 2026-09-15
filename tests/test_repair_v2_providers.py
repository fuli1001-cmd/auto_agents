from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.models import ProviderConfig
from auto_agents.repair_v2.providers import NativeDriver, AgentSandbox, toml, read_toml


@pytest.mark.parametrize('kind,binary,flag', [
    ('claude-code', 'claude', '--permission-mode'), ('copilot-cli', 'copilot', '--mode'),
    ('antigravity', 'agy', '--mode')])
def test_native_modes_and_explicit_session_handles(kind, binary, flag, tmp_path):
    config = ProviderConfig(kind=kind, binary=binary, profile_map={})
    with patch('shutil.which', return_value='/usr/bin/' + binary):
        driver = NativeDriver(config, None)
    with patch.object(driver, 'selected', return_value=('selected-model', {})):
        plan = driver.arguments('plan', 'plan this', '', None)
        implement = driver.arguments('implement', 'implement this', 'exact-session', None)
        review = driver.arguments('review', 'review this', '', None)
    assert plan[plan.index(flag) + 1] == 'plan'
    assert implement[implement.index(flag) + 1] != 'plan'
    assert review[review.index(flag) + 1] != 'plan'
    assert any('exact-session' in arg for arg in implement)
    assert all('--model' in args for args in (plan, implement, review))


def test_private_toml_preserves_native_model_settings(tmp_path):
    settings = {'model': 'selected-model', 'model_providers': {'custom': {'base_url': 'http://localhost:1000',
                'supports_websockets': False}}, 'features': {'apps': True}, 'list': ['one', 'two']}
    p = tmp_path / 'native.toml'
    p.write_text('\n'.join('"' + k + '" = ' + toml(v) for k, v in settings.items()))
    assert read_toml(p) == settings


def test_agent_boundary_exposes_no_live_project_or_controller_storage(tmp_path):
    root = tmp_path / 'candidate'; root.mkdir(); (root / '.git').mkdir()
    sandbox = AgentSandbox(tmp_path / 'agent-state', 'pinned-image')
    with patch.object(sandbox, 'home', return_value=tmp_path / 'private-home'), \
         patch('auto_agents.repair_v2.docker.run', return_value=(0, '')):
        for role in ('plan', 'implement', 'review'):
            with sandbox.command(role, root, ['/usr/bin/tool']) as args:
                mounts = [args[i + 1] for i, value in enumerate(args) if value == '--mount']
                source = next(m for m in mounts if ',dst=' + str(root) in m and '/.git' not in m)
                assert source.endswith(',readonly') == (role != 'implement')
                assert any(m.endswith('/.git,readonly') for m in mounts)
                assert not any('docker.sock' in m for m in mounts)
                assert not any('dst=' + str(tmp_path / 'agent-state') in m for m in mounts)


def test_native_jsonc_preserves_urls_and_auth_without_copying_history(tmp_path):
    from auto_agents.repair_v2.providers import read_native_json
    p = tmp_path / 'config.json'
    p.write_text('// generated configuration\n{ "url": "https://host/a//b", /* note */ "token": "x,}", }')
    assert read_native_json(p) == {'url': 'https://host/a//b', 'token': 'x,}'}


def test_antigravity_does_not_invent_an_unsupported_effort_flag(tmp_path):
    config = ProviderConfig(kind='antigravity', binary='agy', profile_map={})
    with patch('shutil.which', return_value='/usr/bin/agy'):
        driver = NativeDriver(config, None)
    with patch.object(driver, 'selected', return_value=('configured model', {})):
        assert '--effort' not in driver.arguments('plan', 'inspect this', '', None)


def test_codex_preserves_separate_implementation_and_review_effort(tmp_path, monkeypatch):
    native = tmp_path / '.codex'; native.mkdir()
    (native / 'config.toml').write_text('model="base"\n')
    (native / 'deep.config.toml').write_text('model="configured"\nmodel_reasoning_effort="high"\n')
    (native / 'max.config.toml').write_text('model="configured"\nmodel_reasoning_effort="max"\n')
    monkeypatch.setenv('CODEX_HOME', str(native))
    config = ProviderConfig(kind='codex', binary='codex', profile_map={'deep': 'deep', 'max': 'max'})
    with patch('shutil.which', return_value='/usr/bin/codex'):
        driver = NativeDriver(config, None, effort='deep', review_effort='max')
    assert driver.selected('implement')[1]['model_reasoning_effort'] == 'high'
    assert driver.selected('plan')[1]['model_reasoning_effort'] == 'max'
    assert driver.selected('review')[1]['model_reasoning_effort'] == 'max'


def test_provider_credentials_are_scoped_to_selected_backend(tmp_path, monkeypatch):
    sandbox = AgentSandbox(tmp_path / 'native', 'image', kind='codex')
    root = tmp_path / 'source'; root.mkdir()
    monkeypatch.setenv('OPENAI_API_KEY', 'selected')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'unrelated')
    monkeypatch.setenv('GH_TOKEN', 'unrelated')
    with patch.object(sandbox, 'home', return_value=tmp_path / 'private'), \
         patch('auto_agents.repair_v2.docker.run', return_value=(0, '')):
        with sandbox.command('plan', root, ['/usr/bin/codex', '--help']) as command:
            environment = [command[i + 1] for i, arg in enumerate(command) if arg == '-e']
    assert 'OPENAI_API_KEY=selected' in environment
    assert not any('unrelated' in value for value in environment)


def test_transport_mapping_does_not_rewrite_proxy_credentials():
    from auto_agents.repair_v2.providers import bridge_url
    assert bridge_url('http://localhost-user:localhost-pass@localhost:7890') == \
        'http://localhost-user:localhost-pass@host.docker.internal:7890'


def test_codex_resume_keeps_native_context_without_exporting_entire_history(tmp_path):
    import io
    import json
    config = ProviderConfig(kind='codex', binary='codex', profile_map={})
    with patch('shutil.which', return_value='/usr/bin/codex'):
        driver = NativeDriver(config, None)
    messages = [
        ('stdout', json.dumps({'id': 1, 'result': {}})),
        ('stdout', json.dumps({'id': 2, 'result': {'thread': {'id': 'existing'}, 'model': 'configured'}})),
        ('stdout', json.dumps({'method': 'item/completed', 'params': {'item': {
            'type': 'agentMessage', 'phase': 'final_answer', 'text': 'done'}}})),
        ('stdout', json.dumps({'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}})),
    ]
    process = SimpleNamespace(stdin=io.StringIO(), terminate=lambda: None)
    with patch.object(driver, 'next_message', side_effect=messages), \
         patch.object(driver, 'selected', return_value=('configured', {'model_reasoning_effort': 'max'})):
        result = driver.codex(process, None, 'implement', 'continue', tmp_path, 'existing', None, None, None)
    packets = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    resume = next(row for row in packets if row['method'] == 'thread/resume')
    assert resume['params']['threadId'] == 'existing' and resume['params']['excludeTurns'] is True
    assert result.ok and result.session == 'existing' and result.text == 'done'
