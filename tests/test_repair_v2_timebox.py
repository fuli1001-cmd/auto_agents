from types import SimpleNamespace
from unittest.mock import patch
import io
import json

import pytest

from auto_agents.models import ProviderConfig
from auto_agents.repair_v2.providers import AgentSandbox, NativeDriver
from auto_agents.repair_v2.types import AgentReply, RepairBlocked
from auto_agents.repair_v2.workspace import source_identity
from test_repair_v2_controller import job, controller, Driver


def test_timed_out_implementation_with_edits_enters_full_acceptance(job):
    driver = Driver(); original = driver.run
    def run(role, *args, **kwargs):
        reply = original(role, *args, **kwargs)
        if role == 'implement':
            return AgentReply(False, session=reply.session, error='time limit', timed_out=True)
        return reply
    driver.run = run
    runner = controller(job, driver)
    result = runner.run()
    assert result['status'] == 'ready' and result['attempts'] == 1
    assert len(runner.verifier.calls) == 1
    assert driver.calls == [('plan', ''), ('implement', 'writer'), ('review', '')]
    assert result['implementation_timebox']['source_before'] != result['implementation_timebox']['source_after']


def test_timed_out_partial_candidate_must_pass_independent_review(job):
    driver = Driver(); original = driver.run
    implementation = 0
    def run(role, *args, **kwargs):
        nonlocal implementation
        reply = original(role, *args, **kwargs)
        if role == 'implement':
            implementation += 1
            if implementation == 1:
                return AgentReply(False, session=reply.session, error='time limit', timed_out=True)
        if role == 'review' and implementation == 1:
            return AgentReply(True, json.dumps({'decision': 'REJECT', 'coverage': [], 'findings': [{
                'requirement': 'value', 'reason': 'unfinished branch', 'counterexample': 'edge input fails',
                'check': 'exercise the edge input'}]}), 'reviewer')
        return reply
    driver.run = run
    runner = controller(job, driver); result = runner.run()
    assert result['status'] == 'ready' and result['attempts'] == 2
    assert [role for role, _ in driver.calls].count('plan') == 1
    assert [role for role, _ in driver.calls].count('review') == 2


def test_timeout_without_source_progress_does_not_launch_acceptance(job):
    driver = Driver(); original = driver.run
    def run(role, *args, **kwargs):
        if role == 'implement': return AgentReply(False, session='writer', error='time limit', timed_out=True)
        return original(role, *args, **kwargs)
    driver.run = run
    runner = controller(job, driver); result = runner.run()
    assert result['blocker']['code'] == 'provider_timeout' and not runner.verifier.calls
    assert result['attempts'] == 0 and not result.get('receipt')


def test_other_provider_errors_with_edits_remain_failures(job):
    driver = Driver(); original = driver.run
    def run(role, *args, **kwargs):
        reply = original(role, *args, **kwargs)
        if role == 'implement': return AgentReply(False, session='writer', error='external input required')
        return reply
    driver.run = run
    runner = controller(job, driver); result = runner.run()
    assert result['blocker']['code'] == 'provider_failed' and not runner.verifier.calls


def test_legacy_timeout_uses_retained_partial_source_before_any_model_call(job):
    driver = Driver(); original = driver.run
    def interrupted(role, *args, **kwargs):
        reply = original(role, *args, **kwargs)
        if role == 'implement':
            return AgentReply(False, session='writer', error='provider call exceeded its configured time budget')
        return reply
    driver.run = interrupted
    runner = controller(job, driver); saved = runner.run()
    assert saved['status'] == 'blocked' and saved['attempts'] == 0
    before = source_identity(runner.workspace.candidate)
    # Older controllers recorded the call's input only in their journal.
    saved.pop('active_call'); runner.store.save(saved)
    with (runner.store.root / 'events.jsonl').open('a') as stream: stream.write('{"partial":')
    driver.run = original
    resumed = controller(job, driver); resumed.resume_token = 'new-explicit-invocation'
    result = resumed.run()
    assert result['status'] == 'ready' and result['attempts'] == 1
    assert driver.calls == [('plan', ''), ('implement', 'writer'), ('review', '')]
    assert result['snapshot'] == before
    assert resumed.run()['attempts'] == 1


@pytest.mark.parametrize('kind', ['codex', 'claude-code'])
def test_native_protocols_report_timeout_separately_from_interruption(tmp_path, kind):
    with patch('shutil.which', return_value='/usr/bin/native'):
        driver = NativeDriver(ProviderConfig(kind=kind, binary='native'), None)
    process = SimpleNamespace(stdin=io.StringIO(), terminate=lambda: None)
    for error in (TimeoutError('time limit'), InterruptedError('cancelled')):
        with patch.object(driver, 'next_message', side_effect=error), \
             patch.object(driver, 'selected', return_value=('model', {})):
            result = (driver.codex(process, None, 'implement', 'continue', tmp_path, '', None, None, None)
                      if kind == 'codex' else driver.cli(process, None, None, None))
        assert not result.ok
        assert result.timed_out == isinstance(error, TimeoutError)
        assert result.interrupted == isinstance(error, InterruptedError)


def test_provider_keeps_image_toolchain_path_and_requires_cleanup(tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    home = tmp_path / 'home'; home.mkdir()
    sandbox = AgentSandbox(tmp_path / 'sandbox', 'fixed-image')
    with patch.object(sandbox, 'home', return_value=home), \
         patch('auto_agents.repair_v2.docker.run', return_value=(0, '')):
        with sandbox.command('implement', root, ['/usr/bin/codex']) as command:
            environment = [command[i+1] for i, arg in enumerate(command) if arg == '-e']
            assert not any(value.startswith('PATH=') for value in environment)
    def run(command, **kwargs):
        return (1, 'daemon unavailable') if command[1] == 'rm' else (0, '')
    with patch.object(sandbox, 'home', return_value=home), \
         patch('auto_agents.repair_v2.docker.run', side_effect=run):
        with pytest.raises(RepairBlocked, match='could not be stopped'):
            with sandbox.command('implement', root, ['/usr/bin/codex']): pass
