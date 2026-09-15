"""Conservative dependency witnesses; unknown observations never certify reuse."""
import ast
from pathlib import Path
import shlex

from .store import digest


def pytest_parts(command):
    try: args = shlex.split(command)
    except ValueError: return None
    if not args or any(token in args for token in ('&&', '||', ';', '|', '>', '<')): return None
    if Path(args[0]).name == 'pytest': rest = args[1:]
    elif Path(args[0]).name.startswith('python') and args[1:3] == ['-m', 'pytest']: rest = args[3:]
    else: return None
    return rest


def witness(snapshot, unit, runtime):
    """A closed, import-only unit can cross snapshots; everything else is opaque.

    Include all conftest/config files and both branches of local imports. File
    I/O, subprocesses, dynamic imports, environment/clock/random use and package
    introspection prevent a static witness. No runtime tracing is imposed on
    opaque tests solely to increase cache hits.
    """
    root = Path(snapshot)
    parts = pytest_parts(unit.command)
    files, pending, opaque = {}, [], parts is None
    targets = [p.split('::')[0] for p in (parts or []) if p.endswith('.py') or '.py::' in p]
    if not targets: opaque = True
    for target in targets:
        p = root / target
        if p.is_file() and p.resolve().is_relative_to(root.resolve()): pending.append(p)
        else: opaque = True
    configs = [*root.rglob('conftest.py'), *[root / n for n in (
        'pyproject.toml', 'pytest.ini', 'setup.cfg', 'setup.py', 'pytest.toml', '.pytest.toml')]]
    pending.extend(p for p in configs if p.is_file() and '.git' not in p.parts)
    risky = {'os', 'pathlib', 'subprocess', 'importlib', 'inspect', 'random', 'time', 'socket',
             'requests', 'urllib', 'ctypes', 'tempfile', 'sys', 'pkgutil', 'runpy'}
    safe_external = {'pytest', 'typing', 'collections', 'dataclasses', 'enum', 'functools',
                     'itertools', 'math', 'operator', 're', 'json', 'copy', 'abc', 'decimal'}
    while pending:
        path = pending.pop()
        name = str(path.relative_to(root))
        if name in files: continue
        if path.is_symlink(): opaque = True; continue
        text = path.read_text()
        files[name] = digest(text)
        if path.suffix != '.py': continue
        try: tree = ast.parse(text)
        except SyntaxError: opaque = True; continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == 'pytest_plugins': opaque = True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # A fixture argument can expose arbitrary file/network objects
                # without importing pathlib/os in this module.
                chain = node.func
                while isinstance(chain, ast.Attribute): chain = chain.value
                if not (isinstance(chain, ast.Name) and chain.id == 'pytest'): opaque = True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                    'open', 'eval', 'exec', '__import__', 'getattr', 'globals', 'locals', 'compile'):
                opaque = True
            imports = ([a.name for a in node.names] if isinstance(node, ast.Import)
                       else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
            for module in imports:
                if module.split('.')[0] in risky: opaque = True
                relative = module.replace('.', '/')
                locations = [root / 'src' / relative, root / relative, path.parent / relative]
                found = [p for base in locations for p in (base.with_suffix('.py'), base / '__init__.py') if p.is_file()]
                if found: pending.extend(found)
                elif module.split('.')[0] not in safe_external: opaque = True
                if isinstance(node, ast.ImportFrom) and node.level: opaque = True
    return {'complete': not opaque, 'files': files, 'runtime': runtime,
            'command': unit.command, 'profile': unit.profile, 'expected_nodes': list(unit.expected_nodes)}


def cache_key(snapshot_identity, unit, inputs):
    return digest({'version': 2, 'inputs': inputs,
                   'snapshot': None if inputs['complete'] else snapshot_identity})
