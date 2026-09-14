"""Provider interruptions retain a draft and a bounded transport retry window."""
import re

from .repair_control import digest

MAX_REVIEW_DISPATCHES = 3


def invoke_review(runner, workspace, stage, instruction, context):
    from .repair_planning import _invoke, PlanningBlocked
    from .repair_capability_checks import planning_capability_fingerprint
    episode = runner._experiment.repair_episodes[context['planning_episode']]
    binding = digest({**{key: context.get(key) for key in (
        'source', 'environment', 'engine_base', 'contract_fingerprint', 'proposed_plan',
        'scope_findings', 'revision')},
        'capabilities': planning_capability_fingerprint(context.get('runtime_capabilities'))})
    recovery = episode.get('review_recovery', {})
    if recovery.get('binding') != binding or recovery.get('status') == 'complete':
        recovery = {'binding': binding, 'dispatches': 0}
        episode['review_recovery'] = recovery
    elif re.fullmatch('[a-f0-9]{32}', str(episode.get('last_request', ''))):
        previous = runner._experiment_store.root / 'planning' / episode['last_request']
        context['interrupted_review'] = {
            'request_ref': str(previous / 'request.json'), 'partial_output_ref': str(previous / 'output.json'),
            'instruction': 'Partial review observations are unverified evidence. Check them against current '
                           'source and finish the independent verdict; no earlier approval is implied.'}
    limit = 1 if episode.get('approval_recovery_used') else MAX_REVIEW_DISPATCHES
    if recovery['dispatches'] >= limit:
        raise PlanningBlocked('plan review transport recovery exhausted; retained proposal is still available',
            code='provider_recovery_exhausted', retry_kind='transport',
            actual=recovery, evidence=episode.get('last_request', ''))
    recovery.update(dispatches=recovery['dispatches'] + 1, status='running')
    runner._experiment_store.save(runner._experiment)
    try:
        result = _invoke(runner, workspace, stage, instruction, context)
    except BaseException as error:
        recovery.update(status='failed', error=type(error).__name__)
        try:
            runner._experiment_store.save(runner._experiment)
        except OSError:
            pass
        raise
    recovery['status'] = 'complete'
    runner._experiment_store.save(runner._experiment)
    return result
