from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.error import HTTPError

import pytest

from auto_agents import provider_reference_review as review
from auto_agents.requirements import stamp_provider_reference_consumer_hashes, provider_reference_effective_status

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
REF = '.auto-agents/docs/provider_references/provider.md'


def scene(tmp_path):
    p = tmp_path / REF
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('# Prior evidence\nExisting exact endpoint and approved conservative fallback.\n')
    trace = {'contract_identity_schema_version': 1, 'requirements': [{
        'id': 'REQ-001', 'text': 'Use the API', 'status': 'active',
        'provider_reference': REF, 'external_docs_required': True,
    }]}
    lock, _ = stamp_provider_reference_consumer_hashes({'references': {'provider': {
        'path': REF, 'status': 'assumption_approved', 'contract_version': 2,
        'source_urls': ['https://example.com/docs'], 'retrieved_at': (NOW - timedelta(days=5)).isoformat(),
        'notes': 'Approved existing fallback only.',
    }}}, trace)
    return trace, lock


def decision(*, context, **changes):
    return {**{'review_id': context[REF]['review_id'], 'applicability': 'covered', 'reason': 'The existing request and refusal sections cover the new local retry budget.',
        'evidence_refs': [REF + '#request'], 'freshness': 'not_checked',
        'facts': [{'requirement_id': rid, 'fact': 'Request/refusal fields for the locked endpoint',
            'result': 'covered' if changes.get('applicability', 'covered') == 'covered' else 'missing',
            'evidence_ref': REF + '#request'} for rid in context[REF]['requirement_ids']],
        'target': {'provider': 'provider', 'model': 'locked', 'endpoint': '/v1/generate', 'version': 'v1'}}, **changes}


def test_consumer_changes_request_local_assessment_without_network_or_invalidation(tmp_path, monkeypatch):
    trace, lock = scene(tmp_path)
    original = deepcopy(lock)
    trace['requirements'].append({**trace['requirements'][0], 'id': 'REQ-002', 'text': 'Local retry budget is four'})
    monkeypatch.setattr(review, 'check_source', lambda *a: pytest.fail('new consumer alone must not refetch'))
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    assert context[REF]['reasons'] == ['applicability']
    assert lock == original
    assert provider_reference_effective_status(lock, trace, REF) == 'needs_assessment'
    lock['references']['provider']['review'] = decision(context=context)
    assert review.validate(lock, trace, context) == []
    review.finish(tmp_path, lock, context, now=NOW)
    assert provider_reference_effective_status(lock, trace, REF) == 'assumption_approved'
    assert lock['references']['provider']['retrieved_at'] == original['references']['provider']['retrieved_at']


def test_due_age_schedules_review_without_revoking_validity(tmp_path):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    assert not review.review_due(entry, now=NOW)
    entry['review_interval_days'] = 3
    assert review.review_due(entry, now=NOW)
    assert provider_reference_effective_status(lock, trace, REF) == 'assumption_approved'
    entry['freshness'] = {'outcome': 'unavailable', 'last_attempt_at': NOW.isoformat()}
    assert not review.review_due(entry, now=NOW + timedelta(hours=12))
    assert review.review_due(entry, now=NOW + timedelta(days=1))


def test_failed_fetch_keeps_approval_and_last_successful_check(tmp_path, monkeypatch):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    entry['review_interval_days'] = 1
    prior_date = entry['retrieved_at']
    monkeypatch.setattr(review, 'check_source', lambda url, previous: {'url': url, 'outcome': 'unavailable'})
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    entry['review'] = decision(context=context, freshness='unavailable')
    assert not review.validate(lock, trace, context)
    review.finish(tmp_path, lock, context, now=NOW)
    assert entry['status'] == 'assumption_approved'
    assert entry['retrieved_at'] == prior_date
    assert 'last_checked_at' not in entry['freshness']
    assert entry['freshness']['last_attempt_at'] == NOW.isoformat()
    saved = review.retained(tmp_path, REF, entry)
    assert 'approved conservative fallback' in saved['text']


@pytest.mark.parametrize('status', ['blocked', 'needs_user_input', 'verified'])
def test_covered_reference_cannot_drop_or_expand_previous_approval(tmp_path, status):
    trace, lock = scene(tmp_path)
    trace['requirements'][0]['text'] = 'New local policy'
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    lock['references']['provider'].update(status=status, review=decision(context=context, freshness='unavailable'))
    assert any('prior validity' in error for error in review.validate(lock, trace, context))


def test_real_uncovered_requirement_remains_blocking(tmp_path):
    trace, lock = scene(tmp_path)
    trace['requirements'][0]['text'] = 'Requires a new callback API'
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    entry = lock['references']['provider']
    entry.update(status='blocked', review=decision(context=context, applicability='gap', affected_requirement_ids=['REQ-001'],
        reason='The locked /v1 endpoint documents polling only; the required callback has no supporting evidence.'))
    assert not review.validate(lock, trace, context)
    review.finish(tmp_path, lock, context, now=NOW)
    assert provider_reference_effective_status(lock, trace, REF) == 'blocked'
    assert review.retained(tmp_path, REF, entry)['entry']['status'] == 'assumption_approved'


