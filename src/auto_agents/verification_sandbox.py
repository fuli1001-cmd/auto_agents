"""Local, model-free filesystem isolation for engine verification processes."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import ctypes

# The managed interpreter may retain an older installed engine. Direct launches
# (including the private /tmp preflight) must import helpers from this runtime,
# before importing any auto_agents module. Do not change the test command's env.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_agents import artifact_temp as tempfile


_active_writer_boundary = ContextVar('auto_agents_writer_boundary', default=None)

def provider_probe_command(argv):
    # Capability probes execute the same provider binary as a writer. Keep
    # them within the active boundary even when no AgentRequest is accepted.
    boundary = _active_writer_boundary.get()
    if boundary is None:
        return argv, {}
    command, env = boundary.dispatch(argv, dict(os.environ), boundary.root)
    return command, {'cwd': boundary.root, 'env': env}

def landlock_abi():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(444, None, 0, 1)


def restrict_nested_writes(roots):
    """Further restrict a verification child without creating another user ns.

    The outer Codex filesystem sandbox and private network namespace remain in
    force. Landlock adds an inherited write boundary for nested sandbox tests.
    ABI 3 is required so truncation and cross-directory rename are covered.
    """
    if landlock_abi() < 3:
        raise RuntimeError("nested verification requires Landlock ABI 3 or newer")
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    writes = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
    allowed = writes & ~((1 << 6) | (1 << 11))  # No device creation.
    rules = Ruleset(writes)
    fd = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "could not create verification ruleset")
    try:
        # Git opens /dev/null read-write even for read-only repository probes.
        # Permit writes to that sink only, not arbitrary device files.
        for root, access in [*( (root, allowed) for root in roots), ("/dev/null", 1 << 1)]:
            path_fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, path_fd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), "could not bind verification write root")
            finally:
                os.close(path_fd)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
            raise OSError(ctypes.get_errno(), "could not enforce verification write boundary")
    finally:
        os.close(fd)



def restrict_writer_metadata():
    """Close Landlock's metadata gap when nesting inside an existing sandbox.

    Landlock covers content, links, renames and deletion, but not chmod/chown
    or timestamps/xattrs. A nested writer conservatively cannot alter these
    metadata fields, even on private files; creation modes remain supported.
    The filter is inherited across exec and by every tool descendant.
    """
    import errno
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError('writer metadata confinement is unavailable')
    try:
        for name in ('chmod', 'fchmod', 'fchmodat', 'fchmodat2', 'chown', 'lchown',
                     'fchown', 'fchownat', 'utime', 'utimes', 'futimesat', 'utimensat',
                     'setxattr', 'lsetxattr', 'fsetxattr', 'removexattr', 'lremovexattr', 'fremovexattr'):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                raise RuntimeError('writer metadata syscall is unavailable: ' + name)
            if library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0):
                raise RuntimeError('could not bind writer metadata filter')
        if library.seccomp_load(context):
            raise RuntimeError('could not enforce writer metadata filter')
    finally:
        library.seccomp_release(context)


def namespace_exec(payload):
    """Give tests a private /tmp as well as private PID/mount/network spaces."""
    kept = []
    try:
        for value in sorted(set(payload["preserve"]), key=lambda item: len(Path(item).parts)):
            path = Path(value).resolve()
            if str(path).startswith("/tmp/") and path.is_dir():
                kept.append((str(path), os.open(path, os.O_PATH | os.O_CLOEXEC)))
        if payload.get("ip"):
            subprocess.run([payload["ip"], "link", "set", "lo", "up"], check=True)
        subprocess.run([payload["mount"], "-t", "tmpfs", "-o", "mode=1777", "tmpfs", "/tmp"], check=True)
        for path, fd in kept:
            Path(path).mkdir(parents=True, exist_ok=True)
            subprocess.run([payload["mount"], "--no-canonicalize", "--rbind", f"/proc/self/fd/{fd}", path], pass_fds=(fd,), check=True)
        os.chdir(payload["cwd"])
    finally:
        for _, fd in kept:
            os.close(fd)
    os.execvp(payload["command"][0], payload["command"])


@contextmanager
def verification_argv(argv, cwd: Path, real_project: Path, *, read_roots=(), write_roots=(), path_entries=(),
                      python_paths=(), node_paths=(), library_paths=(), execution_environment=None):
    root, target = Path(cwd).resolve(), Path(real_project).resolve()
    if root == target or root in target.parents or target in root.parents:
        raise RuntimeError("verification workspace overlaps the live target project")
    executable = shutil.which("codex")
    if not executable:
        from auto_agents.verification_dependencies import MissingDependency, VerificationDependencyError
        raise VerificationDependencyError(MissingDependency("executable", "codex"),
            "engine verification needs a local Codex sandbox executable; no model calls are made by this command")
    temporary_parent = os.environ.get("TMPDIR", "/tmp") if os.environ.get("AUTO_AGENTS_VERIFICATION_SANDBOX") else "/tmp"
    with tempfile.TemporaryDirectory(prefix="aav-", dir=temporary_parent) as temporary:
        scratch = Path(temporary)
        if target == scratch or target in scratch.parents:
            raise RuntimeError("verification scratch directory overlaps the live project")
        home = scratch / "home"
        codex_home = scratch / "codex-home"
        home.mkdir()
        codex_home.mkdir()
        (home / ".gitconfig").write_text("[user]\n name = auto-agents-verification\n email = verification@example.invalid\n", encoding="utf-8")
        entries = {":root": "read", "/tmp": "write", "/run": "deny",
                   str(root): "write", str(scratch): "write", str(target): "read"}
        preserve = [str(root), str(scratch), str(target), str(Path(__file__).resolve().parents[2])]
        writable = [str(root), str(scratch)]
        metadata_readonly = [str(target), *map(str, read_roots)]
        for value in write_roots:
            extra = Path(value).resolve()
            if extra == target or extra in target.parents or target in extra.parents or extra == Path("/"):
                raise RuntimeError("verification write root overlaps the live project")
            entries[str(extra)] = "write"
            preserve.append(str(extra))
            writable.append(str(extra))
        for value in read_roots:
            readonly = Path(value).resolve()
            if readonly == root or readonly in root.parents:
                raise RuntimeError("read-only verification input overlaps the writable workspace")
            entries[str(readonly)] = "read"
            preserve.append(str(readonly))
        if execution_environment is not None:
            # Preserve retained launchers/import paths under the private /tmp mount.
            # Environment values stay in env, never in command-line audit records.
            for key in ('PATH', 'PYTHONPATH', 'NODE_PATH', 'LD_LIBRARY_PATH', 'CONDA_PREFIX'):
                for value in execution_environment.get(key, '').split(os.pathsep):
                    try:
                        if value and Path(value).is_dir():
                            extra = Path(value).resolve()
                            preserve.append(str(extra))
                            if str(extra).startswith('/tmp/') and str(extra) not in entries:
                                entries[str(extra)] = 'read'
                    except OSError:
                        # An inherited PATH may name directories already hidden
                        # by an ancestor. Preserve the environment, not access.
                        continue
        for name in (() if execution_environment is not None else (".ssh", ".gnupg", ".codex")):
            sensitive = Path.home() / name
            if sensitive.exists():
                entries[str(sensitive)] = "deny"
        control = os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_CONFIG")
        if control:
            config = json.loads(Path(control).read_text())
            entries[str(Path(config["root"]).resolve())] = "read"
            entries[str(root)] = "write"
            preserve.append(str(Path(config["root"]).resolve()))
        for directory in (() if execution_environment is not None else (root, target)):
            if (directory / ".env").exists():
                entries[str(directory / ".env")] = "deny"
        for name in (".git", ".agents", ".codex"):
            if (root / name).exists():
                entries[str(root / name)] = "read"
                metadata_readonly.append(str(root / name))
        filesystem = ",".join(json.dumps(key) + "=" + json.dumps(value) for key, value in entries.items())
        profile = '{filesystem={' + filesystem + '},network={enabled=true}}'
        executable_path = os.pathsep.join([*(str(Path(value).resolve()) for value in path_entries),
                                           os.environ.get("PATH", os.defpath)])
        clean_environment = ["env", "-i", "PATH=" + executable_path,
                             "HOME=" + str(home), "CODEX_HOME=" + str(codex_home),
                             "TMPDIR=" + temporary, "LANG=C.UTF-8",
                             "PYTHONPATH=" + os.pathsep.join([str(root / "src"), *map(str, python_paths)]),
                             "PYTHONDONTWRITEBYTECODE=1",
                             "AUTO_AGENTS_TEST=True", "TESTING=True", "AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1",
                             "AUTO_AGENTS_VERIFICATION_SANDBOX=1"]
        if execution_environment is not None:
            # Project verification retains operator inputs, activation and networking.
            # Only sandbox bookkeeping goes to a fresh private home.
            clean_environment = ['env', 'CODEX_HOME=' + str(codex_home),
                                 'AUTO_AGENTS_VERIFICATION_SANDBOX=1']
        if node_paths:
            clean_environment.append("NODE_PATH=" + os.pathsep.join(map(str, node_paths)))
        if library_paths:
            clean_environment.append("LD_LIBRARY_PATH=" + os.pathsep.join(map(str, library_paths)))
        if os.environ.get("AUTO_AGENTS_VERIFICATION_SANDBOX"):
            launcher = Path(__file__).resolve()
            yield [sys.executable, str(launcher), "--metadata", json.dumps({
                'roots': writable, 'readonly': metadata_readonly}), *clean_environment, *argv]
            return
        metadata = [sys.executable, str(Path(__file__).resolve()), '--metadata',
                    json.dumps({'roots': [*writable, '/tmp'], 'readonly': metadata_readonly})]
        sandbox = [executable, "sandbox", "-c", "features.network_proxy=false",
                   "-c", "permissions.autoagents_verify=" + profile,
                   "-P", "autoagents_verify", "-C", str(root), "--include-managed-config", "--", *clean_environment, *metadata, *argv]
        if not os.environ.get("AUTO_AGENTS_VERIFICATION_SANDBOX"):
            unshare, ip, mount = shutil.which("unshare"), shutil.which("ip"), shutil.which("mount")
            required = [("unshare", unshare), ("mount", mount)]
            if execution_environment is None:
                required.append(("ip", ip))
            if any(not value for _, value in required):
                from auto_agents.verification_dependencies import MissingDependency, VerificationDependencyError
                missing = next(name for name, value in required if not value)
                raise VerificationDependencyError(MissingDependency("executable", missing),
                    "verification requires " + ', '.join(name for name, _ in required) + " for private test namespaces")
            payload = {"cwd": str(root), "preserve": preserve, "command": sandbox, "ip": ip if execution_environment is None else None, "mount": mount}
            sandbox = [unshare, "--user", "--map-root-user", "--mount", *(["--net"] if execution_environment is None else []), "--pid", "--fork", "--mount-proc",
                       sys.executable, str(Path(__file__).resolve()), "--namespace", json.dumps(payload)]
        yield ["env", "TMPDIR=" + temporary, *sandbox]



class CandidateWriterBoundary:
    """Use the verification sandbox's mount isolation for provider descendants.

    Providers retain their inherited execution environment and network. Only
    their state directories and temporary storage move into this private area.
    The private /tmp is necessary for the sandbox launcher's mount registry.
    """

    def __init__(self, root, scratch, state):
        from .session_verification import ownership_error
        self.root, self.scratch, self.state = root.resolve(), scratch.resolve(), state
        self.shared = Path(state.verification_binding['repository']).resolve()
        if self.root == self.shared or self.shared.is_relative_to(self.root):
            raise ownership_error(state, 'writer confinement requires a private candidate checkout')
        # Custody admission supports both registered external runtimes and
        # retained independent legacy repositories beneath the control tree.
        # Grant writes to that validated checkout, never to its shared parent.
        from .session_source import validate_checkout
        validate_checkout(self.shared, state, root)
        self.nested = bool(os.environ.get('AUTO_AGENTS_VERIFICATION_SANDBOX'))
        if self.nested and landlock_abi() < 3:
            raise ownership_error(state, 'writer confinement is unavailable', detail='Landlock ABI 3 is required')
        self.executables = {name: shutil.which(name) for name in (() if self.nested else ('codex', 'unshare', 'mount'))}
        if not all(self.executables.values()):
            raise ownership_error(state, 'writer confinement is unavailable',
                                  missing_executables=[k for k, v in self.executables.items() if not v])
        for name in ('home', 'codex', 'claude', 'tmp', 'cache', 'config', 'data', 'state'):
            (scratch / name).mkdir()
        self.prepared = False
        self.read_roots = []

    def _environment(self, env):
        result = dict(env)
        result.update(HOME=str(self.scratch / 'home'), CODEX_HOME=str(self.scratch / 'codex'),
                      CLAUDE_CONFIG_DIR=str(self.scratch / 'claude'), TMPDIR=str(self.scratch / 'tmp'),
                      XDG_CACHE_HOME=str(self.scratch / 'cache'), XDG_CONFIG_HOME=str(self.scratch / 'config'),
                      XDG_DATA_HOME=str(self.scratch / 'data'), XDG_STATE_HOME=str(self.scratch / 'state'))
        return result

    def _command(self, argv, env):
        if self.nested:
            return [sys.executable, str(Path(__file__).resolve()), '--writer-landlock',
                    json.dumps([str(self.root), str(self.scratch)]), *argv]
        from .gate_execution import discover_dependency_links
        entries = {':root': 'read', '/tmp': 'write', str(self.root): 'write',
                   str(self.scratch): 'write'}
        # /tmp is a fresh mount, not the host's temporary directory. Shared
        # inputs restored below it are explicitly read-only, including metadata.
        preserve = [self.root, self.scratch, self.shared, Path(__file__).resolve().parents[2]]
        preserve.extend(self.read_roots)
        preserve.extend(Path(value) for value in discover_dependency_links(self.root).values())
        executable = shutil.which(argv[0], path=env.get('PATH'))
        if executable:
            preserve.append(Path(executable).resolve().parent)
        for variable in ('PATH', 'PYTHONPATH', 'NODE_PATH', 'LD_LIBRARY_PATH'):
            for value in env.get(variable, '').split(os.pathsep):
                try:
                    if value and Path(value).is_dir():
                        preserve.append(Path(value).resolve())
                except OSError:
                    # Inherited PATH can include locations already denied by
                    # an outer sandbox. Preserve the environment, not access.
                    continue
        for path in preserve:
            path = path.resolve()
            if path not in (self.root, self.scratch) and str(path).startswith('/tmp/'):
                entries[str(path)] = 'read'
        entries[str(self.root)] = entries[str(self.scratch)] = 'write'
        profile = '{filesystem={' + ','.join(json.dumps(k) + '=' + json.dumps(v)
                                             for k, v in entries.items()) + '},network={enabled=true}}'
        sandbox = [self.executables['codex'], 'sandbox', '-c', 'features.network_proxy=false',
                   '-c', 'permissions.autoagents_writer=' + profile, '-P', 'autoagents_writer',
                   '-C', str(self.root), '--include-managed-config', '--', *argv]
        payload = {'cwd': str(self.root), 'preserve': list(map(str, preserve)),
                   'command': sandbox, 'mount': self.executables['mount']}
        return [self.executables['unshare'], '--user', '--map-root-user', '--mount',
                '--pid', '--fork', '--mount-proc', sys.executable, str(Path(__file__).resolve()),
                '--namespace', json.dumps(payload)]

    def check(self):
        from .session_verification import ownership_error
        # Probe both data and metadata denial through the very same launcher.
        # No provider is invoked if the host cannot install the boundary.
        outside = self.scratch.parent / (self.scratch.name + '-readonly')
        outside.mkdir()
        protected = outside / 'input'
        protected.write_text('retained')
        self.read_roots.append(outside)
        try:
            code = ("from pathlib import Path; import tempfile; "
                    "f=tempfile.TemporaryFile(dir='.'); f.write(b'private'); f.close(); "
                    f"p=Path({str(protected)!r}); assert p.read_text() == 'retained'\n"
                    "for operation in (lambda: p.write_text('bad'), lambda: p.chmod(0o777)):\n"
                    " try: operation()\n"
                    " except OSError: pass\n"
                    " else: raise RuntimeError('writer boundary allowed a shared write')\n")
            env = self._environment(os.environ)
            result = subprocess.run(self._command([sys.executable, '-I', '-c', code], env),
                                    cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stderr[-2000:])
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise ownership_error(self.state, 'writer confinement is unavailable', detail=str(error)) from error
        finally:
            self.read_roots.remove(outside)
            shutil.rmtree(outside)

    def dispatch(self, argv, env, cwd):
        from .session_verification import ownership_error
        if Path(cwd).resolve() != self.root:
            raise ownership_error(self.state, 'writer dispatch left its private candidate checkout')
        if not self.prepared:
            # Copy only provider configuration/credentials, never session/cache
            # trees or links granting write access back to shared provider state.
            home = Path(env.get('HOME', str(Path.home())))
            sources = [(Path(env.get('CODEX_HOME', str(home / '.codex'))), self.scratch / 'codex',
                        ('config.toml', 'auth.json')),
                       (Path(env.get('CLAUDE_CONFIG_DIR', str(home / '.claude'))), self.scratch / 'claude',
                        ('settings.json', '.credentials.json')),
                       (home, self.scratch / 'home', ('.claude.json', '.gitconfig'))]
            for source, destination, names in sources:
                for name in names:
                    if (source / name).is_file():
                        shutil.copyfile(source / name, destination / name)
            # Profiles may carry the selected model/runtime settings.
            source = sources[0][0]
            for path in source.glob('*.config.toml'):
                if path.is_file():
                    shutil.copyfile(path, self.scratch / 'codex' / path.name)
            self.prepared = True
        environment = self._environment(env)
        return self._command(argv, environment), environment


@contextmanager
def candidate_writer_boundary(root, state):
    with tempfile.TemporaryDirectory(prefix='auto-agents-writer-') as temporary:
        try:
            boundary = CandidateWriterBoundary(Path(root), Path(temporary), state)
            boundary.check()
        except (OSError, subprocess.SubprocessError) as error:
            from .session_verification import ownership_error
            raise ownership_error(state, 'writer confinement is unavailable', detail=str(error)) from error
        token = _active_writer_boundary.set(boundary)
        try:
            yield boundary
        finally:
            _active_writer_boundary.reset(token)


def check_verification_sandbox(root: Path, python: str, real_project: Path):
    """Fail before generation when the host cannot enforce the write boundary."""
    if landlock_abi() < 3:
        raise RuntimeError("verification host needs Landlock ABI 3 or newer")
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-", dir=root) as probe:
        workspace, readonly = Path(probe) / "candidate", Path(probe) / "readonly"
        workspace.mkdir()
        readonly.mkdir()
        outside = readonly / "outside"
        code = (
            "from pathlib import Path; import socket; "
            "Path('inside').write_text('ok'); "
            "s=socket.socket(); s.bind(('127.0.0.1',0)); s.close(); "
            f"p=Path({str(outside)!r}); "
            "\ntry: p.write_text('not-allowed'); blocked=False\n"
            "except OSError: blocked=True\n"
            "assert blocked, 'sandbox allowed an out-of-workspace write'"
        )
        try:
            with verification_argv([python, "-c", code], workspace, readonly) as command:
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError("verification sandbox is unavailable: " + result.stderr[-1000:])
        finally:
            # This is a uniquely named probe file, never a project input.
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--namespace":
        namespace_exec(json.loads(sys.argv[2]))
        raise SystemExit(3)
    if len(sys.argv) < 5 or sys.argv[1] not in {"--landlock", "--writer-landlock", "--metadata"}:
        raise SystemExit("internal verification launcher requires --landlock ROOTS COMMAND")
    from auto_agents.verification_metadata import metadata_exec
    policy = json.loads(sys.argv[2])
    if isinstance(policy, list):
        policy = {'roots': policy}
    raise SystemExit(metadata_exec(sys.argv[3:], policy['roots'], policy.get('readonly', [])))
