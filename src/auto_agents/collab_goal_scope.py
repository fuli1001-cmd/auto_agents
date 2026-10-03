"""Keep explicitly existing-behavior acceptance within its user-owned scope."""
import re
from pathlib import Path


def acceptance_only(goal):
    text = re.sub(r'\s+', ' ', str(goal)).lower()
    existing = re.search(r'已有功能|现有功能|existing (?:behavior|functionality|features|product)', text)
    acceptance = re.search(r'验收|acceptance|end.to.end|\be2e\b', text)
    bounded = re.search(r'不新增产品需求|不新增功能|不重新设计或规划|'
                        r'no new (?:product )?(?:requirements|features)|acceptance.only|not development', text)
    return bool(existing and acceptance and bounded)


def route_error(session, state, target, payload):
    if session.mode != 'collab' or not acceptance_only(state.goal):
        return ''
    from .execution_binding import repository_binding_error
    # Independently admitted engine maintenance still has its own scope guard.
    if repository_binding_error(session.project_root, payload):
        return ''
    from .session_acceptance import is_request
    if target == 'run' and not is_request(target, payload):
        return ('The user requested acceptance of existing behavior without new requirements or planning. '
                'Do not adopt a development run, old task plan or unfinished feature backlog. '
                'Route acceptance to observe the exact code revision, runtime and browser behavior first. '
                'A concrete blocker may use a focused fix followed by acceptance; prior approved specs alone '
                'do not prove a feature is missing or authorize completing it now.')
    if target == 'resume':
        try:
            from .workflow_chain import WorkflowStore
            root = Path(getattr(session, '_custody_control_root', session.project_root))
            original = WorkflowStore(root).resolve_handoff_chain(
                payload.get('resume_handoff_id', ''), workflow_id=state.workflow_id)[-1]
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return ''  # The ordinary identity guard owns malformed resume routes.
        if original.target == 'run':
            return 'The existing-behavior acceptance goal cannot resume a development run. Reassess retained browser/runtime evidence and route acceptance or a focused blocker fix.'
    if target == 'fix':
        seed = payload.get('issue_seed', {})
        scope = seed.get('verification_scope', {}) if isinstance(seed, dict) else {}
        if (not isinstance(scope, dict) or scope.get('mode') != 'focused_fix'
                or seed.get('task_id') or seed.get('task_ids')):
            return ('Use verification_scope={"mode":"focused_fix"} for this acceptance blocker. '
                    'Do not adopt saved task ownership or the whole development plan. '
                    'Retain the observed failure, reproduction and targeted regression checks; '
                    'the parent acceptance owns browser execution and video/content verification.')
        reproduction = seed.get('reproduction')
        if (not reproduction or not isinstance(reproduction, (str, list))
                or not str(seed.get('verification_command', '')).strip()):
            return ('A focused acceptance fix requires the concrete observed failure/reproduction and '
                    'a targeted verification_command. Inspect current browser/runtime evidence first; '
                    'a planned requirement, old prototype or task completion claim alone is not a blocker diagnosis.')
    return ''


INSTRUCTION = (
    'The explicit user goal is existing-behavior acceptance only. Use the existing frontend and current '
    'authorized code revision. Do not turn missing/hidden UI observed once into an unimplemented feature: '
    'check entry reachability, project recovery eligibility, running service code/configuration and earlier '
    'delivery evidence first. Historical specs or prototypes are context, not a request to finish their backlog. '
    'On a concrete acceptance blocker, route only a focused fix with reproduction evidence and targeted '
    'regression checks, then return to acceptance. Do not route run, resume a development child, replan, '
    'regenerate requirements or require whole-project done-task proofs to execute the browser goal.'
)
