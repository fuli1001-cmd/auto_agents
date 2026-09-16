"""Closed static Python witnesses; unprovable execution always binds full source."""
import ast
import hashlib
import os
from pathlib import Path
import shlex

from .store import digest


def pytest_parts(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split = True
        args = list(lexer)
    except ValueError:
        return None
    quote, escaped = None, False
    for char in command:
        if escaped: escaped = False; continue
        if quote == "'":
            if char == "'": quote = None
            continue
        if char == '\\': escaped = True; continue
        if char == '"': quote = None if quote == '"' else '"'; continue
        if char == "'" and quote is None: quote = "'"; continue
        if char in '$`': return None
        if quote is None and char in '*?~[': return None
    if not args or any(token and all(c in ';&|<>' for c in token) for token in args): return None
    if args[0] == 'pytest': return args[1:]
    if args[0] in ('python', 'python3') and args[1:3] == ['-m', 'pytest']: return args[3:]
    return None


def namespace(root):
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [n for n in directories if n not in ('.git', '__pycache__', '.venv', 'node_modules')]
        files.extend((Path(current) / n).relative_to(root).as_posix() for n in names
                     if n.endswith(('.py', '.pth')) or n in ('pyproject.toml', 'pytest.ini', 'setup.cfg', 'pytest.toml', '.pytest.toml'))
        # Adding/removing an importable link or namespace package changes import
        # resolution even when none of the previously read files changed.
        files.extend((Path(current) / n).relative_to(root).as_posix() + '/' for n in directories)
    return sorted(files)


def witness(snapshot, unit, runtime):
    root = Path(snapshot).resolve()
    parts = pytest_parts(unit.command)
    names = namespace(root)
    files, pending, opaque = {}, [], parts is None
    targets = [p.split('::')[0] for p in (parts or []) if p.endswith('.py') or '.py::' in p]
    if not targets: opaque = True
    # Plugins, alternate configuration and arbitrary pytest flags can load code
    # outside the inferred imports. Their results may only reuse the same source.
    if any(p.startswith('-') and p not in ('-q', '-qq', '-v', '-vv', '-x', '--disable-warnings') for p in (parts or [])):
        opaque = True
    pending.extend(root / p for p in targets)
    pending.extend(root / n for n in names if Path(n).name in (
        'conftest.py', 'sitecustomize.py', 'usercustomize.py', 'pyproject.toml',
        'pytest.ini', 'setup.cfg', 'pytest.toml', '.pytest.toml') or n.endswith('.pth'))
    risky = {'os', 'pathlib', 'subprocess', 'importlib', 'inspect', 'random', 'time', 'socket',
             'requests', 'urllib', 'ctypes', 'tempfile', 'sys', 'pkgutil', 'runpy'}
    external = {'pytest', 'typing', 'collections', 'dataclasses', 'enum', 'functools',
                'itertools', 'math', 'operator', 're', 'json', 'copy', 'abc', 'decimal'}
    dangerous = {'open', 'eval', 'exec', '__import__', '__builtins__', 'getattr', 'setattr',
                 'globals', 'locals', 'vars', 'compile', 'input', 'breakpoint', 'pytest_plugins'}

    def add_packages(path):
        for parent in path.parents:
            if parent == root: break
            if root not in parent.parents: break
            if (parent / '__init__.py').is_file(): pending.append(parent / '__init__.py')

    # Once an input is opaque, no amount of further traversal can establish a
    # closed witness. The fallback key already binds the complete snapshot.
    # Walking the entire engine after discovering a fixture/importlib call
    # used to repeat millions of AST operations for every test batch.
    while pending and not opaque:
        path = pending.pop()
        if not path.is_file() or not path.resolve().is_relative_to(root):
            opaque = True; continue
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            opaque = True; continue
        name = path.relative_to(root).as_posix()
        if name in files: continue
        try: text = path.read_text()
        except (OSError, UnicodeError): opaque = True; continue
        files[name] = digest([text, path.stat().st_mode & 0o777])
        add_packages(path)
        if path.suffix != '.py':
            if path.suffix == '.pth': opaque = True
            continue
        try: tree = ast.parse(text)
        except SyntaxError: opaque = True; continue
        parameters = {arg.arg for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                      for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in dangerous: opaque = True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith('pytest_') or node.name.startswith('test_') and (node.args.args or node.args.kwonlyargs):
                    opaque = True
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in parameters: opaque = True
                if isinstance(node.func, ast.Attribute):
                    chain = node.func
                    while isinstance(chain, ast.Attribute): chain = chain.value
                    if not (isinstance(chain, ast.Name) and chain.id == 'pytest'
                            and node.func.attr in ('raises', 'warns', 'parametrize', 'fixture')): opaque = True
            if isinstance(node, ast.Attribute) and node.attr.startswith('__'): opaque = True
            imports = ([a.name for a in node.names] if isinstance(node, ast.Import)
                       else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
            for module in imports:
                if isinstance(node, ast.ImportFrom) and node.level: opaque = True
                if module.split('.')[0] in risky: opaque = True
                relative = module.replace('.', '/')
                locations = [root / 'src' / relative, root / relative, path.parent / relative]
                found = [p for base in locations for p in (base.with_suffix('.py'), base / '__init__.py') if p.is_file()]
                if found:
                    pending.extend(found)
                    for p in found: add_packages(p)
                    if isinstance(node, ast.ImportFrom):
                        for p in found:
                            if p.name == '__init__.py':
                                pending.extend(q for alias in node.names for q in
                                    (p.parent / (alias.name + '.py'), p.parent / alias.name / '__init__.py') if q.is_file())
                elif module.split('.')[0] not in external: opaque = True
    return {'complete': not opaque, 'files': files, 'namespace': digest(names), 'runtime': runtime,
            'command': unit.command, 'profile': unit.profile, 'expected_nodes': list(unit.expected_nodes)}


def cache_key(snapshot_identity, unit, inputs):
    return digest({'version': 3, 'inputs': inputs,
                   'snapshot': None if inputs['complete'] else snapshot_identity})


def execution_fingerprint(root):
    """Bind opaque checks to every copied input, including Git/ignored files.

    This deliberately includes modification times and modes: filesystem and
    Git tests can observe them even when the delivered source bytes match.
    Links are recorded, never traversed. Unsupported entries disable caching.
    """
    root = Path(root)
    info = root.stat()
    entries = {'.': ['directory', info.st_mode & 0o7777, info.st_mtime_ns]}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in sorted([*directories, *files]):
            path = Path(current) / name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if path.is_symlink(): value = ['link', os.readlink(path)]
            elif path.is_file(): value = ['file', hashlib.sha256(path.read_bytes()).hexdigest()]
            elif path.is_dir(): value = ['directory']
            else: return None
            entries[relative] = [*value, info.st_mode & 0o7777, info.st_mtime_ns]
    return digest(entries)
