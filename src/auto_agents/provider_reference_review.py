"""Separate reference applicability, source freshness and protocol validity.

A source check is an observation, never permission to invalidate an approved
contract. Research owns the semantic comparison; the controller retains the
prior evidence, checks the comparison record and timestamps admitted results.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

RESOLVED = {'verified', 'assumption_approved', 'deferred'}
DEFAULT_REVIEW_DAYS = 30
RETRY_DAYS = 1
HISTORY = '.auto-agents/state/provider-reference-history'


def now_utc():
    return datetime.now(timezone.utc)


def date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def review_due(entry, *, now=None):
    """A due review does not change effective protocol validity."""
    now = now or now_utc()
    freshness = entry.get('freshness') or {}
    last_attempt = date(freshness.get('last_attempt_at'))
    if (freshness.get('outcome') == 'unavailable' and last_attempt
            and timedelta(0) <= now - last_attempt < timedelta(days=RETRY_DAYS)):
        return False
    checked = date(freshness.get('last_checked_at') or entry.get('retrieved_at'))
    interval = entry.get('review_interval_days', DEFAULT_REVIEW_DAYS)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        interval = DEFAULT_REVIEW_DAYS
    # Legacy entries without a retrieval date are assessed when next used,
    # rather than manufacturing a successful source-check timestamp.
    return checked is None or now - checked >= timedelta(days=interval) or checked > now


def check_source(url, previous=None):
    """Bounded, credential-free conditional GET; changes still need interpretation."""
    previous = previous or {}
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return {'url': url, 'outcome': 'unavailable', 'reason': 'requires a credential-free HTTPS source'}
    headers = {'User-Agent': 'auto-agents-reference-review/1'}
    for key, header in [('etag', 'If-None-Match'), ('last_modified', 'If-Modified-Since')]:
        if previous.get(key) and previous.get('content_sha256'):
            headers[header] = previous[key]
    try:
        with urlopen(Request(url, headers=headers), timeout=10) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('source exceeds bounded retrieval size')
            text = raw.decode('utf-8', errors='replace')
            # Strip executable/style content before detecting empty JS shells.
            body = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', text, flags=re.I | re.S)
            body = re.sub(r'<[^>]+>', ' ', body)
            body = re.sub(r'\s+', ' ', body).strip()
            if len(body) < 120:
                raise ValueError('no usable document body (possibly a JavaScript shell)')
            checksum = hashlib.sha256(body.encode()).hexdigest()
            return {'url': url, 'final_url': response.url, 'http_status': response.status,
                    'etag': response.headers.get('ETag', ''),
                    'last_modified': response.headers.get('Last-Modified', ''),
                    'content_sha256': checksum,
                    'outcome': 'unchanged' if checksum == previous.get('content_sha256') else 'content_available'}
    except HTTPError as error:
        if error.code == 304 and previous.get('content_sha256'):
            return {**previous, 'url': url, 'http_status': 304, 'outcome': 'unchanged'}
        return {'url': url, 'outcome': 'unavailable', 'http_status': error.code}
    except (OSError, URLError, ValueError) as error:
        return {'url': url, 'outcome': 'unavailable', 'reason': str(error)[:300]}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def retain(project, reference, entry):
    """Retain exact existing evidence, including the scope of earlier approvals."""
    from .io_utils import write_json
    path = Path(project) / reference
    if path.is_symlink() or not path.resolve().is_relative_to(Path(project).resolve()):
        raise ValueError('provider evidence path escapes project')
    record = {'reference': reference, 'entry': {k: v for k, v in entry.items() if k != 'evidence_snapshot'},
              'text': path.read_text() if path.is_file() else ''}
    relative = f'{HISTORY}/{_digest(record)}.json'
    target = Path(project) / relative
    if not target.resolve().is_relative_to(Path(project).resolve()):
        raise ValueError('provider history path escapes project')
    if not target.exists():
        write_json(target, record)
    return relative


def retained(project, reference, entry):
    relative = entry.get('evidence_snapshot', '')
    if not isinstance(relative, str) or not re.fullmatch(re.escape(HISTORY) + r'/[a-f0-9]{64}\.json', relative):
        return None
    path = Path(project) / relative
    if path.is_symlink() or not path.resolve().is_relative_to(Path(project).resolve()):
        raise ValueError('provider history path escapes project')
    record = json.loads(path.read_text())
    if record.get('reference') != reference or _digest(record) != path.stem:
        raise ValueError('provider history identity mismatch')
    return record


def reasons(entry, trace, reference, *, now=None):
    from .requirements import provider_reference_consumer_contract_sha256
    result = []
    if entry.get('status') not in RESOLVED:
        result.append('unresolved_protocol')
    if trace.get('contract_identity_schema_version') and entry.get('consumer_contract_sha256') != provider_reference_consumer_contract_sha256(trace, reference):
        result.append('applicability')
    # Enrol v2 references in scheduled review. Legacy references retain their
    # existing behaviour until upgraded, rather than gaining invented metadata.
    try:
        version = int(entry.get('contract_version', 1))
    except (ValueError, TypeError):
        version = 1
    if version >= 2 and review_due(entry, now=now):
        result.append('freshness')
    return result


def prepare(project, trace, lock, references, *, requirement_ids=None, fetch=True, now=None):
    from .requirements import provider_reference_consumer_contract_sha256, provider_reference_paths
    now = now or now_utc()
    context = {}
    checks = {}
    entries = lock.get('references', {})
    for reference in sorted(set(references)):
        entry = next((v for v in entries.values() if isinstance(v, dict) and v.get('path') == reference), {})
        why = reasons(entry, trace, reference, now=now)
        if not why:
            continue
        history = retained(project, reference, entry)
        prior = history['entry'] if history and history['entry'].get('status') in RESOLVED else dict(entry)
        snapshot = entry.get('evidence_snapshot') if history else retain(project, reference, entry)
        context[reference] = {'reasons': why, 'prior': prior, 'evidence_snapshot': snapshot,
            'consumer_contract_sha256': provider_reference_consumer_contract_sha256(trace, reference),
            'sources': [], 'prepared_at': now.isoformat(),
            'requirement_ids': sorted(r['id'] for r in trace.get('requirements', [])
                if r.get('status', 'active') == 'active' and reference in provider_reference_paths(r)
                and (requirement_ids is None or r['id'] in requirement_ids))}
        if fetch and 'freshness' in why:
            previous = {s['url']: s for s in (entry.get('freshness') or {}).get('sources', []) if isinstance(s, dict) and s.get('url')}
            for url in entry.get('source_urls', []):
                checks.setdefault(url, previous.get(url, {}))
    if checks:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = dict(zip(checks, pool.map(lambda u: check_source(u, checks[u]), checks)))
        for reference, item in context.items():
            entry = next((v for v in entries.values() if isinstance(v, dict) and v.get('path') == reference), {})
            if 'freshness' in item['reasons']:
                item['sources'] = [results[url] for url in entry.get('source_urls', []) if url in results]
    for reference, item in context.items():
        item['review_id'] = _digest([reference, item['consumer_contract_sha256'], item['evidence_snapshot'], item['reasons'], item['sources'], item['requirement_ids']])
    return context


INSTRUCTION = '''REFERENCE REVIEW POLICY:
First read the retained local evidence and historical approvals. A new requirement ID,
changed retry budget or consumer hash is a reason to assess applicability, not proof
that provider documentation expired. Map each required protocol fact to existing
sections for the exact provider/model/endpoint/version. Reuse covered facts and
approvals within their original scope. Never ask to reapprove an unchanged assumption.
The default source-review interval is 30 days, overridable by positive integer
review_interval_days on each lock entry. Time only schedules review; it does not revoke
validity. Use supplied conditional-source observations (ETag/Last-Modified/body hash).
Content changes need semantic comparison: unrelated model versions/layout/examples do
not invalidate the deployed contract. A failed fetch or JS shell is unavailable
freshness, not missing historical evidence. Do not discard old facts or approvals.
For each requested reference write a review object in its lock entry:
{"review_id":"echo supplied review_id", "applicability":"covered|gap|conflict", "reason":"specific comparison",
 "evidence_refs":["existing local section or official source"],
 "affected_requirement_ids":["current requirement IDs, required for gap/conflict"],
 "facts":[{"requirement_id":"REQ-001","fact":"specific protocol dependency",
 "result":"covered|missing|contradicted","evidence_ref":"specific local section or official source"}],
 "freshness":"not_checked|unchanged|changed_unrelated|changed_relevant|unavailable",
 "target":{"provider":"...","model":"...","endpoint":"...","version":"..."}}.
Cover every supplied requirement_id with facts. Local policy changes can cite existing
protocol facts without claiming a new provider capability. Only a concrete new uncovered protocol dependency or relevant contradictory evidence
can block. Explain the requirement and exact fact; retrieval failure alone is not a gap.
Preserve the prior verified/assumption_approved status when covered, including on
unavailable freshness. Keep source timestamps, validators and historical evidence;
consumer hashes and admitted timestamps are controller-owned. Do not approve new
assumptions or infer expanded authorization. Do not claim unchanged/relevant change
without inspectable official evidence. New provider abilities still need evidence.
'''


def validate(lock, trace, context):
    """Reject blanket invalidation and status changes without a scoped comparison."""
    errors = []
    from .requirements import provider_reference_consumer_contract_sha256
    for reference, baseline in context.items():
        if provider_reference_consumer_contract_sha256(trace, reference) != baseline['consumer_contract_sha256']:
            errors.append(f'{reference}: requirements changed during review; reassess the current contract')
            continue
        entry = next((v for v in lock.get('references', {}).values() if isinstance(v, dict) and v.get('path') == reference), {})
        prior = baseline['prior']
        # New/missing evidence uses the existing v2 contract validation. Prior
        # usable evidence requires an explicit decision before it can be changed.
        if prior.get('status') not in RESOLVED:
            continue
        review = entry.get('review') or {}
        if not isinstance(review, dict):
            errors.append(f'{reference}: review must be an object')
            continue
        applicability = review.get('applicability')
        freshness = review.get('freshness')
        if (review.get('review_id') != baseline['review_id']
                or applicability not in {'covered', 'gap', 'conflict'}
                or not isinstance(review.get('reason'), str) or not review['reason'].strip()
                or not isinstance(review.get('evidence_refs'), list) or not review['evidence_refs']
                or any(not isinstance(v, str) or not v.strip() for v in review['evidence_refs'])
                or not isinstance(review.get('target'), dict) or not all(isinstance(review['target'].get(k), str) and review['target'][k].strip() for k in ('provider','model','endpoint','version'))
                or freshness not in {'not_checked','unchanged','changed_unrelated','changed_relevant','unavailable'}):
            errors.append(f'{reference}: supply a scoped applicability/freshness review against retained evidence')
            continue
        facts = review.get('facts')
        expected_ids = set(baseline['requirement_ids'])
        if (not isinstance(facts, list) or not facts
                or any(not isinstance(f, dict) or not isinstance(f.get('requirement_id'), str) or f['requirement_id'] not in expected_ids
                    or f.get('result') not in {'covered', 'missing', 'contradicted'}
                    or not isinstance(f.get('fact'), str) or not f['fact'].strip()
                    or not isinstance(f.get('evidence_ref'), str) or not f['evidence_ref'].strip() for f in facts)
                or {f.get('requirement_id') for f in facts} != expected_ids):
            errors.append(f'{reference}: map every current requirement to concrete protocol facts and existing evidence')
            continue
        uncovered = {f['requirement_id'] for f in facts if f['result'] != 'covered'}
        if applicability == 'covered' and uncovered:
            errors.append(f'{reference}: uncovered facts cannot be marked covered')
        if applicability != 'covered' and not uncovered:
            errors.append(f'{reference}: blocking requires a concrete uncovered or contradicted protocol fact')
        if applicability == 'covered':
            if entry.get('status') != prior.get('status'):
                errors.append(f'{reference}: covered evidence must retain its prior validity/approval status')
            if freshness == 'changed_relevant':
                errors.append(f'{reference}: relevant protocol changes require a conflict/gap assessment')
        else:
            affected = review.get('affected_requirement_ids', [])
            if (not isinstance(affected, list) or not affected
                    or any(not isinstance(v, str) for v in affected) or not set(affected).issubset(expected_ids)
                    or set(affected) != uncovered):
                errors.append(f'{reference}: identify affected requirements and the concrete uncovered/conflicting fact')
            if entry.get('status') in RESOLVED:
                errors.append(f'{reference}: unresolved protocol gaps/conflicts cannot be marked usable')
        sources = baseline['sources']
        if 'freshness' in baseline['reasons'] and freshness == 'not_checked':
            errors.append(f'{reference}: source review is due; record a real check or unavailable outcome')
        if sources and all(s['outcome'] == 'unavailable' for s in sources) and freshness in {'unchanged','changed_unrelated','changed_relevant'}:
            errors.append(f'{reference}: unavailable source checks cannot establish current protocol changes or unchanged content; retain unavailable freshness and the historical evidence')
    return errors


def finish(project, lock, context, *, now=None):
    """Stamp admitted observations without refreshing timestamps on failed fetches."""
    now = now or now_utc()
    for entry in lock.get('references', {}).values():
        reference = entry.get('path')
        if reference not in context:
            continue
        item = context[reference]
        review = entry.get('review') or {}
        entry['evidence_snapshot'] = item['evidence_snapshot']
        if item['prior'].get('status') not in RESOLVED:
            continue
        freshness = dict(item['prior'].get('freshness') or {})
        outcome = review.get('freshness')
        if outcome in {'not_checked', 'unavailable'}:
            # A local assessment or failed fetch cannot pretend to refresh the source.
            if 'retrieved_at' in item['prior']:
                entry['retrieved_at'] = item['prior']['retrieved_at']
            else:
                entry.pop('retrieved_at', None)
        if outcome != 'not_checked':
            freshness.update(last_attempt_at=now.isoformat(), outcome=outcome)
            if outcome in {'unchanged','changed_unrelated','changed_relevant'}:
                freshness['last_checked_at'] = now.isoformat()
            # Retain validators when a retrieval failed, for the next attempt.
            old = {s['url']: s for s in freshness.get('sources', []) if s.get('url')}
            for source in item['sources']:
                old[source['url']] = {**old.get(source['url'], {}), **source}
            freshness['sources'] = list(old.values())
        entry['freshness'] = freshness
        entry['applicability_checked_at'] = now.isoformat()
        if review.get('applicability') == 'covered':
            entry['consumer_contract_sha256'] = item['consumer_contract_sha256']
            entry['evidence_snapshot'] = retain(project, reference, entry)
    return lock


def reuse_unchanged(project, lock, context, *, now=None):
    """Reuse an already applicable contract when every source is unchanged."""
    now = now or now_utc()
    remaining = dict(context)
    for entry in lock.get('references', {}).values():
        reference = entry.get('path')
        item = context.get(reference)
        if (not item or item['reasons'] != ['freshness'] or not item['sources']
                or any(source['outcome'] != 'unchanged' for source in item['sources'])):
            continue
        entry['freshness'] = {**(entry.get('freshness') or {}),
            'last_attempt_at': now.isoformat(), 'last_checked_at': now.isoformat(),
            'outcome': 'unchanged', 'sources': item['sources'], 'review_id': item['review_id']}
        entry['evidence_snapshot'] = retain(project, reference, entry)
        remaining.pop(reference)
    return remaining
