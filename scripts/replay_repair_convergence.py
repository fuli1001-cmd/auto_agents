#!/usr/bin/env python3
"""Replay recorded code-review routing in private scratch state, without models."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def replay(experiment_path, candidates=()):
    from auto_agents.repair_convergence import route
    from auto_agents.repair_memory import component_key, read_record
    from auto_agents.self_repair_search import SelfRepairExperiment, SelfRepairFinding
    raw = json.loads(experiment_path.read_text())
    selected = {key: value for key, value in raw['candidates'].items()
                if key != 'base' and (not candidates or key in candidates)}
    results = []
    with tempfile.TemporaryDirectory(prefix='repair-history-replay-') as directory:
        root = Path(directory)
        (root / 'planning').mkdir()
        for path in (experiment_path.parent / 'planning').glob('*/memory.json'):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            target = root / 'planning' / path.parent.name / 'memory.json'
            target.parent.mkdir()
            shutil.copy2(path, target)
        store = SimpleNamespace(root=root, save=lambda state: (root / 'experiment.json').write_text(json.dumps(state.to_dict())))
        lookup = SimpleNamespace(_experiment_store=store)
        references = {ref.get('id'): ref for ref in raw.get('review_facts', {}).values()}
        for value in raw.get('component_memory', {}).values():
            if value.get('code_review'):
                references[value['code_review']['id']] = value['code_review']
        for reference in references.values():
            review = read_record(lookup, reference)
            if not review or review.get('kind') != 'code_review' or not review.get('result', {}).get('findings'):
                continue
            owner = review.get('component', {}).get('group_id')
            matching = [(key, value) for key, value in selected.items()
                        if value.get('candidate_commit') == review.get('source_commit')
                        and value.get('finding_group_id') == owner]
            for identity, original in matching:
                state = SelfRepairExperiment.from_dict(deepcopy(raw))
                state.work_items = {}
                group = next((g for g in state.finding_groups if g.get('group_id') == owner), None)
                if group is None:
                    continue
                findings = review['result']['findings']
                state.findings = {row['finding_id']: SelfRepairFinding.from_dict({**row, 'status': 'confirmed',
                                  'repair_group_id': owner}) for row in findings}
                group['finding_ids'] = list(state.findings)
                state.component_memory = {component_key(review['component']): {'code_review': reference}}
                runner = SimpleNamespace(_experiment=state, _experiment_store=store, _candidate_group=group)
                action = route(runner, state.candidates[identity], {'kind': 'implement', 'evidence_ids': []})
                context_sizes = None
                if action['kind'] == 'repair_code':
                    from auto_agents.repair_context import writer_packet
                    from auto_agents.repair_planning import _implementation_bindings
                    receipt = next(row for row in state.planning_receipts.values()
                                   if row.get('request_id') == review['component'].get('planning_receipt'))
                    active = {**group, **receipt['plan'], 'planning_receipt': receipt['request_id'],
                              'finding_scenario_bindings': _implementation_bindings(runner, receipt, findings)}
                    search = {**state.prompt_context(), 'next_action': action}
                    small, component, _ = writer_packet(runner, search, active, {})
                    context_sizes = {'original_payload_chars': len(json.dumps([search, active], ensure_ascii=False)),
                                     'working_payload_chars': len(json.dumps([small, component], ensure_ascii=False)),
                                     'scope': 'working JSON payload only; referenced evidence remains available'}
                historical = experiment_path.parent / identity / 'result.json'
                previous = json.loads(historical.read_text()).get('next_action', {}) if historical.is_file() else {}
                results.append({'candidate': identity, 'source_commit': original.get('candidate_commit'),
                    'review': reference, 'findings': list(state.findings), 'recorded_action': previous.get('kind'),
                    'routed_action': action['kind'], 'cause': action.get('cause'), 'context_sizes': context_sizes,
                    'repair_approved': False})
    return {'experiment': str(experiment_path), 'scope': 'read-only historical evidence routing',
            'models_called': 0, 'candidate_commands_executed': 0, 'original_state_modified': False,
            'limitations': 'Routing is not candidate correctness, plan approval or an end-to-end latency measurement.',
            'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--candidate', action='append', default=[])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve().is_relative_to(args.experiment.resolve().parent):
        parser.error('write the report outside the retained experiment and evidence directory')
    result = replay(args.experiment.resolve(), args.candidate)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end='')
