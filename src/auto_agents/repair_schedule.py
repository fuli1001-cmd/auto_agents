"""Serial, prerequisite-aware verification selection without deleting obligations."""
import shlex

from .repair_test_refs import migrate_review_commands, pytest_targets

QUICK_COMMAND_TARGET = 3
QUICK_NODE_TARGET = 12
QUICK_SECONDS_TARGET = 180


def quick_verification_plan(experiment, active):
    """Select current negative/positive oracles, preserving full acceptance.

    Commands are atomic: batching never changes a pytest invocation's cohort,
    options or fixture lifetime. A budget is a scheduling target, not a waiver.
    """
    scenarios = active.get('scenarios', [])
    required = set(active.get('finding_ids', []))
    bindings = active.get('finding_scenario_bindings', {})
    from .repair_memory import component_key
    timings = experiment.component_memory.get(component_key(active), {}).get('check_timings', {})
    def cost(row):
        old = timings.get(row.get('quick_check') or row['check'], {})
        return (not bool(old), old.get('seconds', 0), old.get('collected_cases') or 0)
    selected, reasons = [], []
    # Recheck the actual failed acceptance cohort before paying for another
    # semantic review. Do not split its fixture lifetime or invent new commands
    # from failure prose, and keep future-component checks deferred.
    expanded = verification_plan(experiment, active)
    allowed, _ = canonical_commands(expanded['commands'])
    for record in sorted(experiment.candidates.values(), key=lambda item: item.created_at, reverse=True):
        if record.finding_group_id != active.get('group_id'):
            continue
        for evidence in record.failure_evidence:
            if evidence.get('phase') == 'baseline' or evidence.get('resolved'):
                continue
            command = evidence.get('command')
            if not pytest_parts(command):
                continue
            canonical = canonical_commands([command])[0][0]
            if canonical in allowed:
                selected.append(canonical)
        break
    if selected:
        reasons.append('latest failed acceptance cohorts run first, unchanged, before semantic review')
    for finding in sorted(required) or [None]:
        relevant = [row for row in scenarios if finding is None or finding in row.get('finding_ids', [])
                    or row.get('scenario_id') in bindings.get(finding, [])]
        negatives = [row for row in relevant if row.get('kind') == 'failure']
        positives = [row for row in relevant if row.get('kind') == 'compatibility']
        if not positives:
            owners = {key for row in negatives for key in row.get('obligation_ids', [])}
            positives = [row for row in scenarios if row.get('kind') == 'compatibility'
                         and owners.intersection(row.get('obligation_ids', []))]
        # Each required finding has a negative oracle and a compatible control.
        # If mapping is incomplete, retain the supplied quick set rather than guess.
        if not negatives or not positives:
            selected.extend(active.get('quick_checks', []))
            reasons.append('incomplete oracle mapping: retained supplied quick checks')
        else:
            for row in (min(negatives, key=cost), min(positives, key=cost)):
                selected.append(row.get('quick_check') or row['check'])
    for row in scenarios:
        if row.get('quick_required') is True:
            selected.append(row.get('quick_check') or row['check'])
    if not selected:
        selected = list(active.get('quick_checks', []))
    commands, requests = canonical_commands(selected)
    inventory = allowed
    targets = sum(len((pytest_parts(command) or ([], []))[1]) for command in commands)
    if len(commands) > QUICK_COMMAND_TARGET or targets > QUICK_NODE_TARGET:
        reasons.append('required oracles exceed selection target; preserve them in serial batches')
    batches, batch, cases, seconds = [], [], 0, 0.0
    for command in commands:
        old = timings.get(command, {})
        count = old.get('collected_cases')
        duration = old.get('seconds', 0)
        if batch and (len(batch) >= QUICK_COMMAND_TARGET or cases + (count or 0) > QUICK_NODE_TARGET
                      or seconds + duration > QUICK_SECONDS_TARGET):
            batches.append(batch)
            batch, cases, seconds = [], 0, 0.0
        batch.append(command)
        cases += count or 0
        seconds += duration
        if count is None:
            reasons.append('unknown collection size: preserve explicit oracle; prefer a reviewed parameter case')
        elif count > QUICK_NODE_TARGET or duration > QUICK_SECONDS_TARGET:
            reasons.append('atomic required command exceeds target; preserve its fixture semantics')
    if batch:
        batches.append(batch)
    return {'commands': commands, 'requests': requests,
            'batches': batches,
            'acceptance_inventory': inventory,
            'deferred': [{'command': c, 'reason': 'expanded acceptance after code review'}
                         for c in inventory if c not in commands],
            'budget': {'commands': QUICK_COMMAND_TARGET, 'collected_cases': QUICK_NODE_TARGET,
                       'estimated_seconds': QUICK_SECONDS_TARGET},
            'budget_exceptions': list(dict.fromkeys(reasons)),
            'estimated_seconds': sum(timings[c].get('seconds', 0) for c in commands) if all(c in timings for c in commands) else None}


