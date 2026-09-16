from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from auto_agents.repair_v2.docker import DockerVerifier
from auto_agents.repair_v2.pytest_driver import Evidence
from auto_agents.repair_v2.workspace import git


@pytest.mark.parametrize('phase,xfail,expected', [('setup', False, []), ('teardown', False, []),
                                               ('call', True, []), ('call', False, ['tests/test_x.py::test_x'])])
def test_only_unexpected_test_body_failures_supply_counterexamples(phase, xfail, expected):
    report = SimpleNamespace(when=phase, failed=True, passed=False, skipped=False, nodeid='tests/test_x.py::test_x')
    if xfail: report.wasxfail = 'expected failure'
    evidence = Evidence(); evidence.pytest_runtest_logreport(report)
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
        calls.append(unit.command)
        portable = 'test_portable' in unit.command
        node = 'tests/test_portable.py::test_value'
        return dict(unit=unit.identity, command=unit.command, ok=False, returncode=code if portable else 2,
                    failed=[node] if portable and code == 1 else [], missing=[], infrastructure=False,
                    call_failed=[node] if portable and body_fail else [], excerpt='baseline evidence', output='log')
    verifier.execute = execute
    result = verifier.regression('snapshot', source, source, base,
        [{'nodes': ['tests/test_new_api.py::test_api', 'tests/test_portable.py::test_value']}], threading.Event())
    assert len(calls) == 2 and all(not ('test_new_api' in c and 'test_portable' in c) for c in calls)
    assert result['ok'] is accepted and result['demonstrated_regression'] is accepted
    assert result['counterexamples'] == (['tests/test_portable.py::test_value'] if accepted else [])
