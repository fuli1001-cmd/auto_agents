"""Reuse certified logical checks before executing the remaining batch."""
from dataclasses import replace
import hashlib
import shlex

from .execution_binding import test_invocations
from .gate_execution import _effective_result_cache_scope, _metadata_signature
from .gates import GateCommandMetadata
from .models import CommandResult


def constituents(executor, command):
    item = executor.metadata.get(command)
    members = getattr(item, 'constituents', {})
    if (not members or not executor.use_result_cache
            or executor._cache_miss_reasons.get(command) == 'proof_audit_sample'):
        return [], []
    cached, missing = [], []
    for original, raw in members.items():
        metadata = GateCommandMetadata(**raw)
        result, _ = executor.result_cache.lookup_with_reason(original,
            source_fingerprint=executor.snapshot.tree_sha, cache_scope=metadata.cache_scope,
            result_cache_scope=_effective_result_cache_scope(metadata),
            metadata_signature=_metadata_signature(metadata, executor.dependency_links))
        if (result is not None and metadata.proof_ids and 'pytest' in original
                and '--collect-only' not in original and not result.executed_tests):
            result = None
        if result is not None and result.backend == 'result-cache-observed-inputs' and executor.input_reuse_mode != 'on':
            result = None
        if result is not None and executor.proof_audit_sample_rate:
            bucket = int(hashlib.sha256(f'{executor.snapshot.tree_sha}\0{original}'.encode()).hexdigest()[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
            if bucket < executor.proof_audit_sample_rate:
                result = None
        if result is None:
            missing.append(original)
        else:
            cached.append(result)
    return cached, missing


def combine(command, results):
    result = CommandResult(command, all(row.ok for row in results),
                           next((row.returncode for row in results if not row.ok), 0),
                           cached=all(row.cached for row in results), backend='coalesced-proofs')
    for row in results:
        result.stdout += row.stdout
        result.stderr += row.stderr
        result.duration_seconds += row.duration_seconds
        result.queue_seconds += row.queue_seconds
        result.executed_tests.extend(node for node in row.executed_tests if node not in result.executed_tests)
        result.test_results.update(row.test_results)
        if row.cached:
            for node in row.executed_tests:
                result.test_results.setdefault(node, {'phases': {phase: 'passed' for phase in ('setup', 'call', 'teardown')},
                                                      'certificate_reused': True})
        result.artifacts.update(row.artifacts)
        result.mutation_paths.extend(row.mutation_paths)
        result.infrastructure_error |= row.infrastructure_error
        result.cleanup_incomplete |= row.cleanup_incomplete
        result.termination_reason = row.termination_reason or result.termination_reason
        result.infrastructure_failure_id = row.infrastructure_failure_id or result.infrastructure_failure_id
        result.network_observed |= row.network_observed
        result.test_timings.extend(row.test_timings)
    result.input_trace_complete = all(row.input_trace_complete for row in results)
    for row in results:
        for path, value in row.observed_inputs.items():
            if path in result.observed_inputs and result.observed_inputs[path] != value:
                result.input_trace_complete = False
            result.observed_inputs[path] = value
    if not result.input_trace_complete:
        result.observed_inputs = {}
    result.process_snapshot['reused_tests'] = [node for row in results if row.cached for node in row.executed_tests]
    result.process_snapshot['constituent_proofs'] = [{'command': row.command, 'cached':row.cached, 'proof_ref':row.proof_ref}
                                                   for row in results]
    return result


def run_remaining(executor, command, cached, missing, execute):
    metadata = executor.metadata[command]
    targets, remaining = set(), []
    proof_ids = []
    for original, raw in metadata.constituents.items():
        invocations = test_invocations(original)
        if len(invocations) != 1 or not invocations[0].targets:
            return None
        targets.update(invocations[0].targets)
        if original in missing:
            remaining.extend(invocations[0].targets)
            proof_ids.extend(raw['proof_ids'])
    actual = shlex.join([arg for arg in shlex.split(command) if arg not in targets] + sorted(set(remaining)))
    executor.metadata[actual] = replace(metadata, proof_ids=list(dict.fromkeys(proof_ids)), constituents={})
    try:
        fresh = execute(actual)
    finally:
        if actual != command:
            executor.metadata.pop(actual, None)
        else:
            executor.metadata[command] = metadata
    return combine(command, [*cached, fresh])
