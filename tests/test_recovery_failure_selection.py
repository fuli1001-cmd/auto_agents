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


def test_old_baseline_errors_are_not_selected_for_new_repair(monkeypatch):
    from auto_agents import verification_context
    ref = 'tests/old.py::test_old'
    state = SimpleNamespace(baseline_failures=[ref], execution_log=[{
        'action': 'receipt_verification', 'verification': {'ok': False, 'diagnostic': {'failure_ids': [ref]}}}])
    monkeypatch.setattr(verification_context, 'current_context',
                        lambda *args: (_ for _ in ()).throw(AssertionError('baseline requires no recovery selection')))
    assert retained_failure_commands(SimpleNamespace(_recovery_policy_active=True), state, ['python -m pytest tests']) == []


def test_priority_probe_with_only_baseline_failures_still_runs_full_verification(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from auto_agents import session as session_module
    from auto_agents.models import CommandResult, GateResult, SessionState
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.recovery import observations, policy
    from test_session_verification_ownership import project
    root, _ = project(tmp_path)
    session = Session(Orchestrator(root), mode='collab', auto_approve=True)
    command = 'python -m pytest tests/test_value.py'
    baseline = 'tests/test_value.py::test_preexisting'
    session._current_state = SessionState('baseline', mode='collab', baseline_failures=[baseline])
    session._recovery_policy_active = True
    plan = SimpleNamespace(commands=[command], parallel_groups=[], metadata={})
    monkeypatch.setattr(session, '_verification_plan_commands', lambda *args: (plan, [command]))
    monkeypatch.setattr(session, '_session_gate_executor_context', lambda *args, **kw: nullcontext(None))
    monkeypatch.setattr(policy, 'retained_failure_commands', lambda *args: [command])
    monkeypatch.setattr(observations, 'retained_progress_checks', lambda *args: [])
    calls = []
    def run(commands, groups, root, **kwargs):
        calls.append(list(commands))
        return GateResult(False, [CommandResult(command, False, 1,
            stdout='FAILED ' + baseline + ' - AssertionError',
            executed_tests=['tests/test_value.py::test_repaired'])], summary='Only the known baseline still fails')
    monkeypatch.setattr(session_module, 'run_gate_plan', run)
    result = session._run_baseline_diff_verify()
    assert calls == [[command], [command]]
    assert result['ok'] is True


def test_new_retained_failure_precedes_broad_collection_and_only_admits_observed_nodes(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from auto_agents import session as session_module
    from auto_agents.models import CommandResult, GateResult, SessionState
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.recovery import observations, policy
    from test_session_verification_ownership import project
    root, _ = project(tmp_path)
    session = Session(Orchestrator(root), mode='collab', auto_approve=True)
    narrow, broad = 'python -m pytest tests/test_value.py::test_new', 'python -m pytest tests'
    state = SessionState('priority', mode='collab', verification_binding={'contract_fingerprint': 'frozen'})
    session._current_state, session._recovery_policy_active = state, True
    plan = SimpleNamespace(commands=[narrow, broad], parallel_groups=[], metadata={})
    monkeypatch.setattr(session, '_verification_plan_commands', lambda *args: (plan, [narrow, broad]))
    monkeypatch.setattr(session, '_session_gate_executor_context', lambda *args, **kw: nullcontext(None))
    validated = []
    monkeypatch.setattr(session_module, 'validate_selected_contracts', lambda *args, **kw: validated.append(True))
    monkeypatch.setattr(policy, 'retained_failure_commands', lambda *args: [narrow])
    admitted = []
    monkeypatch.setattr(observations, 'retained_progress_checks', lambda session, state, commands: admitted.extend(commands) or [])
    calls = []
    def run(commands, groups, root, **kwargs):
        assert validated
        calls.append(list(commands))
        assert list(commands) == [narrow]
        return GateResult(False, [CommandResult(narrow, False, 1,
            stdout='FAILED tests/test_value.py::test_new - AssertionError')])
    monkeypatch.setattr(session_module, 'run_gate_plan', run)
    result = session._run_baseline_diff_verify()
    assert result['ok'] is False and result['retry_fix'] is True
    assert calls == [[narrow]] and admitted == [narrow]
