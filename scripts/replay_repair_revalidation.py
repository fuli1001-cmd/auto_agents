#!/usr/bin/env python3
"""Check archived delta envelopes without models, test execution or workflow writes.

This is a protocol replay, not fresh repair acceptance. Original model replies
and controller receipts remain immutable. Run regression tests separately to
exercise current scheduling, execution and safety guards.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def replay(experiment, review_id, unchanged_id):
    from auto_agents.repair_control import digest
    from auto_agents.repair_delta_protocol import validate_reply
    from auto_agents.repair_planning import _validate_scope, PlanFormatError
    from auto_agents.self_repair_search import SelfRepairExperiment
    root = experiment.parent
    observed = {}

    def read(path):
        if path.is_symlink():
            raise ValueError('symbolic link in archived evidence')
        data = path.read_bytes()
        observed[path] = hashlib.sha256(data).hexdigest()
        return json.loads(data)

    def request(identity):
        if not re.fullmatch('[a-f0-9]{32}', identity):
            raise ValueError('use exact archived request IDs')
        directory = root / 'planning' / identity
        incoming = read(directory / 'input.json')
        original = Path(incoming['completion_ref'])
        proof = read(root / 'planning' / original.parent.name / 'memory.json')
        return directory, incoming, proof

    raw = read(experiment)
    state = SelfRepairExperiment.from_dict(deepcopy(raw))
    directory, context, proof = request(review_id)
    payload = read(directory / 'result.json')
    before = deepcopy(payload)
    legacy = ''
    try:
        _validate_scope(payload, context['findings'], state.contract_obligation_ids)
    except PlanFormatError as error:
        legacy = error.detail['message']
    started = time.monotonic()
    validated = validate_reply(SimpleNamespace(_experiment=state), payload, context, proof)
    duration = time.monotonic() - started
    _, unchanged, receipt = request(unchanged_id)
    refs = [reference for work in raw['work_items'].values()
            for reference in [work['memory'].get('completion', {}), *work['memory'].get('completion_history', [])]]
    impact = unchanged['verification_impact']
    reuse = (impact['reason'] == 'completion evidence remains valid' and impact['affected_checks'] == 0
             and impact['reusable_checks'] == len(receipt['commands'])
             and unchanged['source_delta'].get('available') is True
             and unchanged['source_delta'].get('changed_paths') == []
             and unchanged.get('findings') == [] and unchanged['new_evidence']['count'] == 0
             and unchanged['source'] == receipt['source_identity']
             and {'id': receipt['id'], 'digest': digest(receipt)} in refs)
    return {'mode': 'offline protocol replay; not fresh acceptance', 'provider_calls': 0,
            'project_resumed': False, 'repair_accepted': False,
            'delta_review': {'request': review_id, 'legacy_error': legacy,
                'decision': validated['decision'], 'requested_rows': len(context['findings']),
                'original_rows': len(payload['decisions']), 'validated_rows': len(validated['decisions']),
                'requested_decisions_unchanged': all(row in before['decisions'] for row in validated['decisions']),
                'seconds': duration},
            'unchanged_review': {'request': unchanged_id, 'matches_reselection_regression': reuse,
                'reusable_checks': impact['reusable_checks'], 'source_commit': unchanged['source_commit']},
            'original_payload_unchanged': before == payload,
            'archive_unchanged': all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in observed.items()),
            'input_digests': {str(p): h for p, h in observed.items()}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--delta-review', required=True)
    parser.add_argument('--unchanged-review', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.experiment.resolve().parent):
        parser.error('write the report outside the original archive')
    result = replay(args.experiment.resolve(), args.delta_review, args.unchanged_review)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'input_digests'}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['archive_unchanged'] and result['unchanged_review']['matches_reselection_regression'] else 1)
