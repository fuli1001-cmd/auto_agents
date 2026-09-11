"""Bounded component verification in independent, committed engine worktrees."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
import os
import subprocess

from .repair_memory import component_key
from .repair_schedule import pytest_parts


def run_component_checks(runner, commands, workspace):
    """Keep every command's cohort/options and stop dispatch after a failure.

    Quick checks remain serial. Expanded pytest commands get separate worktrees,
    private sandbox HOME/tmp/network and the existing host resource leases.
    Shell preparation or uncommitted inputs retain the original serial path.
    """
    from .self_repair import _FullSuiteShard, _FullSuiteSlots, _VerificationResult

    workers = min(2, max(1, (os.cpu_count() or 2) // 2))
    parsed = [pytest_parts(command) for command in commands]
    if workers == 1 or len(commands) < 2 or not all(parsed):
        return runner._run_verification_commands(commands, workspace)
    clean = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=all'],
                           cwd=workspace, capture_output=True, check=True)
    committed = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'],
                               cwd=workspace, capture_output=True)
    if clean.stdout or committed.returncode:
        return runner._run_verification_commands(commands, workspace)
    environment = runner._full_suite_environment_fingerprint()
    group = runner._candidate_group
    timings = runner._experiment.component_memory.get(component_key(group), {}).get('check_timings', {})
    pending = []
    for index, (command, parts) in enumerate(zip(commands, parsed)):
        resources = set()
        files = {}
        for target in parts[1]:
            files.setdefault(target.split('::', 1)[0], []).append(target)
        for name, targets in files.items():
            resources.update(runner._full_suite_shard_resources(workspace, name, tuple(targets))[0])
        seconds = timings.get(command, {}).get('seconds', 60)
        # A short-check wave detects fixture failures before launching matrices.
        priority = int(isinstance(seconds, (int, float)) and seconds > 180)
        pending.append((index, _FullSuiteShard(str(index), '', tuple(parts[1]),
            parallel_safe=True, resource_locks=tuple(sorted(resources)), isolated=True,
            priority=priority, command=command)))
    slots, results = _FullSuiteSlots(workers), {}

    def execute(shard, resources):
        try:
            return runner._execute_full_suite_shard(workspace, shard)
        finally:
            slots.release(resources)

    stopped = False
    for priority in sorted({shard.priority for _, shard in pending}):
        batch = [(index, shard) for index, shard in pending if shard.priority == priority]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            while batch or futures:
                for index, shard in list(batch):
                    if stopped or len(futures) >= workers:
                        break
                    resources = slots.acquire_ready(shard)
                    if resources is None:
                        continue
                    try:
                        future = pool.submit(copy_context().run, execute, shard, resources)
                    except BaseException:
                        slots.release(resources)
                        raise
                    futures[future] = index
                    batch.remove((index, shard))
                if stopped:
                    batch.clear()
                if not futures:
                    break
                finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in finished:
                    index = futures.pop(future)
                    result = results[index] = future.result()
                    stopped = stopped or not result.ok or result.recoverable or result.timed_out
        if stopped:
            break

    if runner._full_suite_environment_fingerprint() != environment:
        # A prerequisite prepared by one worker invalidates mixed-environment
        # results. Revalidate the same retained source in the now-ready runtime.
        return runner._run_verification_commands(commands, workspace)
    ordered = sorted(results.items())
    payload = {key: [] for key in ('source_commands', 'command_timings', 'failure_evidence',
                                  'proof_refs', 'executed_tests', 'nonfatal_source_commands')}
    payload.update(parallel_workers=workers, planned_commands=len(commands),
                   completed_commands=len(ordered), certificate_hits=0)
    for index, result in ordered:
        payload['source_commands'].append(commands[index])
        for key in ('command_timings', 'failure_evidence', 'proof_refs', 'executed_tests', 'nonfatal_source_commands'):
            payload[key].extend(result.payload.get(key, []))
        payload['certificate_hits'] += result.payload.get('certificate_hits', 0)
    return _VerificationResult(
        not stopped and len(ordered) == len(commands),
        '\n\n'.join(result.summary for _, result in ordered),
        commands=tuple(command for _, result in ordered for command in result.commands),
        returncodes=tuple(code for _, result in ordered for code in result.returncodes),
        termination_reasons=tuple(reason for _, result in ordered for reason in result.termination_reasons),
        duration_seconds=sum(result.duration_seconds for _, result in ordered),
        recoverable=any(result.recoverable for _, result in ordered), payload=payload)
