"""Small working inputs with explicit links to complete planning evidence."""
from copy import deepcopy
import subprocess

from .repair_feedback import sanitize_evidence


def source_delta(workspace, commit):
    """Inspection aid only; never authorizes reuse of an incomplete dependency fact."""
    if not commit:
        return {'available': False, 'reason': 'no prior source revision'}
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', commit, 'HEAD'],
                              cwd=workspace, capture_output=True)
    if ancestor.returncode:
        return {'available': False, 'reason': 'prior revision is unavailable or not an ancestor'}
    changed = subprocess.check_output(['git', 'diff', '--name-only', commit, '--'], cwd=workspace, text=True)
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=workspace, text=True)
    return {'available': True, 'from_commit': commit,
            'changed_paths': sorted(set(changed.splitlines() + untracked.splitlines())),
            'instruction': 'Inspect the relevant diff and affected dependencies. Unchanged paths alone '
                           'do not resolve unproved dynamic or external inputs.'}


def plan_delta(previous, plan):
    old = (previous or {}).get('draft') or {}
    if not isinstance(old, dict):
        old = {}
    old_scenarios = {s['scenario_id']: s for s in (old.get('scenarios') or [])
                     if isinstance(s, dict) and s.get('scenario_id')}
    scenarios = {s['scenario_id']: s for s in (plan.get('scenarios') or [])
                 if isinstance(s, dict) and s.get('scenario_id')}
    return {'parent_revision': (previous or {}).get('id'),
            'changed_fields': [key for key in sorted(set(old) | set(plan)) if old.get(key) != plan.get(key)],
            'changed_scenario_ids': [key for key in sorted(set(old_scenarios) | set(scenarios))
                                     if old_scenarios.get(key) != scenarios.get(key)],
            'retained_scenario_ids': [key for key in sorted(scenarios)
                                      if old_scenarios.get(key) == scenarios[key]]}


def working_input(context, directory, stage):
    """Keep active evidence inline and move repeated background behind explicit references."""
    working = deepcopy(sanitize_evidence(context))
    full = str(directory / 'input.json')
    def reference(section):
        return {'complete_input_ref': full, 'section': section,
                'retrieve_when': 'needed to inspect an affected mechanism or resolve missing evidence'}
    scope = stage == 'self_repair_scope_review'
    incremental = isinstance(working.get('previous_revision'), dict)
    if scope or incremental:
        for key in ('history', 'root_cause', 'previous_code_review'):
            if key in working:
                working[key] = reference(key)
        # The original request, current contract, findings and unresolved feedback
        # remain inline. Historical component instructions are not a new design.
        component = working.get('component', {})
        prior_component = (working.get('previous_revision') or {}).get('component', {})
        for key in ('implementation_steps', 'focused_tests'):
            if key in component and (scope or component[key] == prior_component.get(key)):
                component[key] = reference('component.' + key)
    if scope:
        if 'previous_revision' in working:
            working['previous_revision'] = reference('previous_revision')
        for identity, row in working.get('scope_revalidation', {}).items():
            old = row.get('previous')
            if isinstance(old, dict):
                row['previous'] = {key: old.get(key) for key in (
                    'verdict', 'reason', 'trigger', 'consequence', 'support_basis', 'disproof',
                    'evidence', 'request_id', 'source_commit', 'fact_ref')}
                row['complete_previous'] = reference('scope_revalidation.' + identity + '.previous')
    elif incremental:
        previous = working['previous_revision']
        draft = previous.get('draft')
        if isinstance(draft, dict):
            summary = {key: previous.get(key) for key in (
                'id', 'parent_revision', 'status', 'source_commit', 'step_ids', 'review', 'feedback')}
            summary['draft_ref'] = reference('previous_revision.draft')
            if working.get('proposed_plan') == draft:
                summary['draft_location'] = 'proposed_plan (identical current draft)'
            else:
                summary['step_index'] = [{'step_id': identity, 'excerpt': str(text)[:300],
                                           'omitted_chars': max(0, len(str(text)) - 300)}
                    for identity, text in zip(previous.get('step_ids', []), draft.get('implementation_steps') or [])]
                summary['scenario_index'] = [{key: row.get(key) for key in (
                    'scenario_id', 'kind', 'trigger', 'expected', 'obligation_ids', 'finding_ids')}
                    for row in (draft.get('scenarios') or []) if isinstance(row, dict)]
            working['previous_revision'] = summary
    return working
