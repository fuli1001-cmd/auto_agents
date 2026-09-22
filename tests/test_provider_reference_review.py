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


@pytest.mark.parametrize('prior_status', ['assumption_approved', 'verified'])
def test_relevant_source_change_with_covered_dependencies_is_admitted_and_reused(
    tmp_path, monkeypatch, prior_status,
):
    """Synthetic source/research responses; real validation and durable admission."""
    import json
    from auto_agents.config import (
        load_run_state, provider_references_lock_path, requirements_trace_path,
        save_run_state, task_plan_path,
    )
    from auto_agents.io_utils import write_json, write_text
    from auto_agents.models import AgentResult, TaskSpec
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.requirements import (
        provider_reference_consumer_contract_sha256, stamp_requirement_contract_hashes,
    )
    from test_provider_contract_policy import _sourced_reference_markdown
    from test_requirements_trace import _requirement

    root = tmp_path / 'project'
    Orchestrator.init_project(root, 'review', 'mock')
    trace = {'version': 1, 'contract_identity_schema_version': 1, 'requirements': [
        _requirement(id=rid, external_docs_required=True, provider_reference=REF)
        for rid in ('REQ-001', 'REQ-002')
    ]}
    old_date = (NOW - timedelta(days=40)).isoformat()
    lock, _ = stamp_provider_reference_consumer_hashes({'references': {'provider': {
        'path': REF, 'status': prior_status, 'contract_version': 2,
        'retrieved_at': old_date, 'source_urls': ['https://example.com/docs'],
        'notes': 'Existing request omits optional quality; unknown completion cannot be resubmitted.',
    }}}, trace)
    prior_entry = deepcopy(lock['references']['provider'])
    trace['requirements'][0]['text'] = 'Local recovery compares candidates within the existing request scope.'
    trace, _ = stamp_requirement_contract_hashes(trace)
    write_json(requirements_trace_path(root), trace)
    write_json(provider_references_lock_path(root), lock)
    historical = (
        '\n## Historical source comparison\n\n'
        'Synthetic retained source: timeout guidance was 300 seconds; optional quality '
        'was described as rejected. The approved payload omits quality.\n'
    )
    prior_document = _sourced_reference_markdown() + historical
    write_text(root / REF, prior_document)
    tasks = [dict(task_id=f'T{i:02}', title='Retained task', description='Existing work',
                  acceptance=['Keep the existing contract.'], requirement_ids=['REQ-001', 'REQ-002'])
             for i in range(19)]
    write_json(task_plan_path(root), {'tasks': tasks})
    write_text(root / 'unrelated.txt', 'Unrelated unfinished work.\n')
    protected_paths = [task_plan_path(root), requirements_trace_path(root),
                       root / '.auto-agents/config.json', root / 'unrelated.txt']
    protected = {path: path.read_bytes() for path in protected_paths}
    state = load_run_state(root)
    state.current_stage = 'plan'
    state.tasks = [TaskSpec.from_dict(task) for task in tasks]
    state.agent_attempts = {'plan': 3, 'provider_research': 2}
    state.approved_gates = ['requirements', 'architecture']
    state.resume_context = {'workflow_id': 'wf-retained', 'max_tasks': 19, 'skip_validate': False}
    save_run_state(root, state)
    preserved_state = deepcopy(state)
    orch = Orchestrator(root)
    monkeypatch.setattr(review, 'now_utc', lambda: NOW)
    source_calls, research_calls = [], []

    def source(url, previous):
        source_calls.append(url)
        return {'url': url, 'outcome': 'content_available', 'content_sha256': 'b' * 64}

    monkeypatch.setattr(review, 'check_source', source)
    captured = {}

    def research(**kwargs):
        research_calls.append(kwargs)
        assert kwargs['stage'] == 'provider_research'
        assert 'retained local evidence' in kwargs['prompt']
        context = orch._provider_reference_review_context
        assert set(context[REF]['reasons']) == {'applicability', 'freshness'}
        assert context[REF]['prior'] == prior_entry
        snapshot = root / context[REF]['evidence_snapshot']
        captured['prior_snapshot'] = snapshot
        captured['prior_bytes'] = snapshot.read_bytes()
        generated = json.loads(provider_references_lock_path(root).read_text())
        assessment = decision(
            context=context, freshness='changed_relevant', affected_requirement_ids=[],
            reason='The deployed timeout recommendation changed; the request and recovery dependencies remain covered.',
            evidence_refs=[REF + '#historical-source-comparison', REF + '#current-source-comparison'],
        )
        for fact in assessment['facts']:
            fact.update(fact='The unchanged request omits optional quality; reuse completed receipts and stop on unknown completion.',
                        evidence_ref=REF + '#current-source-comparison')
        generated['references']['provider']['review'] = assessment
        captured['assessment'] = deepcopy(assessment)
        current_document = prior_document + (
            '\n## Current source comparison\n\n'
            'Synthetic official-source update: https://example.com/docs now recommends '
            '360 seconds and describes optional quality as accepted but not guaranteed. '
            'This is a relevant change, not an unresolved required capability: the approved '
            'request still omits quality. Retain conservative timeout guidance, existing '
            'refusal handling and zero duplicate submission after unknown completion.\n'
        )
        write_text(root / REF, current_document)
        captured['document'] = current_document
        write_json(provider_references_lock_path(root), generated)
        result = AgentResult(True, [], root / 'result.md', summary='Relevant update assessed; required dependencies covered.')
        # This acceptance assertion fails on the base engine's freshness rejection.
        assert kwargs['validation_feedback'](result) is None
        return result

    monkeypatch.setattr(orch, '_run_agent_with_retries', research)
    state = orch._run_provider_research(load_run_state(root), root / 'spec.md')
    assert state.current_stage == 'provider_research' and not state.last_error
    save_run_state(root, state)
    admitted = json.loads(provider_references_lock_path(root).read_text())
    entry = admitted['references']['provider']
    assert entry['status'] == prior_status
    assert entry['review'] == captured['assessment']
    assert entry['freshness']['outcome'] == 'changed_relevant'
    assert entry['freshness']['last_checked_at'] == NOW.isoformat()
    assert entry['applicability_checked_at'] == NOW.isoformat()
    assert entry['consumer_contract_sha256'] == provider_reference_consumer_contract_sha256(trace, REF)
    assert entry['consumer_contract_sha256'] != prior_entry['consumer_contract_sha256']
    assert provider_reference_effective_status(admitted, trace, REF) == prior_status
    saved = review.retained(root, REF, entry)  # Verifies the persisted content-addressed identity.
    assert saved['entry']['review'] == captured['assessment']
    assert saved['entry']['consumer_contract_sha256'] == entry['consumer_contract_sha256']
    assert saved['text'] == captured['document']
    assert historical in saved['text']
    assert captured['prior_snapshot'].read_bytes() == captured['prior_bytes']
    assert root / entry['evidence_snapshot'] != captured['prior_snapshot']
    assert review.reasons(entry, trace, REF, now=NOW) == []

    reloaded = Orchestrator(root)
    monkeypatch.setattr(reloaded, '_run_agent_with_retries', lambda **kw: pytest.fail('admitted review must be reused'))
    reused = reloaded._run_provider_research(load_run_state(root), root / 'spec.md')
    assert reused.current_stage == 'provider_research' and not reused.last_error
    assert reloaded.provider_research_blockers(requirement_ids={'REQ-001', 'REQ-002'}) == []
    assert len(research_calls) == 1 and source_calls == ['https://example.com/docs']
    assert json.loads(provider_references_lock_path(root).read_text()) == admitted
    for field in ('run_id', 'tasks', 'agent_attempts', 'approved_gates', 'resume_context'):
        assert getattr(reused, field) == getattr(preserved_state, field)
    assert len(reused.tasks) == 19
    assert {path: path.read_bytes() for path in protected_paths} == protected


