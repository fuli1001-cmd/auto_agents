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
    scope = stage in {'self_repair_scope_review', 'self_repair_scope_format'}
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
    if scope or working.get('scope_findings'):
        # Scope needs the observation's evidence and original obligations, not
        # a second copy of every implementation scenario and approval receipt.
        component = working.get('component', {})
        for key in list(component):
            if key not in {'group_id', 'title', 'contract_obligation_ids', 'finding_ids',
                           'touched_paths', 'depends_on', 'status'}:
                component[key] = reference('component.' + key)
        if scope and 'previous_revision' in working:
            working['previous_revision'] = reference('previous_revision')
        for identity, row in working.get('scope_revalidation', {}).items():
            old = row.get('previous')
            if isinstance(old, dict):
                row['previous'] = {key: old.get(key) for key in (
                    'verdict', 'reason', 'trigger', 'consequence', 'support_basis', 'disproof',
                    'evidence', 'request_id', 'source_commit', 'fact_ref')}
                row['complete_previous'] = reference('scope_revalidation.' + identity + '.previous')
    if not scope and incremental:
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
    # In long archives every finding carried the same 64 unresolved entries.
    # Intern identical lists without dropping a single unresolved dependency.
    unresolved = working.get('scope_unresolved_dependencies', {})
    if sum(len(str(value)) for value in unresolved.values()) > 4000:
        catalogue, index = {}, {}
        for identity, value in unresolved.items():
            from .repair_control import digest
            key = digest(value)
            if key not in index:
                index[key] = 'dependencies_' + str(len(index) + 1)
                catalogue[index[key]] = value
            unresolved[identity] = {'dependency_set': index[key]}
        working['dependency_sets'] = catalogue
    plan = working.get('proposed_plan')
    if isinstance(plan, dict):
        # Commands remain exact atomic invocations. A table removes repetition
        # between scenario checks and quick checks without flattening cohorts.
        catalogue = {}
        def command_ref(command):
            if not isinstance(command, str):
                return command
            if command not in catalogue:
                catalogue[command] = 'command_' + str(len(catalogue) + 1)
            return {'command_ref': catalogue[command]}
        plan['quick_checks'] = [command_ref(command) for command in plan.get('quick_checks', [])]
        for scenario in plan.get('scenarios', []):
            for key in ('check', 'quick_check'):
                if key in scenario:
                    scenario[key] = command_ref(scenario[key])
        working['command_table'] = {identity: command for command, identity in catalogue.items()}
    return working
