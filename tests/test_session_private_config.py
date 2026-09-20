"""Private execution must preserve historical config while using current defaults."""
import json

from auto_agents.config import save_session_state
from auto_agents.session_candidate import execution_checkout
from test_session_verification_ownership import project, git, _binding_fixture


def test_retained_legacy_config_does_not_migrate_during_private_supervision(tmp_path):
    root, state = project(tmp_path)
    path = root / '.auto-agents/config.json'
    raw = json.loads(path.read_text())
    raw.setdefault('execution', {}).setdefault('smart_timeout', {})['safety_ceiling_seconds'] = 42
    path.write_text(json.dumps(raw))
    git(root, 'add', '.'); git(root, 'commit', '-m', 'retained legacy config')
    state.baseline_head_ref = state.baseline_git_ref = git(root, 'rev-parse', 'HEAD').strip()
    save_session_state(root, state)
    session = _binding_fixture(root, state)
    original = path.read_bytes()
    with execution_checkout(session, state):
        private = session.project_root / '.auto-agents/config.json'
        before = private.read_bytes()
        config = session.config
        assert session._prepare_project_config_for_supervision() is False
        assert session.orch._prepare_project_config_for_supervision() is False
        assert private.read_bytes() == before == original
        assert session.config is config
    assert path.read_bytes() == original
