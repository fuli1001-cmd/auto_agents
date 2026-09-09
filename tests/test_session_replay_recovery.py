"""Replay the retained mixed-repository recovery shape through Session.resume."""
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

from auto_agents.config import load_session_state, save_session_state
from auto_agents.git_ops import head_ref
from auto_agents.root_cause import RootCauseCoordinator
from auto_agents.self_repair import AutoAgentsSelfRepairRunner, auto_agents_repo_root
from test_engine_child_recovery import parent_workflow
from test_session_verification_ownership import git, project


def test_replay_resumes_mixed_verification_child_for_reconciliation(tmp_path):
    root, child = project(tmp_path)
    (root / '.conda').unlink()
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'source snapshot without dependencies')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    child.current_attempt = 3
    child.provider_continuations = {'fix': {'provider_session_id': 'obsolete-fix'}}
    foreign_ref = str(tmp_path / 'engine/tests/test_engine.py::test_contract')
    child.fix_verify_command = (
        'conda run -p ./.conda python -m pytest -q tests/test_owned.py::test_owned'
        ' && conda run -p ./.conda python -m pytest -q ' + foreign_ref
    )
    # The engine binding lives on the original handoff, with an active resume
    # wrapper selecting its existing failed child. Dependencies are absent,
    # just as in diagnostic copies; no target verification can run here.
    store, snapshot, original = parent_workflow(root, child)
    original.payload['issue_seed'] = {'target_repository': str(tmp_path / 'engine'), 'scope': 'session recovery'}
    store.save_handoff(original)
    store.record_result(snapshot, original, status='failed', result={'status': 'failed'})
    store.consume_result(snapshot, original, operation_id='prior-return')
    resume = store.prepare_handoff(snapshot, parent=snapshot.root, target='resume',
        goal=child.goal, reason='resume after engine repair', payload={'resume_handoff_id': original.handoff_id})
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = resume.handoff_id
    save_session_state(root, parent)
    protected = [root / 'foreign.py', root / '.git/index', store.handoff_path(original.handoff_id),
                 root / f'.auto-agents/state/sessions/{child.session_id}/session_state.json']
    before = {str(path): path.read_bytes() for path in protected}
    frozen = tmp_path / 'frozen'
    RootCauseCoordinator._copy_diagnostic_tree(root, frozen)

    # Run the actual session_replay.py entrypoint inside its real sandbox.
    # Inspect the copied child after the driver stops to prove that the
    # provider boundary belongs to classification, not to the parent after
    # a failed child, and that no check or completed fix is being attested.
    guard = tmp_path / 'guard.py'
    guard.write_text('''import json, subprocess, sys
from pathlib import Path
protected, child_id = json.loads(sys.argv[1]), sys.argv[2]
for raw in protected:
    path = Path(raw)
    content = path.read_bytes()
    try:
        path.write_bytes(content + b"unexpected mutation")
    except OSError:
        pass
    else:
        raise AssertionError("replay input was writable: " + raw)
arguments = sys.argv[3:]
target = Path(arguments[-3])
process = subprocess.run(arguments, capture_output=True, text=True)
assert process.returncode == 0, process.stderr
payload = json.loads(process.stdout.splitlines()[-1])
state = json.loads((target / ".auto-agents/state/sessions" / child_id / "session_state.json").read_text())
original = json.loads(Path(protected[-1]).read_text())
assert state["mode"] == "fix" and state["status"] == "conversing", process.stderr
assert state["current_attempt"] == 0 and state["verification_binding"] == {}, process.stderr
assert state["provider_continuations"] == {}
assert state["fix_verify_command"] == original["fix_verify_command"]
assert state["goal"] == original["goal"]
assert state["goal_execution_environment"] == original["goal_execution_environment"]
entry = next(row for row in state["execution_log"] if row["action"] == "engine_verification_reconciliation")
assert entry["verification_command"] == state["fix_verify_command"]
assert entry["engine_verification_refs"]
assert len(list((target / ".auto-agents/state/sessions").iterdir())) == 2
print(json.dumps(payload))
''')
    runner = AutoAgentsSelfRepairRunner.__new__(AutoAgentsSelfRepairRunner)
    runner.target_project_root = frozen
    runner._real_project_root = root
    runner._invocation_context = {'command': 'collab', 'session_id': parent.session_id,
                                  'engine_route': original.payload}
    runner._verification_python = lambda: sys.executable
    runner._autonomy_config = lambda: SimpleNamespace(replay_timeout_seconds=60)
    verification = runner._verification_argv

    @contextmanager
    def guarded(arguments, cwd, **kwargs):
        arguments = [sys.executable, str(guard), json.dumps(list(before)), child.session_id, *arguments]
        kwargs['read_roots'] = [*kwargs.get('read_roots', []), tmp_path]
        with verification(arguments, cwd, **kwargs) as command:
            yield command
    runner._verification_argv = guarded
    result = runner._session_probe(auto_agents_repo_root())
    assert result.get('route_consumed') is True, result
    assert result['ok'] is True and result['status'] == 'next_provider_boundary', result
    assert {str(path): path.read_bytes() for path in protected} == before
    assert load_session_state(root, child.session_id).status == 'failed'
