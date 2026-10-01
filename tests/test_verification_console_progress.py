"""Console counts describe logical batches; liveness never invents a passed check."""
import io
import json
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


@pytest.mark.parametrize('mode', ['auto', 'plain'])
@pytest.mark.parametrize('context,heading', [
    ('candidate verification', '验证'),
    ('session verification collection', '验证准备（收集测试）'),
    ('baseline verification', '基线验证'),
])
def test_plain_progress_keeps_one_heading_and_diagnostic_counts(view, monkeypatch, mode, context, heading):
    import auto_agents.reporting as reporting
    reporter, stream = view
    reporter.presenter.configure(mode, False)
    clock = [0.0]
    monkeypatch.setattr(reporting.time, 'monotonic', lambda: clock[0])
    observation = GateObservation(progress(reporter, context), reporter, 2)
    command = 'python -m pytest tests/test_owned.py'
    observation('start', command, 0)
    before = stream.getvalue()
    clock[0] = 30
    observation('heartbeat', command, 30)
    assert stream.getvalue() == before
    clock[0] = 61
    observation('heartbeat', command, 61)
    assert stream.getvalue() == before
    assert observation.counts['completed'] == 0 and observation.counts['passed'] == 0
    first = CommandResult(command, True, 0, cached=True)
    observation.result(first); observation.result(first)
    assert stream.getvalue() == before and observation.counts['completed'] == 1
    second = CommandResult('python -m pytest tests/test_other.py', False, 1)
    observation.result(second)
    observation.finish(GateResult(False, [first, second]))
    assert observation.counts['failed'] == 1
    assert stream.getvalue().count(f'    {heading}\n') == 1
    assert stream.getvalue().count('验证未通过') == 1
    assert '0/2' not in stream.getvalue() and '1/2' not in stream.getvalue() and '2/2' not in stream.getvalue()
    user_log = (reporter.root / 'user.log').read_text()
    assert user_log.count(f'    {heading}\n') == 1
    assert '已运行' not in user_log and '\x1b' not in stream.getvalue()
    events = [json.loads(line) for line in (reporter.root / 'events.jsonl').read_text().splitlines()]
    updates = [event for event in events if event['type'] == 'verification.progress']
    assert any('已运行 61 秒' in event['message'] and event['data']['completed'] == 0 for event in updates)
    assert updates[-1]['data']['completed'] == 2 and updates[-1]['data']['failed'] == 1


@pytest.mark.parametrize('context,heading', [
    ('candidate verification', '验证'),
    ('session verification collection', '验证准备（收集测试）'),
    ('baseline verification', '基线验证'),
])
def test_live_terminal_refreshes_one_action_instead_of_appending_progress(view, monkeypatch, context, heading):
    reporter, stream = view
    frames = []
    history = []
    monkeypatch.setattr(reporter.presenter, '_ensure_started', lambda: None)
    reporter.presenter._live = SimpleNamespace(update=lambda frame, **kw: frames.append(str(frame)), stop=lambda: None)
    reporter.presenter._console = SimpleNamespace(width=120, print=lambda value: history.append(value.plain))
    observation = GateObservation(progress(reporter, context), reporter, 2)
    observation('start', 'python -m pytest tests/test_owned.py', 0)
    first = CommandResult('python -m pytest tests/test_owned.py', True, 0)
    observation.result(first)
    frame = reporter.presenter._frame(reporter)
    assert '1/2' in frame and heading in frame
    assert len(frame.splitlines()) == 1
    assert stream.getvalue() == ''
    assert len(reporter.action_lines) == 1
    observation.finish(GateResult(True, [first, CommandResult('second', True, 0)]))
    assert len(history) == 1 and history[0].endswith(f'    {heading}')
    assert stream.getvalue() == ''
    assert reporter.presenter._frame(reporter) == ''


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
    assert len(text.splitlines()) == 1
    assert '1/2' not in text and '2/2' not in text
    assert '    验证\n' not in text


def test_debug_output_retains_progress_updates(view):
    reporter, stream = view
    reporter.presenter.configure('debug', False)
    observation = GateObservation(progress(reporter), reporter, 1)
    observation.finish(GateResult(True, [CommandResult('check', True, 0)]))
    assert '验证 0/1' in stream.getvalue() and '验证 1/1' in stream.getvalue()
