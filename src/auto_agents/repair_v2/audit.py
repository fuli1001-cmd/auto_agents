"""Controller-side protection against deleting or bypassing existing tests."""
import ast
from collections import Counter
import copy
import itertools
from pathlib import Path

from .types import RepairBlocked
from .workspace import git


class TestProtectionError(RepairBlocked):
    def __init__(self, findings):
        self.findings = findings
        super().__init__(findings[0]['code'], '; '.join(row['reason'] for row in findings))


def _scopes(tree):
    result = {}
    def visit(node, scope):
        result[scope] = node
        def children(parent):
            for child in ast.iter_child_nodes(parent):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(child, (*scope, child.name))
                else: children(child)
        children(node)
    visit(tree, ())
    return result


def _local_nodes(node):
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _local_nodes(child)


def _parameters(node):
    """Recognize literal pytest tables; opaque expressions remain conservative."""
    tables = {}
    for decorator in getattr(node, 'decorator_list', ()):
        if not (isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == 'pytest.mark.parametrize'):
            continue
        keywords = {k.arg: k.value for k in decorator.keywords}
        names = decorator.args[0] if decorator.args else keywords.get('argnames')
        values = decorator.args[1] if len(decorator.args) > 1 else keywords.get('argvalues')
        if values is None: continue
        try:
            names = ast.literal_eval(names)
            names = tuple(n.strip() for n in names.split(',')) if isinstance(names, str) else tuple(names)
        except (ValueError, TypeError, SyntaxError): continue
        if not names or not all(isinstance(n, str) and n for n in names): continue
        rows, domains = Counter(), {name: [] for name in names}
        literal = isinstance(values, (ast.List, ast.Tuple))
        for row in values.elts if literal else ():
            if isinstance(row, ast.Call) and ast.unparse(row.func) == 'pytest.param':
                entries = row.args
            elif len(names) == 1: entries = [row]
            elif isinstance(row, (ast.List, ast.Tuple)): entries = row.elts
            else: literal = False; break
            if len(entries) != len(names): literal = False; break
            rows[tuple(ast.dump(value) for value in entries)] += 1
            for name, value in zip(names, entries):
                try: value = ast.literal_eval(value)
                except (ValueError, TypeError, SyntaxError): value = _UNKNOWN
                if type(value) not in (str, bool, int, float, type(None)): value = _UNKNOWN
                domains[name].append(value)
        indirect = keywords.get('indirect')
        if indirect is not None and not (isinstance(indirect, ast.Constant) and indirect.value is False):
            domains = {}
        tables[names] = {'rows': rows if literal else None, 'syntax': ast.dump(values),
                         'domains': domains if literal else {},
                         'indirect': ast.dump(indirect) if indirect is not None else ast.dump(ast.Constant(False))}
    return tables


_UNKNOWN = object()


def _value(node, bindings):
    if isinstance(node, ast.Name): return bindings.get(node.id, _UNKNOWN)
    if isinstance(node, ast.Constant): return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [_value(item, bindings) for item in node.elts]
        return tuple(values) if all(v is not _UNKNOWN for v in values) else _UNKNOWN
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _value(node.operand, bindings)
        return not value if value is not _UNKNOWN else _UNKNOWN
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = _value(node.left, bindings), _value(node.comparators[0], bindings)
        if left is _UNKNOWN or right is _UNKNOWN: return _UNKNOWN
        op = node.ops[0]
        if isinstance(op, ast.Eq): return left == right
        if isinstance(op, ast.NotEq): return left != right
        if isinstance(op, ast.Is) and (right is None or type(right) is bool): return left is right
        if isinstance(op, ast.IsNot) and (right is None or type(right) is bool): return left is not right
        if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, (str, tuple)):
            try: return (left in right) if isinstance(op, ast.In) else (left not in right)
            except TypeError: pass
    return _UNKNOWN


