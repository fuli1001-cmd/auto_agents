"""Offline acceptance endpoint; only the model's reply is simulated."""
import json
from pathlib import Path
import sys

sys.path.insert(0, '/work/src')
from auto_agents.config import load_session_state
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.session_source import register_checkout

request = json.loads(Path('/result/candidate-request.json').read_text())
root = Path(request['project'])
child = load_session_state(root, request['child'])
parent = load_session_state(root, request['parent'])
register_checkout(root, child, Path(child.candidate_custody['checkout']))
budget = lambda s: (s.current_attempt, s.attempt_epoch, s.attempts_since_progress, s.max_attempts, s.hard_ceiling)
original_budget, child_budget = budget(parent), budget(child)
candidate = child.candidate_custody['receipt']['fingerprint']
calls = []

def provider(self, req):
    assert req.purpose == 'proof_review', 'Unexpected model invocation: ' + req.purpose
    calls.append(req.purpose)
    assert req.sandbox_mode == 'read-only' and not req.resume_session_id
    value = json.loads(req.prompt.split('\n', 1)[1])
    assert value['candidate'] == candidate
    return AgentResult(True, [], req.output_path, summary=json.dumps({
        'decision': 'approve', 'reason': 'Fixture approval for the retained requirement; real tests must still execute.',
        'change_coverage': [{'path': p, 'reason': 'Snapshot repair and its regression'} for p in value['delta']],
        'coverage': [{'path': name, 'requirement': 'original_goal',
                      'reason': 'Preserves reference digest and duration/resolution checks; explicit FPS remains rejected.'}
                     for name in value['changes']]}))

class Returned(BaseException):
    pass

phase = Session._phase_collab_loop
def observe_parent(self, state):
    saved = load_session_state(root, child.session_id)
    if saved.status == 'completed':
        raise Returned()
    return phase(self, state)

Orchestrator._call_with_failover = provider
Session._phase_collab_loop = observe_parent
try:
    result = Session(Orchestrator(root), mode='collab', auto_approve=parent.auto_approve).resume(parent.session_id)
    raise AssertionError('Parent did not continue after candidate delivery: ' + result.status + ':' + result.resolution)
except Returned:
    pass
child = load_session_state(root, child.session_id)
parent = load_session_state(root, parent.session_id)
verifications = [r for r in child.execution_log if r.get('action') == 'receipt_verification'
                 and r.get('verification', {}).get('ok')]
assert child.status == 'completed' and child.candidate_custody.get('delivered_revision')
assert child.candidate_custody['receipt']['fingerprint'] == candidate
assert budget(child) == child_budget and budget(parent) == original_budget
assert len(calls) == 1 and verifications
assert verifications[-1]['verification']['executed_commands'] > 0
Path('/result/candidate-result.json').write_text(json.dumps({'ok': True, 'calls': calls,
    'candidate_preserved': True, 'parent_budget_preserved': True, 'child_budget_preserved': True,
    'executed_commands': verifications[-1]['verification']['executed_commands'],
    'delivered_revision': child.candidate_custody['delivered_revision']}))
