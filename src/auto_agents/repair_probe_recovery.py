"""Durable, bounded correction of inconclusive scope diagnostics."""
from copy import deepcopy
import subprocess
import shlex

from .repair_control import digest

MAX_PROBE_CORRECTIONS = 2


def probe_id(specification):
    return digest({key: value for key, value in specification.items() if key != 'replaces_probe'})


def queue_probes(runner, key, proposals):
    from .repair_planning import PlanningBlocked, validate_probes
    state = runner._experiment.planning_receipts.setdefault(key, {})
    previous = state.get('probe_results', [])
    proposals = validate_probes(proposals)
    queued = []
    if not previous:
        queued = [{'specification': p, 'index': index} for index, p in enumerate(proposals)]
    else:
        if state.get('probe_corrections', 0) >= MAX_PROBE_CORRECTIONS:
            raise PlanningBlocked('scope probe corrections exhausted; retained diagnostics remain inconclusive',
                                  code='scope_probe_exhausted', evidence=key)
        eligible = {probe_id(p['specification']): index for index, p in enumerate(previous)
                    if p.get('outcome') == 'inconclusive' and not p.get('matches')}
        used = set()
        for proposal in proposals:
            identity = probe_id(proposal)
            if any(probe_id(p['specification']) == identity and p.get('matches') for p in previous):
                continue  # Retain successful probes without running them again.
            replaced = proposal.get('replaces_probe')
            if not replaced and len(eligible) == 1:
                replaced = next(iter(eligible))
            index = eligible.get(replaced)
            if (index is None or index in used or identity == replaced
                    or shlex.split(proposal['command']) == shlex.split(previous[index]['specification']['command'])
                    or proposal['expected'] != previous[index]['specification']['expected']):
                raise PlanningBlocked('probe correction must replace one inconclusive diagnostic and preserve its expected outcome',
                                      code='scope_probe_correction_invalid', evidence=key)
            used.add(index)
            queued.append({'specification': proposal, 'index': index})
        if not queued:
            return False
        state['probe_corrections'] = state.get('probe_corrections', 0) + 1
    state['pending_probes'] = deepcopy(queued)
    runner._experiment_store.save(runner._experiment)  # Spend the batch before dispatch.
    callback = getattr(runner, '_control_phase_callback', None)
    if callback and previous:
        callback('scope_probe_correction', {'scope': key, 'correction': state['probe_corrections'],
                                           'replacements': len(queued)})
    run_pending_probes(runner, key)
    return True


def run_pending_probes(runner, key):
    from .repair_planning import _probe
    from .repair_feedback import sanitize_evidence
    state = runner._experiment.planning_receipts.setdefault(key, {})
    batch = state.get('pending_probes')
    if not batch:
        return
    results = state.setdefault('probe_results', [])
    for row in batch:
        if row.get('done'):
            continue
        spec = row['specification']
        if row.get('running'):
            result = {'specification': spec, 'matches': False, 'outcome': 'inconclusive',
                      'result': {'summary': 'probe interrupted; its consumed attempt cannot be repeated on restart'}}
        else:
            row['running'] = True
            runner._experiment_store.save(runner._experiment)
            try:
                result = _probe(runner, runner._scope_probe_workspace, spec)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                result = {'specification': spec, 'matches': False, 'outcome': 'inconclusive',
                          'result': {'summary': str(error)}}
        result = {**sanitize_evidence(result), 'probe_id': probe_id(spec)}
        # Executable specifications are data bound by the scope request. Keep
        # their original bytes so redaction cannot alias distinct corrections.
        result['specification'] = deepcopy(spec)
        index = row['index']
        if index < len(results):
            state.setdefault('probe_history', []).append(results[index])
            results[index] = result
        else:
            results.append(result)
        row['done'] = True
        runner._experiment_store.save(runner._experiment)
    state.pop('pending_probes')
    for name in ('draft', 'request_id', 'format_error', 'format_calls', 'decision'):
        state.pop(name, None)
    state['decision'] = 'scope_pending'
    runner._experiment_store.save(runner._experiment)
