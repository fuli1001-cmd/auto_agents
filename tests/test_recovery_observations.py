from auto_agents.models import CommandResult, GateResult
from auto_agents.recovery.observations import gate_checks


def test_old_success_certificate_retains_exact_passed_nodes():
    result = CommandResult('python -m pytest tests/test_value.py', True, 0,
                           cached=True, executed_tests=['tests/test_value.py::test_value'])
    rows = gate_checks(GateResult(True, [result]))
    assert next(row for row in rows if row['id'] == 'tests/test_value.py::test_value')['status'] == 'passed'


def test_failed_command_can_retain_real_passes_but_never_infer_other_passes():
    result = CommandResult('python -m pytest tests/test_value.py', False, 1,
        stdout='FAILED tests/test_value.py::test_other - AssertionError',
        executed_tests=['tests/test_value.py::test_value'])
    rows = {row['id']: row['status'] for row in gate_checks(GateResult(False, [result]))}
    assert rows['tests/test_value.py::test_value'] == 'passed'
    assert rows['tests/test_value.py::test_other'] == 'failed'
    assert len(rows) == 3


def test_uncertain_execution_does_not_publish_partial_pass_credit():
    result = CommandResult('python -m pytest tests/test_value.py', False, -1,
        executed_tests=['tests/test_value.py::test_value'], cleanup_incomplete=True)
    assert {row['status'] for row in gate_checks(GateResult(False, [result]))} == {'blocked'}
