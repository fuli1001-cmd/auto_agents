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
