"""Keep proof-review prompts bounded while preserving exact immutable inputs."""
import difflib
import json

from .proof_support.store import digest


def render(store, value):
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= 24000:
        return text
    reference = store.artifact('review-inputs', value)
    path = store.root / reference['path']
    summary = {key: value.get(key) for key in ('policy', 'owner', 'session_id', 'binding',
        'candidate', 'source_revision', 'contract_revision', 'requirements')}
    summary['changes'] = list(value['changes'])
    summary['delta'] = {}
    for name, change in value['delta'].items():
        before, after = change.get('preimage', {}), change.get('postimage', {})
        diff = ''.join(difflib.unified_diff(before.get('text', '').splitlines(True),
            after.get('text', '').splitlines(True), fromfile='before/' + name, tofile='after/' + name))
        summary['delta'][name] = {'before_sha256': before.get('sha256'), 'after_sha256': after.get('sha256'),
                                'diff': diff[:6000], 'diff_truncated': len(diff) > 6000}
    navigation = json.dumps(summary, ensure_ascii=False)
    if len(navigation) > 24000:
        navigation = 'Large review: inspect owner, requirements, changes, and every delta entry in the evidence file.'
    return ('Read the complete sealed review input at ' + str(path) + '\n'
            'Input digest: ' + digest(value) + '\n'
            'Use bounded JSON queries to inspect the exact before/after code. '
            'The summary below is only navigation; do not approve if evidence is unavailable.\n' + navigation)
