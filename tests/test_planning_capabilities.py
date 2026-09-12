"""Planning observations must be isolated, bounded, and never acceptance proof."""
import errno
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents import planning_capabilities as capabilities


def test_native_observation_is_confined_to_child():
    status = Path('/proc/self/status')
    def filters():
        return [line for line in status.read_text().splitlines() if line.startswith(('Seccomp', 'NoNewPrivs'))] if status.exists() else []
    before = filters()
    process = subprocess.run([sys.executable, capabilities.__file__], capture_output=True, text=True, timeout=10, check=True)
    observed = json.loads(process.stdout)
    assert observed['acceptance_proof'] is False
    assert observed['python_apis']['memfd_create'] == hasattr(__import__('os'), 'memfd_create')
    notification = observed['seccomp_notification']
    if notification.get('second_listener_errno') == errno.EBUSY or notification.get('errno') == errno.EBUSY:
        assert notification['additional_listener_supported'] is False
    assert filters() == before


def test_observation_is_cached_per_interpreter():
    runner = SimpleNamespace(_verification_python=lambda: sys.executable)
    observed = {'version': 1, 'acceptance_proof': False, 'python_apis': {'memfd_create': False}}
    with patch.object(capabilities.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(observed))) as run:
        assert capabilities.planning_capabilities(runner) == observed
        assert capabilities.planning_capabilities(runner) == observed
        assert run.call_count == 1
        runner._verification_python = lambda: '/unavailable/interpreter'
        capabilities.planning_capabilities(runner)
        assert run.call_count == 2


@pytest.mark.parametrize('output', ['[]', '{}', 'invalid', '{"version":1,"acceptance_proof":true}'])
def test_bad_child_output_is_inconclusive(output):
    runner = SimpleNamespace(_verification_python=lambda: sys.executable)
    with patch.object(capabilities.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output)):
        result = capabilities.planning_capabilities(runner)
    assert result['status'] == 'inconclusive'
    assert result['acceptance_proof'] is False


def test_timeout_is_inconclusive():
    runner = SimpleNamespace(_verification_python=lambda: sys.executable)
    with patch.object(capabilities.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', 10)):
        assert capabilities.planning_capabilities(runner)['status'] == 'inconclusive'