class _OriginalCases(ast.NodeTransformer):
    def __init__(self, domains): self.domains = domains

    def visit_IfExp(self, node):
        node = self.generic_visit(node)
        names = sorted({n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)})
        if any(n not in self.domains for n in names): return node
        choices = [self.domains[n] for n in names]
        count = 1
        for items in choices: count *= len(items)
        if not count or count > 64: return node
        outcomes = set()
        for values in itertools.product(*choices):
            if any(v is _UNKNOWN for v in values): return node
            result = _value(node.test, dict(zip(names, values)))
            if result is _UNKNOWN: return node
            outcomes.add(bool(result))
        if len(outcomes) == 1: return node.body if outcomes == {True} else node.orelse
        return node


def _additional_bindings(node, tables):
    """An old test's assertions must coexist in one retained parameter row."""
    expressions = [child.test for child in _local_nodes(node) if isinstance(child, ast.Assert)]
    expressions += [child for child in _local_nodes(node) if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and (child.func.attr.startswith('assert') or child.func.attr in ('raises', 'warns'))]
    names = {n.id for expression in expressions for n in ast.walk(expression) if isinstance(n, ast.Name)}
    choices = []
    for table in tables:
        relevant = sorted(names & table.keys())
        if relevant:
            rows = [dict(zip(relevant, values)) for values in zip(*(table[n] for n in relevant))]
            if any(v is _UNKNOWN for row in rows for v in row.values()): return [{}]
            choices.append(rows)
    count = 1
    for rows in choices: count *= len(rows)
    if not count or count > 64: return [{}]
    return [{name: value for row in rows for name, value in row.items()}
            for rows in itertools.product(*choices)]


def _checks(node, domains, bindings=None):
    checks, bypass = Counter(), Counter()
    # A reassigned parameter is no longer a static description of an old case.
    assigned = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
    normalizer = _OriginalCases({k: v for k, v in domains.items() if k not in assigned})
    bindings = {k: v for k, v in (bindings or {}).items() if k not in assigned}
    class Substitute(ast.NodeTransformer):
        def visit_Name(self, node):
            return ast.Constant(bindings[node.id]) if node.id in bindings else node
    def assertion(expression):
        expression = normalizer.visit(Substitute().visit(copy.deepcopy(expression)))
        def conjuncts(value):
            if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.And):
                return tuple(part for child in value.values for part in conjuncts(child))
            return (ast.unparse(value),)
        checks[conjuncts(expression)] += 1
    nodes = list(_local_nodes(node))
    called = {id(child.func) for child in nodes if isinstance(child, ast.Call)}
    for child in nodes:
        if isinstance(child, ast.Assert): assertion(child.test)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr.startswith('assert') or child.func.attr in ('raises', 'warns'):
                assertion(child)
            if child.func.attr in ('skip', 'skipif', 'xfail'):
                bypass[ast.unparse(child)] += 1
        if isinstance(child, ast.Attribute) and child.attr in ('skip', 'skipif', 'xfail') and id(child) not in called:
            bypass[ast.unparse(child)] += 1
    return checks, bypass


def _missing_checks(original, current):
    """Each original assertion must run first, in its original short-circuit order.

    Appending checks is strengthening; inserting/reordering them can change
    state before the retained predicate runs. One new assertion cannot stand
    in for several original assertions (which may evaluate side effects twice).
    """
    remaining, missing = current.copy(), Counter()
    for check, count in sorted(original.items(), key=lambda item: -len(item[0])):
        for _ in range(count):
            matches = [new for new, available in remaining.items()
                       if available and new[:len(check)] == check]
            if matches:
                remaining[min(matches, key=len)] -= 1
            else:
                missing[repr(check)] += 1
    return missing


