import json

from auto_agents.proof_review_prompt import render
from auto_agents.repair_v2.store import Store, digest


def test_oversized_review_preserves_complete_code_and_bounds_inline_diff(tmp_path):
    before = '# retained context\n' * 100000 + 'budget = 1\n'
    after = before[:-len('budget = 1\n')] + 'budget = 2\n'
    value = {'owner': {'goal': 'Exercise the existing two-call simulation'},
        'changes': {'tests/test_patch.py': {'before': before, 'after': after}},
        'delta': {'tests/test_patch.py': {'preimage': {'text': before}, 'postimage': {'text': after}}}}
    store = Store(tmp_path)
    prompt = render(store, value)
    assert len(prompt) < 25000
    assert '-budget = 1' in prompt and '+budget = 2' in prompt
    evidence = tmp_path/'artifacts/review-inputs'/(digest(value) + '.json')
    assert str(evidence) in prompt and json.loads(evidence.read_text()) == value
    assert 'do not approve if evidence is unavailable' in prompt


def test_many_large_deltas_keep_all_evidence_instead_of_truncating_authority(tmp_path):
    value = {'owner': {'goal': 'g'*30000}, 'changes': {'test.py': {}},
             'delta': {str(i): {'preimage': {'text': 'a\n'}, 'postimage': {'text': 'b\n'*5000}}
                       for i in range(12)}}
    prompt = render(Store(tmp_path), value)
    assert len(prompt) < 25000
    path = tmp_path/'artifacts/review-inputs'/(digest(value) + '.json')
    assert json.loads(path.read_text()) == value


def test_small_review_stays_inline(tmp_path):
    value = {'changes': {'test.py': {}}, 'delta': {}, 'owner': {'goal': 'Retained'}}
    assert json.loads(render(Store(tmp_path), value)) == value
