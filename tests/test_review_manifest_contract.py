"""Frozen delivery trees and requirement identifiers drive independent review."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from auto_agents.models import AgentResult
from auto_agents.recovery import KernelError
from auto_agents.recovery.protocol import ReviewManifest
from auto_agents.repair_v2.controller import REVIEW_SCHEMA
from auto_agents.repair_v2.scope import changes
from test_recovery_convergence_policy import scene, verify
from test_recovery_kernel import emit


def finding(requirement='undeclared prose requirement'):
    return {'requirement': requirement, 'reason': 'A required delivery file is missing',
            'counterexample': 'A clean checkout cannot collect the regression',
            'check': 'git show candidate:tests/test_new.py',
            'summary_zh': '交付文件缺失', 'summary_en': 'Delivery file missing'}


def test_wire_schema_and_format_diagnostic_use_the_same_requirement_ids():
    manifest = ReviewManifest('source', 'base', 'contract', ('REQ-owned',), {})
    schema = manifest.schema(REVIEW_SCHEMA)
    assert schema['properties']['findings']['items']['properties']['requirement']['enum'] == [
        'REQ-owned', 'repair-scope', 'repair-regression']
    assert schema['properties']['coverage']['items']['properties']['requirement']['enum'] == ['REQ-owned']
    value = {'decision': 'REJECT', 'findings': [finding()], 'coverage': [], 'change_coverage': []}
    with pytest.raises(KernelError) as blocked: manifest.validate(json.dumps(value))
    assert blocked.value.details['field'] == 'findings'
    assert blocked.value.details['invalid_rows'][0]['unknown_requirement'] is True
    correction = manifest.correction('original-response', json.dumps(value), blocked.value)
    assert 'findings[].requirement' in correction and 'REQ-owned' in correction
    corrected = deepcopy(value); corrected['findings'][0]['requirement'] = 'repair-scope'
    assert manifest.validate(json.dumps(corrected))['decision'] == 'REJECT'
    assert manifest.same_judgment(value, corrected)
    corrected['findings'][0]['counterexample'] = 'Changed the substantive claim'
    assert not manifest.same_judgment(value, corrected)


def test_format_correction_cannot_replace_a_valid_requirement_or_approve_rejection():
    manifest = ReviewManifest('source', 'base', 'contract', ('REQ-owned',), {})
    previous = {'decision': 'REJECT', 'findings': [finding('REQ-owned')]}
    current = deepcopy(previous); current['findings'][0]['requirement'] = 'repair-scope'
    assert not manifest.same_judgment(previous, current)
    current = {**previous, 'decision': 'APPROVE'}
    assert not manifest.same_judgment(previous, current)


def test_declared_frontend_nodes_are_valid_coverage_but_unrelated_paths_are_not():
    from auto_agents.repair_v2.controller import review_result
    from auto_agents.repair_v2.types import RepairBlocked
    target = 'workbench/src/components/home.test.tsx'
    value = {'decision': 'APPROVE', 'findings': [], 'change_coverage': [],
             'coverage': [{'requirement': target, 'nodes': [target + '::renders']}]}
    assert review_result(json.dumps(value), 'source', {target}, {}).ok
    for node in ['workbench/src/other.test.tsx', '/tmp/private.tsx', 'tests/../../private.py']:
        value['coverage'][0]['nodes'] = [node]
        with pytest.raises(RepairBlocked): review_result(json.dumps(value), 'source', {target}, {})


def test_native_review_includes_untracked_file_from_frozen_delivery_tree(scene, tmp_path, monkeypatch):
    from auto_agents.gate_execution import GateSnapshotManager
    from auto_agents.recovery import native
    from test_session_verification_ownership import project, git
    root, _ = project(tmp_path)
    base = git(root, 'rev-parse', 'HEAD').strip()
    path = 'tests/test_new_delivery.py'
    (root/path).write_text('def test_delivery():\n    assert True\n')
    checkpoint = GateSnapshotManager(root, 'review-delivery').create(paths=[path])
    assert not git(root, 'ls-files', '--', path).strip()
    assert not any(path in text for text in changes(root, base).values())
    frozen = changes(root, base, revision=checkpoint.commit_sha)
    assert any(path in text for text in frozen.values())
    store, original = scene
    contract = replace(original, task_id='fix:child')
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    verify(store, contract, 'verified', {'tests/test_owned.py::test_owned': 'passed'}, ok=True)
    state = SimpleNamespace(candidate_custody={'receipt': {'fingerprint': 'receipt', 'base_revision': base,
                                             'source_revision': checkpoint.commit_sha}})
    captured = []
    def provider(request):
        captured.append(request)
        value = {'decision': 'APPROVE', 'findings': [],
            'coverage': [{'requirement': check, 'nodes': ['tests/test_owned.py::test_owned']} for check in contract.required_checks],
            'change_coverage': [{'change': key, 'requirement': 'repair-regression', 'reason': 'Owned regression test',
                                'evidence': path + '::test_delivery'} for key in frozen]}
        return AgentResult(True, [], request.output_path, summary=json.dumps(value))
    owner = SimpleNamespace(project_root=root, orch=SimpleNamespace(_call_with_failover_owned=provider))
    monkeypatch.setattr(native, 'context', lambda *args: (store, 'workflow', root, 'fix', 'child', state))
    monkeypatch.setattr(native, '_source', lambda *args: 'a'*64)
    monkeypatch.setattr(native, 'perform', lambda owner, phase, key, execute, classify, **kw: execute())
    result = native.review_candidate(owner, state, {'ok': True})
    assert result['ok']
    assert checkpoint.commit_sha in str(captured[0].prompt) and path in str(captured[0].prompt)
    assert set(captured[0].response_schema['properties']['change_coverage']['items']['properties']['change']['enum']) == set(frozen)