@pytest.mark.parametrize('applicability', ['covered', 'gap', 'conflict'])
@pytest.mark.parametrize('result', ['missing', 'contradicted'])
def test_relevant_change_cannot_contain_an_unresolved_required_fact(
    tmp_path, applicability, result,
):
    trace, lock = scene(tmp_path)
    trace['requirements'][0]['text'] = 'Requires a callback capability'
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    entry = lock['references']['provider']
    prior_hash = entry['consumer_contract_sha256']
    entry['review'] = decision(
        context=context, applicability=applicability, freshness='changed_relevant',
        affected_requirement_ids=['REQ-001'],
        conflict_disposition={'blocking': False, 'status': 'contained_within_retained_scope'},
    )
    entry['review']['facts'][0]['result'] = result
    errors = review.validate(lock, trace, context)
    expected = 'uncovered facts cannot be marked covered' if applicability == 'covered' else 'unresolved protocol gaps/conflicts cannot be marked usable'
    assert any(expected in error for error in errors)
    assert entry['consumer_contract_sha256'] == prior_hash
    if applicability != 'covered':
        entry['status'] = 'blocked'
        assert review.validate(lock, trace, context) == []
        review.finish(tmp_path, lock, context, now=NOW)
        assert provider_reference_effective_status(lock, trace, REF) == 'blocked'
        assert entry['consumer_contract_sha256'] == prior_hash
        assert review.retained(tmp_path, REF, entry)['entry']['status'] == 'assumption_approved'


