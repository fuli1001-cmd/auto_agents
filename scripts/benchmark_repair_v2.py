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


def run(output, configuration_project, provider, image, *, timeout=1200):
    from auto_agents.config import load_project_config
    from auto_agents.models import ProjectConfig
    from auto_agents.repair_v2 import Acceptance, Controller, RepairRequest, ValidationUnit
    from auto_agents.repair_v2.docker import DockerVerifier
    from auto_agents.repair_v2.providers import AgentSandbox, NativeDriver
    from auto_agents.repair_v2.store import Store, atomic_json
    from auto_agents.repair_v2.workspace import Workspace, git
    spec = importlib.util.spec_from_file_location('alias_fixture', Path(__file__).with_name('pilot_repair_convergence.py'))
    fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
    output.mkdir(parents=True, exist_ok=False)
    repository = output / 'source'; repository.mkdir()
    git(repository, 'init', '-q')
    (repository / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n.auto-agents/\n')
    (repository / 'source.py').write_text(fixture.BROKEN)
    (repository / 'tests').mkdir()
    (repository / 'tests/test_alias.py').write_text(fixture.TESTS)
    (repository / 'conftest.py').write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent))\n')
    git(repository, 'add', '.'); git(repository, 'commit', '-qm', 'Freeze synthetic regression fixture')
    request = RepairRequest('synthetic-alias-repair', git(repository, 'rev-parse', 'HEAD'),
        'Repair registered dependency alias reuse in source.py. Do not change the provided tests. '
        'Registered aliases must retain correct invalidation behavior across changed inputs and retargeting.',
        (Acceptance('alias-reuse', 'Registered aliases reuse matching content; unregistered links, changed input '
                    'and changed link targets must not reuse. Restoring the original input or target restores reuse.'),), provider)
    configured = load_project_config(configuration_project)
    store = Store(output / 'state')
    verifier = DockerVerifier(output / 'verification', image=image)
    selected = configured.providers.get(provider) or ProjectConfig(project_name='synthetic-benchmark').providers[provider]
    driver = NativeDriver(selected, AgentSandbox(output / 'native', image), timeout=timeout)
    workspace = Workspace(output / 'workspace', repository, request.engine_base)
    runner = Controller(request, store, workspace, driver, verifier,
        units=lambda _: [ValidationUnit('alias', 'python -m pytest -q tests/test_alias.py')])
    start = time.monotonic()
    result = runner.run()
    report = {'provider': provider, 'seconds': time.monotonic() - start, 'status': result['status'],
              'attempts': result['attempts'], 'model_calls': result['calls'], 'replans': result['replans'],
              'image': image, 'original_project_modified': False, 'engine_published': False,
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
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output.resolve(), args.configuration_project.resolve(), args.provider,
                             args.image, timeout=args.timeout) else 1)
