"""Replay archived planning decisions without models, tests or job mutations.

Only scheduling/admission are measured here. Historical model durations are not
an estimate of the next live collab run. Use benchmark_verification.py separately
to measure real isolated verification execution and certificate hits.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.repair_planning import PlanFormatError, PlanningBlocked, validate_plan
from auto_agents.repair_schedule import canonical_commands, quick_verification_plan, verification_plan
from auto_agents.self_repair_search import SelfRepairExperiment


def replay(experiment_path):
    raw = experiment_path.read_bytes()
    state = SelfRepairExperiment.from_dict(json.loads(raw))
    rows = []
    for request_path in sorted((experiment_path.parent / 'planning').glob('*/request.json')):
        if request_path.is_symlink() or request_path.parent.is_symlink():
            continue
        try:
            request = json.loads(request_path.read_text())
            if request.get('stage') != 'self_repair_component_plan':
                continue
            input_path = request_path.with_name('input.json')
            result_path = request_path.with_name('result.json')
            if input_path.is_symlink() or result_path.is_symlink():
                continue
            context = json.loads(input_path.read_text())
            proposed = json.loads(result_path.read_text())
            group = context['component']
            checks = proposed['quick_checks']
            old, _ = canonical_commands(checks)
            row = {'request_id': request_path.parent.name, 'component': group['group_id'],
                   'old_quick_commands': len(old), 'old_quantity_rejection': len(checks) > 8,
                   'quantity_constraint_handled': len(checks) > 8,
                   'global_replans_for_quantity': 0, 'empty_candidates': 0}
            try:
                plan = validate_plan(proposed, group, set(state.contract_obligation_ids))
            except PlanningBlocked as error:
                row.update(new_admission='format_correction_required' if isinstance(error, PlanFormatError) else 'blocked',
                           reason=str(error), error=error.detail)
            else:
                active = {**group, **plan}
                quick = quick_verification_plan(state, active)
                isolated_state = deepcopy(state)
                full = verification_plan(isolated_state, active)
                inventory, _ = canonical_commands([*checks, *group.get('focused_tests', []),
                                                   *(s['check'] for s in plan['scenarios'])])
                row.update(new_admission='awaits_independent_review',
                    new_quick_commands=len(quick['commands']), batches=len(quick['batches']),
                    acceptance_preserved=set(inventory).issubset(full['commands']),
                    global_replans_for_quantity=0, empty_candidates=0)
            rows.append(row)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # Interrupted requests are counted separately, never accepted.
    if experiment_path.read_bytes() != raw:
        raise RuntimeError('source experiment changed during read-only replay')
    return {'kind': 'offline_scheduling_replay', 'model_calls': 0, 'test_executions': 0,
        'source_mutated': False, 'retained_candidate': state.current_candidate_id,
        'historical_attempts': state.attempt_count, 'plans': rows,
        'totals': {'replayed_plans': len(rows),
            'quantity_rejections_removed': sum(r.get('quantity_constraint_handled', False) for r in rows),
            'comparable_plans': sum('new_quick_commands' in r for r in rows),
            'old_quick_requests': sum(r['old_quick_commands'] for r in rows if 'new_quick_commands' in r),
            'new_quick_requests': sum(r.get('new_quick_commands', 0) for r in rows)},
        'limitation': 'Command counts are replayed scheduling results, not measured end-to-end model speedups. '
                      'No plan approval or verification receipt is manufactured.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = replay(args.experiment)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        if args.output.resolve().is_relative_to(args.experiment.parent.resolve()):
            parser.error('replay output must be outside the retained experiment')
        args.output.write_text(rendered + '\n')
    print(rendered)
    return int(any(r.get('acceptance_preserved') is False for r in result['plans']))


if __name__ == '__main__':
    raise SystemExit(main())