def test_changed_page_does_not_automatically_revoke_contract(tmp_path, monkeypatch):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    entry['review_interval_days'] = 1
    monkeypatch.setattr(review, 'check_source', lambda u, p: {'url':u,'outcome':'content_available','content_sha256':'a'*64})
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    entry['review'] = decision(context=context, freshness='changed_unrelated', reason='Only another model was added; deployed v1 contract is unchanged.')
    assert not review.validate(lock, trace, context)
    review.finish(tmp_path, lock, context, now=NOW)
    assert entry['freshness']['last_checked_at'] == NOW.isoformat()
    assert entry['status'] == 'assumption_approved'


def test_conditional_get_reuses_validated_content_on_304(monkeypatch):
    prior = {'etag':'"v1"','last_modified':'yesterday','content_sha256':'a'*64}
    def fetch(request, timeout):
        assert request.get_header('If-none-match') == '"v1"'
        assert request.get_header('If-modified-since') == 'yesterday'
        raise HTTPError(request.full_url, 304, '', {}, None)
    monkeypatch.setattr(review, 'urlopen', fetch)
    result = review.check_source('https://example.com/docs', prior)
    assert result['outcome'] == 'unchanged' and result['content_sha256'] == 'a'*64


def test_js_shell_does_not_establish_new_protocol_evidence(monkeypatch):
    response = BytesIO(b'<html><script>loadDocs()</script><div>Loading</div></html>')
    response.headers, response.status, response.url = {}, 200, 'https://example.com/docs'
    monkeypatch.setattr(review, 'urlopen', lambda *a, **k: response)
    assert review.check_source(response.url)['outcome'] == 'unavailable'


def test_historical_snapshot_survives_rejected_refresh_and_detects_tampering(tmp_path):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    entry['evidence_snapshot'] = review.retain(tmp_path, REF, entry)
    entry['status'] = 'blocked'
    (tmp_path / REF).write_text('New retrieval failed; old evidence should remain.')
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW, fetch=False)
    assert context[REF]['prior']['status'] == 'assumption_approved'
    (tmp_path / entry['evidence_snapshot']).write_text('{}')
    with pytest.raises(ValueError, match='identity mismatch'):
        review.prepare(tmp_path, trace, lock, [REF], fetch=False)


def test_old_assessment_cannot_authorize_a_new_consumer(tmp_path):
    trace, lock = scene(tmp_path)
    trace['requirements'][0]['text'] = 'Local policy changed'
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    lock['references']['provider']['review'] = decision(context=context)
    trace['requirements'][0]['text'] = 'A new callback capability is now required'
    newer = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    assert review.validate(lock, trace, newer)


def test_unavailable_cannot_fabricate_a_successful_refresh_date(tmp_path, monkeypatch):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    entry['review_interval_days'] = 1
    old_date = entry['retrieved_at']
    monkeypatch.setattr(review, 'check_source', lambda u,p: {'url':u,'outcome':'unavailable'})
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    entry['review'] = decision(context=context, freshness='unavailable')
    entry['retrieved_at'] = NOW.isoformat()  # Untrusted model metadata is not a source observation.
    review.finish(tmp_path, lock, context, now=NOW)
    assert entry['retrieved_at'] == old_date


def test_new_protocol_conflict_requires_affected_requirement_evidence(tmp_path, monkeypatch):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    entry['review_interval_days'] = 1
    monkeypatch.setattr(review, 'check_source', lambda u,p: {'url':u,'outcome':'content_available','content_sha256':'b'*64})
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    entry.update(status='blocked', review=decision(context=context, applicability='conflict',
        freshness='changed_relevant', reason='Official endpoint removal contradicts REQ-001.', affected_requirement_ids=['REQ-001']))
    assert not review.validate(lock, trace, context)
    review.finish(tmp_path, lock, context, now=NOW)
    assert provider_reference_effective_status(lock, trace, REF) == 'blocked'


