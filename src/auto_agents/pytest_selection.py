"""Observe filtered pytest selection on retained source without running tests."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


PLUGIN = '''import json
from pathlib import Path
import pytest

DESELECTED = []

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
                    'selected': nodes(session.items), 'deselected': nodes(DESELECTED)}))
'''


def selected_nodes(session, state, invocation):
    from .execution_binding import prepare_conda_prefix, prepare_dependency_scratch, RunnerContextError
    from .gate_execution import discover_dependency_links
    from .session_candidate import _clone
    from .session_verification import _contract_source_revision, fingerprint
    from .verification_context import current_context
    from .verification_sandbox import verification_argv
    from .execution_recovery import redact_incident_text

    root = Path(getattr(session, '_retained_source_root', session.project_root))
    revision = _contract_source_revision(session, state) or 'HEAD'
    environment = dict(current_context(session, state).environment)
    key = fingerprint([str(root), revision, invocation.raw, invocation.cwd,
                       invocation.shell_cwd, environment, PLUGIN])
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
            if result.returncode not in (0, 5):
                error = RunnerContextError('discovery', 'retained pytest selection failed', invocation.raw)
                error.diagnostic.update(returncode=result.returncode,
                    stdout_tail=redact_incident_text(result.stdout)[-2000:],
                    stderr_tail=redact_incident_text(result.stderr)[-2000:])
                raise error
            observed = json.loads(report.read_text())
            if (observed.get('version') != 1 or observed.get('exitstatus') != result.returncode
                    or any(not isinstance(observed.get(key), list)
                           or not all(isinstance(node, str) for node in observed[key])
                           for key in ('selected', 'deselected'))):
                raise ValueError('invalid pytest selection report')
            selected = (frozenset(observed['selected']), frozenset(observed['deselected']))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise RunnerContextError('discovery', 'retained pytest selection unavailable: ' + str(error),
                                 invocation.raw) from error
    cache[key] = selected
    return selected
