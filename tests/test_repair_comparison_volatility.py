"""Only process-generated noise may vary in reproduced baseline failures."""
from copy import deepcopy

import pytest

from auto_agents.repair_v2.comparison import POLICY, failure_message, matched, unchanged, verify
from auto_agents.repair_v2.store import digest
from test_goal_scoped_repair import report


def message(directory='abcdefgh', address='0x123abc'):
    path = '/tmp/auto-agents-session-replay-' + directory + '/target/.auto-agents/state/sessions/child/logs/diagnostics.json'
    method = '<built-in method get of dict object at ' + address + '>'
    return ("AssertionError: {'ok': False, 'error': '...ostics: " + path + "'}\n"
            "assert None is True\n +  where None = " + method + "('route_consumed')\n"
            ' +    where ' + method + " = {'error': 'Diagnostics: " + path + "'}.get")


def test_volatile_failure_equivalence_preserves_raw_evidence():
    current, baseline = report(message=message()), report('base', message('8765abcd', '0xfeed1234'))
    before = deepcopy([current, baseline])
    proof = {'policy': POLICY, 'ok': True, 'snapshot': 'candidate', 'base': 'base',
             'validation_digest': digest(current), 'baseline_report': baseline,
             'unchanged_tests': ['tests/test_old.py::test_old']}
    assert verify(proof, current, base='base')
    assert matched(proof, current) == {'tests/test_old.py::test_old'}
    assert [current, baseline] == before
    assert not current['ok'] and not baseline['ok']
    assert not verify({**proof, 'policy': 'no-new-failures-v1'}, current, base='base')


@pytest.mark.parametrize('mutation', [
    lambda value: value.replace('None is True', 'None is False'),
    lambda value: value.replace('AssertionError', 'RuntimeError'),
    lambda value: value.replace("'ok': False", "'ok': True"),
    lambda value: value.replace('route_consumed', 'another_route'),
    lambda value: value.replace('/sessions/child/', '/sessions/another-child/'),
    lambda value: value.replace('/logs/diagnostics.json', '/logs/another.json'),
    lambda value: value.replace('0xfeed1234', '0xother', 1),
    lambda value: value.replace('0xfeed1234', '0x567abc', 1),
    lambda value: value.replace('8765abcd', 'second12', 1),
])
def test_normalization_cannot_hide_changed_assertions_causes_or_identity_relationships(mutation):
    assert not unchanged(report(message=message()), report('base', mutation(message('8765abcd', '0xfeed1234'))))


@pytest.mark.parametrize('text', [
    'assert 25 == 32', 'assert 0x123abc == 0xfeed1234',
    'assert "<built-in method get of dict object at 0x123abc>" == expected',
    'AssertionError: assert "Diagnostics: /tmp/auto-agents-session-replay-abcdefgh/target/.auto-agents/state/sessions/child/logs/diagnostics.json" == expected',
    'AssertionError: /tmp/user-output-abcdefgh/video.mp4 was missing',
    'Diagnostics: /tmp/other-job-abcdefgh/target/.auto-agents/state/sessions/child/logs/diagnostics.json',
    'Pointer value: <built-in method get of dict object at 0x123abc>',
])
def test_asserted_values_and_unrecognized_paths_remain_exact(text):
    assert failure_message(text) == text
