#!/usr/bin/env python3
"""Exercise full planning admission on a private copy of an archived repair.

Default: capture the next required independent request, without calling a model
or executing probes. --probes executes the real probes without sending a model
request. --native runs the real bounded probes and one independent
Codex review. Neither mode generates code, publishes an engine or resumes the
original project. Reports distinguish observations from repair acceptance.
"""
import argparse
from copy import deepcopy
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class RequestCaptured(RuntimeError):
    pass


def run(args):
    from auto_agents.adapters.codex import CodexAdapter
    from auto_agents.config import load_project_config
    from auto_agents.git_ops import head_ref
    from auto_agents.models import ProjectConfig
    from auto_agents.repair_planning import prepare_component
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision
    from auto_agents.self_repair_search import SelfRepairExperiment, SelfRepairExperimentStore
    from auto_agents.verification_ledger import source_identity
    output = args.output.resolve()
    if (any(output.is_relative_to(path.resolve()) for path in (
            args.experiment.parent, args.source, args.configuration_project))
            or not re.fullmatch('[a-f0-9]{32}', args.request)
            or args.scope_request and not re.fullmatch('[a-f0-9]{32}', args.scope_request)):
        raise ValueError('use exact request IDs and an output directory outside retained projects/evidence')
    output.mkdir(parents=True, exist_ok=False)
    for name in ('AUTO_AGENTS_STORAGE_ROOT', 'AUTO_AGENTS_VERIFICATION_ROOT',
                 'AUTO_AGENTS_WORKER_ROOT', 'AUTO_AGENTS_CLUSTER_HOME'):
        os.environ[name] = str(output / name.lower())
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    archive = args.experiment.resolve()
    archive_before = archive.read_bytes()
    original_source_before = source_identity(args.source.resolve())
    incoming = json.loads((archive.parent / 'planning' / args.request / 'input.json').read_text())
    state = SelfRepairExperiment.from_dict(json.loads(archive.read_text()))
    engine, target = output / 'engine', output / 'target'
    subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks',
                    str(args.source.resolve()), str(engine)], check=True)
    subprocess.run(['git', '-c', 'advice.detachedHead=false', 'checkout', '--quiet', incoming['source_commit']], cwd=engine, check=True)
    target.mkdir()
    subprocess.run(['git', 'init', '--quiet'], cwd=target, check=True)
    (target / 'sentinel').write_text('private rehearsal target\n')
    # Restore scope as it was at the beginning of the failed run, if supplied.
    if args.scope_request:
        prior = json.loads((archive.parent / 'planning' / args.scope_request / 'input.json').read_text())
        for identity, row in prior.get('scope_revalidation', {}).items():
            if row.get('previous'):
                state.scope_decisions[identity] = deepcopy(row['previous'])
    config = ProjectConfig(project_name='archived-repair-resume-rehearsal')
    selected = load_project_config(args.configuration_project.resolve())
    config.providers, config.efforts, config.execution = selected.providers, selected.efforts, selected.execution
    config.active_provider = 'codex'
    adapter = CodexAdapter(config.providers['codex'], config.execution.smart_timeout)
    calls = []
    def invoke(request):
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        working = (request.output_path.parent / 'working_input.json').read_text()
        record = {'stage': request.stage, 'request': request.output_path.parent.name,
                  'working_input_chars': len(working), 'scope_findings': [f['finding_id'] for f in context.get('scope_findings', [])],
                  'scenario_ids': [s['scenario_id'] for s in context.get('proposed_plan', {}).get('scenarios', [])],
                  'probe_outcomes': [{key: p.get(key) for key in ('outcome', 'matches')}
                                     for p in context.get('probe_results', [])]}
        old_plan, current_plan = incoming.get('proposed_plan', {}), context.get('proposed_plan', {})
        record['retained_checks_unchanged'] = (
            old_plan.get('quick_checks') == current_plan.get('quick_checks')
            and [(s['scenario_id'], s.get('check'), s.get('quick_check')) for s in old_plan.get('scenarios', [])]
            == [(s['scenario_id'], s.get('check'), s.get('quick_check')) for s in current_plan.get('scenarios', [])])
        calls.append(record)
        (output / 'calls.json').write_text(json.dumps(calls, indent=2))
        if not args.native:
            raise RequestCaptured('captured the next required independent review')
        if len(calls) != 1 or request.stage != 'self_repair_plan_review':
            raise RequestCaptured('further planning required; native rehearsal stops after one independent review')
        started = time.monotonic()
        print('native review started', flush=True)
        result = adapter.run(request)
        record.update(seconds=time.monotonic() - started, ok=result.ok)
        (output / 'calls.json').write_text(json.dumps(calls, indent=2))
        (output / 'provider.stderr').write_text(result.stderr)
        print('native review finished: ' + str(result.ok), flush=True)
        return result
    orchestrator = SimpleNamespace(config=config, _current_provider='codex', _call_with_failover=invoke)
    diagnosis = SimpleNamespace(to_dict=lambda: incoming['root_cause'],
        final=SimpleNamespace(expected_postconditions=[]))
    runner = AutoAgentsSelfRepairRunner(orchestrator, target_project_root=target,
        error=RuntimeError('archived self-repair resume'), diagnosis=diagnosis,
        decision=SelfRepairDecision(True, category='archive_resume', reason='exercise real retained evidence'))
    runner.repo_root = engine
    runner._verification_python_cache = str(args.python.absolute())
    runner._verification_fresh = True
    runner._real_project_root = target
    runner._candidate_is_final_group = False
    runner._experiment = state
    runner._experiment_store = SelfRepairExperimentStore(target, state.run_id, state.root_fingerprint)
    runner._candidate_group = deepcopy(next(g for g in state.finding_groups
                                           if g['group_id'] == incoming['component']['group_id']))
    runner._candidate_next_action = {'kind': 'repair_code'}
    runner._invocation_context = deepcopy(incoming['original_request'])
    runner._compact_diagnosis_payload = lambda: deepcopy(incoming['root_cause'])
    for path in (archive.parent / 'planning').glob('*/*.json'):
        if path.name not in {'memory.json', 'request.json', 'input.json', 'result.json'}:
            continue
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('symbolic link in retained planning artifacts')
        dest = runner._experiment_store.root / 'planning' / path.parent.name / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    runner._experiment_store.save(state)
    before = source_identity(engine)
    started = time.monotonic()
    report = {'native': args.native, 'real_probes': args.native or args.probes, 'code_generated': False,
              'engine_published': False, 'original_project_resumed': False, 'repair_accepted': False,
              'source_commit': head_ref(engine), 'planning_approved': False}
    try:
        if args.native or args.probes:
            receipt = prepare_component(runner, engine)
            report['planning_approved'] = receipt['decision'] == 'APPROVE'
        else:
            # Control-flow replay only; archived observations never become new proof.
            with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'replay-only'}), \
                 patch('auto_agents.repair_capability_checks.production_capabilities',
                       return_value=incoming['runtime_capabilities'].get('production_namespace', {})):
                prepare_component(runner, engine)
    except Exception as error:
        report['stopped'] = {'type': type(error).__name__, 'message': str(error)[:1600]}
    finally:
        report.update(seconds=time.monotonic() - started, calls=calls,
                      source_unchanged=source_identity(engine) == before,
                      original_state_modified=archive.read_bytes() != archive_before,
                      original_source_unchanged=source_identity(args.source.resolve()) == original_source_before,
                      experiment=str(runner._experiment_store.root / 'experiment.json'))
        (output / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['planning_approved'] or not args.native and calls else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--request', required=True, help='retained plan-review request ID')
    parser.add_argument('--scope-request', help='restore the scope inputs of the failed run')
    parser.add_argument('--configuration-project', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True, help='prepared verification interpreter')
    parser.add_argument('--output', type=Path, required=True, help='new private directory')
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--probes', action='store_true', help='real local probes; capture review without a provider call')
    raise SystemExit(run(parser.parse_args()))
