import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from auto_agents.models import GateConfig, VerificationStep
from auto_agents.verification_selection import select_verification_steps
from auto_agents.verification_v5 import changed_symbols, coalesce_steps
from auto_agents.gates import resolve_gate_plan_from_verification_steps


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    (root / 'tests').mkdir(parents=True)
    (root / 'service.py').write_text('def used():\n    return 1\n\ndef unrelated():\n    return 3\n')
    (root / 'tests/test_service.py').write_text('import service\n\ndef test_used():\n    assert service.used() == 1\n\ndef test_other():\n    assert service.unrelated() == 3\n')
    for args in [('init',), ('config','user.name','Test'), ('config','user.email','test@example.com'), ('add','.'), ('commit','-m','base')]:
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    (root / 'service.py').write_text('def used():\n    return 2\n\ndef unrelated():\n    return 3\n')
    return root


def proofs():
    return [VerificationStep(proof_id='used.proof', runner='pytest', levels=['affected'],
                targets=['tests/test_service.py::test_used'], risk='critical',
                impact_symbols=['service.py::used']),
            VerificationStep(proof_id='other.proof', runner='pytest', levels=['affected'],
                targets=['tests/test_service.py::test_other'], impact_symbols=['service.py::unrelated']),
            VerificationStep(proof_id='release.proof', runner='pytest', levels=['release'],
                targets=['tests/test_release.py'])]


def test_body_change_keeps_critical_proof_without_whole_release(project):
    assert changed_symbols(project, 'service.py') == {'used'}
    selected = select_verification_steps(proofs(), project, GateConfig(verification_policy_version=5),
        level='affected', changed_paths=['service.py'], preserve_release_targets=True)
    assert selected.level == 'affected'
    assert selected.proof_ids == ['used.proof']
    assert selected.selection_reasons == {'used.proof': ['impact:service.py']}


@pytest.mark.parametrize('source', ['VALUE=2\ndef used(): return 2\ndef unrelated(): return 3\n',
                                  'def used(value=1): return 2\ndef unrelated(): return 3\n',
                                  '@staticmethod\ndef used(): return 2\ndef unrelated(): return 3\n'])
def test_initialization_and_signature_changes_keep_broad_coverage(project, source):
    (project / 'service.py').write_text(source)
    assert changed_symbols(project, 'service.py') is None
    selection = select_verification_steps(proofs(), project, GateConfig(verification_policy_version=5),
        level='affected', changed_paths=['service.py'])
    assert {'used.proof', 'other.proof'}.issubset(selection.proof_ids)


def test_release_covers_all_proofs_without_ordering_edges(project):
    selection = select_verification_steps(proofs(), project, GateConfig(verification_policy_version=5),
        level='release', preserve_release_targets=True)
    assert selection.proof_ids == [item.proof_id for item in proofs()]


def test_new_test_is_executed_without_fallback(project):
    (project / 'tests/test_new.py').write_text('def test_new(): assert True\n')
    selection = select_verification_steps(proofs(), project,
        GateConfig(verification_policy_version=5, fallback_proof_ids=['other.proof']),
        level='affected', changed_paths=['tests/test_new.py'])
    assert len(selection.steps) == 1
    assert selection.steps[0].targets == ['tests/test_new.py']
    assert not selection.unmapped_paths


def test_changed_test_file_runs_new_nodes_even_if_old_nodes_are_selected(project):
    selection = select_verification_steps(proofs(), project, GateConfig(verification_policy_version=5),
        level='affected', changed_paths=['tests/test_service.py'])
    assert any(item.targets == ['tests/test_service.py'] for item in selection.steps)


def test_explicit_release_and_unknown_changes_are_not_trimmed(project):
    steps = proofs(); steps[0].release_trigger = True
    selected = select_verification_steps(steps, project, GateConfig(verification_policy_version=5),
        level='affected', changed_paths=['service.py'])
    assert selected.level == 'release'
    unknown = select_verification_steps(proofs(), project, GateConfig(verification_policy_version=5),
        level='affected', changed_paths=['unknown.cfg'])
    assert unknown.level == 'release'
    assert set(unknown.proof_ids) == {item.proof_id for item in proofs()}


def test_v4_and_default_serialization_remain_compatible(project):
    selected = select_verification_steps(proofs(), project, GateConfig(verification_policy_version=4),
        level='affected', changed_paths=['service.py'], preserve_release_targets=True)
    assert selected.level == 'release'
    assert not {'impact_symbols', 'release_trigger', 'coalesce_safe', 'node_replay_safe'} & VerificationStep().to_dict().keys()


def test_coalescing_preserves_proof_ids_and_respects_dependencies(project):
    steps = [replace(item, risk='medium', coalesce_safe=True) for item in proofs()[:2]]
    plan = resolve_gate_plan_from_verification_steps(steps, project, coalesce=True)
    assert plan.unique_command_count == 1
    assert set(plan.metadata[plan.commands[0]].proof_ids) == {'used.proof', 'other.proof'}
    assert all(target in plan.commands[0] for item in steps for target in item.targets)
    steps[1].depends_on_proofs = ['used.proof']
    assert resolve_gate_plan_from_verification_steps(steps, project, coalesce=True).unique_command_count == 2
    independent = [replace(item, depends_on_proofs=[]) for item in steps]
    limited = resolve_gate_plan_from_verification_steps(independent, project, coalesce=True,
        estimates={'used.proof':200,'other.proof':200}, target_seconds=300)
    assert limited.unique_command_count == 2


def test_coalescing_requires_opt_in_and_compatible_environment(project):
    steps = proofs()[:2]
    assert resolve_gate_plan_from_verification_steps(steps, project, coalesce=True).unique_command_count == 2
    for item in steps: item.coalesce_safe = True
    steps[1].args = ['-m', 'slow']
    assert resolve_gate_plan_from_verification_steps(steps, project, coalesce=True).unique_command_count == 2