def test_stage_applies_local_assessment_without_refetch_or_new_approval(tmp_path, monkeypatch):
    import json
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.config import load_run_state, provider_references_lock_path, requirements_trace_path
    from auto_agents.models import AgentResult
    from auto_agents.io_utils import write_json, write_text
    from test_requirements_trace import _requirement
    from test_provider_contract_policy import _sourced_reference_markdown

    root = tmp_path / 'project'
    Orchestrator.init_project(root, 'review', 'mock')
    trace = {'version':1, 'contract_identity_schema_version':1, 'requirements':[
        _requirement(external_docs_required=True, provider_reference=REF)]}
    lock, _ = stamp_provider_reference_consumer_hashes({'references': {'provider': {
        'path':REF, 'status':'assumption_approved', 'contract_version':2,
        'retrieved_at':NOW.isoformat(), 'source_urls':['https://example.com/docs'],
    }}}, trace)
    trace['requirements'][0]['text'] = 'Local retry budget changed; the same protocol is used.'
    from auto_agents.requirements import stamp_requirement_contract_hashes
    trace, _ = stamp_requirement_contract_hashes(trace)
    write_json(requirements_trace_path(root), trace)
    write_json(provider_references_lock_path(root), lock)
    write_text(root / REF, _sourced_reference_markdown())
    orch = Orchestrator(root)
    monkeypatch.setattr(review, 'now_utc', lambda: NOW)
    monkeypatch.setattr(review, 'check_source', lambda *a: pytest.fail('local assessment must not fetch'))
    calls = []
    def research(**kwargs):
        calls.append(kwargs)
        assert 'retained local evidence' in kwargs['prompt']
        context = orch._provider_reference_review_context
        generated = json.loads(provider_references_lock_path(root).read_text())
        generated['references']['provider']['review'] = decision(context=context)
        write_json(provider_references_lock_path(root), generated)
        result = AgentResult(True, [], root/'result.md', summary='Existing evidence covers the new local budget.')
        assert kwargs['validation_feedback'](result) is None
        return result
    monkeypatch.setattr(orch, '_run_agent_with_retries', research)
    state = orch._run_provider_research(load_run_state(root), root/'spec.md')
    final = json.loads(provider_references_lock_path(root).read_text())
    assert len(calls) == 1 and state.current_stage == 'provider_research'
    assert provider_reference_effective_status(final, trace, REF) == 'assumption_approved'
    assert final['references']['provider']['retrieved_at'] == NOW.isoformat()
    orch._run_provider_research(state, root/'spec.md')
    assert len(calls) == 1  # Durable admission is reused, not another research loop.
    final['references']['provider']['review_interval_days'] = 1
    write_json(provider_references_lock_path(root), final)
    later = NOW + timedelta(days=2)
    monkeypatch.setattr(review, 'now_utc', lambda: later)
    monkeypatch.setattr(review, 'check_source', lambda u,p: {
        'url':u, 'outcome':'unchanged', 'http_status':304, 'content_sha256':'a'*64})
    orch._run_provider_research(state, root/'spec.md')
    assert len(calls) == 1  # An unchanged conditional GET also needs no model call.
    updated = json.loads(provider_references_lock_path(root).read_text())
    assert updated['references']['provider']['freshness']['last_checked_at'] == later.isoformat()
    assert updated['references']['provider']['status'] == 'assumption_approved'


def test_resumed_implementation_schedules_due_review_without_resetting_work(tmp_path, monkeypatch):
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.config import load_run_state, requirements_trace_path, provider_references_lock_path
    from auto_agents.io_utils import write_json
    root = tmp_path/'project'
    Orchestrator.init_project(root, 'review', 'mock')
    trace, lock = scene(root)
    lock['references']['provider']['review_interval_days'] = 1
    write_json(requirements_trace_path(root), trace)
    write_json(provider_references_lock_path(root), lock)
    orch = Orchestrator(root)
    state = load_run_state(root)
    state.status = 'pending'
    state.current_stage = 'implement'
    state.stage_summaries = {'plan':'accepted', 'provider_research':'previously verified', 'implement':'retained product proof'}
    state.agent_attempts = {'implement-T1':2}
    original_tasks = deepcopy(state.tasks)
    monkeypatch.setattr(review, 'now_utc', lambda: NOW)
    assert orch._schedule_due_provider_reference_review(state)
    assert state.stage_summaries == {'plan':'accepted','implement':'retained product proof'}
    assert state.agent_attempts == {'implement-T1':2}
    assert state.tasks == original_tasks and state.status == 'pending'
    assert not orch._schedule_due_provider_reference_review(state)


def test_all_unchanged_sources_reuse_applicable_contract_without_model(tmp_path, monkeypatch):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    entry['review_interval_days'] = 1
    monkeypatch.setattr(review, 'check_source', lambda u,p: {'url':u,'outcome':'unchanged',
        'http_status':304,'etag':'v1','content_sha256':'a'*64})
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    assert context[REF]['reasons'] == ['freshness']
    assert review.reuse_unchanged(tmp_path, lock, context, now=NOW) == {}
    assert entry['status'] == 'assumption_approved'
    assert entry['freshness']['last_checked_at'] == NOW.isoformat()
    trace['requirements'][0]['text'] = 'New capability'
    new_context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    assert review.reuse_unchanged(tmp_path, lock, new_context, now=NOW)  # Still assess the new dependency.
