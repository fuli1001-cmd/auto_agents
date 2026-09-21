"""Evidence for terminal workflow results that did not raise an exception."""
import hashlib
import json
from pathlib import Path

from .config import run_path, session_dir
from .diagnostic_output import clean_payload
from .io_utils import write_json


class ControlledWorkflowFailure(RuntimeError):
    def __init__(self, evidence):
        self.evidence = clean_payload(evidence)
        self.fingerprint = hashlib.sha256(json.dumps(self.evidence, sort_keys=True).encode()).hexdigest()
        super().__init__(f"{evidence['kind']} {evidence['subject_id']} returned {evidence['status']} "
                         f"({evidence['resolution']}): {self.evidence['reason']}")


def capture(state):
    """A reported blocker is diagnostic evidence, never an ownership decision."""
    if getattr(state, 'status', '') not in {'blocked', 'failed'}:
        return None
    session_id, run_id = getattr(state, 'session_id', ''), getattr(state, 'run_id', '')
    if not session_id and not run_id:
        return None
    evidence = {'schema_version': 1, 'kind': 'session' if session_id else 'run',
                'subject_id': session_id or run_id, 'status': state.status,
                'resolution': getattr(state, 'resolution', '')}
    if session_id:
        acceptance = getattr(state, 'acceptance_execution', {}) or {}
        relevant = acceptance if evidence['resolution'].startswith('acceptance_') else {}
        result, review = relevant.get('result') or {}, relevant.get('review') or {}
        recent = list(getattr(state, 'execution_log', []) or [])[-5:]
        recent_reason = str(recent[-1].get('result', '')) if recent else ''
        evidence.update(
            mode=state.mode, workflow_id=getattr(state, 'workflow_id', ''),
            goal=getattr(state, 'goal', ''),
            user_instructions=[item.get('content', '') for item in getattr(state, 'conversation', [])
                               if item.get('role') == 'user'],
            state_ref=f'.auto-agents/state/sessions/{session_id}/session_state.json',
            code_root=(getattr(state, 'candidate_custody', {}) or {}).get('checkout', ''),
            reason=(relevant.get('error') or
                    (review.get('reason') if evidence['resolution'] == 'acceptance_review_rejected' else '') or
                    result.get('summary') or evidence['resolution'] or recent_reason or 'session stopped without a reason'),
            acceptance={k: relevant[k] for k in ('identity', 'phase', 'directory', 'result', 'review', 'error')
                        if k in relevant},
            derived_acceptance_plan=(relevant.get('inputs') or {}).get('request', {}),
            recent_execution=recent,
        )
    else:
        blocker = getattr(state, 'active_blocker', {}) or {}
        evidence.update(mode='run', workflow_id=(getattr(state, 'resume_context', {}) or {}).get('workflow_id', ''),
                        reason=blocker.get('reason') or getattr(state, 'last_error', '') or 'run stopped without a reason',
                        stage=getattr(state, 'current_stage', ''), blocker=blocker,
                        state_ref='.auto-agents/state/run_state.json')
    return ControlledWorkflowFailure(evidence)


def record(project_root, failure, triage=None, *, error=''):
    """Persist diagnosis beside its subject without rewriting an ambient run."""
    evidence = failure.evidence
    diagnosis = triage.to_dict() if triage is not None else None
    final = getattr(getattr(triage, 'root_cause', None), 'final', None)
    judgment = getattr(triage, 'judgment', None)
    owner = getattr(final or judgment, 'owner', 'unknown')
    payload = clean_payload({'schema_version': 1, 'fingerprint': failure.fingerprint,
                             'failure': evidence, 'owner': owner, 'triage': diagnosis,
                             'diagnosis_error': error})
    root = (session_dir(Path(project_root), evidence['subject_id']) if evidence['kind'] == 'session' else
            run_path(Path(project_root), evidence['subject_id']) / 'outputs')
    path = root / 'terminal-triage.json'
    write_json(path, payload)
    return path, owner
