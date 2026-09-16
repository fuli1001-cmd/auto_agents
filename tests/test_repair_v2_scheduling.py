import json
from pathlib import Path
import threading

import pytest

from auto_agents.repair_v2.dependencies import execution_fingerprint
from auto_agents.repair_v2.docker import DockerVerifier
from auto_agents.repair_v2.scheduling import Timings
from auto_agents.repair_v2.types import RepairBlocked, ValidationUnit
from auto_agents.repair_v2.workspace import git, source_identity


def result(unit, *, ok=True, infrastructure=False):
    return dict(unit=unit.identity, command=unit.command, ok=ok, infrastructure=infrastructure,
                failed=[] if ok else [unit.identity], missing=[], excerpt='failure' if not ok else '',
                collected=[unit.identity], passed=[unit.identity] if ok else [], seconds=1)


def test_central_acceptance_reports_independent_failures_together(tmp_path):
    verifier = DockerVerifier(tmp_path, workers=1)
    verifier.execute = lambda _, __, unit, cancel: result(unit, ok=unit.identity == 'pass')
    units = [ValidationUnit(name, name) for name in ('first failure', 'pass', 'second failure')]
    observed = verifier.validate_suite('source', tmp_path, units, threading.Event())
    assert [row['unit'] for row in observed.checks] == [unit.identity for unit in units]
    assert [row['unit'] for row in observed.failures] == ['first failure', 'second failure']


def test_parallel_acceptance_stops_dispatch_on_infrastructure_failure(tmp_path):
    verifier = DockerVerifier(tmp_path, workers=2)
    verifier.concurrency = lambda: 2
    barrier, seen = threading.Barrier(2), []
    def execute(_, __, unit, cancel):
        seen.append(unit.identity); barrier.wait(timeout=3)
        return result(unit, ok=False, infrastructure=True)
    verifier.execute = execute
    observed = verifier.validate_suite('source', tmp_path, [ValidationUnit(str(i), str(i)) for i in range(10)], threading.Event())
    assert observed.infrastructure and not observed.ok and set(seen) == {'0', '1'}


def test_worker_exception_cancels_running_sibling_before_join(tmp_path):
    verifier = DockerVerifier(tmp_path, workers=2); verifier.concurrency = lambda: 2
    barrier, stopped = threading.Barrier(2), threading.Event()
    def execute(_, __, unit, cancel):
        barrier.wait(timeout=3)
        if unit.identity == 'bad': raise RepairBlocked('disk_space', 'disk full')
        import time
        end = time.monotonic() + 3
        while not cancel.is_set() and time.monotonic() < end: time.sleep(.01)
        assert cancel.is_set(), 'executor waited for sibling without cancelling it'
        stopped.set(); return result(unit)
    verifier.execute = execute
    with pytest.raises(RepairBlocked, match='disk full'):
        verifier.validate_suite('source', tmp_path, [ValidationUnit(n, n) for n in ('bad', 'waiting')], threading.Event())
    assert stopped.is_set()


def test_packing_retains_every_node_and_runs_long_batches_first(tmp_path):
    groups = {f'tests/test_fast_{i}.py': [f'tests/test_fast_{i}.py::test_value'] for i in range(16)}
    groups['tests/test_slow.py'] = [f'tests/test_slow.py::test_value[{i}]' for i in range(40)]
    timings = Timings(tmp_path)
    timings.nodes = {n: 30 if 'slow' in n else 1 for nodes in groups.values() for n in nodes}
    units = timings.batches(groups)
    expected = sorted(n for nodes in groups.values() for n in nodes)
    assert sorted(n for unit in units for n in unit.expected_nodes) == expected
    assert len(units) == 4 and 'test_slow.py' in units[0].command
    assert all(unit.profile == 'sandbox' for unit in units)
    baseline = timings.batches(groups, prefix='behavior-baseline', fresh=True, pack=False)
    assert sorted(n for unit in baseline for n in unit.expected_nodes) == expected
    assert all(unit.fresh and len(unit.expected_nodes) <= 32 for unit in baseline)


def test_corrupt_or_unusable_timings_never_remove_tests(tmp_path):
    path = tmp_path / 'timings.json'
    groups = {'tests/test_x.py': ['tests/test_x.py::test_value']}
    for text in ('{', '[]', '{"nodes":{"tests/test_x.py::test_value":NaN}}'):
        path.write_text(text)
        assert Timings(tmp_path).batches(groups)[0].expected_nodes == tuple(groups['tests/test_x.py'])


