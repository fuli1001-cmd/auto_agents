from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from auto_agents.repair_v2.docker import DockerVerifier
from auto_agents.repair_v2.pytest_driver import Evidence
from auto_agents.repair_v2.workspace import git


@pytest.mark.parametrize('phase,xfail,exception,expected', [('setup', False, True, []), ('teardown', False, True, []),
    ('call', True, True, []), ('call', False, False, []), ('call', False, True, ['tests/test_x.py::test_x'])])
def test_only_unexpected_test_body_failures_supply_counterexamples(phase, xfail, exception, expected):
    report = SimpleNamespace(when=phase, failed=True, passed=False, skipped=False, nodeid='tests/test_x.py::test_x')
    if xfail: report.wasxfail = 'expected failure'
    evidence = Evidence(); evidence.pytest_runtest_logreport(report)
    def body(): pass
    excinfo = SimpleNamespace(traceback=[SimpleNamespace(frame=SimpleNamespace(code=SimpleNamespace(raw=body.__code__)))])
    hook = evidence.pytest_runtest_makereport(SimpleNamespace(obj=body), SimpleNamespace(when=phase, excinfo=excinfo if exception else None))
    next(hook)
    with pytest.raises(StopIteration): hook.send(SimpleNamespace(get_result=lambda: report))
    assert evidence.call_failed == expected


@pytest.mark.parametrize('code,body_fail,accepted', [(4, False, False), (2, False, False),
                                                   (1, False, False), (1, True, True)])
