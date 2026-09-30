"""Console counts describe logical batches; liveness never invents a passed check."""
import io
from types import SimpleNamespace
import sys

import pytest

from auto_agents.execution_display import ExecutionDisplay
from auto_agents.models import CommandResult, GateResult
from auto_agents.reporting import Reporter, GateObservation


@pytest.fixture
def view(tmp_path):
    stream = io.StringIO()
    reporter = Reporter(tmp_path, stream, language='zh')
    reporter.bind('run', 'example')
    yield reporter, stream
    reporter.close()


def progress(reporter, context='candidate verification'):
    def callback(*args): pass
    callback.reporter = reporter
    callback.context = context
    return callback


def test_plain_counts_and_minute_heartbeat_do_not_advance_unfinished_checks(view, monkeypatch):
    import auto_agents.reporting as reporting
    reporter, stream = view
    clock = [0.0]
    monkeypatch.setattr(reporting.time, 'monotonic', lambda: clock[0])
    observation = GateObservation(progress(reporter), reporter, 2)
    command = 'python -m pytest tests/test_owned.py'
    observation('start', command, 0)
    before = stream.getvalue()
    clock[0] = 30
    observation('heartbeat', command, 30)
    assert stream.getvalue() == before
    clock[0] = 61
    observation('heartbeat', command, 61)
    assert '验证 0/2' in stream.getvalue() and '已运行 61 秒' in stream.getvalue()
    assert observation.counts['completed'] == 0 and observation.counts['passed'] == 0
    first = CommandResult(command, True, 0, cached=True)
    observation.result(first); observation.result(first)
    assert '验证 1/2' in stream.getvalue() and observation.counts['completed'] == 1
    second = CommandResult('python -m pytest tests/test_other.py', False, 1)
    observation.result(second)
    observation.finish(GateResult(False, [first, second]))
    assert '验证 2/2' in stream.getvalue() and observation.counts['failed'] == 1
    assert stream.getvalue().count('    验证\n') == 1


def test_live_terminal_refreshes_one_action_instead_of_appending_progress(view, monkeypatch):
    reporter, stream = view
    frames = []
    monkeypatch.setattr(reporter.presenter, '_ensure_started', lambda: None)
    reporter.presenter._live = SimpleNamespace(update=lambda frame, **kw: frames.append(str(frame)), stop=lambda: None)
    observation = GateObservation(progress(reporter), reporter, 2)
    observation('start', 'python -m pytest tests/test_owned.py', 0)
    observation.result(CommandResult('python -m pytest tests/test_owned.py', True, 0))
    frame = reporter.presenter._frame(reporter)
    assert '1/2' in frame
    assert '验证 1/2' not in stream.getvalue()
    assert len(reporter.action_lines) == 1


def test_stale_check_set_cannot_publish_progress_for_new_activity(view, monkeypatch):
    import auto_agents.reporting as reporting
    reporter, stream = view
    observation = GateObservation(progress(reporter), reporter, 2)
    before = stream.getvalue()
    reporter.display = ExecutionDisplay()
    monkeypatch.setattr(reporting.time, 'monotonic', lambda: observation._last_progress_at + 70)
    observation('heartbeat', 'python -m pytest tests/test_old.py', 70)
    assert stream.getvalue() == before


def test_collection_commands_have_one_heading_and_real_batch_counts(view):
    from auto_agents.gates import run_gate_plan
    reporter, stream = view
    commands = [f'{sys.executable} -c "print({i})"' for i in range(2)]
    result = run_gate_plan(commands, [], reporter.project_root, collect_all=False,
                          progress=progress(reporter, 'session verification collection'))
    assert result.ok
    text = stream.getvalue()
    assert text.count('验证准备（收集测试）') == 1
    assert '验证准备 1/2' in text and '验证准备 2/2' in text
    assert '    验证\n' not in text