def _changed_checks(original, current):
    old_scopes, new_scopes = _scopes(ast.parse(original)), _scopes(ast.parse(current))
    changes = []
    for scope, before in old_scopes.items():
        after = new_scopes.get(scope)
        label = '.'.join(scope) or '<module>'
        if after is None:
            if _checks(before, {})[0] or scope and scope[-1].startswith('test_'):
                changes.append({'scope': label, 'missing_entry': True})
            continue
        old_tables, new_tables = _parameters(before), _parameters(after)
        domains = {}
        for names, table in old_tables.items():
            newer = new_tables.get(names)
            preserved = newer is not None and newer['indirect'] == table['indirect'] and (newer['syntax'] == table['syntax'] or
                table['rows'] is not None and newer['rows'] is not None and not table['rows'] - newer['rows'])
            if not preserved:
                changes.append({'scope': label, 'missing_parameter_cases': list(names)})
            else: domains.update(table['domains'])
        old_checks, old_skip = _checks(before, domains)
        old_variables = {n.id for n in ast.walk(before) if isinstance(n, ast.Name)}
        old_variables.update(n.arg for n in ast.walk(before) if isinstance(n, ast.arg))
        added_tables = [table['domains'] for names, table in new_tables.items()
                        if not old_variables.intersection(names)]
        new_checks, new_skip = _checks(after, domains)
        if _missing_checks(old_checks, new_checks) and added_tables:
            variants = [_checks(after, domains, bindings)[0] for bindings in _additional_bindings(after, added_tables)]
            new_checks = min(variants, key=lambda checks: sum(_missing_checks(old_checks, checks).values()))
        missing, bypass = _missing_checks(old_checks, new_checks), new_skip - old_skip
        if missing or bypass:
            changes.append({'scope': label, 'missing_assertions': dict(missing), 'added_bypass': dict(bypass)})
    # New helpers must not introduce module-wide skipping either.
    for scope in new_scopes.keys() - old_scopes.keys():
        bypass = _checks(new_scopes[scope], {})[1]
        if bypass: changes.append({'scope': '.'.join(scope), 'added_bypass': dict(bypass)})
    return changes


def protect_tests(base_repository, base_commit, candidate):
    findings = test_protection_findings(base_repository, base_commit, candidate)
    if findings: raise TestProtectionError(findings)


def test_protection_findings(base_repository, base_commit, candidate):
    """Collect every actionable failure before spending another model turn."""
    candidate = Path(candidate)
    findings = []
    def record(code, name, reason, **details):
        findings.append({'unit': 'test-preservation:' + name, 'code': code, 'path': name,
                         'baseline': base_commit, 'reason': reason + ': ' + name, **details})
    try: protect_selection(base_repository, base_commit, candidate)
    except RepairBlocked as error:
        record(error.code, '<selection>', str(error))
    names = git(base_repository, 'ls-tree', '-r', '--name-only', base_commit, '--', 'tests').splitlines()
    for name in names:
        if not name.endswith('.py'): continue
        original = git(base_repository, 'show', base_commit + ':' + name)
        path = candidate / name
        linked = path.is_symlink() or any(p.is_symlink() for p in path.parents
                                         if p != candidate and candidate in p.parents)
        if not path.is_file() or linked:
            record('tests_weakened', name, 'existing regression source removed or replaced by a link')
            continue
        try: current = path.read_text()
        except UnicodeError as error:
            record('tests_invalid', name, str(error))
            continue
        if current.strip() == original.strip(): continue
        try:
            changes = _changed_checks(original, current)
        except SyntaxError as error:
            record('tests_invalid', name, str(error))
            continue
        if changes:
            record('tests_weakened', name, 'existing assertions, test entries or parameter cases changed', changes=changes)
    return findings


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
        if not isinstance(values, dict): raise ValueError('pytest ini_options must be a table')
        return {key: value for key, value in values.items() if key in selection}
    import subprocess
    for name in paths:
        process = subprocess.run(['git', '-C', str(base_repository), 'show', base_commit + ':' + name],
                                 capture_output=True, text=True)
        path = Path(candidate) / name
        if path.is_symlink():
            raise RepairBlocked('tests_weakened', 'regression selection file replaced by a link: ' + name)
        try:
            before = options(name, process.stdout) if process.returncode == 0 else {}
            after = options(name, path.read_text()) if path.is_file() else {}
        except (ValueError, configparser.Error) as error:
            raise RepairBlocked('tests_invalid', 'invalid regression selection configuration: ' + name + ': ' + str(error)) from error
        if before != after:
            raise RepairBlocked('tests_weakened', 'regression selection settings changed: ' + name)
