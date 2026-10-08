from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from auto_agents.config import create_session, provider_references_lock_path, requirements_trace_path
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, TaskSpec
from auto_agents.provider_contract import PROVIDER_REFERENCE_CONTRACT_VERSION, PROVIDER_REFERENCE_V2_HEADINGS, provider_policy_prompt_lines, validate_provider_reference_v2
from auto_agents.session import Session

def _reference_markdown() -> str:
    lines = ['# Provider contract']
    for heading in PROVIDER_REFERENCE_V2_HEADINGS:
        lines.extend(['', f'## {heading}', '', 'Not applicable: covered by this fixture.'])
    return '\n'.join(lines) + '\n'

def _sourced_reference_markdown() -> str:
    sections: dict[str, str] = {'Prompt / Content Construction': '**[Provider official]** The request accepts a prompt.\n\n- **[Local policy]** Compile the prompt before sending it.', 'Safety / Content Policy': '**[Provider official]** Structured refusals use a stable code.\n\n**[Project observed]** A compatibility gateway returned a wrapped refusal.\n\n**[Local policy]** Ambiguous refusals fail closed.', 'Semantic Error Routing': '**[Local policy]** Parse the bounded body before HTTP fallback.\n\n1. **[Provider official]** A stable refusal code is documented; **[Local policy]** map it to the safety category.\n2. **[Compatibility assumption]** A wrapped status token is equivalent.', 'Retry / Recovery Matrix': '| Outcome | Retry | Provenance |\n| --- | --- | --- |\n| Safety refusal | Forbidden | **[Local policy]** |\n\n**[Local policy]** Retry budgets remain independent.'}
    lines = ['# Provider contract']
    for heading in PROVIDER_REFERENCE_V2_HEADINGS:
        lines.extend(['', f'## {heading}', '', sections.get(heading, 'Not applicable: covered by this fixture.')])
    return '\n'.join(lines) + '\n'

class ProviderContractPolicyTests(unittest.TestCase):

    def test_v2_reference_requires_version_nonempty_sections_and_allows_explained_na(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / 'provider.md'
            write_text(reference, '# Provider\n\n## Status\n\nverified\n')
            errors = validate_provider_reference_v2(reference, {'path': 'provider.md', 'status': 'verified', 'contract_version': 1})
            self.assertTrue(any(('contract_version' in item for item in errors)), errors)
            self.assertTrue(any(('Safety / Content Policy' in item for item in errors)), errors)
            write_text(reference, _reference_markdown())
            self.assertEqual(validate_provider_reference_v2(reference, {'path': 'provider.md', 'status': 'verified', 'contract_version': PROVIDER_REFERENCE_CONTRACT_VERSION}), [])

    def test_v2_reference_requires_rule_and_recovery_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / 'provider.md'
            lock_entry = {'path': 'provider.md', 'status': 'verified', 'contract_version': PROVIDER_REFERENCE_CONTRACT_VERSION}
            write_text(reference, _sourced_reference_markdown())
            self.assertEqual(validate_provider_reference_v2(reference, lock_entry), [])
            missing_routing_source = _sourced_reference_markdown().replace('2. **[Compatibility assumption]** A wrapped status token is equivalent.', '2. A wrapped status token is equivalent.')
            write_text(reference, missing_routing_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertTrue(any(('Semantic Error Routing' in item and 'provenance' in item for item in errors)), errors)
            missing_matrix_source = _sourced_reference_markdown().replace('| Outcome | Retry | Provenance |', '| Outcome | Retry |').replace('| --- | --- | --- |\n| Safety refusal | Forbidden | **[Local policy]** |', '| --- | --- |\n| Safety refusal | Forbidden |')
            write_text(reference, missing_matrix_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertTrue(any(('Source or Provenance column' in item for item in errors)), errors)
            malformed_routing_source = _sourced_reference_markdown().replace('1. **[Provider official]** A stable refusal code is documented; ', '1. **Provenance: [Provider official]** A stable refusal code is documented; ')
            write_text(reference, malformed_routing_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertEqual(sum(('unsupported provenance syntax' in item for item in errors)), 1, errors)
            self.assertTrue(any(('**[Provider official]**' in item for item in errors)))
            malformed_matrix_source = _sourced_reference_markdown().replace('| Safety refusal | Forbidden | **[Local policy]** |', '| Safety refusal | Forbidden | **Source: [Local policy]** |')
            write_text(reference, malformed_matrix_source)
            errors = validate_provider_reference_v2(reference, lock_entry)
            self.assertTrue(any(('Retry / Recovery Matrix row 3 uses unsupported provenance syntax' in item for item in errors)), errors)

    def test_v2_reference_accepts_composite_recovery_provenance_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / 'provider.md'
            lock_entry = {'path': 'provider.md', 'status': 'verified', 'contract_version': PROVIDER_REFERENCE_CONTRACT_VERSION}
            composite_matrix_header = _sourced_reference_markdown().replace('| Outcome | Retry | Provenance |', '| Outcome | Retry | Source or Provenance |')
            write_text(reference, composite_matrix_header)
            self.assertEqual(validate_provider_reference_v2(reference, lock_entry), [])
if __name__ == '__main__':
    unittest.main()
