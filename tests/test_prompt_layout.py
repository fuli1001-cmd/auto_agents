"""Lossless rule deduplication and stable prompt prefixes."""
from auto_agents.prompting import PromptBlock, compose_prompt

def test_stable_rules_precede_dynamic_paths_and_duplicate_aliases_are_recorded():
    from auto_agents.prompting import render_prompt

    def build(root):
        return compose_prompt([f'Project root: {root}', PromptBlock('Preserve every contract.', 'contract.primary'), PromptBlock('Preserve every contract.', 'contract.duplicate'), PromptBlock('Return DECISION.', kind='output')], purpose='review')
    first, metadata = render_prompt(build('/tmp/a').spec)
    second, _ = render_prompt(build('/tmp/b').spec)
    assert first.split('CONTEXT DATA')[0] == second.split('CONTEXT DATA')[0]
    assert first.count('Preserve every contract.') == 1
    assert metadata['rule_aliases']['contract.duplicate'] == 'contract.primary'
    assert 'Project root: /tmp/a' in first
    assert first.endswith('Return DECISION.')
