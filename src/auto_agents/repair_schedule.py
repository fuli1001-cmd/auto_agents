"""Serial, prerequisite-aware verification selection without deleting obligations."""
import shlex

from .repair_test_refs import migrate_review_commands, pytest_targets


def pytest_parts(command):
    try:
        args = shlex.split(command)
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
    commands = list(dict.fromkeys([*failures, *focused, *regressions]))
    return {'commands': commands, 'deferred': deferred}
