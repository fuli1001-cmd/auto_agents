"""Native structured replies reduce protocol retries; validators retain authority."""
from copy import deepcopy


def obj(properties):
    return {'type': 'object', 'properties': deepcopy(properties), 'required': list(properties), 'additionalProperties': False}


def normalize_reply(stage, payload):
    """Only erase absent optional values; no factual decision is inferred."""
    if stage in {'self_repair_scope_review', 'self_repair_scope_format'}:
        if payload.get('probes') is None:
            payload.pop('probes', None)
        for probe in payload.get('probes', []) if isinstance(payload.get('probes', []), list) else []:
            if isinstance(probe, dict) and probe.get('replaces_probe') is None:
                probe.pop('replaces_probe', None)
    return payload


def array(value):
    return {'type': 'array', 'items': value}


TEXT = {'type': 'string'}
TEXTS = array(TEXT)
SCOPE = obj({**{key: TEXT for key in ('finding_id', 'obligation_id', 'trigger', 'consequence',
                                   'support_basis', 'reason', 'disproof')},
             'verdict': {'type': 'string', 'enum': ['required', 'not_applicable', 'follow_up', 'unknown']},
             'evidence': TEXTS})
CONTROL = obj({'command': TEXT, 'scenario_ids': TEXTS, 'purpose': TEXT})
CONTROLS = {**obj({'negative': CONTROL, 'positive': CONTROL}), 'type': ['object', 'null']}


def schema_for(stage):
    if stage in {'self_repair_scope_review', 'self_repair_scope_format'}:
        probe = obj({'command': TEXT, 'expected': {'type': 'string', 'enum': ['pass', 'behavior_failure']},
                     'purpose': TEXT, 'replaces_probe': {'type': ['string', 'null']}})
        return obj({'decisions': array(SCOPE), 'probes': {'type': ['array', 'null'], 'items': probe}})
    if stage == 'self_repair_plan_review':
        issue = obj({key: TEXT for key in ('scenario_id', 'reason', 'counterexample', 'requested_change')})
        return obj({'decision': {'type': 'string', 'enum': ['APPROVE', 'REVISE']}, 'reason': TEXT,
                    'scenario_ids': TEXTS, 'issues': array(issue), 'implementation_required': {'type': 'boolean'},
                    'remaining_changes': TEXTS, 'decisions': {'type': ['array', 'null'], 'items': SCOPE}})
    if stage == 'self_repair_candidate_review':
        finding = obj({**{key: TEXT for key in ('finding_id', 'causal_obligation_id', 'reason', 'counterexample',
                                               'required_test', 'defer_until', 'repair_group_id')},
            'severity': {'type': 'string', 'enum': ['fatal', 'hard', 'repairable']},
            'disposition': {'type': 'string', 'enum': ['contract_violation', 'candidate_regression', 'unrelated_observation']},
            'affected_paths': TEXTS, 'evidence': TEXTS, 'scenario_ids': TEXTS,
            'repair_kind': {'type': 'string', 'enum': ['implementation', 'plan_gap']},
            'scope': SCOPE, 'controls': CONTROLS})
        return obj({'decision': {'type': 'string', 'enum': ['APPROVE', 'REJECT']}, 'reason': TEXT,
                    'findings': array(finding), 'resolved_finding_ids': TEXTS})
    if stage == 'self_repair_failure_diagnosis':
        return obj({'kind': {'type': 'string', 'enum': ['repair_code', 'repair_verification', 'blocked']},
                    **{key: TEXT for key in ('cause', 'completion', 'invalidated_assumption', 'next_check')},
                    'evidence_ids': TEXTS})
    return None
