#!/usr/bin/env python3
"""Read one repair job's stage and verification costs without resuming it."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def report(control_root, job):
    root = Path(control_root).resolve()
    with sqlite3.connect((root / 'control.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('select state,updated,result from jobs where id=?', (job,)).fetchone()
        if row is None:
            raise ValueError('unknown repair job')
        events = [(kind, json.loads(payload), created) for kind, payload, created in
                  db.execute('select kind,payload,created from events where job=? order by sequence', (job,))]
        verifications = [(identity, json.loads(payload)) for identity, payload in
                         db.execute('select id,payload from verifications')]
    phase = defaultdict(lambda: {'count': 0, 'seconds': 0.0})
    outcomes = Counter()
    for kind, payload, _ in events:
        if kind == 'phase_finished':
            cost = phase[payload['phase']]
            cost['count'] += 1
            cost['seconds'] += payload.get('duration_seconds', 0)
        if kind == 'candidate_result':
            outcomes[payload.get('status', 'unknown')] += 1
    commands = []
    for identity, payload in verifications:
        if payload.get('job') != job:
            continue
        path = root / 'verifications' / identity / 'result.json'
        if not path.is_file():
            continue
        result = json.loads(path.read_text())
        verification = result.get('verification', {})
        details = verification.get('payload', {})
        for command in details.get('command_timings', []):
            commands.append({**command, 'verification_id': identity, 'ok': result.get('ok'),
                             'evidence_ref': str(path)})
    return {
        'job': job, 'state': row[0], 'terminal_result': json.loads(row[2]),
        'started_at': datetime.fromtimestamp(events[0][2], timezone.utc).isoformat() if events else None,
        'last_state_change': datetime.fromtimestamp(row[1], timezone.utc).isoformat(),
        'phase_costs': dict(phase), 'candidate_outcomes': dict(outcomes),
        'verification_commands': sorted(commands, key=lambda c: c.get('seconds', 0), reverse=True),
        'cache_hit_commands': sum(bool(c.get('cache_hit')) for c in commands),
        'interpretation': 'Phase costs can nest; do not sum them as wall time. Counts describe this job only. '
                          'No counterfactual run is available to infer net speedup from review costs alone.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-root', type=Path, required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.control_root.resolve()):
        parser.error('write the report outside the retained control directory')
    args.output.write_text(json.dumps(report(args.control_root, args.job), ensure_ascii=False, indent=2) + '\n')
    print(args.output)


if __name__ == '__main__':
    main()
