"""Switch providers only after the kernel seals a pre-execution quota refusal."""
from dataclasses import replace

from .model import KernelError, digest
from .rejections import quota_rejection


def _result(value, request):
    if not isinstance(value, dict): return value
    from ..models import AgentResult, AgentUsage, AgentTermination
    return AgentResult(**{**value, 'output_path': request.output_path,
        'usage': AgentUsage(**value['usage']) if value.get('usage') else None,
        'termination': AgentTermination(**value['termination']) if value.get('termination') else None})


def call(owner, request, key, dispatch, identity, store, stream):
    from ..prompting.core import fresh_request
    from ..provider_usage import with_attempt_usage
    tried, attempts = set(), []
    selected, attempt_key = request, key
    while True:
        try:
            result = _result(dispatch(selected, attempt_key), request)
            return with_attempt_usage(result, [*attempts, *result.usage_attempts])
        except KernelError as error:
            snapshot = store.load(stream)
            command = snapshot['commands'].get(error.details.get('command_id'), {})
            outcome = command.get('outcome') or {}
            details = outcome.get('details') or {}
            if (error.code != 'environment_blocked' or command.get('status') != 'finished'
                    or not details.get('provider_quota') or not details.get('native_result')
                    or details.get('post_source') != command.get('source')):
                raise
            receipt = store.read(details['native_result'])
            if quota_rejection(receipt) != details['provider_quota']: raise
            refused = _result(receipt, request)
            attempts.extend(refused.usage_attempts)
            provider = refused.usage_attempts[-1].get('provider') if refused.usage_attempts else details.get('provider')
            if provider not in owner.config.providers or provider in tried: raise
            tried.add(provider)
            owner._record_provider_failure(provider, refused, category='quota')
            next_provider = None
            for candidate in owner._failover_provider_order():
                if candidate in tried: continue
                candidate_key = digest([key, 'quota-failover', command['command_id'], candidate])
                operation = identity(candidate_key, candidate)
                # Cached/running attempts must be reconciled even if their
                # binary has since disappeared. Never redispatch them.
                if operation not in snapshot['operations']:
                    adapter = owner.adapter if candidate == owner.config.active_provider else owner._build_adapter_for_provider(candidate)
                    available = getattr(adapter, 'available', None)
                    if available is not None and not available():
                        tried.add(candidate)
                        owner._record_provider_failure(candidate, category='unavailable', detail='provider binary is unavailable')
                        continue
                next_provider, attempt_key = candidate, candidate_key
                break
            if next_provider is None:
                reporter = getattr(owner, 'reporter', None)
                if reporter is not None: reporter.emit('provider.unavailable', provider=provider, category='quota')
                raise
            owner.logger.info('[failover] provider=%s quota refusal sealed; trying provider=%s', provider, next_provider)
            reporter = getattr(owner, 'reporter', None)
            if reporter is not None: reporter.emit('provider.recovering', provider=provider, category='quota')
            selected = fresh_request(request, 'provider-switch',
                owner._provider_failover_handoff(selected, provider, refused))
            selected = replace(selected, usage_context={**selected.usage_context,
                'kernel_provider': next_provider, 'quota_predecessor': command['command_id']})
