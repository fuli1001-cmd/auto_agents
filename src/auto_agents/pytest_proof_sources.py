"""Resolve imported pytest bodies using retained source, without importing code."""
from __future__ import annotations

import ast
from fnmatch import fnmatch
import posixpath
import shlex


def effective_options(settings, arguments):
    """Apply pytest's config addopts, environment and command-line order."""
    options = dict(settings)
    addopts = options.get('addopts', [])
    arguments = [*(shlex.split(addopts) if isinstance(addopts, str) else addopts), *arguments]
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg == '--':
            break
        if arg == '--import-mode' and index + 1 < len(arguments):
            index += 1
            options['import_mode'] = arguments[index]
        elif arg.startswith('--import-mode='):
            options['import_mode'] = arg.partition('=')[2]
        value = ''
        if arg in {'-o', '--override-ini'} and index + 1 < len(arguments):
            index += 1
            value = arguments[index]
        elif arg.startswith('--override-ini='):
            value = arg.partition('=')[2]
        elif arg.startswith('-o') and len(arg) > 2:
            value = arg[2:]
        key, separator, value = value.partition('=')
        if separator:
            options[key.strip()] = value
        index += 1
    return options


def option_words(value):
    return shlex.split(value) if isinstance(value, str) else list(value)


def imported_test_sources(seeds, *, options, config_directory, cwd, read_source):
    """Return modules defining collected imports, including re-export chains.

    Business imports used by a test remain ordinary candidate code. Only
    collected exports and their defining modules become immutable proof inputs.
    All reads are supplied by the caller's authenticated historical source.
    """
    patterns = [*option_words(options.get('python_functions', ['test'])),
                *option_words(options.get('python_classes', ['Test']))]
    paths = [posixpath.normpath(posixpath.join(config_directory, path))
             for path in option_words(options.get('pythonpath', []))]
    sources, trees = {}, {}

    def collected(name):
        return any(name.startswith(pattern) or fnmatch(name, pattern) for pattern in patterns)

    def source(path):
        path = posixpath.normpath(path)
        if path.startswith('/') or path == '..' or path.startswith('../'):
            return None
        return read_source(path)

    def tree(path):
        if path not in trees:
            text = source(path)
            if text is None:
                raise ValueError('retained imported test source is unavailable: ' + path)
            trees[path] = ast.parse(text)
        return trees[path]

    def protect(path):
        sources[path] = source(path)

    def module(origin, name, level=0):
        if level:
            base = posixpath.dirname(origin)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            roots = [base]
        else:
            local = posixpath.dirname(origin)
            mode = options.get('import_mode', 'prepend')
            roots = ([local, *paths, cwd, 'src', '.'] if mode == 'prepend'
                     else [*paths, cwd, 'src', '.', *([local] if mode == 'append' else [])])
        for root in dict.fromkeys(roots):
            base = posixpath.normpath(posixpath.join(root, name.replace('.', '/')))
            if base.startswith('/') or base == '..' or base.startswith('../'):
                continue  # External imports remain confined environment inputs.
            # Retain failed local lookups too. A candidate must not replace a
            # shared package with a newly introduced local module/initializer.
            parent = base
            while parent not in {root, '.', ''}:
                for lookup in (parent + '/__init__.py', parent + '.py'):
                    sources.setdefault(lookup, source(lookup))
                parent = posixpath.dirname(parent)
            for path in (base + '/__init__.py', base + '.py'):
                if source(path) is not None:
                    # Importing a submodule also executes package initializers.
                    parent = posixpath.dirname(base)
                    while parent not in {root, '.', ''} and (
                            root == '.' or parent.startswith(root.rstrip('/') + '/')):
                        initializer = parent + '/__init__.py'
                        if source(initializer) is not None:
                            protect(initializer)
                        parent = posixpath.dirname(parent)
                    return path
        # e.g. FastAPI's TestClient and unittest's TestCase are shared imports,
        # not missing repository test bodies. Collection diagnoses unavailable
        # environments; the public executor keeps these dependencies read-only.
        return None

    def bindings(path):
        found, stars = {}, []
        for node in tree(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found[node.name] = node
            elif isinstance(node, (ast.ImportFrom, ast.Import)):
                for alias in node.names:
                    if alias.name == '*':
                        stars.append(node)
                    else:
                        found[alias.asname or alias.name.split('.')[0]] = (node, alias)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Name):
                        found[target.id] = node.value
        return found, stars

    def resolve_attribute(path, value, seen):
        attributes = []
        while isinstance(value, ast.Attribute):
            attributes.insert(0, value.attr)
            value = value.value
        if not isinstance(value, ast.Name):
            return False
        imported = bindings(path)[0].get(value.id)
        if not isinstance(imported, tuple):
            return False
        node, alias = imported
        if isinstance(node, ast.Import):
            # ``import pkg.helpers`` binds pkg; ``import pkg.helpers as h``
            # binds the complete module. Preserve both qualification forms.
            prefix = alias.name if alias.asname else alias.name.split('.')[0]
            level = 0
        else:
            prefix = '.'.join(filter(None, (node.module, alias.name)))
            level = node.level
        target = module(path, '.'.join([prefix, *attributes[:-1]]), level)
        if target is not None:
            resolve(target, attributes[-1], seen)
        elif ((len(attributes) > 1 and module(path, prefix, level) is not None)
              or (isinstance(node, ast.ImportFrom)
                  and module(path, node.module or '', node.level) is not None)):
            # A local object's attributes are not necessarily submodules.
            # Do not silently discard unresolved inherited verification code.
            raise ValueError('retained qualified test export cannot be resolved: '
                             + path + '::' + '.'.join([value.id, *attributes]))
        return True

    def resolve(path, name, seen):
        identity = (path, name)
        if identity in seen:
            raise ValueError('cyclic retained test re-export: ' + path + '::' + name)
        seen = seen | {identity}
        protect(path)
        found, stars = bindings(path)
        value = found.get(name)
        if isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(value, ast.ClassDef):
                for base in value.bases:
                    # Generic parameters do not remove the base's inherited
                    # checks. Follow the defining class without evaluating
                    # __class_getitem__ or importing candidate code.
                    while isinstance(base, ast.Subscript):
                        base = base.value
                    if isinstance(base, ast.Name) and base.id in found:
                        resolve(path, base.id, seen)
                    elif isinstance(base, ast.Attribute):
                        if not resolve_attribute(path, base, seen):
                            raise ValueError('retained class base cannot be resolved: '
                                             + path + '::' + name)
                    elif not isinstance(base, ast.Name):
                        raise ValueError('retained class base cannot be resolved: '
                                         + path + '::' + name)
            return
        if isinstance(value, tuple):
            node, alias = value
            if isinstance(node, ast.ImportFrom):
                target = module(path, node.module or '', node.level)
                if target is not None:
                    resolve(target, alias.name, seen)
                return
        if isinstance(value, ast.Name):
            resolve(path, value.id, seen)
            return
        if isinstance(value, ast.Attribute) and resolve_attribute(path, value, seen):
            return
        if value is None:
            for star in stars:
                target = module(path, star.module or '', star.level)
                if target is None:
                    continue
                exports, nested = bindings(target)
                if name in exports or nested:
                    resolve(target, name, seen)
                    return
            if path.endswith('/__init__.py'):
                # ``from package import test_helpers`` can bind a module,
                # which pytest does not collect as a callable.
                base = posixpath.dirname(path) + '/' + name
                if any(source(candidate) is not None for candidate in
                       (base + '/__init__.py', base + '.py')):
                    return
        if isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
            return  # A test-prefixed data constant is not a collected callable.
        raise ValueError('retained test export cannot be resolved: ' + path + '::' + name)

    def scan(path, seen):
        if path in seen:
            raise ValueError('cyclic retained star import: ' + path)
        found, stars = bindings(path)
        for name, value in found.items():
            if collected(name):
                # Plain definitions are already protected by the source
                # inventory; inspect imports, aliases and inherited classes.
                if (isinstance(value, tuple) and isinstance(value[0], ast.ImportFrom)
                        or isinstance(value, (ast.Name, ast.Attribute, ast.ClassDef))):
                    resolve(path, name, set())
        for star in stars:
            target = module(path, star.module or '', star.level)
            if target is None:
                continue
            protect(target)
            scan(target, seen | {path})

    for path in seeds:
        scan(path, set())
    return sources
