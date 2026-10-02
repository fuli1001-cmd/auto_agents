"""Restore controller-bound continuation inputs without rewriting history."""
from pathlib import Path

from .config import save_run_state
from .io_utils import read_text
from .workflow_chain import IterationSpecBuilder, WorkflowRef, WorkflowStore, sha256_text


RESTORE = 'iteration_spec_context_restore'
EXISTING_SCOPE = 'routed_iteration_handoff_id'


def existing_scope(handoff, seed):
    """An explicit existing project or contract carries prior requirements."""
    return ({EXISTING_SCOPE: handoff.handoff_id}
            if seed.get('existing_project_id') or seed.get('approved_contract_refs') else {})


def restore(project_root, state, spec_file):
    """Upgrade only a verified legacy request before requirements are delivered."""
    root = Path(project_root).resolve()
    context = state.resume_context
    if (not context.get('parent_handoff_id') or not context.get('workflow_id')
            or state.current_stage != 'clarify' or state.stage_summaries or state.tasks
            or state.pending_approval or state.approved_gates or state.rejected_stage
            or state.status not in {'pending', 'failed', 'paused', 'waiting_user', 'running'}):
        return Path(spec_file)
    path = Path(spec_file).expanduser().resolve()
    retained = Path(str(context.get('spec_file', ''))).expanduser().resolve()
    if path != retained or not path.is_relative_to(root / 'specs/iterations'):
        return Path(spec_file)
    store = WorkflowStore(root)
    chain = store.resolve_handoff_chain(str(context['parent_handoff_id']), workflow_id=str(context['workflow_id']))
    handoff = chain[-1]
    if handoff.target != 'run' or handoff.child != WorkflowRef('run', state.run_id):
        raise RuntimeError('iteration spec context has a conflicting run owner')
    seed = dict(handoff.payload.get('spec_seed', {}))
    environment = handoff.payload.get('goal_execution_environment')
    if isinstance(environment, dict) and environment:
        seed.setdefault('goal_execution_environment', dict(environment))
    # Known v1 fields already survived rendering. Only restore when actual
    # continuation information was omitted, rather than rerunning discovery.
    known = {'title', 'summary', 'goal', 'gap', 'actual', 'capability', 'requested_change',
             'acceptance', 'non_goals', 'evidence', 'open_decisions', 'goal_execution_environment'}
    if not (set(seed) - known):
        return Path(spec_file)
    title = str(seed.get('title') or seed.get('summary') or handoff.goal or 'Iteration').strip()
    builder = IterationSpecBuilder(root)
    content = read_text(path)
    old_sha = sha256_text(content)
    if old_sha != context.get('iteration_spec_sha256'):
        raise RuntimeError('iteration spec changed outside its retained handoff')
    if content == builder.render(handoff, seed, title=title):
        return Path(spec_file)
    if content != builder.render(handoff, seed, title=title, legacy=True):
        raise RuntimeError('iteration spec is not the retained legacy request')
    spec = builder.materialize(handoff, seed)
    updated = root / spec['path']
    context.update(spec_file=str(updated), iteration_spec_sha256=spec['sha256'],
                   iteration_spec_commit=spec['commit_sha'])
    context.update(existing_scope(handoff, seed))
    context[RESTORE] = {'handoff_id': handoff.handoff_id, 'previous_spec_file': str(path),
                      'previous_spec_sha256': old_sha, 'spec_sha256': spec['sha256']}
    for key in ('authorization_policy', 'goal_execution_environment'):
        if isinstance(handoff.payload.get(key), dict):
            context.setdefault(key, dict(handoff.payload[key]))
    # Neither approval flags, stage summaries nor attempt counters are changed.
    save_run_state(root, state)
    return updated


def reconcile_history(state, spec_file, history):
    receipt = state.resume_context.get(RESTORE, {})
    if not receipt or receipt.get('spec_sha256') != sha256_text(read_text(Path(spec_file))):
        return False
    identity = receipt['spec_sha256']
    if any(isinstance(row, dict) and row.get(RESTORE) == identity for row in history):
        return False
    history.append({'role': 'orchestrator', RESTORE: identity, 'content':
        'The controller restored continuation inputs omitted by the old spec renderer. '
        'Read the corrected immutable spec: ' + str(spec_file) + '. '
        'Earlier questions and readiness based on the incomplete spec remain historical context. '
        'Reassess them against the retained steps, project identity, contract references and constraints. '
        'Preserve every actual user reply; this message supplies context, not a user answer or a new approval. '
        'Proceed when the retained scope is sufficient; ask only about genuinely unresolved decisions.'})
    return True