def test_baseline_collection_errors_cannot_mask_or_replace_behavior(tmp_path, code, body_fail, accepted):
    source = tmp_path / 'source'; source.mkdir(); (source / 'tests').mkdir()
    git(source, 'init', '-q')
    (source / 'value.py').write_text('value = 0\n')
    (source / 'tests/test_portable.py').write_text('def test_value(): assert True\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'original')
    base = git(source, 'rev-parse', 'HEAD')
    (source / 'value.py').write_text('value = 1\n')
    (source / 'tests/test_portable.py').write_text('def test_value(): assert value == 1\n')
    (source / 'tests/test_new_api.py').write_text('from missing_api import new_api\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'candidate')
    verifier = DockerVerifier(tmp_path / 'verification', workers=2); verifier.root.mkdir()
    calls = []
    def execute(identity, baseline, unit, cancel):
        assert (baseline / 'value.py').read_text() == 'value = 0\n'
        assert (baseline / 'tests/test_portable.py').read_text() == 'def test_value(): assert value == 1\n'
        portable = 'test_portable' in unit.command
        node = 'tests/test_portable.py::test_value' if portable else 'tests/test_new_api.py::test_api'
        if '--collect-only' in unit.command:
            return dict(unit=unit.identity, command=unit.command, ok=True, returncode=0,
                        collected=[node], failed=[], missing=[], infrastructure=False, source_unchanged=True)
        calls.append(unit.command)
        return dict(unit=unit.identity, command=unit.command, ok=False, returncode=code if portable else 2,
                    failed=[node] if portable and code == 1 else [], missing=[], infrastructure=False,
                    call_failed=[node] if portable and body_fail else [], source_unchanged=True,
                    excerpt='baseline evidence', output='log')
    verifier.execute = execute
    result = verifier.regression('snapshot', source, source, base,
        [{'nodes': ['tests/test_new_api.py::test_api', 'tests/test_portable.py::test_value']}], threading.Event())
    assert len(calls) == 2 and all(not ('test_new_api' in c and 'test_portable' in c) for c in calls)
    assert result['ok'] is accepted and result['demonstrated_regression'] is accepted
    assert result['counterexamples'] == (['tests/test_portable.py::test_value'] if accepted else [])


def test_real_pytest_strict_xpass_and_fixture_errors_are_not_body_failures(tmp_path):
    import json
    import os
    import subprocess
    import sys
    source = '''import pytest
import unittest
@pytest.mark.xfail(strict=True)
def test_unexpected_pass(): assert True
@pytest.mark.xfail(strict=True)
def test_expected_failure(): assert False
def test_body_failure(): assert False
@pytest.fixture
def broken(): raise RuntimeError('setup failure')
def test_setup(broken): pass
@pytest.fixture
def bad_cleanup():
    yield
    raise RuntimeError('teardown failure')
def test_teardown(bad_cleanup): pass
class TestUnittest(unittest.TestCase):
    def test_body(self): self.assertEqual(1, 2)
class TestUnittestSetup(unittest.TestCase):
    def setUp(self): raise RuntimeError('unittest setup')
    def test_body(self): pass
class TestUnittestTeardown(unittest.TestCase):
    def tearDown(self): raise RuntimeError('unittest teardown')
    def test_body(self): assert True
class TestUnittestCleanup(unittest.TestCase):
    def test_body(self):
        self.addCleanup(self.broken_cleanup)
        assert True
    def broken_cleanup(self): raise RuntimeError('unittest cleanup')
class TestAsyncBody(unittest.IsolatedAsyncioTestCase):
    async def test_body(self): self.assertEqual(1, 2)
class TestAsyncSetup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): raise RuntimeError('async setup')
    async def test_body(self): pass
class TestAsyncTeardown(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self): raise RuntimeError('async teardown')
    async def test_body(self): assert True
'''
    (tmp_path / 'test_cases.py').write_text(source)
    script = '''import json,pytest
from pathlib import Path
from auto_agents.repair_v2.pytest_driver import Evidence
e=Evidence()
code=pytest.main(['-q','-p','no:cacheprovider','test_cases.py'],plugins=[e])
Path('evidence.json').write_text(json.dumps({'code':code,'body':e.call_failed,'failed':e.failed}))
'''
    process = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads((tmp_path / 'evidence.json').read_text())
    assert result['code'] == 1
    assert set(result['body']) == {'test_cases.py::test_body_failure', 'test_cases.py::TestUnittest::test_body',
                                  'test_cases.py::TestAsyncBody::test_body'}
    assert 'test_cases.py::test_unexpected_pass' in result['failed']


@pytest.mark.parametrize('mutated', [False, True])
def test_baseline_shards_parameter_cases_and_rejects_source_mutation(tmp_path, mutated):
    source = tmp_path / 'source'; source.mkdir(); (source / 'tests').mkdir()
    git(source, 'init', '-q'); (source / 'value.py').write_text('value = 0\n')
    (source / 'tests/test_value.py').write_text('def test_value(): pass\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'old')
    base = git(source, 'rev-parse', 'HEAD'); (source / 'value.py').write_text('value = 1\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'new')
    nodes = [f'tests/test_value.py::test_value[{i}]' for i in range(75)]
    executed = []
    verifier = DockerVerifier(tmp_path / 'verification', workers=2); verifier.root.mkdir()
    verifier.concurrency = lambda: 2
    def execute(identity, baseline, unit, cancel):
        collect = '--collect-only' in unit.command
        batch = nodes if collect else list(unit.expected_nodes)
        if not collect: executed.append(batch)
        return dict(unit=unit.identity, command=unit.command, ok=collect,
                    returncode=0 if collect else 1, collected=batch, source_unchanged=collect or not mutated,
                    failed=[] if collect else batch, call_failed=[] if collect else batch,
                    missing=[], infrastructure=False, excerpt='old behavior fails')
    verifier.execute = execute
    observed = verifier.regression('source', source, source, base,
        [{'nodes': ['tests/test_value.py::test_value']}], threading.Event())
    assert len(executed) == 3 and all(len(batch) <= 32 for batch in executed)
    assert sorted(n for batch in executed for n in batch) == sorted(nodes)
    assert observed['ok'] is (not mutated)
    assert observed['counterexamples'] == ([] if mutated else sorted(nodes))