@pytest.mark.parametrize('case, diagnostic', [
    ('missing_mapping', 'map every current requirement'),
    ('missing_evidence', 'scoped applicability/freshness review'),
    ('missing_fact_evidence', 'map every current requirement'),
    ('missing_target', 'scoped applicability/freshness review'),
    ('stale_review', 'scoped applicability/freshness review'),
    ('stale_consumer', 'requirements changed during review'),
    ('expanded_approval', 'prior validity/approval status'),
    ('unsupported_conflict', 'blocking requires a concrete uncovered'),
    ('unavailable_source', 'unavailable source checks cannot establish'),
])
def test_relevant_covered_change_preserves_review_safeguards(tmp_path, case, diagnostic):
    trace, lock = scene(tmp_path)
    trace['requirements'][0]['text'] = 'New local recovery policy'
    context = review.prepare(tmp_path, trace, lock, [REF], now=NOW)
    entry = lock['references']['provider']
    entry['review'] = assessment = decision(context=context, freshness='changed_relevant')
    if case == 'missing_mapping':
        assessment['facts'] = []
    elif case == 'missing_evidence':
        assessment['evidence_refs'] = []
    elif case == 'missing_fact_evidence':
        assessment['facts'][0]['evidence_ref'] = ''
    elif case == 'missing_target':
        assessment['target'].pop('endpoint')
    elif case == 'stale_review':
        assessment['review_id'] = 'stale'
    elif case == 'stale_consumer':
        trace['requirements'][0]['text'] = 'A different required capability'
    elif case == 'expanded_approval':
        entry['status'] = 'verified'
    elif case == 'unsupported_conflict':
        assessment.update(applicability='conflict', affected_requirement_ids=['REQ-001'],
                          conflict_disposition={'blocking': False})
    elif case == 'unavailable_source':
        context[REF]['sources'] = [{'url': 'https://example.com/docs', 'outcome': 'unavailable'}]
    assert any(diagnostic in error for error in review.validate(lock, trace, context))


