"""Conservative command rewriting and repository-bound workflow preflight."""
from __future__ import annotations

import re
import shlex
import os
import stat
import posixpath
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


class ExecutionBindingError(ValueError):
    """A command cannot be executed by the currently bound repository."""


# Only these derived fields may change during this inventory transition.
# Tasks (including requirement hashes), task_scope, handoff and source authority
# stay fixed. task_ids/requirement_ids summarize proof owners, including retained
# prerequisites; enriching those summaries does not grant new task authority.
_INVENTORY_FIELDS = frozenset({
    'schema_version', 'binding_fingerprint', 'proof_inventory_version', 'proof_graph',
    'required_references', 'required_proof_ids', 'proof_owners', 'required_commands',
    'required_proofs', 'regression_dependencies', 'verification_policy',
    'proof_control_paths', 'proof_config_paths', 'proof_sources',
    'task_ids', 'requirement_ids',
})


def _custody_identity(custody):
    return {key: value for key, value in custody.items()
            if key not in {'binding_migration', 'receipt', 'delivered_revision'}}


def _unchanged_authority(old, new):
    return all(new.get(key) == value for key, value in old.items()
               if key not in _INVENTORY_FIELDS)


def bridge_inventory_upgrade(state, original):
    """Seal one validated enrichment without rewriting custody or writer identity."""
    from copy import deepcopy
    from .session_verification import fingerprint, ownership_error

    binding, custody = state.verification_binding, state.candidate_custody
    if not _unchanged_authority(original, binding):
        raise ownership_error(state, 'inventory migration changed retained candidate authority')
    # A later parser inventory can follow an earlier recovery. Validate that
    # bridge against its original binding before extending the same custody.
    from copy import copy
    previous = copy(state)
    previous.verification_binding = original
    validate_custody_binding(previous)
    if custody.get('binding_migration'):
        original = custody['binding_migration']['original_binding']
    bridge = {'schema_version': 1, 'original_binding': deepcopy(original),
              'inventory_fingerprint': binding['binding_fingerprint'],
              'custody_identity': fingerprint(_custody_identity(custody)),
              'receipt': deepcopy(custody.get('receipt'))}
    bridge['fingerprint'] = fingerprint(bridge)
    custody['binding_migration'] = bridge
    validate_custody_binding(state)


def validate_custody_binding(state):
    """Shared admission check for execution, receipts, source and delivery."""
    from .session_verification import fingerprint, ownership_error

    binding, custody = state.verification_binding, state.candidate_custody
    if not custody:
        return
    if (custody.get('session_id') != state.session_id
            or custody.get('repository') != binding.get('repository')):
        raise ownership_error(state, 'candidate custody conflicts with session authority')
    current = binding.get('binding_fingerprint')
    bridge = custody.get('binding_migration')
    if not bridge:
        if custody.get('binding_fingerprint') != current:
            raise ownership_error(state, 'candidate custody conflicts with session authority')
        return
    old = bridge.get('original_binding', {})
    if (bridge.get('schema_version') != 1
            or bridge.get('fingerprint') != fingerprint({k: v for k, v in bridge.items() if k != 'fingerprint'})
            or old.get('binding_fingerprint') != fingerprint({k: v for k, v in old.items() if k != 'binding_fingerprint'})
            or old.get('binding_fingerprint') != custody.get('binding_fingerprint')
            or bridge.get('inventory_fingerprint') != current
            or current != fingerprint({k: v for k, v in binding.items() if k != 'binding_fingerprint'})
            or any(binding.get(key) != expected for key, expected in (
                ('session_id', state.session_id), ('workflow_id', state.workflow_id),
                ('original_handoff_id', state.parent_handoff_id),
                ('authorization', state.authorization_policy),
                ('execution_environment', state.goal_execution_environment), ('session_mode', state.mode)))
            or not _unchanged_authority(old, binding)
            or bridge.get('custody_identity') != fingerprint(_custody_identity(custody))):
        raise ownership_error(state, 'candidate inventory migration bridge conflicts with retained authority')
    receipt = custody.get('receipt')
    if bridge.get('receipt') and not receipt:
        raise ownership_error(state, 'candidate inventory migration lost its retained receipt')
    if receipt and receipt.get('binding_fingerprint') != current and receipt != bridge.get('receipt'):
        raise ownership_error(state, 'candidate inventory migration bridge does not identify this receipt')


