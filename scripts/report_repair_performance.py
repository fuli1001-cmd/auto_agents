#!/usr/bin/env python3
"""Read one repair job's stage and verification costs without resuming it."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def legacy_runner_commands(root, job, events):
    """Read retained schedules from the reported invocation, excluding imports.

    Legacy schedules lack job/candidate bindings. Require both a candidate from
    this job and an artifact mtime inside its event interval; label that fallback
    explicitly. New events include checks from candidates that did not finish.
    """
    ids = {payload.get('candidate_id') for kind, payload, _ in events if kind == 'candidate_result'}
    if not ids or not events:
        return []
    covered = {(p.get('candidate_id'), p.get('phase')) for k, p, _ in events
               if k == 'verification_command_finished'}
    commands = []
    base = root / 'jobs' / job / 'working-evidence' / '.auto-agents' / 'runs'
    for path in base.glob('*/self-repair/*/experiment.json'):
        experiment = json.loads(path.read_text())
        commits = defaultdict(list)
        for candidate in ids:
            commit = experiment.get('candidates', {}).get(candidate, {}).get('candidate_commit')
            if commit:
                commits[commit].append(candidate)
        seen = set()
        memories = [*experiment.get('component_memory', {}).values(),
                    *(row.get('memory', {}) for row in experiment.get('work_items', {}).values())]
        for memory in memories:
            for ref in memory.get('verification_history', []):
                identity = ref.get('id', '')
                if identity in seen or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
                    continue
                seen.add(identity)
                record_path = path.parent / 'planning' / identity / 'memory.json'
                if not record_path.is_file():
                    continue
                record = json.loads(record_path.read_text())
                candidates = commits.get(record.get('source_commit'), [])
                explicit = record.get('job') == job and record.get('candidate_id') in ids
                legacy = (not record.get('job') and len(candidates) == 1
                          and events[0][2] <= record_path.stat().st_mtime <= events[-1][2])
                if not explicit and not legacy:
                    continue
                candidate = record['candidate_id'] if explicit else candidates[0]
                phase = {'quick': 'quick_verification', 'expanded': 'focused_verification'}.get(record.get('phase'))
                if (candidate, phase) in covered:
                    continue
                for command in record.get('timings', []):
                    commands.append({**command, 'candidate_id': candidate, 'phase': phase,
                        'generation': record.get('generation'), 'origin': 'runner_schedule',
                        'attribution': 'explicit' if explicit else 'legacy_source_and_mtime',
                        'evidence_ref': str(record_path)})
    return commands


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
    commands = [{**payload, 'origin': 'runner_event', 'created': created}
                for kind, payload, created in events if kind == 'verification_command_finished']
    commands.extend(legacy_runner_commands(root, job, events))
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
                             'origin': 'provider_verification_tool', 'generation': payload.get('generation'),
                             'evidence_ref': str(path)})
    return {
        'job': job, 'state': row[0], 'terminal_result': json.loads(row[2]),
        'started_at': datetime.fromtimestamp(events[0][2], timezone.utc).isoformat() if events else None,
        'last_state_change': datetime.fromtimestamp(row[1], timezone.utc).isoformat(),
        'wall_seconds': max(e[2] for e in events) - min(e[2] for e in events) if events else None,
        'phase_costs': dict(phase), 'candidate_outcomes': dict(outcomes),
        'verification_commands': sorted(commands, key=lambda c: c.get('seconds', 0), reverse=True),
        'cache_hit_commands': sum(bool(c.get('cache_hit')) for c in commands),
        'cache_miss_reasons': dict(Counter(c.get('cache_miss_reason') or 'not_recorded'
                                          for c in commands if not c.get('cache_hit'))),
        'input_trace_reasons': dict(Counter(c.get('input_trace_reason') or 'not_recorded'
                                           for c in commands if not c.get('cache_hit') and not c.get('input_trace_complete'))),
        'command_origins': dict(Counter(c['origin'] for c in commands)),
        'review_reuses': sum(k == 'review_reused' for k, _, _ in events),
        'repair_transitions': [payload for kind, payload, _ in events if kind == 'repair_transition'],
        'routing_actions': dict(Counter(payload.get('action', payload.get('event', 'unknown'))
                                        for kind, payload, _ in events if kind == 'repair_transition')),
        'probe_corrections': sum(k == 'scope_probe_correction' for k, _, _ in events),
        'deterministic_plan_migrations': sum(k == 'plan_references_normalized' for k, _, _ in events),
        'component_completion_checks': [payload for kind, payload, _ in events
                                        if kind == 'component_completion_checked'],
        'reused_completed_groups': sorted({payload['component'] for kind, payload, _ in events
                                          if kind == 'component_completion_checked' and payload.get('restored_completion')}),
        'completion_check_reuses': [payload for kind, payload, _ in events if kind == 'component_checks_reused'],
        'interpretation': 'Phase costs can nest; do not sum them as wall time. Counts describe this job only. '
                          'Wall time spans retained events, excluding later administrative state updates. '
                          'Command times are nested inside phases, and parallel command times are work, not wall time. '
                          'Legacy schedule attribution uses candidate source and artifact mtime; incomplete evidence is not a cache miss proof. '
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