def blocked_provider_review_scene(tmp_path):
    """Synthetic retained run with an accepted plan and a rejected source review."""
    from auto_agents.config import (
        load_run_state, provider_references_lock_path, requirements_trace_path,
        save_run_state, task_plan_path,
    )
    from auto_agents.io_utils import write_json, write_text
    from auto_agents.models import TaskSpec
    from auto_agents.orchestrator import Orchestrator
    from test_provider_contract_policy import _sourced_reference_markdown

    root = tmp_path / 'project'
    Orchestrator.init_project(root, 'review recovery', 'mock')
    trace, lock = scene(root)
    write_text(root / REF, _sourced_reference_markdown())
    trace['requirements'][0]['text'] = 'Local recovery still uses the approved request without optional quality.'
    context = review.prepare(root, trace, lock, [REF], fetch=False, now=NOW)
    entry = lock['references']['provider']
    entry['evidence_snapshot'] = context[REF]['evidence_snapshot']
    entry['review'] = decision(context=context, freshness='changed_relevant')
    entry['review']['facts'].append({
        'requirement_id': 'REQ-001', 'fact': 'Historical universal rejection of optional quality.',
        'result': 'contradicted', 'evidence_ref': REF + '#source-comparison',
    })
    entry['review'].update(applicability='conflict', affected_requirement_ids=['REQ-001'],
                           conflict_disposition={'blocking': False})
    write_text(root / REF, _sourced_reference_markdown() + (
        '\n## Source comparison\n\nSynthetic documentation update: timeout guidance '
        'changed from 300 to 360 seconds, and universal quality rejection is disputed. '
        'The approved request omits quality and never duplicates an unknown submission.\n'
    ))
    write_json(requirements_trace_path(root), trace)
    write_json(provider_references_lock_path(root), lock)
    spec = root / 'spec.md'
    write_text(spec, '# Synthetic bounded recovery iteration\nPreserve the accepted task plan.\n')
    tasks = [TaskSpec(task_id=f'T{i:02}', title='Existing task', description='Retained work',
                      acceptance=['Preserve the current contract.'], requirement_ids=['REQ-001'])
             for i in range(19)]
    write_json(task_plan_path(root), {'tasks': [task.to_dict() for task in tasks]})
    write_text(root / 'unrelated.txt', 'Unrelated unfinished work.\n')
    write_json(root / '.auto-agents/history/task_plans/stopped.json', {'status': 'stopped'})
    state = load_run_state(root)
    state.current_stage = 'plan'
    state.stage_summaries = dict(clarify='accepted', prototype='not requested', design='accepted', plan='accepted')
    state.tasks = tasks
    state.agent_attempts = {'plan': 3, 'provider_research': 2}
    state.approved_gates = ['requirements', 'architecture']
    state.resume_context.update(spec_file=str(spec), max_tasks=19, skip_validate=False)
    state.last_recovery_route = dict(outcome='iteration_plan_scope_reconciled', from_stage='plan', to_stage='provider_research')
    state.status = 'blocked'
    state.active_blocker = dict(owner='auto_agents', category='provider_reference_freshness_validity_conflation',
                                status='blocked', fingerprint='retained-review-failure', reason='Relevant coverage rejected')
    state.last_error = state.active_blocker['reason']
    save_run_state(root, state)
    return root, Orchestrator(root), trace, context


def test_pending_review_is_revalidated_after_status_changes_on_restart(tmp_path):
    trace, lock = scene(tmp_path)
    entry = lock['references']['provider']
    entry['evidence_snapshot'] = review.retain(tmp_path, REF, entry)
    entry['status'] = 'blocked'
    trace['requirements'][0]['text'] = 'New local policy within the existing protocol'
    dispatched = review.prepare(tmp_path, trace, lock, [REF], fetch=False, now=NOW)
    entry.update(status='assumption_approved', review=decision(context=dispatched, freshness='changed_relevant'))
    restarted = review.prepare(tmp_path, trace, lock, [REF], fetch=False, now=NOW)
    assert restarted[REF]['review_id'] != dispatched[REF]['review_id']
    recovered = review.pending_context(tmp_path, lock, restarted)
    assert recovered == dispatched
    assert review.validate(lock, trace, recovered) == []
    review.finish(tmp_path, lock, recovered, now=NOW)
    assert provider_reference_effective_status(lock, trace, REF) == 'assumption_approved'


