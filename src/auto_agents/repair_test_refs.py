"""Extract executable pytest references from review prose without its punctuation."""
import hashlib
import re
import shlex
from pathlib import PurePosixPath


_REFERENCE = re.compile(
    r'''(?P<quote>[`"'])(?P<quoted>(?:\./)?tests/[^\r\n]*?)(?P=quote)'''
    r'''|(?<![\w/.])(?:\./)?(?P<file>tests/[^\s`"',;()<>:\[\]]+\.py)''')
_NAME = re.compile(r'[\w./-]+')
# The retired projection is used only to identify its persisted output. It must
# never generate new verification commands.
_LEGACY_REFERENCE = re.compile(r'tests/[^\s`"\x27,;]+\.py(?:::[\w\[\]./-]+)*')


def pytest_targets(text, *, prose=True):
    """Quoted references are literal; bare references may end a sentence.

    Parameter IDs can contain punctuation and whitespace. Never trim their
    contents, or turn an incomplete parameter selection into a whole-function
    selection. These references are later shell-quoted by the trusted executor.
    """
    result = []
    consumed = 0
    for match in _REFERENCE.finditer(text):
        if match.start() < consumed:
            continue
        if match.group('quoted') is not None:
            target = match.group('quoted')
            consumed = match.end()
        else:
            end = match.end()
            valid = True
            while text[end:end + 2] == '::':
                name = _NAME.match(text, end + 2)
                if name is None:
                    valid = False
                    break
                end = name.end()
                if prose and text[end:end + 1] not in {'[', ':'}:
                    end -= len(name.group()) - len(name.group().rstrip('.'))
                if end == name.start():
                    valid = False
                    break
                if text[end:end + 1] == '[':
                    depth = 1
                    end += 1
                    while end < len(text) and text[end] not in '\r\n' and depth:
                        depth += (text[end] == '[') - (text[end] == ']')
                        end += 1
                    if depth:
                        valid = False
                        break
            consumed = end
            if not valid:
                continue
            following = text[end:end + 1]
            if following and (following.isalnum() or following in '_/'
                              or following == '.' and text[end + 1:end + 2].isalnum()):
                continue
            target = text[match.start():end]
        target = target.removeprefix('./')
        path = target.split('::', 1)[0]
        if not path.endswith('.py') or '..' in PurePosixPath(path).parts:
            continue
        if target not in result:
            result.append(target)
    return result


def _command_key(command):
    if not isinstance(command, str):
        return ()
    try:
        return tuple(shlex.split(command))
    except (ValueError, TypeError):
        return ()


def _recorded_migrations(experiment):
    result = {}
    for item in getattr(experiment, 'diagnostic_actions', {}).values():
        if item.get('kind') != 'review_command_migration':
            continue
        original, corrected = _command_key(item.get('original_command')), _command_key(item.get('command'))
        if (original[:4] == corrected[:4] == ('python', '-m', 'pytest', '-q')
                and len(original) == len(corrected)
                and all(old == new or old.rstrip('.') == new for old, new in zip(original[4:], corrected[4:]))):
            result[original] = item['command']
    return result


def migrate_review_commands(experiment):
    """Correct only proven legacy projections, retaining the original evidence."""
    aliases = _recorded_migrations(experiment)
    failures = {}
    for record in getattr(experiment, 'candidates', {}).values():
        for evidence in record.failure_evidence:
            if evidence.get('returncode') == 4 and evidence.get('failure_kind') == 'verification':
                failures.setdefault(_command_key(evidence.get('command')), []).append(evidence)
    changed = False
    for finding in getattr(experiment, 'findings', {}).values():
        if (finding.disposition not in {'contract_violation', 'candidate_regression'}
                or getattr(finding, 'causal_obligation_id', '') not in getattr(experiment, 'contract_obligation_ids', [])):
            continue
        legacy = _LEGACY_REFERENCE.findall(finding.required_test)
        corrected = pytest_targets(finding.required_test)
        if (not legacy or legacy == corrected or len(legacy) != len(corrected)
                or not all(old == new or old.rstrip('.') == new for old, new in zip(legacy, corrected))):
            continue
        original = ('python', '-m', 'pytest', '-q', *legacy)
        evidence = [item for item in failures.get(original, []) if any(
            old != new and any(old in line for line in item.get('excerpt', '').splitlines()
                              if line.startswith(('ERROR: not found:', 'ERROR: file or directory not found:')))
            for old, new in zip(legacy, corrected))]
        if not evidence or original in aliases:
            continue
        command = shlex.join(['python', '-m', 'pytest', '-q', *corrected])
        key = 'review-command:' + hashlib.sha256(shlex.join(original).encode()).hexdigest()
        experiment.diagnostic_actions[key] = {
            'kind': 'review_command_migration', 'finding_id': finding.finding_id,
            'source_sha256': hashlib.sha256(finding.required_test.encode()).hexdigest(),
            'original_command': shlex.join(original), 'command': command,
            'evidence_ids': [item.get('evidence_id', '') for item in evidence],
        }
        aliases[original] = command
        changed = True
    previous = list(experiment.sticky_verification_commands)
    experiment.sticky_verification_commands = list(dict.fromkeys(
        aliases.get(_command_key(command), command) for command in previous))
    return changed or previous != experiment.sticky_verification_commands


def review_action(experiment, action):
    """Show the usable command alongside, rather than overwrite, failed evidence."""
    migrate_review_commands(experiment)
    command = action.get('command')
    corrected = _recorded_migrations(experiment).get(_command_key(command))
    if corrected and corrected != command:
        return {**action, 'original_command': command, 'command': corrected,
                'command_source': 'review_command_migration'}
    return action
