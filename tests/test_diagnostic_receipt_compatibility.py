"""Receipt compatibility at the real snapshot and submission boundaries."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from auto_agents.repair_control import digest as control_digest
from auto_agents.repair_v2.diagnostic_replay import (
    copy_submission_evidence, observe_submission, retained_diagnosis,
)
from auto_agents.repair_v2.scope import ScopeGuard
from auto_agents.root_cause import RootCauseCoordinator
from test_diagnostic_recovery_boundary import retained_submission_scene


def test_cited_prior_diagnosis_does_not_replace_the_sealed_submission(tmp_path):
    from auto_agents.repair_v2.store import atomic_json
    from auto_agents.root_cause import RootCauseDiagnosis

    with retained_submission_scene() as (root, orch, original, _, request, runtime, _, diagnosis):
        prior = deepcopy(diagnosis.to_dict())
        prior['diagnosis_id'] = 'prior-failure'
        prior['evidence_path'] = str(root / '.auto-agents/runs/prior/missing.json')
        certificate = '.auto-agents/state/root_cause_certificates/prior.json'
        atomic_json(root / certificate, {'certificate_key': 'prior', 'diagnosis': prior})
        current = deepcopy(diagnosis.to_dict())
        current['final']['necessity']['evidence_refs'].append({'origin': 'target', 'path': certificate})
        diagnosis = RootCauseDiagnosis.from_dict(current)
        request['diagnosis'] = diagnosis.to_dict()
        request['boundary'] = {'kind': 'run_stage', 'stage': 'implement'}
        guard = ScopeGuard(tmp_path / 'scope', request, root, runtime['runtime_root'])
        request['scope_receipt'] = guard.store.read(guard.admit(diagnosis.scope_necessity(root)))
        Path(diagnosis.evidence_path).unlink()

        snapshot = tmp_path / 'snapshot'
        RootCauseCoordinator._copy_diagnostic_tree(root, snapshot)
        copy_submission_evidence(root, snapshot, request)
        parsed, _ = retained_diagnosis(snapshot, request)
        assert parsed.to_dict() == current
        result = observe_submission(orch, original, parsed, request, runtime, tmp_path / 'proof')
        assert result['accepted']
        assert result['diagnosis_digest'] == control_digest(current)


@pytest.mark.parametrize('explicit_empty_workflow', [False, True])
def test_run_only_receipt_submits_after_temporary_diagnosis_is_deleted(tmp_path, explicit_empty_workflow):
    with retained_submission_scene() as (root, orch, original, _, request, runtime, _, diagnosis):
        request['invocation'] = {'run_id': original.run_id}
        if explicit_empty_workflow:
            request['invocation']['workflow_id'] = ''
        request['boundary'] = {'kind': 'run_stage', 'stage': 'implement'}
        invocation = deepcopy(request['invocation'])
        guard = ScopeGuard(tmp_path / 'original-scope', request, root, runtime['runtime_root'])
        reference = guard.admit(diagnosis.scope_necessity(root))
        receipt = guard.store.read(reference)
        assert receipt['context']['owner']['workflow_id'] == ''
        request['scope_receipt'] = receipt
        unchanged = deepcopy(diagnosis.to_dict())
        Path(diagnosis.evidence_path).unlink()
        assert guard.current() == reference
        parsed, document = retained_diagnosis(root, request)
        assert parsed.to_dict() == unchanged
        assert document.parent == Path('.auto-agents/state/repair-scope-evidence')
        # These are the two copy boundaries used for initial snapshot preparation
        # and fresh subscriber validation. Neither may resurrect the temp file.
        source = root
        for stage in ('snapshot', 'subscriber'):
            target = tmp_path / stage
            RootCauseCoordinator._copy_diagnostic_tree(source, target)
            copy_submission_evidence(source, target, request)
            assert (target / document).read_bytes() == (root / document).read_bytes()
            assert not (target / Path(diagnosis.evidence_path).relative_to(root)).exists()
            worker = ScopeGuard(tmp_path / (stage + '-scope'), request, target, runtime['runtime_root'])
            assert worker.import_receipt(receipt) is not None
            assert worker.current() is not None
            source = target
        result = observe_submission(orch, original, diagnosis, request, runtime, tmp_path / 'proof')
        assert result['accepted'] is True
        assert result['workflow_id'] == original.resume_context['workflow_id']
        assert result['scope_receipt_digest'] == control_digest(receipt)
        # The client looked up the original run-only key, not a new workflow key.
        restarted = ScopeGuard(tmp_path / 'proof/submission-control/scope-inputs' / control_digest(invocation),
                               request, root, runtime['runtime_root'])
        assert restarted.current() is not None
        assert restarted.store.read(restarted.current()) == receipt
        assert request['invocation'] == invocation
        assert diagnosis.to_dict() == unchanged
        assert not Path(diagnosis.evidence_path).exists()


@pytest.mark.parametrize('tamper', ['owner', 'document', 'pointer', 'report'])
def test_snapshot_reuse_rejects_foreign_or_changed_retained_binding(tmp_path, tamper):
    with retained_submission_scene() as (root, _, _, _, request, runtime, _, diagnosis):
        guard = ScopeGuard(tmp_path / 'scope', request, root, runtime['runtime_root'])
        receipt = guard.store.read(guard.admit(diagnosis.scope_necessity(root)))
        request['scope_receipt'] = deepcopy(receipt)
        Path(diagnosis.evidence_path).unlink()
        if tamper == 'owner':
            request['scope_receipt']['context']['owner']['subject'] = 'run:foreign'
        elif tamper == 'document':
            path = root / receipt['proposal']['evidence_refs'][0]['path']
            path.write_text(json.dumps({'repair_case': 'changed'}))
        elif tamper == 'pointer':
            request['scope_receipt']['proposal']['evidence_refs'][0]['pointer'] = '/foreign'
        else:
            request['diagnosis']['final']['necessity']['blocked_step'] = 'unrelated work'
        with pytest.raises((ValueError, RuntimeError)):
            copy_submission_evidence(root, tmp_path / 'copy', request)
