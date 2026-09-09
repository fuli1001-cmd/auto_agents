"""Make declared pytest ini overrides explicit while preserving precedence.

Some pytest parsers apply ini overrides during configuration discovery, before
reading configuration-file addopts. Forwarding those overrides on the command
line makes collection and execution agree with the frozen selection contract.
Only idempotent ini overrides are repeated; arbitrary addopts are not duplicated.
"""
import os
import re
from pathlib import Path
import shlex


def _overrides(args):
    result = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in {'-o', '--override-ini'} and index + 1 < len(args):
            index += 1
            result += ['-o', args[index]]
        elif value.startswith('--override-ini='):
            result += ['-o', value.partition('=')[2]]
        elif value.startswith('-o') and '=' in value[2:]:
            result += ['-o', value[2:]]
        index += 1
    return result


def compile_ini_overrides(command, cwd, environment=None):
    from .execution_binding import command_spans, executable_tokens
    from .session_verification import _pytest_config_options
    environment = environment or os.environ
    try:
        spans = command_spans(command)
        # Shell chains retain their own cwd and expansion semantics.
        if len(spans) != 1 or any(marker in command for marker in ('$', '`')):
            return command
        tokens = shlex.split(command)
        args = executable_tokens(command)
        prefix = (3 if len(args) >= 3 and args[1:3] == ['-m', 'pytest'] else
                  1 if args and Path(args[0]).name in {'pytest', 'py.test'} else 0)
        if not prefix:
            return command
        executable_index = len(tokens) - len(args)
        if tokens[executable_index:] != args:
            return command
        if tokens[:1] == ['env'] and any(token.startswith('-') for token in tokens[1:executable_index]):
            return command
        env_options = environment.get('PYTEST_ADDOPTS', '')
        for token in tokens[:executable_index]:
            if token.startswith('PYTEST_ADDOPTS='):
                env_options = token.partition('=')[2]
        env_args = shlex.split(env_options)
        selection = env_args + args[prefix:]
        explicit = None
        for index, arg in enumerate(selection):
            if arg in {'-c', '--inifilename', '--config-file'} and index + 1 < len(selection):
                explicit = selection[index + 1]
            elif arg.startswith(('--inifilename=', '--config-file=')):
                explicit = arg.partition('=')[2]
        if explicit:
            candidates = [Path(cwd) / explicit]
        else:
            candidates = []
            directory = Path(cwd)
            while True:
                candidates.extend(directory / name for name in (
                    'pytest.toml', '.pytest.toml', 'pytest.ini', '.pytest.ini',
                    'pyproject.toml', 'tox.ini', 'setup.cfg'))
                if (directory / '.git').exists() or directory.parent == directory:
                    break
                directory = directory.parent
        settings = None
        for path in candidates:
            if path.is_file():
                options = _pytest_config_options(path.name, path.read_text())
                if options is not None:
                    settings = options
                    break
        extra = (settings or {}).get('addopts', [])
        configured = _overrides(shlex.split(extra) if isinstance(extra, str) else list(extra))
        if not configured:
            return command
        # CLI > environment > config, including when only some keys overlap.
        offset = executable_index + prefix
        expanded = tokens[:offset] + configured + _overrides(env_args) + tokens[offset:]
        rendered = []
        for index, token in enumerate(expanded):
            name, separator, value = token.partition('=')
            if index < executable_index and separator and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                rendered.append(name + '=' + shlex.quote(value))
            else:
                rendered.append(shlex.quote(token))
        return ' '.join(rendered)
    except (OSError, ValueError, TypeError):
        return command  # Let the real runner report unsupported/invalid syntax.
