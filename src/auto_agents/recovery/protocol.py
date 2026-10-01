"""One controller manifest drives prompts, schemas and response diagnostics."""
from dataclasses import dataclass
import json
import re

from .model import KernelError, canonical, digest, require


@dataclass(frozen=True)
class ReviewManifest:
    source: str
    base: str
    contract: str
    requirements: tuple
    changes: dict
    schema_version: int = 1

    @property
    def identity(self):
        return digest({'source': self.source, 'base': self.base, 'contract': self.contract,
                       'requirements': self.requirements, 'changes': self.changes, 'schema': self.schema_version})

    def schema(self, base_schema):
        from copy import deepcopy
        schema = deepcopy(base_schema)
        if 'findings' in schema['properties']:
            findings = schema['properties']['findings']['items']['properties']
            findings['requirement'] = {'type': 'string', 'enum': [*self.requirements, 'repair-scope', 'repair-regression']}
        if self.requirements and 'coverage' in schema['properties']:
            schema['properties']['coverage']['items']['properties']['requirement'] = {
                'type': 'string', 'enum': list(self.requirements)}
        if 'coverage' in schema['properties']:
            paths = sorted({r.partition('::')[0] for r in self.requirements
                            if '/' in r and not r.startswith(('/', 'tests/')) and '..' not in r.split('/')})
            prefixes = ['tests/.+', *[re.escape(path) + r'(?:::.+)?' for path in paths]]
            schema['properties']['coverage']['items']['properties']['nodes'] = {
                'type': 'array', 'minItems': 1, 'items': {'type': 'string',
                    'description': 'Relative test file or node ID, such as tests/test_value.py::test_value. '
                                   'Evidence digests and change IDs belong in change_coverage.',
                    'pattern': r'^(?:' + '|'.join(prefixes) + ')$'}}
        allowed = sorted(self.changes)
        change = {'type': 'string', 'enum': allowed} if allowed else {'type': 'string'}
        schema['properties']['change_coverage'] = {'type': 'array', 'minItems': len(allowed),
            'maxItems': len(allowed), 'items': {'type': 'object', 'additionalProperties': False,
                'properties': {'change': change,
                    'requirement': {'type': 'string', 'enum': [*self.requirements, 'repair-regression']},
                    'reason': {'type': 'string', 'minLength': 1}, 'evidence': {'type': 'string', 'minLength': 1}},
                'required': ['change', 'requirement', 'reason', 'evidence']}}
        # Rejections identify violations; they need not claim change approval.
        schema['properties']['change_coverage']['minItems'] = 0
        schema['required'] = list(dict.fromkeys([*schema.get('required', []), 'change_coverage']))
        return schema

    def instruction(self):
        return ('CONTROLLER REVIEW MANIFEST ' + self.identity + '\n'
                + canonical({'mode': 'code-change' if self.changes else 'behavior-only',
                    'allowed_change_ids': sorted(self.changes), 'changes': self.changes,
                    'allowed_finding_requirements': [*self.requirements, 'repair-scope', 'repair-regression'],
                    'required_approval_count': len(self.changes)})
                + '\nUse only the supplied change IDs, never filenames or line numbers. '
                  'For behavior-only review, change_coverage MUST be []. Historical/upstream '
                  'diffs are background, not additional items in this manifest. '
                  'Behavioral requirement coverage is still mandatory: include each required check exactly once. '
                  'coverage[].nodes names relative test files or node IDs, such as tests/test_value.py::test_value, '
                  'or exact declared frontend test targets. Change IDs and evidence digests are not test nodes; '
                  'put those references in change_coverage evidence.')

    def coverage_diagnostics(self, value):
        from ..repair_v2.controller import review_test_node
        rows = value.get('coverage')
        covered = [row.get('requirement') for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        return {'field': 'coverage', 'expected': 'array' if not isinstance(rows, list) else None,
                'required': list(self.requirements),
                'missing': sorted(set(self.requirements) - {item for item in covered if isinstance(item, str)}),
                'invalid_rows': [index for index, row in enumerate(rows if isinstance(rows, list) else [])
                    if not isinstance(row, dict) or row.get('requirement') not in self.requirements
                    or not isinstance(row.get('nodes'), list) or not row['nodes']
                    or any(not review_test_node(node, self.requirements) for node in row['nodes'])]}

    def finding_diagnostics(self, value):
        allowed = {*self.requirements, 'repair-scope', 'repair-regression'}
        rows = value.get('findings')
        invalid = []
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                invalid.append({'index': index, 'expected': 'object'})
                continue
            missing = [key for key in ('reason', 'counterexample', 'check')
                       if not isinstance(row.get(key), str) or not row[key].strip()]
            if row.get('requirement') not in allowed or missing:
                invalid.append({'index': index, 'requirement': row.get('requirement'),
                                'unknown_requirement': row.get('requirement') not in allowed,
                                'missing_fields': missing})
        return {'field': 'findings', 'allowed_requirements': sorted(allowed), 'invalid_rows': invalid,
                'expected': 'array' if not isinstance(rows, list) else None}

    def same_judgment(self, previous, current):
        """Allow correction of unknown IDs while preserving substantive findings."""
        if previous.get('decision') != current.get('decision'): return False
        before, after = previous.get('findings'), current.get('findings')
        if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after): return False
        allowed = {*self.requirements, 'repair-scope', 'repair-regression'}
        for left, right in zip(before, after):
            if not isinstance(left, dict) or not isinstance(right, dict): return False
            if {k: v for k, v in left.items() if k != 'requirement'} != {k: v for k, v in right.items() if k != 'requirement'}:
                return False
            if right.get('requirement') not in allowed or left.get('requirement') in allowed and left['requirement'] != right['requirement']:
                return False
        return True

    def diagnose(self, value):
        rows = value.get('change_coverage')
        if not isinstance(rows, list):
            return {'manifest': self.identity, 'field': 'change_coverage', 'expected': 'array'}
        ids = [row.get('change') if isinstance(row, dict) else None for row in rows]
        strings = [item for item in ids if isinstance(item, str)]
        return {'manifest': self.identity, 'field': 'change_coverage', 'allowed': sorted(self.changes),
                'expected_count': len(self.changes), 'actual_count': len(rows),
                'missing': sorted(set(self.changes) - set(strings)),
                'extra': sorted(set(strings) - set(self.changes)),
                'duplicates': sorted({item for item in strings if strings.count(item) > 1}),
                'invalid_rows': [i for i, value in enumerate(ids) if not isinstance(value, str)]}

    def validate(self, text):
        try: value = json.loads(text)
        except (TypeError, ValueError) as error:
            raise KernelError('protocol_invalid', 'Review response is not JSON', manifest=self.identity) from error
        require(isinstance(value, dict), 'protocol_invalid', 'Review must return an object')
        finding_details = self.finding_diagnostics(value)
        require(finding_details['expected'] is None and not finding_details['invalid_rows'],
                'protocol_invalid', 'Review findings do not match the controller requirements', **finding_details)
        if value.get('decision') == 'APPROVE':
            details = self.diagnose(value)
            require(details.get('expected') is None and details['expected_count'] == details['actual_count']
                    and not any(details[k] for k in ('missing', 'extra', 'duplicates', 'invalid_rows')),
                    'protocol_invalid', 'Review coverage does not match the controller manifest', **details)
        return value

    def correction(self, response_id, text, problem):
        try:
            value = json.loads(text)
            details = {**self.diagnose(value), 'findings': self.finding_diagnostics(value),
                       'coverage': self.coverage_diagnostics(value)}
        except (ValueError, TypeError, AttributeError): details = {'field': 'envelope', 'expected': 'JSON object'}
        return (self.instruction() + '\nCorrect the response protocol once. Preserve the substantive '
                'verdict and findings; replace invalid identifiers with exact manifest IDs. '
                'Do not invent evidence or remove findings to obtain approval. '
                'findings[].requirement must be an exact allowed_finding_requirements ID. '
                'Replace an unknown requirement ID only; preserve every other finding field verbatim. '
                'When allowed_change_ids is empty, remove only the inapplicable change_coverage rows.\n'
                'coverage must include each required check exactly once; coverage[].nodes must name '
                'concrete relative test files or test node IDs (tests/test_value.py::test_value), '
                'or the exact declared frontend test target. Never put change IDs, command IDs, '
                'observation IDs or evidence digests in nodes. Keep those references in change_coverage evidence.\n'
                + canonical({'response_id': response_id, 'schema_version': self.schema_version,
                             'problem': str(problem), 'details': details}) + '\nPrevious response:\n' + text)
