import subprocess
from pathlib import Path

import pytest

from auto_agents.recovery import KernelStore, KernelError
from auto_agents.recovery.upgrade import MANDATORY_CHECKS, activate, independent_verify, rollback
from auto_agents.recovery.release import prepare_runtime
from test_recovery_kernel import scene, reserve


def artifact(tmp_path, store, label):
    source = tmp_path/label; source.mkdir()
    def git(*args):
        subprocess.run(['git','-c','user.name=Test','-c','user.email=test@localhost',*args],
                       cwd=source,check=True,capture_output=True)
    git('init'); (source/'version.py').write_text('VERSION = ' + repr(label) + '\n')
    git('add','.'); git('commit','-m',label)
    return prepare_runtime(store, source)


def receipt(store, runtime):
    store.set_meta('trusted_verifier', 'd'*64)
    return independent_verify(store, runtime,
        {name: lambda candidate: {'ok':True, 'observation':candidate['artifact_id']} for name in MANDATORY_CHECKS}, 'd'*64)


def test_compatible_rollback_preserves_all_business_events_and_project_membership(scene, tmp_path):
    store, contract = scene
    first = artifact(tmp_path, store, 'first'); first_receipt = receipt(store, first)
    activate(store, first, first_receipt)
    store.set_meta('activation', {'projects': [str(tmp_path/'project')]})
    before = store.replay('workflow')
    second = artifact(tmp_path, store, 'second'); activate(store, second, receipt(store, second))
    assert store.meta('active_runtime') == second
    assert rollback(store, first_receipt)['ok']
    assert store.meta('active_runtime') == first
    assert store.meta('activation')['projects'] == [str(tmp_path/'project')]
    assert store.replay('workflow') == before


def test_incomplete_or_candidate_forged_receipt_never_activates(scene, tmp_path):
    store, _ = scene
    runtime = artifact(tmp_path, store, 'candidate')
    verified = receipt(store, runtime)
    forged = {**verified, 'rpc_protocol':3}
    with pytest.raises(KernelError, match='protocol'): activate(store, runtime, forged)
    store.set_meta('verified_upgrades', [])
    with pytest.raises(KernelError, match='sealed'): activate(store, runtime, verified)
    assert store.meta('active_runtime') is None


def test_reserved_operation_blocks_cutover_without_changing_state(scene, tmp_path):
    store, contract = scene
    runtime = artifact(tmp_path, store, 'candidate'); verified = receipt(store, runtime)
    reserve(store, contract, 'implement', 'pending', True)
    before = store.replay('workflow')
    with pytest.raises(KernelError, match='Unsettled'): activate(store, runtime, verified)
    assert store.meta('active_runtime') is None
    assert store.replay('workflow') == before


def test_failed_independent_gate_cannot_issue_receipt(scene, tmp_path):
    store, _ = scene
    runtime = artifact(tmp_path, store, 'candidate')
    store.set_meta('trusted_verifier', 'd'*64)
    checks = {name: lambda _: {'ok':False} for name in MANDATORY_CHECKS}
    with pytest.raises(KernelError, match='failed gate'): independent_verify(store, runtime, checks, 'd'*64)
    assert store.meta('verified_upgrades', []) == []