@pytest.mark.parametrize('change', ['unregistered', 'consumer', 'source', 'snapshot', 'required_fact'])
def test_pending_review_cannot_bypass_changed_inputs_or_uncovered_facts(tmp_path, change):
    trace, lock = scene(tmp_path)
    trace['requirements'][0]['text'] = 'Local policy update'
    context = review.prepare(tmp_path, trace, lock, [REF], fetch=False, now=NOW)
    entry = lock['references']['provider']
    entry['evidence_snapshot'] = context[REF]['evidence_snapshot']
    entry['review'] = decision(context=context, freshness='changed_relevant')
    assert review.pending_context(tmp_path, lock, context) == context
    if change == 'unregistered':
        entry['review']['review_id'] = '0' * 64
    elif change == 'consumer':
        trace['requirements'][0]['text'] = 'New provider capability'
    elif change == 'source':
        context[REF]['sources'] = [{'url':'https://example.com/docs', 'outcome':'content_available', 'content_sha256':'f'*64}]
    elif change == 'snapshot':
        entry['evidence_snapshot'] = review.retain(tmp_path, REF, entry)
    else:
        entry['review']['facts'][0]['result'] = 'missing'
    current = (context if change == 'source' else review.prepare(tmp_path, trace, lock, [REF], fetch=False, now=NOW))
    recovered = review.pending_context(tmp_path, lock, current)
    assert recovered is None or review.validate(lock, trace, recovered)


def test_provider_recovery_replay_requires_admission_not_only_flag_clearance():
    from test_iteration_plan_scope import continuation_probe_project
    from auto_agents.repair_v2.boundary_driver import observe_run_continuation
    with continuation_probe_project() as (_, orch, original, plan, request, runtime, frozen):
        original.active_blocker['category'] = 'provider_reference_freshness_validity_conflation'
        with pytest.raises(RuntimeError, match='fresh provider-review admission'):
            observe_run_continuation(orch, original, plan, request, runtime, frozen)


def test_applied_provider_repair_only_routes_to_reassessment(tmp_path, monkeypatch):
    from auto_agents.config import load_run_state, provider_references_lock_path
    root, orch, _, _ = blocked_provider_review_scene(tmp_path)
    state = load_run_state(root)
    before = provider_references_lock_path(root).read_bytes()
    attempts = deepcopy(state.agent_attempts)
    monkeypatch.setattr(orch, '_call_with_failover', lambda *a,**k: pytest.fail('routing cannot execute a provider'))
    state = orch.mark_self_repair_applied('approved-candidate')
    assert orch._resume_blocked_run(state)
    assert state.current_stage == 'plan' and 'provider_research' not in state.stage_summaries
    assert state.agent_attempts == attempts
    assert state.last_recovery_route['outcome'] == 'provider_reference_review_repaired'
    assert provider_references_lock_path(root).read_bytes() == before


def test_approved_defer_rebinds_only_remaining_review_consumers(tmp_path):
    trace, lock = scene(tmp_path)
    trace['requirements'].append({**trace['requirements'][0], 'id': 'REQ-002'})
    context = review.prepare(tmp_path, trace, lock, [REF], fetch=False, now=NOW)
    lock['references']['provider']['review'] = decision(context=context)
    previous = deepcopy(context)
    unchanged_lock = deepcopy(lock)
    trace['requirements'][0]['status'] = 'deferred'
    updated = review.after_approved_defer(tmp_path, trace, context, {'REQ-001'})
    assert context == previous and lock == unchanged_lock
    assert updated[REF]['prior'] == context[REF]['prior']
    assert updated[REF]['evidence_snapshot'] == context[REF]['evidence_snapshot']
    assert updated[REF]['requirement_ids'] == ['REQ-002']
    assert review.validate(lock, trace, updated)  # The old review cannot approve the new scope.
    lock['references']['provider']['review'] = decision(context=updated)
    assert review.validate(lock, trace, updated) == []
    trace['requirements'][1]['status'] = 'deferred'
    assert review.after_approved_defer(tmp_path, trace, updated, {'REQ-002'}) == {}


def test_unapproved_defer_keeps_the_original_review_blocker(tmp_path):
    trace, lock = scene(tmp_path)
    trace['requirements'].append({**trace['requirements'][0], 'id': 'REQ-002'})
    context = review.prepare(tmp_path, trace, lock, [REF], fetch=False, now=NOW)
    trace['requirements'][0]['status'] = 'deferred'
    unchanged = review.after_approved_defer(tmp_path, trace, context, set())
    assert unchanged == context
    assert any('requirements changed' in error for error in review.validate(lock, trace, unchanged))
