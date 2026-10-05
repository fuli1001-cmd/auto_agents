from test_session_verification_ownership import git
import json
import sys
from pathlib import Path
"""Business-only workflow fixtures; no engine repair controller."""
import pytest
from auto_agents.config import save_session_state, load_session_state
from auto_agents.models import SessionState, AgentResult
from auto_agents.git_ops import head_ref
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
from test_session import _confirm_collab_state
REAL_PROVIDER_CALL = Orchestrator._call_with_failover

class ObservationBoundary(BaseException):
    pass

def parent_workflow(root, child, *, engine=False):
    store = WorkflowStore(root)
    parent = _confirm_collab_state(SessionState(
        session_id='parent', mode='collab', status='waiting_child', auto_approve=True,
        goal='Continue the already selected real project; request browser observation.',
    ), 'real')
    snapshot = store.create_root(WorkflowRef('collab', parent.session_id))
    parent.workflow_id = child.workflow_id = snapshot.workflow_id
    original = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix',
        goal=child.goal, reason='repair the saved defect', payload={'child_session_id': child.session_id,
            'head_before': head_ref(root), 'auto_approve': True, 'task_id': 'task-owned'})
    store.bind_child(snapshot, original, WorkflowRef('fix', child.session_id))
    child.parent_handoff_id = original.handoff_id
    child.goal_execution_environment = dict(parent.goal_execution_environment)
    child.conversation = [{'role': 'user', 'content': 'Reuse the existing project and verified provider semantics.'}]
    save_session_state(root, child)
    handoff = original
    if engine:
        store.record_result(snapshot, original, status='failed', result={'status': 'failed', 'resolution': 'verification_inconclusive'})
        store.consume_result(snapshot, original, operation_id='original-return')
        handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix',
            goal='Repair the engine then continue the saved child', reason='engine recovery',
            payload={'child_session_id': child.session_id,
                     'issue_seed': {'target_repository': str(root.parent / 'engine'), 'scope': 'session verification'}})
    parent.active_handoff_id = handoff.handoff_id
    save_session_state(root, parent)
    return store, snapshot, handoff

def resume_to_observation(root, monkeypatch, action, *, observe=None, real_dispatch=False):
    def agent(self, request):
        if request.purpose.startswith('collab'):
            if observe is not None:
                observe(request)
            raise ObservationBoundary()
        state = load_session_state(root, 'owned-child')
        reply = action(state, request.prompt, request.cwd)
        if real_dispatch:
            assert request.writer_boundary is not None
            return REAL_PROVIDER_CALL(self, request)
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', agent)
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')

def configure_local_writer(root, child, program, *, read_only_program=''):
    """Freeze deterministic transport before the retained baseline is captured."""
    from auto_agents.config import load_project_config, save_project_config
    from auto_agents.models import ProviderConfig
    binary = root / 'receipt-provider'
    controls = '''
import ctypes, errno, os, tempfile
with tempfile.NamedTemporaryFile(dir=os.environ['TMPDIR']) as private:
    p = Path(private.name); p.chmod(0o750)
    assert p.stat().st_mode & 0o777 == 0o750
    os.fchmod(private.fileno(), 0o640)
    assert p.stat().st_mode & 0o777 == 0o640
for name in SHARED:
    p=Path(name); before,mode=p.read_bytes(),p.stat().st_mode
    fd=os.open(p,os.O_RDONLY)
    try:
        for action in (lambda: p.write_bytes(b'bad'), lambda: p.chmod(0o777),
                       lambda: os.fchmod(fd,0o777), lambda: os.chmod('/proc/self/fd/'+str(fd),0o777)):
            try: action()
            except OSError as error: assert error.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
            else: raise AssertionError('shared input was writable')
        assert p.read_bytes()==before and p.stat().st_mode==mode
    finally: os.close(fd)
'''
    binary.write_text('#!' + sys.executable + '\nimport json,sys,subprocess,shutil\nfrom pathlib import Path\n'
        "if '--help' in sys.argv or '--version' in sys.argv:\n    print('local Claude fixture --output-format --permission-mode --dangerously-skip-permissions'); sys.exit(0)\n"
        'PROMPT = sys.stdin.read()\n' + read_only_program + '\nSHARED=' + repr([str(root / 'foreign.py'), str(root / '.git/index')]) + '\n'
        + controls + '\n' + program + '\n'
        "print(json.dumps({'type':'result','subtype':'success','result':'Fixed\\nCOMMIT_MESSAGE: Repair owned value'}))\n")
    binary.chmod(0o755)
    config = load_project_config(root)
    config.active_provider = 'claude-code'
    config.providers = {'claude-code': ProviderConfig(kind='claude-code', binary=str(binary), profile_map={})}
    save_project_config(root, config)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain deterministic provider transport')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
