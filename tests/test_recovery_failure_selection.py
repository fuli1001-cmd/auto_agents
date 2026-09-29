from types import SimpleNamespace

from auto_agents.recovery.policy import retained_failure_commands


def test_priority_uses_actual_nodes_and_never_matches_deselect_text(monkeypatch):
    from auto_agents import pytest_selection, session_verification, verification_context
    first, second = 'tests/failure.py::test_a', 'tests/failure.py::test_b'
    unrelated = 'python -m pytest tests/other.py --deselect=' + first
    filtered = 'python -m pytest tests/failure.py --deselect=' + first + ' --deselect=' + second
    broad = 'python -m pytest tests'
    a, b = 'python -m pytest ' + first, 'python -m pytest ' + second
    commands = [unrelated, filtered, broad, a, b]
    selections = {unrelated: {'tests/other.py::test_other'}, filtered: {'tests/failure.py::test_c'},
                  broad: {first, second, *['tests/other.py::test_' + str(i) for i in range(50)]},
                  a: {first}, b: {second}}
    targets = {unrelated: ['tests/other.py'], filtered: ['tests/failure.py'], broad: ['tests'],
               a: [first], b: [second]}
    monkeypatch.setattr(verification_context, 'current_context', lambda *args: SimpleNamespace(
        invocations=lambda command: [SimpleNamespace(runner='pytest', repository_targets=targets[command], raw=command)]))
    monkeypatch.setattr(pytest_selection, 'selected_nodes', lambda session, state, invocation, **kw:
                        (selections[invocation.raw], {}, []))
    monkeypatch.setattr(session_verification, '_mandatory_refs', lambda state: set())
    state = SimpleNamespace(execution_log=[{'action': 'receipt_verification', 'verification': {
        'ok': False, 'reason': '2 new failure(s) introduced: ' + first + ', ' + second}}])
    session = SimpleNamespace(_recovery_policy_active=True)
    assert retained_failure_commands(session, state, commands) == [a, b]


def test_recorded_priority_list_does_not_bypass_current_selector_validation(monkeypatch):
    from auto_agents import verification_context
    state = SimpleNamespace(execution_log=[{'action': 'receipt_verification', 'verification': {
        'ok': False, 'reason': 'Retained failure', 'diagnostic': {'failure_ids': ['tests/failure.py::test_a']},
        'recovery_priority_commands': ['python -m pytest tests/other.py']}}])
    monkeypatch.setattr(verification_context, 'current_context', lambda *args: SimpleNamespace(
        invocations=lambda command: [SimpleNamespace(runner='pytest', repository_targets=['tests/other.py'])]))
    assert retained_failure_commands(SimpleNamespace(_recovery_policy_active=True), state,
                                     ['python -m pytest tests/other.py']) == []


def test_parameterized_failure_matches_its_declared_function_selector(monkeypatch):
    from auto_agents import pytest_selection, session_verification, verification_context
    ref, command = 'tests/failure.py::test_case[second]', 'python -m pytest tests/failure.py::test_case'
    state = SimpleNamespace(execution_log=[{'action': 'receipt_verification', 'verification': {
        'ok': False, 'diagnostic': {'failure_ids': [ref]}}}])
    monkeypatch.setattr(verification_context, 'current_context', lambda *args: SimpleNamespace(
        invocations=lambda value: [SimpleNamespace(runner='pytest', repository_targets=['tests/failure.py::test_case'])]))
    monkeypatch.setattr(pytest_selection, 'selected_nodes', lambda *args, **kw: ({ref}, {}, []))
    monkeypatch.setattr(session_verification, '_mandatory_refs', lambda state: set())
    assert retained_failure_commands(SimpleNamespace(_recovery_policy_active=True), state, [command]) == [command]
