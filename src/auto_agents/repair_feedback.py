"""Failure evidence survives presentation limits and drives the next action."""
from __future__ import annotations

from pathlib import Path
import re

from .execution_recovery import redact_incident_text
from .repair_control import digest
from .verification_dependencies import detect_verification_dependencies


def sanitize_evidence(value, key=''):
    if isinstance(value, str):
        if re.search(r'(?i)(?:^|_)(?:api_key|password|secret|access_token|authorization)$', key):
            return '[REDACTED]' if value else value
        return redact_incident_text(value)
    if isinstance(value, dict):
        return {str(name): sanitize_evidence(item, str(name)) for name, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [sanitize_evidence(item) for item in value]
    return value if value is None or isinstance(value, (bool, int, float)) else str(value)


def failure_excerpt(value, limit=2400):
    text = redact_incident_text(str(value))
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    failure = re.compile(r'(?i)(timed? out|timeout|stalled|traceback|assertion|\bFAILED\b|\bERROR\b|exception|exit=[1-9]|"ok": false|\x27ok\x27: False)')
    selected = set()
    for index, line in enumerate(lines):
        if failure.search(line):
            selected.update(range(max(0, index - 1), min(len(lines), index + 4)))
    # Keep the actionable middle before filling remaining space with context.
    important = '\n'.join(lines[i] for i in sorted(selected))
    marker = '\n[additional diagnostic context in full evidence artifact]\n'
    if len(important) >= limit:
        return important[:limit - len(marker)] + marker
    remaining = limit - len(important) - len(marker)
    if remaining <= 0:
        return important[:limit]
    return (important + marker + text[:remaining // 2] + text[-(remaining - remaining // 2):])[:limit]


def normalized_failure(value):
    text = str(value)
    text = re.sub(r'/tmp/[^\s:\"\x27]+', '<temporary>', text)
    text = re.sub(r'\b(?:c\d+-[a-f0-9]+|[a-f0-9]{32,64})\b', '<identity>', text)
    text = re.sub(r'line \d+', 'line <n>', text)
    return ' '.join(text.split())


def command_evidence(process, command, *, phase, root, candidate='', environment=''):
    artifacts = dict(process.process_snapshot.get('diagnostic_artifacts', {}))
    streams = {}
    for name in ('stdout', 'stderr'):
        value = getattr(process, name, '') or ''
        path = artifacts.get(name)
        if path and Path(path).is_file():
            value = Path(path).read_text(errors='replace')
        streams[name] = redact_incident_text(value)
    output = '\n'.join(streams.values())
    dependencies = detect_verification_dependencies(output, workspace=root,
        structured=process.process_snapshot.get('verification_missing_dependencies'))
    failures = sanitize_evidence(process.process_snapshot.get('verification_failures', []))
    termination = getattr(process, 'termination_reason', '')
    if dependencies:
        kind, action = 'dependency', 'prepare_environment'
    elif getattr(process, "infrastructure_failure_id", "") == "verification_source_changed":
        kind, action = "verification", "repair_verification"
    elif termination:
        kind, action = 'execution', 'diagnose_execution'
    elif failures or re.search(r'(?i)(AssertionError|\bFAILED\b|assert )', output):
        kind, action = 'assertion', 'repair_code'
    elif re.search(r'(?i)(not found|no tests|ownership|binding|receipt|selector)', output):
        kind, action = 'verification', 'repair_verification'
    else:
        kind, action = 'unknown', 'diagnose_failure'
    entry = sanitize_evidence({'schema_version': 1, 'evidence_id': digest([phase, redact_incident_text(command), process.returncode,
                termination, normalized_failure(str(failures) or failure_excerpt(output))]),
            'phase': phase, 'candidate_id': candidate, 'workspace': str(root),
            'environment': environment, 'command': command, 'executed_command': getattr(process, 'command', command),
            'returncode': process.returncode, 'termination_reason': termination,
            'failure_kind': kind, 'next_action': action, 'failures': failures,
            'dependencies': [item.to_dict() for item in dependencies],
            'excerpt': failure_excerpt(output), 'artifacts': artifacts})
    if artifacts:
        from .repair_control import atomic_json
        path = Path(next(iter(artifacts.values()))).parent / "failure.json"
        atomic_json(path, entry)
        entry['artifacts']['failure'] = str(path)
    return entry


def prompt_evidence(evidence, *, inline_failures=8):
    """Keep full records on disk and explicitly index details not sent inline."""
    result = []
    for entry in evidence:
        reference = entry.get('artifacts', {}).get('failure') or entry.get('artifacts', {}).get('result')
        if not reference:
            result.append(sanitize_evidence(entry))
            continue
        item = {key: value for key, value in entry.items() if key not in {'result', 'failures'}}
        failures = entry.get('failures', [])
        item.update(complete_evidence_ref=reference, failure_count=len(failures),
                    unread_failure_count=max(0, len(failures) - inline_failures),
                    failures=[{**{key: value for key, value in failure.items() if key != 'detail'},
                               'detail_excerpt': failure_excerpt(failure.get('detail', ''), 800)}
                              for failure in failures[:inline_failures]])
        result.append(sanitize_evidence(item))
    return result


def next_action(evidence):
    active = [item for item in evidence if item.get('phase') != 'baseline' and not item.get('resolved')]
    if not active:
        return {'kind': 'implement', 'evidence_ids': []}
    failure = active[-1]
    return {'kind': failure.get('next_action', 'diagnose_failure'), 'evidence_ids': [failure['evidence_id']],
            'command': failure.get('command', ''), 'completion': 'repeat the failed check on the retained candidate'}


def record_stage_failure(runner, phase, result, role):
    """A successful process can still return a failed domain-level replay."""
    if result.ok or result.payload.get('outcome') == 'deferred':
        return
    from .repair_control import atomic_json
    from .verification_ledger import ledger_root

    snapshot = sanitize_evidence(result.to_dict())
    if phase == "boundary_replay":
        snapshot["execution_artifacts"] = sanitize_evidence(getattr(runner, "_replay_diagnostic_artifacts", []))
    sources = result.payload.get('source_commands', [])
    prior = [item for item in getattr(runner, '_candidate_failure_evidence', [])
             if item.get('command') in sources and not item.get('resolved') and item.get('phase') != 'baseline']
    action = prior[-1]['next_action'] if prior else 'repair_verification'
    if phase == 'diagnosis_differential' and not result.payload.get('base_reproduced'):
        action = 'diagnose_failure'
    identity = digest([phase, normalized_failure(result.summary), result.returncodes])
    entry = {'schema_version': 1, 'evidence_id': identity,
             'phase': 'baseline' if role == 'baseline' else phase,
             'candidate_id': getattr(runner, '_candidate_id', ''),
             'command': prior[-1]['command'] if prior else (sources[-1] if sources else phase),
             'failure_kind': 'verification', 'next_action': action,
             'excerpt': failure_excerpt(result.summary), 'result': snapshot, 'artifacts': {}}
    store = getattr(runner, '_experiment_store', None)
    directory = (store.candidate_root(entry['candidate_id'] or 'diagnostic') / 'verification-evidence'
                 if store else ledger_root() / 'repair-evidence' / 'stages')
    try:
        path = directory / (identity + '.json')
        atomic_json(path, snapshot)
        entry['artifacts']['result'] = str(path)
    except OSError as error:
        entry['artifact_error'] = str(error)
    result.payload['failure_evidence'] = [entry]
    runner._candidate_failure_evidence = [*getattr(runner, '_candidate_failure_evidence', []), entry]
