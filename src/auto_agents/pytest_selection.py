"""Observe filtered pytest selection on retained source without running tests."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

from .execution_binding import RunnerContextError


class CandidateSelectionError(RunnerContextError):
    """The authenticated candidate lacks explicitly required exact nodes."""


PLUGIN = '''import json
from pathlib import Path
import pytest

DESELECTED = []
COLLECTION_ERRORS = []

def pytest_collectreport(report):
    if report.failed:
        COLLECTION_ERRORS.append(report.nodeid)

def pytest_addoption(parser):
    parser.addoption('--auto-agents-selection-report')

def pytest_deselected(items):
    DESELECTED.extend(items)

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    root = Path(session.config.getoption('--auto-agents-selection-report')).parent
    def nodes(items):
        result = []
        for item in items:
            path = Path(item.path).resolve().relative_to(root).as_posix()
            suffix = item.nodeid.partition('::')[2]
            result.append(path + ('::' + suffix if suffix else ''))
        return result
    Path(session.config.getoption('--auto-agents-selection-report')).write_text(
        json.dumps({'version': 1, 'exitstatus': int(exitstatus),
                    'selected': nodes(session.items), 'deselected': nodes(DESELECTED),
                    'collection_errors': COLLECTION_ERRORS}))
'''


def _without_missing_targets(invocation, missing, environment):
    """Recollect retained nodes under the same options, omitting only absent exact targets."""
    from .execution_binding import RunnerContextError, test_invocations

    omitted = [raw for raw, reference in zip(invocation.targets, invocation.repository_targets)
               if reference in missing]
    words = shlex.split(invocation.raw[invocation.option_offset:])
    if not omitted or any(words.count(target) != 1 for target in omitted):
        raise RunnerContextError('discovery', 'missing pytest targets cannot be isolated safely', invocation.raw)
    for target in omitted:
        words.remove(target)
    command = invocation.raw[:invocation.option_offset].rstrip() + ' ' + shlex.join(words)
    parsed = test_invocations(command, environment=environment)
    expected_args = list(invocation.arguments)
    for target in omitted:
        expected_args.remove(target)
    expected_targets = tuple(ref for ref in invocation.repository_targets if ref not in missing)
    if (len(parsed) != 1 or parsed[0].runner != 'pytest'
            or parsed[0].cwd != invocation.cwd or parsed[0].shell_cwd != invocation.shell_cwd
            or parsed[0].arguments != tuple(expected_args)
            or tuple(parsed[0].repository_targets) != expected_targets):
        raise RunnerContextError('discovery', 'retained pytest options changed during collection', invocation.raw)
    return parsed[0]


def selected_nodes(session, state, invocation, *, expected_missing=(), candidate=False):
    from .execution_binding import prepare_conda_prefix, prepare_dependency_scratch, RunnerContextError
    from .gate_execution import discover_dependency_links
    from .session_candidate import _clone
    from .session_verification import _contract_source_revision, fingerprint
    from .verification_context import current_context
    from .verification_sandbox import verification_argv
    from .execution_recovery import redact_incident_text

    root = Path(getattr(session, '_retained_source_root', session.project_root))
    revision = _contract_source_revision(session, state) or 'HEAD'
    if candidate:
        from .session_candidate import validate_receipt
        from .session_source import validate_checkout
        validate_receipt(state)
        custody = state.candidate_custody
        root = Path(custody['checkout'])
        validate_checkout(getattr(session, '_custody_control_root', session.project_root), state, root)
        revision = custody['receipt']['source_revision']
    expected_missing = frozenset(expected_missing)
    environment = dict(current_context(session, state).environment)
    key = fingerprint([str(root), revision, invocation.raw, invocation.cwd,
                       invocation.shell_cwd, environment, PLUGIN, sorted(expected_missing), candidate])
    cache = getattr(session, '_pytest_selection_cache', None)
    if cache is None:
        cache = session._pytest_selection_cache = {}
    if key in cache:
        return cache[key]
    try:
        with tempfile.TemporaryDirectory(prefix='auto-agents-pytest-selection-') as temporary:
            base = Path(temporary)
            checkout = base / 'project'
            _clone(root, revision, checkout)
            plugin = base / 'observer'; plugin.mkdir()
            (plugin / '_auto_agents_selection.py').write_text(PLUGIN)
            report = checkout / '.auto-agents-selection.json'
            prefix, conda_sources = prepare_conda_prefix(
                invocation.raw[:invocation.option_offset], checkout,
                checkout / invocation.shell_cwd, environment=environment)
            command = (prefix + ' --collect-only -p _auto_agents_selection'
                       + ' --auto-agents-selection-report=' + shlex.quote(str(report))
                       + ' ' + invocation.raw[invocation.option_offset:])
            prepare_dependency_scratch(checkout)
            # Add only the trusted observer; preserve the retained command,
            # config, launcher, environment filters and working directory.
            environment['PYTHONPATH'] = os.pathsep.join(filter(None, (
                str(plugin), environment.get('PYTHONPATH', ''))))
            shell = 'cd ' + shlex.quote(str(checkout / invocation.shell_cwd)) + ' && ' + command
            with verification_argv(['sh', '-c', shell], checkout, root,
                    read_roots=[plugin, *discover_dependency_links(root).values(), *conda_sources],
                    execution_environment=environment,
                    gate_environment_overrides={'TMPDIR': str(checkout), 'TMP': str(checkout), 'TEMP': str(checkout)}) as argv:
                result = subprocess.run(argv, cwd=checkout, env=environment,
                                        capture_output=True, text=True, timeout=60)
            missing = frozenset()
            try:
                observed = json.loads(report.read_text())
            except (OSError, ValueError):
                observed = {}
            valid = (observed.get('version') == 1 and observed.get('exitstatus') == result.returncode
                     and all(isinstance(observed.get(key), list)
                             and all(isinstance(node, str) for node in observed[key])
                             for key in ('selected', 'deselected', 'collection_errors')))
            eligible_missing = invocation.repository_targets if candidate else expected_missing
            if result.returncode == 4 and eligible_missing and valid and not observed['collection_errors']:
                expected_paths = {
                    str(checkout / ref.split('::', 1)[0]) + '::' + ref.split('::', 1)[1]: ref
                    for ref in eligible_missing if '::' in ref
                }
                errors = re.findall(r'^ERROR: not found: (.+)$', result.stderr, re.MULTILINE)
                other_errors = [line for line in result.stderr.splitlines()
                                if line.startswith('ERROR:') and not line.startswith('ERROR: not found: ')]
                if errors and not other_errors and all(error.strip() in expected_paths for error in errors):
                    missing = frozenset(expected_paths[error.strip()] for error in errors)
            if result.returncode not in (0, 5) and (not missing or candidate):
                kind = 'candidate_selection' if candidate and missing else 'discovery'
                error_type = CandidateSelectionError if kind == 'candidate_selection' else RunnerContextError
                reason = 'retained pytest selection failed'
                if kind == 'candidate_selection':
                    reason += '; candidate is missing required tests: ' + ', '.join(sorted(missing))
                error = error_type(kind, reason, invocation.raw)
                error.diagnostic.update(returncode=result.returncode,
                    phase='runner_discovery',
                    stdout_tail=redact_incident_text(result.stdout)[-2000:],
                    stderr_tail=redact_incident_text(result.stderr)[-2000:])
                if kind == 'candidate_selection':
                    error.diagnostic['missing_nodes'] = sorted(missing)
                raise error
            if not valid:
                raise ValueError('invalid pytest selection report')
            selected = (frozenset(observed['selected']), frozenset(observed['deselected']))
            if missing:
                retained = _without_missing_targets(invocation, missing, environment)
                selected = selected_nodes(session, state, retained)
            if expected_missing:
                selected = (*selected, missing)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise RunnerContextError('discovery', 'retained pytest selection unavailable: ' + str(error),
                                 invocation.raw) from error
    cache[key] = selected
    return selected
