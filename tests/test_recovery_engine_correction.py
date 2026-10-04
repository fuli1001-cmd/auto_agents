"""Stopped engine repairs accept explicit corrections only through verification."""
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from auto_agents.recovery import Command, Event, KernelError, Outcome, OutcomeKind
from auto_agents.recovery.engine import previous_result
from auto_agents.recovery.engine_correction import retain
from auto_agents.recovery.model import digest
from auto_agents.repair_v2.runtime_artifact import build
from auto_agents.repair_v2.workspace import git, source_identity
from test_recovery_kernel import scene, emit, finish


@pytest.fixture
def correction(scene, tmp_path):
    store, original = scene
    project = tmp_path / 'project'; project.mkdir()
    task_id = 'engine:retained:repair'
    working = store.root / 'kernel-engine/engine:retained/workspace/candidate'
    working.mkdir(parents=True)
    git(working, 'init', '-q')
    (working / 'value.py').write_text('VALUE = 0\n')
    (working / 'tests').mkdir()
    (working / 'tests/test_value.py').write_text('def test_value():\n    assert True\n')
    git(working, 'add', '.'); git(working, 'commit', '-qm', 'Retained baseline')
    base = git(working, 'rev-parse', 'HEAD')
    contract = replace(original, task_id=task_id, kind='engine_repair',
        issue_ref=store.put({'base': base}), completion='preflight_recovered',
        phases=('implement', 'verify', 'review'))
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    artifact = build(store.root, working, source_identity(working), {})
    emit(store, 'candidate_retained', {'task_id': task_id, 'candidate_id': digest(artifact),
        'source': artifact['source'], 'base': base, 'receipt': store.put({'artifact': artifact})})
    command = Command('failed-implementation', 'workflow', task_id, 'implement', artifact['source'],
        contract.identity, 'b' * 64, 'c' * 64, 'failed-implementation', True)
    emit(store, 'command_reserved', command.to_dict())
    finish(store, command, Outcome(OutcomeKind.CANDIDATE_REJECTED, 'Verification failed'))
    from auto_agents.recovery.policy import enable
    enable(store, 'workflow')
    source = tmp_path / 'corrected'
    subprocess.run(['git', 'clone', '-q', str(working), str(source)], check=True)
    (source / 'value.py').write_text('VALUE = 1\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'Explicit correction')
    return store, task_id, source, working


def test_explicit_correction_keeps_budget_and_requires_verify_then_review(correction):
    store, task_id, source, working = correction
    before = store.load('workflow')
    result = retain(store, 'workflow', task_id, source)
    state = store.load('workflow')
    task = state['tasks'][task_id]
    assert result['phase'] == task['phase'] == 'verify'
    assert result['progress_credit'] is False
    assert task['status'] == 'ready' and task['proofs'] == {}
    assert task['attempts'] == before['tasks'][task_id]['attempts']
    assert state['budget'] == before['budget']
    assert state['commands'] == before['commands']
    from auto_agents.recovery.convergence import scope, decision
    before_scope, after_scope = scope(before, task_id), scope(state, task_id)
    for field in ('attempts', 'stalled', 'diagnoses', 'credited', 'protected', 'hypotheses'):
        assert after_scope[field] == before_scope[field]
    assert after_scope['scope_changes']['value.py']['source'] == result['source']
    assert decision(state, task_id, 'review', result['source'])['allowed'] is False
    assert task['candidate']['parent'] == before['tasks'][task_id]['candidate']['candidate_id']
    assert previous_result(store, 'workflow', task_id, 'implement')['artifact']['source'] == source_identity(working)
    assert state == store.replay('workflow')
    assert retain(store, 'workflow', task_id, source) == result
    assert store.load('workflow') == state


@pytest.mark.parametrize('violation,code', [('weaken', 'tests_weakened'),
    ('dirty', 'runtime_dirty'), ('unretained', 'candidate_changed'),
    ('unrelated', 'candidate_lineage'), ('unknown', 'outcome_unknown')])
def test_correction_rejects_unowned_or_weakened_inputs(correction, violation, code):
    store, task_id, source, working = correction
    if violation == 'weaken':
        (source / 'tests/test_value.py').write_text('def test_value():\n    pass\n')
        git(source, 'add', '.'); git(source, 'commit', '-qm', 'Weaken assertion')
    elif violation == 'dirty':
        (source / 'value.py').write_text('VALUE = 2\n')
    elif violation == 'unretained':
        (working / 'value.py').write_text('UNRETAINED = 1\n')
    elif violation == 'unrelated':
        git(source, 'checkout', '--orphan', 'unrelated')
        git(source, 'commit', '-qm', 'Unrelated tree')
    else:
        # A separate task's unresolved operation also prevents replacement.
        task = store.load('workflow')['tasks']['task']
        emit(store, 'command_reserved', Command('unknown', 'workflow', 'task', 'implement',
            'a' * 64, task['contract_id'], 'b' * 64, 'c' * 64, 'unknown', False).to_dict())
    before = store.load('workflow')
    with pytest.raises(KernelError) as error:
        retain(store, 'workflow', task_id, source)
    assert error.value.code == code
    assert store.load('workflow') == before