@contextmanager
def anchored_parent(root: Path, relative: str):
    """Open a repository path's parent without traversing any symlinks."""
    parts = relative.split('/')
    if not parts or any(part in {'', '.', '..'} for part in parts):
        raise ExecutionBindingError('invalid private repository path')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def restore_private_modes(root: Path, modes: Mapping[str, int]) -> None:
    """Restore snapshot permissions only on freshly materialized private inodes."""
    for relative, mode in modes.items():
        with anchored_parent(root, relative) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not (stat.S_ISDIR(info.st_mode) or
                        (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                    raise ExecutionBindingError('snapshot mode target is not a private inode')
                os.fchmod(descriptor, mode)
            finally:
                os.close(descriptor)


@dataclass(frozen=True)
class SessionExecutionBinding:
    """An execution location authorized against an existing session contract.

    This engine-created context travels with a private checkout; a copied
    session file or a change of cwd alone grants no execution authority.
    """

    repository: str
    execution_root: str
    session_id: str
    binding_fingerprint: str
    source_revision: str

    @classmethod
    def for_checkout(cls, session, state, execution_root: Path):
        from .session_verification import bind_session, validate_binding

        bind_session(session, state)
        validate_binding(session, state)
        validate_custody_binding(state)
        binding = state.verification_binding
        return cls(binding['repository'], str(execution_root.resolve()), state.session_id,
                   binding['binding_fingerprint'], _session_source_revision(state))


def _session_source_revision(state) -> str:
    binding = state.verification_binding
    validate_custody_binding(state)
    revision = binding.get('contract_revision', '')
    if not revision:
        custody = state.candidate_custody
        if (custody.get('initial_source') and custody.get('session_id') == state.session_id
                and custody.get('repository') == binding.get('repository')):
            revision = custody.get('base_revision', '')
    return revision


def session_execution_error(session, state) -> str:
    binding = state.verification_binding
    root = str(session.project_root.resolve())
    context = getattr(session, '_execution_binding', None)
    if context is None:
        return ('' if binding.get('repository') == root else
                'session verification binding belongs to another repository')
    if binding.get('schema_version', 1) < 13:
        return 'private execution requires an upgraded canonical session binding'
    if (not isinstance(context, SessionExecutionBinding)
            or context.repository != binding.get('repository')
            or context.execution_root != root
            or context.session_id != state.session_id
            or context.binding_fingerprint != binding.get('binding_fingerprint')
            or not context.source_revision
            or context.source_revision != _session_source_revision(state)):
        return 'private execution context conflicts with session verification binding'
    import subprocess

    source = subprocess.run(['git', 'rev-parse', '--verify', f'{context.source_revision}^{{commit}}'],
                            cwd=session.project_root, capture_output=True, text=True)
    if source.returncode or source.stdout.strip() != context.source_revision:
        return 'private execution checkout lacks retained source provenance'
    return ''


def command_spans(command: str) -> list[tuple[int, int]]:
    """Locate simple commands without changing quoting or shell expansion.

    Nested shell programs are deliberately unsupported; never rewrite them as
    though their strings or substitutions were top-level executables.
    """
    spans = []
    start = index = 0
    quote = ""
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and (char == "`" or command.startswith("$(", index)):
                raise ExecutionBindingError("nested shell expansion requires an explicit verification script")
        elif char in "\"'":
            quote = char
        elif char in "()`":
            raise ExecutionBindingError("nested shell syntax requires an explicit verification script")
        elif char == "#" and (index == 0 or command[index - 1].isspace()):
            spans.append((start, index))
            end = command.find("\n", index)
            if end < 0:
                return spans
            start = index = end + 1
            continue
        elif char in ";|&\n" and not (char == "&" and index and command[index - 1] in "<>"):
            spans.append((start, index))
            while index + 1 < len(command) and command[index + 1] == char:
                index += 1
            start = index + 1
        index += 1
    if quote:
        raise ExecutionBindingError("unclosed shell quote")
    spans.append((start, len(command)))
    return spans


def rewrite_simple_commands(command: str, transform: Callable[[str], str]) -> str:
    spans = command_spans(command)
    for start, end in reversed(spans):
        raw = command[start:end]
        stripped = raw.rstrip()
        command = command[:start] + transform(stripped) + raw[len(stripped):] + command[end:]
    return command


def executable_tokens(command: str) -> list[str]:
    """Peel only known launch wrappers, never arbitrary argument substrings."""
    tokens = shlex.split(command)
    while tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        elif tokens[0] in {"exec", "env"}:
            tokens.pop(0)
        elif Path(tokens[0]).name == "conda" and tokens[1:2] == ["run"]:
            tokens = tokens[2:]
            while tokens and tokens[0].startswith("-"):
                option = tokens.pop(0)
                if option == "--":
                    break
                if option in {"-p", "--prefix", "-n", "--name", "--cwd"}:
                    tokens = tokens[1:]
        else:
            break
    return tokens


# Selection is admitted only for options whose arity is known. Keep this
# inventory shared by reference resolution, path validation and diagnostics.
PYTEST_VALUE_OPTIONS = frozenset({
    '-c', '--inifilename', '--config-file', '-o', '--override-ini', '-k', '-m', '-p',
    '--confcutdir', '--durations', '--durations-min', '--ignore', '--ignore-glob',
    '--deselect', '--junitxml', '--junit-xml', '--junit-prefix', '--log-file',
    '--maxfail', '--rootdir', '--basetemp', '--import-mode', '--assert', '--tb',
    '--capture', '--color', '--code-highlight', '--show-capture', '--verbosity',
    '--log-level', '--log-format', '--log-date-format', '--log-cli-level',
    '--log-cli-format', '--log-cli-date-format', '--log-file-level',
    '--log-file-format', '--log-file-date-format', '--log-file-mode', '--log-disable',
    '--override-toml', '-W', '--pythonwarnings', '--doctest-glob', '--doctest-report',
    '--pastebin', '-r', '--debug',
})
_PYTEST_FLAGS = frozenset({
    '-q', '--quiet', '-v', '--verbose', '-s', '-x', '--exitfirst', '-l', '--showlocals',
    '--no-showlocals', '--collect-only', '--co', '--continue-on-collection-errors',
    '--pyargs', '--noconftest', '--keep-duplicates', '--keepduplicates',
    '--collect-in-virtualenv', '--doctest-modules', '--doctest-ignore-import-errors',
    '--doctest-continue-on-failure', '--strict', '--strict-config', '--strict-markers',
    '--disable-warnings', '--disable-pytest-warnings', '--no-header', '--no-summary',
    '--full-trace', '--pdb', '--trace', '--runxfail', '--stepwise', '--sw',
    '--stepwise-skip', '--sw-skip', '--lf', '--last-failed', '--ff', '--failed-first',
    '--nf', '--new-first', '--cache-clear', '--setup-only', '--setup-show',
    '--setup-plan', '--fixtures', '--fixtures-per-test', '--funcargs', '--version',
    '--help', '-h', '--trace-config',
})
_VITEST_VALUES = frozenset({
    '-t', '--testNamePattern', '--test-name-pattern', '-c', '--config', '-r', '--root',
    '--dir', '--reporter', '--outputFile', '--maxWorkers', '--minWorkers', '--pool',
    '--environment', '--exclude', '--project', '--testTimeout', '--hookTimeout',
    '--retry', '--bail', '--shard',
})
_VITEST_FLAGS = frozenset({'--run', '--no-cache', '--no-file-parallelism', '--passWithNoTests'})


def _runner_targets(runner, args, redirections=()):
    values = PYTEST_VALUE_OPTIONS if runner == 'pytest' else _VITEST_VALUES
    flags = _PYTEST_FLAGS if runner == 'pytest' else _VITEST_FLAGS
    # Shell redirection destinations are not passed to the runner. Remove
    # their known syntax before interpreting the runner's '--' delimiter.
    positional_args = []
    shell_index = 0
    while shell_index < len(args):
        arg = args[shell_index]
        if shell_index in redirections:
            if re.fullmatch(r'\d*(?:[<>]|>>|<>|>&|<&)', arg):
                shell_index += 1
                if shell_index == len(args):
                    return None
        else:
            positional_args.append(arg)
        shell_index += 1
    args = positional_args
    targets = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            return [*targets, *args[index + 1:]]
        if not arg.startswith('-'):
            targets.append(arg)
        else:
            option, equals, _ = arg.partition('=')
            if runner == 'pytest' and option in {'-r', '--debug', '--cache-show'}:
                if not equals and index + 1 < len(args) and not args[index + 1].startswith('-'):
                    index += 1
            elif option in values or runner == 'vitest' and option.startswith('--outputFile.'):
                if not equals:
                    index += 1
                    if index == len(args):
                        return None
            elif runner == 'pytest' and len(arg) > 2 and arg[:2] in {'-k', '-m', '-o', '-c', '-p', '-W'}:
                pass  # Attached single-value short options.
            elif runner == 'pytest' and re.fullmatch(r'-[qvsxl]+', arg):
                pass
            elif runner == 'pytest' and arg.startswith('-r') and not arg.startswith('--'):
                pass  # Pytest's optional, attached report selector.
            elif option not in flags:
                return None  # Unknown arity cannot establish executable coverage.
        index += 1
    return targets


@dataclass(frozen=True)
class TestInvocation:
    raw: str
    runner: str
    arguments: tuple[str, ...]
    targets: tuple[str, ...] | None
    cwd: str
    option_offset: int
    shell_cwd: str = '.'

    @property
    def repository_targets(self):
        return [posixpath.normpath(posixpath.join(self.cwd, target.split('::', 1)[0]))
                + ('::' + target.split('::', 1)[1] if '::' in target else '')
                for target in self.targets or ()]


def test_invocations(command: str) -> list[TestInvocation]:
    """Parse retained shell context without evaluating shell or runner code.

    None targets means unknown option arity, distinct from default discovery.
    option_offset addresses the runner boundary in the original shell text,
    so preflight preserves quoting and distinguishes launcher and runner '--'.
    """
    invocations = []
    cwd = '.'
    for start, end in command_spans(command):
        raw = command[start:end].strip()
        tokens = shlex.split(raw)
        args = executable_tokens(raw)
        if not args:
            continue
        if args[:1] == ['cd'] and len(args) == 2:
            cwd = posixpath.normpath(posixpath.join(cwd, args[1]))
            continue
        invocation_cwd = cwd
        prefix = tokens[:len(tokens) - len(args)]
        for index, token in enumerate(prefix):
            if token == '--cwd' and index + 1 < len(prefix):
                invocation_cwd = posixpath.normpath(posixpath.join(cwd, prefix[index + 1]))
            elif token.startswith('--cwd='):
                invocation_cwd = posixpath.normpath(posixpath.join(cwd, token.partition('=')[2]))
        name = Path(args[0]).name
        if name in {'pytest', 'py.test'}:
            runner, options = 'pytest', args[1:]
        elif re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', name) and args[1:3] == ['-m', 'pytest']:
            runner, options = 'pytest', args[3:]
        else:
            if name in {'npm', 'pnpm', 'yarn'} and args[1:2] in (['exec'], ['dlx']):
                args = args[2:]
                if args[:1] == ['--']:
                    args = args[1:]
            elif name == 'npx':
                args = args[1:]
                while args[:1] in (['--yes'], ['-y'], ['--no-install'], ['--']):
                    args = args[1:]
            elif name in {'pnpm', 'yarn'}:
                args = args[1:]
            if not args or Path(args[0]).name != 'vitest':
                continue
            runner, options = 'vitest', args[1:]
            if options[:1] == ['run']:
                options = options[1:]
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ''
        option_start = len(tokens) - len(options)
        raw_words, word_ends = [], []
        previous_offset = 0
        for _ in tokens:
            lexer.get_token()
            end_offset = lexer.instream.tell()
            raw_words.append(raw[previous_offset:end_offset].strip())
            word_ends.append(end_offset)
            previous_offset = end_offset
        offset = word_ends[option_start - 1]
        env_args = []
        if runner == 'pytest':
            for token in prefix:
                if token.startswith('PYTEST_ADDOPTS='):
                    env_args = shlex.split(token.partition('=')[2])
        arguments = [*env_args, *options]
        redirections = {len(env_args) + index for index, word in enumerate(raw_words[option_start:])
                        if re.match(r'^\d*[<>]', word)}
        targets = _runner_targets(runner, arguments, redirections)
        invocations.append(TestInvocation(raw, runner, tuple(arguments),
                           None if targets is None else tuple(targets), invocation_cwd, offset, cwd))
    return invocations


def disable_vitest_cache(command: str) -> str:
    def transform(raw: str) -> str:
        args = executable_tokens(raw)
        if not args:
            return raw
        runner = Path(args[0]).name
        if runner in {"npm", "pnpm", "yarn"} and args[1:2] in (["exec"], ["dlx"]):
            args = args[2:]
            if args[:1] == ["--"]:
                args = args[1:]
        elif runner == "npx":
            args = args[1:]
            while args[:1] in (["--yes"], ["-y"], ["--no-install"], ["--"]):
                args = args[1:]
        elif runner in {"pnpm", "yarn"}:
            args = args[1:]
        if not args or Path(args[0]).name != "vitest":
            return raw
        if any(arg in {"--cache", "--no-cache"} or arg.startswith("--cache=") for arg in args[1:]):
            return raw
        return raw + " --no-cache"

    try:
        return rewrite_simple_commands(command, transform)
    except (ExecutionBindingError, ValueError):
        # Do not corrupt shell syntax that this small transformer cannot parse.
        return command


def route_sources(payload: Mapping[str, object]):
    """Walk the same routing envelopes for admission and execution."""
    sources = [payload]
    while sources:
        source = sources.pop()
        yield source
        for key in reversed(("issue_seed", "spec_seed", "fix_disposition")):
            value = source.get(key)
            if isinstance(value, dict):
                sources.append(value)


def repository_binding_error(project_root: Path, payload: Mapping[str, object]) -> str:
    root = project_root.resolve()
    for source in route_sources(payload):
        target = str(source.get("target_repository", "")).strip()
        if target and (root / Path(target).expanduser()).resolve() != root:
            return (
                f"execution_binding_mismatch: requested repository {target}, "
                f"but workflow, write scope, verification and rollback are bound to {root}. "
                "No cross-repository execution channel is bound; use an engine-owned "
                "self-repair workflow or an explicitly authorized target workflow."
            )
    return ""


def validate_verification_binding(command: str, project_root: Path) -> None:
    """Reject known deterministic environment/cwd errors before model work."""
    root = project_root.resolve()
    cwd = root
    try:
        spans = command_spans(command)
    except ExecutionBindingError:
        # General shell programs retain their existing execution semantics.
        return
    for start, end in spans:
        raw = command[start:end]
        args = executable_tokens(raw)
        tokens = shlex.split(raw)
        if args[:1] == ["cd"]:
            destination = (cwd / Path(args[-1]).expanduser()).resolve()
            if len(args) != 2 or "$" in args[1] or (destination != root and root not in destination.parents):
                raise ExecutionBindingError(f"verification cwd must stay bound to {root}; route a different repository explicitly")
            cwd = destination
        # Inspect only a real conda launcher, not a path/string mentioning it.
        launch = list(tokens)
        while launch and (launch[0] in {"exec", "env"} or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", launch[0])):
            launch.pop(0)
        if launch and Path(launch[0]).name == "conda" and launch[1:2] == ["run"]:
            index = 2
            while index < len(launch) and launch[index].startswith("-"):
                option = launch[index]
                value = ""
                if option in {"-p", "--prefix", "--cwd", "-n", "--name"}:
                    index += 1
                    value = launch[index] if index < len(launch) else ""
                elif option.startswith(("--prefix=", "--cwd=")):
                    option, value = option.split("=", 1)
                if option in {"-p", "--prefix"}:
                    prefix = (cwd / Path(value).expanduser()).resolve()
                    if not value or "$" in value or not (prefix / "conda-meta").is_dir():
                        raise ExecutionBindingError(f"verification conda environment does not exist: {prefix}")
                elif option == "--cwd":
                    destination = (cwd / Path(value).expanduser()).resolve()
                    if not value or "$" in value or (destination != root and root not in destination.parents):
                        raise ExecutionBindingError(f"verification conda cwd must stay bound to {root}")
                index += 1
        if args and args[0].startswith(("./.conda/", ".conda/")) and not (cwd / args[0]).is_file():
            raise ExecutionBindingError(f"verification interpreter does not exist: {cwd / args[0]}")
        if args and (Path(args[0]).name.startswith("python") or Path(args[0]).name in {"pytest", "vitest"}):
            for arg in args[1:]:
                path = Path(arg.split("::", 1)[0])
                if path.is_absolute() and path.suffix in {".py", ".js", ".ts", ".tsx"} and root not in path.resolve().parents:
                    raise ExecutionBindingError(f"verification source belongs to a different repository: {path}")


def engine_verification_command(command: str, root: Path, python: str, source_root: Path) -> str:
    """Bind copied engine checks to the candidate and its explicit interpreter."""
    from .self_repair import self_repair_verification_command
    root, source_root = root.resolve(), source_root.resolve()
    def compile_branch(raw):
        original = shlex.split(raw)
        if not original:
            return raw
        if original[0] == "cd":
            if len(original) != 2:
                raise ExecutionBindingError("engine verification needs an explicit cwd")
            destination = Path(original[1])
            if destination == source_root or original[1] == source_root.name and not (root / destination).is_dir():
                return "cd " + shlex.quote(str(root))
            resolved = (root / destination).resolve()
            if resolved != root and root not in resolved.parents:
                raise ExecutionBindingError("engine verification cannot leave its candidate workspace")
            return raw
        args = executable_tokens(raw)
        if not args:
            return raw
        executable = Path(args[0]).name
        if executable != "pytest" and not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
            return raw
        if any("$" in value or "`" in value for value in original):
            raise ExecutionBindingError("dynamic engine verification must use an explicit verification script")
        index = next((i for i in range(len(original)) if original[i:] == args), 0)
        assignments = [value for value in original[:index] if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value)]
        mapped = []
        for arg in args[1:]:
            if arg.startswith(str(source_root) + "/"):
                arg = str(root) + arg[len(str(source_root)):]
            mapped.append(arg)
        if executable == "pytest" or mapped[:2] == ["-m", "pytest"]:
            tests = mapped if executable == "pytest" else mapped[2:]
            for arg in tests:
                path = Path(arg.split("::", 1)[0])
                if path.is_absolute() and root not in path.resolve().parents:
                    raise ExecutionBindingError("engine pytest selector belongs to another repository")
            result = self_repair_verification_command(shlex.join(["python", "-m", "pytest", *tests]), root, python_executable=python)
        else:
            result = shlex.join([python, *mapped])
        if assignments:
            result = "env " + shlex.join([value.replace(str(source_root), str(root)) for value in assignments]) + " " + result
        return result
    return rewrite_simple_commands(command, compile_branch)
