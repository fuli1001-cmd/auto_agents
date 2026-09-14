#!/usr/bin/env python3
"""Validate or install a manual draft on an inactive engine repair job.

Never starts a worker or model, changes candidate code, approves a plan, resets
attempts, or resumes the subscriber's project. Old evidence remains immutable.
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def revise(control_root, job_id, document_path, *, apply=False):
    from auto_agents.repair_manual_plan import propose, validate_revision
    from auto_agents.repair_restart import _quiescent
    from auto_agents.self_repair_search import SelfRepairExperimentStore
    from auto_agents.verification_ledger import source_identity
    root = control_root.resolve()
    if not re.fullmatch('[a-f0-9]{24}', job_id):
        raise ValueError('invalid repair job ID')
    document = json.loads(document_path.read_text())
    db = sqlite3.connect('file:' + str(root / 'control.sqlite3') + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    db.row_factory = sqlite3.Row
    try:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['state'] not in {'blocked', 'cancelled', 'failed'}:
            raise RuntimeError('manual revision requires a stopped repair job')
        directory = root / 'jobs' / job_id
        if not _quiescent(directory):
            raise RuntimeError('repair processes have not finished stopping')
        payload = json.loads(job['payload'])
        invocation = payload.get('invocation', {})
        subject = ('session-' + invocation['session_id'] if invocation.get('session_id')
                   and not invocation.get('run_id') else invocation.get('run_id', ''))
        if not subject:
            raise ValueError('repair job has no retained workflow identity')
        store = SelfRepairExperimentStore(directory / 'working-evidence', subject, payload['fingerprint'])
        if (store.path.is_symlink() or not store.path.resolve().is_relative_to(directory.resolve())
                or (directory / 'continuous/repair').is_symlink()):
            raise RuntimeError('repair state or candidate leaves the selected job directory')
        state = store.load()
        if state is None:
            raise RuntimeError('retained experiment is missing')
        workspace = directory / 'continuous/repair'
        before = source_identity(workspace)
        runner = SimpleNamespace(_experiment=state, _experiment_store=store)
        group, previous, plan = validate_revision(runner, workspace, document)
        result = {'job': job_id, 'group': group['group_id'], 'scenarios': len(plan['scenarios']),
                  'parent_revision': previous['id'], 'validated': True, 'applied': False,
                  'requires_independent_review': True, 'worker_started': False, 'model_calls': 0}
        if apply:
            result.update(propose(runner, workspace, document), applied=True)
            db.execute('INSERT INTO events(job,kind,payload,created) VALUES(?,?,?,?)',
                       (job_id, 'manual_plan_proposed', json.dumps(result), time.time()))
            db.commit()
        result['candidate_source_unchanged'] = source_identity(workspace) == before
        if not result['candidate_source_unchanged']:
            raise RuntimeError('candidate source changed concurrently; retain the draft for independent revalidation')
        return result
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-root', type=Path, required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--revision', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='store an unapproved draft; otherwise only validate')
    args = parser.parse_args()
    print(json.dumps(revise(args.control_root, args.job, args.revision, apply=args.apply), ensure_ascii=False, indent=2))
