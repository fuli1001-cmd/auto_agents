#!/usr/bin/env python3
"""Real CLI repair on synthetic code only; never resumes or publishes a project."""
import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def run(output, configuration_project, provider, image, *, timeout=1200, interrupt_on_edit=False, case='alias'):
    from auto_agents.config import load_project_config
    from auto_agents.models import ProjectConfig
    from auto_agents.repair_v2 import Acceptance, Controller, RepairRequest, ValidationUnit
    from auto_agents.repair_v2.docker import DockerVerifier
    from auto_agents.repair_v2.providers import AgentSandbox, NativeDriver
    from auto_agents.repair_v2.store import Store, atomic_json
    from auto_agents.repair_v2.workspace import Workspace, git
    spec = importlib.util.spec_from_file_location('alias_fixture', Path(__file__).with_name('pilot_repair_convergence.py'))
    fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
    if case == 'ownership':
        from repair_benchmark_cases import OWNERSHIP_SOURCE, OWNERSHIP_TESTS, OWNERSHIP_SELECTOR, OWNERSHIP_REQUIREMENTS
        fixture.BROKEN, fixture.TESTS = OWNERSHIP_SOURCE, OWNERSHIP_TESTS
    output.mkdir(parents=True, exist_ok=False)
    repository = output / 'source'; repository.mkdir()
    git(repository, 'init', '-q')
    (repository / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n.auto-agents/\n')
    (repository / 'source.py').write_text(fixture.BROKEN)
    if case == 'ownership': (repository / 'command_selector.py').write_text(OWNERSHIP_SELECTOR)
    (repository / 'tests').mkdir()
    (repository / 'tests/test_alias_reuse.py').write_text(fixture.TESTS)
    (repository / 'conftest.py').write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent))\n')
    git(repository, 'add', '.'); git(repository, 'commit', '-qm', 'Freeze synthetic regression fixture')
    request = RepairRequest('synthetic-alias-repair', git(repository, 'rev-parse', 'HEAD'),
        'Repair registered dependency alias reuse in source.py. Do not change the provided tests. '
        'Registered aliases must retain correct invalidation behavior across changed inputs and retargeting.',
        (Acceptance('alias-reuse', 'Registered aliases reuse matching content; unregistered links, changed input '
                    'and changed link targets must not reuse. Restoring the original input or target restores reuse.'),), provider)
    if case == 'ownership':
        request = RepairRequest('synthetic-ownership-repair', request.engine_base,
            'Repair the shared verification command handling across both modules. Preserve all provided tests.',
            tuple(Acceptance(identity, description) for identity, description in OWNERSHIP_REQUIREMENTS), provider)
    configured = load_project_config(configuration_project)
    store = Store(output / 'state')
    verifier = DockerVerifier(output / 'verification', image=image)
    selected = configured.providers.get(provider) or ProjectConfig(project_name='synthetic-benchmark').providers[provider]
    driver = NativeDriver(selected, AgentSandbox(output / 'native', image), timeout=timeout,
        effort=configured.efforts.get('self_repair', 'deep'),
        review_effort=configured.efforts.get('self_repair_review', 'max'))
    workspace = Workspace(output / 'workspace', repository, request.engine_base)
    interrupted = False
    calls = []
    original_run = driver.run
    def controlled(role, prompt, root, **kwargs):
        nonlocal interrupted
        calls.append((role, kwargs.get('session', '')))
        progress = kwargs.get('progress')
        def observe(event):
            nonlocal interrupted
            if progress: progress(event)
            if (interrupt_on_edit and role == 'implement' and not interrupted
                    and (Path(root) / 'source.py').read_text() != fixture.BROKEN):
                interrupted = True
                kwargs['cancel'].set()
        return original_run(role, prompt, root, **{**kwargs, 'progress': observe})
    driver.run = controlled
    def make_controller():
        return Controller(request, store, workspace, driver, verifier,
            regression=lambda i, s, coverage, c: verifier.regression(i, s, repository, request.engine_base, coverage, c),
            units=lambda _: [ValidationUnit('acceptance', 'python -m pytest -q tests')])
    runner = make_controller()
    start = time.monotonic()
    try:
        result = runner.run()
    except KeyboardInterrupt:
        if not interrupted: raise
        saved = store.load()
        plan = saved['plan']
        runner = make_controller()
        result = runner.run()
        assert result['plan'] == plan, 'native recovery discarded its original plan'
    if interrupt_on_edit and not interrupted:
        raise RuntimeError('requested native interruption was not exercised')
    report = {'provider': provider, 'case': case, 'seconds': time.monotonic() - start, 'status': result['status'],
              'attempts': result['attempts'], 'model_calls': result['calls'],
              'interrupted': interrupted, 'native_calls': calls, 'replans': result['replans'],
              'image': image, 'provider_identity': driver.identity(),
              'effort': driver.effort, 'review_effort': driver.review_effort, 'original_project_modified': False, 'engine_published': False,
              'synthetic_only': True, 'blocker': result.get('blocker'), 'snapshot': result.get('snapshot')}
    if result.get('validation'): report['validation'] = store.read(result['validation'])
    atomic_json(output / 'report.json', report)
    print(json.dumps({k: v for k, v in report.items() if k != 'validation'}, ensure_ascii=False, indent=2), flush=True)
    return result['status'] == 'ready'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--configuration-project', type=Path, required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--interrupt-on-edit', action='store_true')
    parser.add_argument('--case', choices=('alias', 'ownership'), default='alias')
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output.resolve(), args.configuration_project.resolve(), args.provider,
                             args.image, timeout=args.timeout, interrupt_on_edit=args.interrupt_on_edit, case=args.case) else 1)
