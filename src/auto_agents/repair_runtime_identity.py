"""Controller-owned observations of the engine actually imported by recovery.

This module uses only the standard library so a trusted launcher can retain it
while replacing the business engine's imports. A package version is descriptive;
module origin and loaded code must match the selected source as well.
"""
import hashlib
import importlib
import importlib.metadata
import marshal
import os
from pathlib import Path
import re
import subprocess
import sys
import types


class RuntimeIdentityError(RuntimeError):
    def __init__(self, report):
        super().__init__('loaded recovery engine does not match the selected runtime')
        self.report = report


_MODULES = {
    'auto_agents': (),
    'auto_agents.config': (),
    'auto_agents.cli': ('main',),
    'auto_agents.session': ('Session.resume', 'Session._retain_resume_authority',
                           'Session._phase_fix_execute'),
    'auto_agents.workflow_chain': ('WorkflowStore.resolve_handoff_chain',),
    'auto_agents.repair_client': ('engine_route', '_remember_engine_receipt'),
    'auto_agents.session_verification': (
        '_reference_kind', '_session_reference_kind', '_mandatory_refs', '_owned_inventory',
        # Classification also reads the retained catalog. Checking only its
        # unchanged callers can attest a stale module under the current path.
        '_retained_reference_catalog'),
    'auto_agents.workflow_runtime': ('WorkflowCoordinator._engine_child_id',
        'WorkflowCoordinator._resume_engine_bound_child', 'WorkflowCoordinator._drive_handoff',
        'WorkflowCoordinator._resume_existing_child', 'WorkflowCoordinator._validated_child_handoff',
        'WorkflowCoordinator._resolved_handoff_chain', 'WorkflowCoordinator._session_result',
        'WorkflowCoordinator._resume_session_state', 'WorkflowCoordinator._pending_engine_resume',
        'WorkflowCoordinator._resume_blocked_engine_handoff'),
}


def _code_at(code, names):
    for name in names:
        code = next((item for item in code.co_consts
                     if isinstance(item, types.CodeType) and item.co_name == name), None)
        if code is None:
            return None
    return code


def observe_engine(runtime, *, expected_commit=None):
    runtime = Path(runtime).resolve()
    commit = subprocess.run(['git', '-C', str(runtime), 'rev-parse', 'HEAD'],
                            capture_output=True, text=True, timeout=30)
    report = {'pid': os.getpid(), 'python': sys.executable, 'python_version': sys.version,
              'runtime_root': str(runtime), 'commit': commit.stdout.strip() if not commit.returncode else '',
              'modules': {}, 'mismatches': []}
    if commit.returncode or not report['commit']:
        report['mismatches'].append('commit_unavailable')
    if expected_commit is not None and report['commit'] != expected_commit:
        report['mismatches'].append('commit')
    try:
        report['distribution_version'] = importlib.metadata.version('auto-agents')
    except importlib.metadata.PackageNotFoundError:
        report['distribution_version'] = None
    try:
        manifest = (runtime / 'pyproject.toml').read_text()
        project = manifest.split('[project]', 1)[1].split('\n[', 1)[0]
        match = re.search(r'^version\s*=\s*[\"\x27]([^\"\x27]+)', project, re.MULTILINE)
        report['source_version'] = match.group(1) if match else None
    except (OSError, IndexError):
        report['source_version'] = None
    for name, functions in _MODULES.items():
        expected = runtime / 'src' / Path(*name.split('.'))
        expected = expected / '__init__.py' if name == 'auto_agents' else expected.with_suffix('.py')
        record = report['modules'][name] = {'expected_path': str(expected), 'functions': {}}
        try:
            module = importlib.import_module(name)
            record.update(path=str(getattr(module, '__file__', '')),
                          origin=str(getattr(getattr(module, '__spec__', None), 'origin', '')))
            if (Path(record['path']).resolve() != expected
                    or Path(record['origin']).resolve() != expected):
                raise ValueError('module origin differs from selected source')
            source = expected.read_bytes()
            record['source_sha256'] = hashlib.sha256(source).hexdigest()
            for function in functions:
                value = module
                for part in function.split('.'):
                    value = getattr(value, part, None)
                actual = getattr(value, '__code__', None)
                # Older engines may lack a recovery entrypoint. Report that
                # absence; the actual resume must still prove its behavior.
                compiled = compile(source, str(expected) if actual is None else actual.co_filename,
                                   'exec', dont_inherit=True, optimize=sys.flags.optimize)
                frozen = _code_at(compiled, function.split('.'))
                if actual is None and frozen is None:
                    record['functions'][function] = {'available': False}
                    continue
                matched = (actual is not None and Path(actual.co_filename).resolve() == expected
                           and actual == frozen)
                record['functions'][function] = {
                    'available': actual is not None, 'matches_source': matched,
                    'code_sha256': hashlib.sha256(marshal.dumps(actual)).hexdigest() if actual else None,
                    'code_filename': actual.co_filename if actual else None,
                }
                if not matched:
                    report['mismatches'].append(name + ':' + function)
        except (ImportError, OSError, ValueError, TypeError, SyntaxError) as error:
            record['error'] = str(error)
            report['mismatches'].append(name)
    report['ok'] = not report['mismatches']
    if not report['ok']:
        raise RuntimeIdentityError(report)
    return report
