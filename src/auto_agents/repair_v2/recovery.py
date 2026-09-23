"""Recovery failures have owners; unknown failures never authorize code changes."""
from dataclasses import asdict

from .store import digest
from .types import RecoveryContext, RepairFailure


def context(payload, request_digest, ledger):
    return asdict(RecoveryContext(request_digest, payload.get('invocation', {}),
                                  payload.get('boundary', {}), payload.get('goal_scope', {}), str(ledger)))


def classify(result, payload):
    observed = result.get('observed') or {}
    if not isinstance(observed, dict): observed = {}
    runtime = observed.get('engine_runtime') or {}
    mismatch = set(runtime.get('mismatches') or [])
    domain, code, owner, stage = 'unknown', 'recovery_unclassified', 'controller', 'subscriber_validation'
    message = '恢复检查未通过，原因尚未明确；已保留代码验收结果，停止自动实施。'
    if result.get('proof_incomplete'):
        domain, code, owner, stage = 'controller', 'recovery_proof_incomplete', 'controller', 'boundary_preflight'
        message = '恢复验证证据不完整；候选已保留，需要更新验证控制器后重新检查原任务。'
    elif result.get('infrastructure') or result.get('cancelled'):
        domain, code, owner = 'environment', 'verification_infrastructure', 'environment'
        message = result.get('reason') or '恢复验证环境不可用；已保留代码验收结果。'
    elif mismatch:
        domain, code, owner, stage = 'artifact', 'runtime_artifact_invalid', 'artifact_builder', 'artifact'
        message = ('代码已验证，但运行目录缺少独立的 Git 信息；需要重新生成运行产物。'
                   if mismatch <= {'commit', 'commit_unavailable'} and 'commit_unavailable' in mismatch
                   else '实际加载的引擎与已验证产物不一致；停止交付并检查运行产物。')
    else:
        child = observed.get('recovery_observation') or {}
        invocation = payload.get('invocation') or {}
        # A trusted, source-matched execution must have reached the original
        # recovery harness. An exception string alone is not a counterexample.
        bound = bool(invocation.get('session_id') and runtime.get('ok') and child and not child.get('ok')
                     and child.get('parent_session_id') == invocation.get('session_id')
                     and child.get('child_session_id'))
        run = bool(invocation.get('run_id') and observed.get('run_id') == invocation['run_id']
                   and observed.get('same_blocker') is True)
        if bound or run:
            domain, code, owner, stage = 'candidate', 'original_boundary_failed', 'candidate', 'implement'
            message = '已验证引擎实际执行后，原任务的恢复条件仍未通过；保留原目标继续修复。'
    return asdict(RepairFailure(domain, code, owner, stage, message, result))


def block(store, state, failure, *, operation):
    prior = state.get('recovery_failure') or {}
    store.transition(state, status='blocked', phase=failure['recover_at'],
                     blocker={'code': failure['code'], 'message': failure['message']},
                     recovery_failure=failure, recovery_operation=operation,
                     repeated_failure=prior == failure)
    store.event('recovery_blocked', domain=failure['domain'], code=failure['code'], operation=operation)


def operation(job, subscriber, artifact, recovery_context):
    return digest([job['id'], job['generation'], subscriber['id'],
                   artifact['artifact_id'], recovery_context])


def completed_result(job):
    """Recover the SQLite projection after a crash following a durable ACK."""
    from .store import Store
    result = job.get('result') or {}
    if result.get('recovery_protocol') not in (1, 2) or not result.get('v2_transaction'):
        return None
    state = Store(result['v2_transaction']).load() or {}
    receipt = state.get('live_recovery') or {}
    if (state.get('status') != 'complete' or state.get('phase') != 'recovered'
            or receipt.get('job') != job['id'] or receipt.get('generation') != job['generation']
            or receipt.get('operation') != state.get('recovery_operation')
            or receipt.get('artifact_id') != result.get('runtime_artifact', {}).get('artifact_id')
            or state.get('receipt') != result.get('v2_receipt', {}).get('reference')):
        return None
    return {**result, 'ok': True, 'status': 'recovered',
            'engine_full_proof': {**result.get('engine_full_proof', {}), 'ok': True, 'recovered': True}}