def canonical_commands(commands):
    """Coalesce identical supported invocations, retaining every request owner.

    Different target cohorts can change fixture lifetimes and are deliberately
    not treated as equivalent merely because they contain a common node.
    """
    selected, requests, seen = [], [], {}
    for original in commands:
        command = shlex.join(shlex.split(original)) if pytest_parts(original) else original
        if command not in seen:
            seen[command] = len(selected)
            selected.append(command)
        requests.append({'original_command': original, 'execution_index': seen[command],
                         'command': command})
    return selected, requests


def pytest_parts(command):
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        args = shlex.split(command)
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ''
        if list(lexer) != args:
            return None  # Shell lists/redirection are not a single pytest invocation.
    except ValueError:
        return None
    if args[:3] != ['python', '-m', 'pytest']:
        return None
    flags = [arg for arg in args[3:] if arg in {'-q', '-x', '--tb=short'}]
    targets = [arg for arg in args[3:] if arg.startswith('tests/') and '.py' in arg]
    if len(flags) + len(targets) != len(args) - 3 or not targets:
        return None
    return args[:3] + flags, targets


def verification_plan(experiment, active):
    migrate_review_commands(experiment)
    focused = list(active.get('focused_tests', []))
    focused.extend(active.get('quick_checks', []))
    focused.extend(active.get('retained_acceptance', []))
    focused.extend(row['check'] for row in active.get('scenarios', []) if row.get('check'))
    # A routed regression retains its concrete reproduction as required proof,
    # even when the original component plan predates the review finding.
    for finding in getattr(experiment, 'findings', {}).values():
        if (finding.disposition != 'candidate_regression'
                or finding.status not in {'confirmed', 'reopened'}
                or finding.finding_id not in active.get('finding_ids', [])):
            continue
        targets = pytest_targets(finding.required_test)
        if targets:
            focused.insert(0, shlex.join(['python', '-m', 'pytest', '-q', *dict.fromkeys(targets)]))
    future_targets = set()
    for group in experiment.finding_groups:
        if group.get('status') != 'completed' and group.get('group_id') != active.get('group_id'):
            for command in group.get('focused_tests', []):
                parsed = pytest_parts(command)
                if parsed:
                    future_targets.update(parsed[1])
    active_targets = {target for command in focused for target in (pytest_parts(command) or ([], []))[1]}
    future_targets.difference_update(active_targets)
    regressions, deferred = [], []
    for command in experiment.sticky_verification_commands:
        parsed = pytest_parts(command)
        if parsed:
            prefix, targets = parsed
            selected = [target for target in targets if target not in future_targets]
            if selected != targets:
                deferred.append({'command': command, 'reason': 'future component prerequisites',
                                 'targets': [target for target in targets if target in future_targets]})
                if not selected:
                    continue
                command = shlex.join(prefix + selected)
        regressions.append(command)
    # Previously failed checks in the active scope run before its wider checks.
    failures = []
    allowed_targets = {target for command in focused + regressions
                       for target in (pytest_parts(command) or ([], []))[1]}
    for record in sorted(experiment.candidates.values(), key=lambda item: item.created_at, reverse=True):
        if record.finding_group_id == active.get('group_id'):
            for evidence in record.failure_evidence:
                command = evidence.get('command')
                if evidence.get('phase') == 'baseline' or evidence.get('resolved'):
                    continue
                parsed = pytest_parts(command or '')
                nodes = [failure['nodeid'] for failure in evidence.get('failures', [])
                         if failure.get('nodeid') and any(
                             failure['nodeid'] == target or failure['nodeid'].startswith(target + '::')
                             for target in allowed_targets)]
                if parsed and nodes:
                    failures.append(shlex.join(parsed[0] + list(dict.fromkeys(nodes))))
                elif command in focused + regressions:
                    failures.append(command)
            break
    commands, requests = canonical_commands([*failures, *focused, *regressions])
    return {'commands': commands, 'deferred': deferred, 'requests': requests,
            'deduplicated_commands': len(requests) - len(commands)}
