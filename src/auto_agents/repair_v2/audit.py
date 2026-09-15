"""Controller-side protection against deleting or bypassing existing tests."""
import ast
from collections import Counter
from pathlib import Path

from .types import RepairBlocked
from .workspace import git


def assertions(text):
    tree = ast.parse(text)
    checks = Counter()
    functions = set()
    bypass = Counter()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
            functions.add(node.name)
        if isinstance(node, ast.Assert): checks[ast.dump(node.test)] += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith('assert') or node.func.attr in ('raises', 'warns'):
                checks[ast.dump(node)] += 1
            if node.func.attr in ('skip', 'skipif', 'xfail'): bypass[ast.dump(node)] += 1
    return functions, checks, bypass


def protect_tests(base_repository, base_commit, candidate):
    candidate = Path(candidate)
    protect_selection(base_repository, base_commit, candidate)
    names = git(base_repository, 'ls-tree', '-r', '--name-only', base_commit, '--', 'tests').splitlines()
    for name in names:
        if not name.endswith('.py'): continue
        original = git(base_repository, 'show', base_commit + ':' + name)
        path = candidate / name
        if not path.is_file() or path.is_symlink():
            raise RepairBlocked('tests_weakened', 'existing regression source removed or replaced by a link: ' + name)
        current = path.read_text()
        if current.strip() == original.strip(): continue
        try:
            old_names, old_checks, old_skip = assertions(original)
            new_names, new_checks, new_skip = assertions(current)
        except SyntaxError as error:
            raise RepairBlocked('tests_invalid', name + ': ' + str(error)) from error
        if not old_names <= new_names or old_checks - new_checks or new_skip - old_skip:
            raise RepairBlocked('tests_weakened', 'existing assertions or test entries weakened: ' + name)


def protect_selection(base_repository, base_commit, candidate):
    """Selection changes must not silently remove existing regression coverage."""
    import configparser
    try: import tomllib
    except ImportError: import tomli as tomllib
    selection = {'addopts', 'testpaths', 'python_files', 'python_classes', 'python_functions', 'norecursedirs'}
    paths = ('pytest.ini', '.pytest.ini', 'pyproject.toml', 'setup.cfg', 'tox.ini')
    def options(name, text):
        if not text.strip(): return {}
        if name == 'pyproject.toml':
            values = tomllib.loads(text).get('tool', {}).get('pytest', {}).get('ini_options', {})
        else:
            parser = configparser.ConfigParser(interpolation=None); parser.read_string(text)
            section = 'tool:pytest' if name == 'setup.cfg' else 'pytest'
            values = dict(parser[section]) if parser.has_section(section) else {}
        return {key: value for key, value in values.items() if key in selection}
    import subprocess
    for name in paths:
        process = subprocess.run(['git', '-C', str(base_repository), 'show', base_commit + ':' + name],
                                 capture_output=True, text=True)
        before = options(name, process.stdout) if process.returncode == 0 else {}
        path = Path(candidate) / name
        after = options(name, path.read_text()) if path.is_file() else {}
        if before != after:
            raise RepairBlocked('tests_weakened', 'regression selection settings changed: ' + name)
