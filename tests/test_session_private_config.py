"""Private execution must preserve historical config while using current defaults."""
import json
from pathlib import Path

import pytest

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
    original_orchestrator = session.orch
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
    assert session.project_root == root and session.orch is original_orchestrator
    assert not getattr(session.orch, '_retained_session_configuration', False)


def test_collab_consumed_delivery_preserves_legacy_config_without_verification_binding(tmp_path, monkeypatch):
    from auto_agents.config import load_session_state, migrate_project_config
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.run_lock import ProjectRunLock, require_project_run_lock
    from auto_agents.session import Session
    from test_engine_child_recovery import (configure_local_writer, parent_workflow,
                                            ObservationBoundary, REAL_PROVIDER_CALL)

    root, child = project(tmp_path)
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')")
    path = root / '.auto-agents/config.json'
    raw = json.loads(path.read_text())
    # Keep this manually configured gate stable during parent preparation.
    raw['gates']['allow_agent_updates'] = False
    raw.setdefault('execution', {}).setdefault('smart_timeout', {})['safety_ceiling_seconds'] = 42
    path.write_text(json.dumps(raw))
    historical = path.read_bytes()
    git(root, 'add', '.auto-agents/config.json')
    git(root, 'commit', '-m', 'historical configuration for retained delivery')
    child.baseline_head_ref = child.baseline_git_ref = git(root, 'rev-parse', 'HEAD').strip()
    save_session_state(root, child)
    store, workflow, handoff = parent_workflow(root, child)
    # Retain the child's contract before the shared configuration is upgraded.
    # Otherwise a newly bound child correctly captures the already migrated file.
    _binding_fixture(root, child)
    calls = []

    def provider(self, request):
        calls.append(request.purpose)
        if request.purpose.startswith('collab'):
            parent = load_session_state(root, 'parent')
            assert not parent.verification_binding
            assert parent.workflow_id == workflow.workflow_id
            assert parent.candidate_custody['consumed_delivery']
            assert request.cwd == Path(parent.candidate_custody['checkout']) != root
            assert self.config is orchestrator.config
            assert (request.cwd / '.auto-agents/config.json').read_bytes() == historical
            assert (request.cwd / 'value.py').read_text() == 'VALUE = 1\n'
            assert require_project_run_lock(root) is lock
            with pytest.raises(RuntimeError, match='requires an acquired ProjectRunLock'):
                require_project_run_lock(request.cwd)
            raise ObservationBoundary()
        assert request.purpose == 'fix'
        return REAL_PROVIDER_CALL(self, request)

    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    with ProjectRunLock(root, environ={}) as lock:
        # The shared project is current and locked; only the historical private
        # checkout needs migration, exactly as in the failed parent continuation.
        assert migrate_project_config(root)
        shared = path.read_bytes()
        assert shared != historical
        for _ in range(2):
            orchestrator = Orchestrator(root)
            session = Session(orchestrator, mode='collab', auto_approve=True)
            with pytest.raises(ObservationBoundary):
                session.resume('parent')
            assert session.project_root == root and session.orch is orchestrator
            assert not getattr(orchestrator, '_retained_session_configuration', False)
            assert path.read_bytes() == shared
        completed = load_session_state(root, child.session_id)
        assert completed.status == 'completed' and completed.current_attempt == 1
        assert store.load_handoff(handoff.handoff_id).returned_at
        assert calls[0] == 'fix' and len(calls) == 3
        assert all(purpose.startswith('collab') for purpose in calls[1:])
        parent = load_session_state(root, 'parent')
        assert parent.candidate_custody['consumed_delivery']['revision'] == completed.candidate_custody['delivered_revision']
        assert (root / 'value.py').read_text() == 'VALUE = 0\n'