@pytest.fixture
def cached_verifier(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.mkdir(); git(source, 'init', '-q')
    (source / '.gitignore').write_text('ignored.txt\n')
    (source / 'test_x.py').write_text('def test_value(tmp_path): assert tmp_path.exists()\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'source')
    verifier = DockerVerifier(tmp_path / 'verification', image='pinned'); verifier.runtime = 'runtime-1'
    node = 'test_x.py::test_value'
    unit = ValidationUnit('test', 'python -m pytest -q test_x.py', expected_nodes=(node,))
    launched = []
    def run(command, **kwargs):
        if command[1] == 'run':
            launched.append(command)
            destination = next(arg for arg in command if arg.startswith('type=bind,src=') and arg.endswith(',dst=/result'))
            out = Path(destination.split('src=', 1)[1].split(',dst=', 1)[0])
            (out / 'pytest.json').write_text(json.dumps({'collected': [node], 'passed': [node]}))
            kwargs['observation'].update(started=True, termination='', exit_code=0)
            return 0, 'passed'
        if command[1] == 'inspect': return 0, '{"OOMKilled":false,"ExitCode":0}'
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    return source, verifier, unit, launched


def test_exact_cache_reuses_opaque_tests_and_binds_all_materialized_inputs(cached_verifier):
    source, verifier, unit, launched = cached_verifier
    def execute(unit=unit): return verifier.execute(source_identity(source), source, unit, threading.Event())
    first = execute(); assert first['ok'] and not first['inputs']['complete']
    assert execute()['cache_hit'] and len(launched) == 1
    # Git and ignored files do not change delivered-source identity, but can
    # change an opaque test's behavior and must invalidate its execution proof.
    identity = source_identity(source)
    (source / 'ignored.txt').write_text('new input')
    assert source_identity(source) == identity and not execute()['cache_hit']
    assert execute()['cache_hit']
    git(source, 'config', 'test.option', 'changed')
    assert source_identity(source) == identity and not execute()['cache_hit']
    verifier.runtime = 'runtime-2'
    assert not execute()['cache_hit']
    fresh = ValidationUnit(unit.identity, unit.command, unit.expected_nodes, fresh=True)
    assert not execute(fresh)['cache_hit'] and not execute(fresh)['cache_hit']
    assert not list(verifier.root.glob('executions/*/source'))


def test_corrupt_cache_or_different_selection_runs_fresh(cached_verifier):
    source, verifier, unit, launched = cached_verifier
    def execute(unit=unit): return verifier.execute(source_identity(source), source, unit, threading.Event())
    assert execute()['ok']
    cache = next((verifier.root / 'cache').glob('*.json')); cache.write_text('{')
    assert not execute()['cache_hit'] and len(launched) == 2
    # A passing command is insufficient when a new mandatory node was not run.
    changed = ValidationUnit(unit.identity, unit.command, (*unit.expected_nodes, 'test_x.py::test_missing'))
    observed = execute(changed)
    assert not observed['ok'] and not observed['cache_hit'] and observed['missing']
    assert not execute(changed)['cache_hit'] and len(launched) == 4


def test_execution_fingerprint_binds_modes_times_and_links_without_following(tmp_path):
    import os
    path = tmp_path / 'value'; path.write_text('same')
    first = execution_fingerprint(tmp_path)
    info = path.stat(); os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    assert execution_fingerprint(tmp_path) != first
    before = execution_fingerprint(tmp_path); path.chmod(0o700)
    assert execution_fingerprint(tmp_path) != before
    (tmp_path / 'link').symlink_to('/does/not/exist')
    assert execution_fingerprint(tmp_path)


def test_timeout_is_infrastructure_and_does_not_request_a_code_rewrite(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.mkdir(); git(source, 'init', '-q')
    (source / 'value.py').write_text('value = 1\n')
    output = tmp_path / 'execution/result'; output.mkdir(parents=True)
    def run(command, **kwargs):
        if command[1] == 'run':
            kwargs['observation'].update(started=True, termination='timeout', exit_code=-15)
            return 130, ''
        if command[1] == 'inspect': return 0, '{"OOMKilled":false,"ExitCode":143}'
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    verifier = DockerVerifier(tmp_path / 'verification', image='fixed')
    observed = verifier._execute(source_identity(source), source, ValidationUnit('test', 'true'), threading.Event(),
        '', tmp_path / 'cache.json', {'complete': False}, 'probe', output.parent, source, output, {'clear': True})
    assert not observed['ok'] and observed['timed_out'] and observed['infrastructure']
    assert 'time limit' in observed['diagnostic']
