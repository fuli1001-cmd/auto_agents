#!/usr/bin/env python3
"""Inspect preserved repair history without reopening or modifying the job."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.repair_planning import history_report
from auto_agents.self_repair_search import SelfRepairExperiment
from auto_agents.repair_restart import _restart_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['GIT_OPTIONAL_LOCKS'] = '0'
    if args.output.resolve() == args.experiment.resolve():
        parser.error('the report cannot overwrite the source experiment')
    raw = args.experiment.read_bytes()
    state = SelfRepairExperiment.from_dict(json.loads(raw))
    report = history_report(state)
    report['retained_commit'] = subprocess.check_output(
        ['git', '--no-optional-locks', '-C', str(args.checkout), 'rev-parse', 'HEAD'], text=True).strip()
    report['retained_worktree_status'] = subprocess.check_output(
        ['git', '--no-optional-locks', '-C', str(args.checkout), 'status', '--porcelain'], text=True)
    report['recorded_model_requests'] = len(list(args.experiment.parent.glob('planning/*/request.json')))
    current = args.experiment.parent / state.current_candidate_id
    checkpoint = current / 'partial-candidate.json'
    report['checkpoint'] = json.loads(checkpoint.read_text()) if checkpoint.is_file() else {}
    report['candidate_acceptance'] = 'unverified' if not (current / 'result.json').is_file() else 'inspect_recorded_result'
    common = subprocess.check_output(['git', '-C', str(args.checkout), 'rev-parse',
        '--path-format=absolute', '--git-common-dir'], text=True).strip()
    store = SimpleNamespace(safe_root=args.experiment.parent.name,
                            candidate_root=lambda identity: args.experiment.parent / identity)
    snapshot, identity = _restart_snapshot(args.checkout.parent.parent, args.checkout, store,
                                          state, SimpleNamespace(cache=Path(common)))
    report['restart_selection'] = {'candidate_id': identity, 'source_commit': snapshot[0],
        'patch_sha256': hashlib.sha256(snapshot[1].encode()).hexdigest() if snapshot[1] else '',
        'untracked_count': len(snapshot[2]),
        'uses_current_commit': snapshot[0] == report['retained_commit']}
    if raw != args.experiment.read_bytes():
        raise RuntimeError('repair history changed while being inspected')
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(args.output), 'retained_commit': report['retained_commit'],
                      'counts': report['counts'], 'candidate_acceptance': report['candidate_acceptance']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
