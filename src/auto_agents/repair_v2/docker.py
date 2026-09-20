"""Disposable verification containers backed by a reusable pinned tool image."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import selectors
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack

from .dependencies import cache_key, pytest_parts, witness
from .scheduling import Timings
from .store import atomic_json, digest
from .types import Cancellation, RepairBlocked, ValidationResult
from .workspace import source_identity
from .storage import disposable_source, execution_lease, recover_executions, require_space
from .cleanup import labels, reap_containers


MAX_OUTPUT_BYTES = 2 * 1024 * 1024
# The session verifier creates its own user/mount/PID namespaces and metadata
# supervisor. Docker's default seccomp profile prevents their startup even for
# an unprivileged user. Keep capabilities dropped and the remaining boundaries.
REPLAY_ISOLATION = {
    'standard': ('--cap-drop', 'ALL', '--security-opt', 'no-new-privileges'),
    'session': ('--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--security-opt', 'seccomp=unconfined'),
}


def replay_infrastructure_reason(observed):
    """Keep sandbox startup failures out of candidate implementation feedback."""
    if observed.get('infrastructure'):
        return observed.get('error') or '隔离恢复环境无法启动。'
    failure = observed.get('recovery_observation', {}).get('current_failure') or {}
    for item in (failure, observed):
        diagnostic = item.get('diagnostic') or {}
        if (item.get('failure_kind') == 'verification_confinement'
                or diagnostic.get('failure_kind') == 'verification_confinement'
                or item.get('error_type') == 'ConfinementPreflightError'):
            detail = diagnostic.get('detail') or item.get('result') or item.get('error') or '沙箱启动检查未通过'
            return '隔离验证环境无法启动，已停止自动代码修复；请检查控制器的容器隔离配置。原因：' + str(detail)
    detail = str(failure.get('result', ''))
    if (failure.get('failure_kind') == 'verification_execution_binding'
            and detail.startswith(('verification conda environment does not exist:',
                                   'verification interpreter does not exist:'))):
        return detail
    return ''


def replay_project_path(payload):
    """Mount only the disposable copy at the retained logical project path."""
    path = Path(payload.get('project') or '/target').expanduser()
    reserved = [Path(value) for value in ('/work', '/result', '/opt', '/usr', '/bin',
                                        '/lib', '/lib64', '/etc', '/proc', '/sys', '/dev', '/tmp/home')]
    if (not path.is_absolute() or '..' in path.parts or ',' in str(path)
            or any(path == value or path in value.parents or value in path.parents for value in reserved)):
        raise RepairBlocked('replay_project_path', 'retained project path conflicts with the replay runtime')
    return path


def container_mounts():
    """Observe mounts without exposing container configuration or racing removal."""
    code, listing = run(['docker', 'ps', '-aq'], timeout=15)
    if code: raise RepairBlocked('docker_unavailable', listing)
    identities = listing.split()
    if not identities: return []
    command = ['docker', 'inspect', '--type', 'container', '--format', '{{json .Mounts}}']
    code, data = run([*command, *identities], timeout=15)
    if code:
        # Verification containers may finish between ps and inspect. Retry
        # individually only on this exceptional path, retaining live mounts.
        rows = []
        for identity in identities:
            code, value = run([*command, identity], timeout=15)
            if code == 1 and value.strip() in {
                    'Error: No such object: ' + identity,
                    'Error: No such container: ' + identity,
                    'Error response from daemon: No such container: ' + identity}:
                continue
            if code: raise RepairBlocked('docker_unavailable', value)
            rows.append(value)
        data = '\n'.join(rows)
    return [Path(m['Source']).resolve() for line in data.splitlines() if line.strip()
            for m in (json.loads(line) or []) if m.get('Source')]


def run(command, *, cancel=None, timeout=1800, output=None, env=None, observation=None):
    """Drain continuously, retain a bounded diagnostic tail and reap this group."""
    buffer, truncated, stopped = bytearray(), False, False
    if observation is not None: observation.update(started=False, termination='', exit_code=None)
    if cancel is not None and cancel.is_set():
        if observation is not None: observation['termination'] = 'cancelled'
        if output: Path(output).write_text('cancelled before process launch\n')
        return 130, 'cancelled before process launch'
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, start_new_session=True) as process:
        if observation is not None: observation['started'] = True
        started = time.monotonic()
        termination = None
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if termination is None and ((cancel is not None and cancel.is_set()) or now - started >= timeout):
                        stopped = True
                        if observation is not None:
                            observation['termination'] = 'cancelled' if cancel is not None and cancel.is_set() else 'timeout'
                        termination = now
                        try: os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError: pass
                    if termination is not None and now - termination >= 3:
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    for key, _ in selector.select(timeout=0.2):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            buffer.extend(chunk)
                            if len(buffer) > MAX_OUTPUT_BYTES:
                                del buffer[:-MAX_OUTPUT_BYTES]
                                truncated = True
                    if termination is not None and now - termination >= 6:
                        break
                process.wait(timeout=5)
                if observation is not None: observation['exit_code'] = process.returncode
            finally:
                if process.poll() is None:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait(timeout=5)
        data = (b'[earlier output truncated; retaining diagnostic tail]\n' if truncated else b'') + buffer
        if output: Path(output).write_bytes(data)
        return (130 if stopped else process.returncode), data.decode(errors='replace')


class DockerVerifier:
    def __init__(self, root, *, image=None, requirements=(), node_version='22.20.0',
                 vitest_version='5.0.0', workers=None, callback=None, timeout=1800, python=None, codex_binary=None):
        self.root = Path(root)
        self.requirements, self.node_version, self.vitest_version = tuple(requirements), node_version, vitest_version
        self.image, self.workers, self.callback, self.timeout = image, workers, callback, timeout
        self.codex_binary = codex_binary
        self.runtime = ''
        self.python = python or __import__('sys').executable

    def prepare(self):
        if not shutil.which('docker'):
            raise RepairBlocked('docker_missing', 'self-repair V2 requires Docker on Linux/WSL')
        code, text = run(['docker', 'info', '--format', '{{.OSType}}'], timeout=15)
        if code or text.strip() != 'linux': raise RepairBlocked('docker_unavailable', text.strip())
        require_space(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        reap_containers(self.root, kind='verification')
        mounts = container_mounts()
        recover_executions(self.root, mounts)
        driver = Path(__file__).with_name('pytest_driver.py').read_text()
        if not self.image:
            from .image import tool_image
            self.image = tool_image(self.root, self.python, driver, codex_binary=self.codex_binary)
        code, identity_text = run(['docker', 'image', 'inspect', self.image, '--format', '{{.Id}}'], timeout=15)
        if code: raise RepairBlocked('image_unavailable', identity_text)
        code, server = run(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=15)
        if code: raise RepairBlocked('docker_unavailable', server)
        self.runtime = digest({'image': identity_text.strip(), 'driver': driver, 'docker': server.strip(),
                               'uid': os.getuid(), 'gid': os.getgid(), 'memory': '1g', 'tmpfs': 'exec,4g',
                               'boundary': Path(__file__).with_name('boundary_driver.py').read_text(),
                               'session': Path(__file__).parent.parent.joinpath('session_replay.py').read_text(),
                               'runtime_identity': Path(__file__).parent.parent.joinpath('repair_runtime_identity.py').read_text(),
                               'replay_environment': Path(__file__).with_name('replay_environment.py').read_text(),
                               'replay_isolation': REPLAY_ISOLATION,
                               'kernel': os.uname().release, 'policy': 8, 'init': True})
        self.image = identity_text.strip()  # A mutable tag is not a verification input.

    def concurrency(self):
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1
        available = next((int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                          if line.startswith('MemAvailable:')), 3 * 1024 ** 3)
        return max(1, min(self.workers or max(1, cpus - 1), max(1, (available - 2 * 1024 ** 3) // 1024 ** 3)))

    def execute(self, snapshot_id, snapshot, unit, cancel):
        if cancel.is_set():
            return {'unit': unit.identity, 'command': unit.command, 'ok': False, 'cancelled': True,
                    'excerpt': 'validation cancelled before dispatch', 'failed': [], 'missing': [], 'infrastructure': False}
        started = time.monotonic()
        inputs = witness(snapshot, unit, self.runtime)
        key = cache_key(snapshot_id, unit, inputs)
        cache = self.root / 'cache' / (key + '.json')
        reusable = inputs['complete'] and not unit.fresh
        if cache.exists() and reusable:
            try:
                envelope = json.loads(cache.read_text())
                result = envelope['result']
                if (isinstance(result, dict) and digest(result) == envelope.get('digest') and result.get('ok')
                        and result.get('inputs') == inputs and result.get('command') == unit.command):
                    return {**result, 'unit': unit.identity, 'cache_hit': True, 'seconds': 0.0,
                            'total_seconds': time.monotonic() - started}
            except (OSError, ValueError, KeyError, TypeError):
                pass  # A corrupt optimization record never blocks fresh proof.

        if cancel.is_set():
            return {'unit': unit.identity, 'command': unit.command, 'ok': False, 'cancelled': True,
                    'excerpt': 'validation cancelled before source copy', 'failed': [], 'missing': [], 'infrastructure': False}
        identity = uuid.uuid4().hex
        base = self.root / 'executions' / identity
        source, output = base / 'source', base / 'result'
        custody = {'clear': True}
        with execution_lease(base), disposable_source(snapshot, source, cleanup=lambda: custody['clear']):
            output.mkdir(exist_ok=True)
            result = self._execute(snapshot_id, snapshot, unit, cancel, key, cache, inputs, identity, base, source, output, custody)
        return {**result, 'total_seconds': time.monotonic() - started}

    def _execute(self, snapshot_id, snapshot, unit, cancel, key, cache, inputs, identity, base, source, output, custody):
        if cancel.is_set():
            return {'unit': unit.identity, 'command': unit.command, 'ok': False, 'cancelled': True,
                    'excerpt': 'validation cancelled before container launch', 'failed': [], 'missing': [], 'infrastructure': False}
        name = 'aav2-' + identity
        command = ['docker', 'run', '--init', '--name', name, *labels(self.root, identity, kind='verification'), '--network', 'none', '--read-only',
            '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '1g', '--pids-limit', '512', '--tmpfs', '/tmp:rw,nosuid,exec,mode=1777,size=4g',
            '-e', 'HOME=/tmp/home', '--workdir', '/work',
            '--mount', 'type=bind,src=' + str(source) + ',dst=/work',
            '--mount', 'type=bind,src=' + str(output) + ',dst=/result',
            '--mount', 'type=bind,src=' + str(Path(__file__).with_name('pytest_driver.py')) + ',dst=/opt/repair/driver.py,readonly',
            '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONNOUSERSITE=1', '-e', 'PYTHONPATH=/work/src',
            '-e', 'AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1', '-e', 'AUTO_AGENTS_STORAGE_ROOT=/tmp/storage',
            '-e', 'AUTO_AGENTS_VERIFICATION_ROOT=/tmp/verification', '-e', 'AUTO_AGENTS_WORKER_ROOT=/tmp/workers']
        if unit.profile == 'sandbox':
            # No host project, credentials, devices, sockets or host namespaces
            # are exposed even for tests of the nested sandbox implementation.
            command += ['--cap-add', 'SYS_ADMIN', '--cap-add', 'SYS_PTRACE', '--security-opt', 'seccomp=unconfined']
        elif unit.profile == 'standard':
            command += ['--cap-drop', 'ALL', '--security-opt', 'no-new-privileges']
        else: raise RepairBlocked('verification_profile', 'unknown validation isolation profile')
        parts = pytest_parts(unit.command)
        if parts is not None:
            command += ['-e', 'REPAIR_PYTEST_ARGS=' + json.dumps(parts), self.image, 'python', '/opt/repair/driver.py']
        else:
            command += [self.image, '/bin/sh', '-c', unit.command]
        started = time.monotonic()
        execution, state, info = {}, {}, ''
        try:
            custody['clear'] = False
            code, text = run(command, cancel=cancel, timeout=self.timeout, output=base / 'output.log', observation=execution)
            if execution.get('started') is not False:
                info_code, info = run(['docker', 'inspect', name, '--format', '{{json .State}}'], timeout=10)
                state = json.loads(info) if not info_code else {}
        finally:
            if execution.get('started') is False:
                custody['clear'] = True
            else:
                removed, _ = run(['docker', 'rm', '-f', name], timeout=15)
                custody['clear'] = removed == 0
        evidence = json.loads((output / 'pytest.json').read_text()) if (output / 'pytest.json').exists() else {}
        collected, passed = set(evidence.get('collected', [])), set(evidence.get('passed', []))
        missing = [node for node in unit.expected_nodes if not any(
            item == node or item.startswith(node + '[') or item.startswith(node + '::') for item in passed)]
        collection = parts is not None and '--collect-only' in parts
        valid_pytest = (parts is None or bool(collected) and (collection or collected <= passed)
                        and not evidence.get('failed') and not evidence.get('skipped') and not missing)
        unchanged = source_identity(source) == snapshot_id
        cancelled = execution.get('termination') == 'cancelled'
        timed_out = execution.get('termination') == 'timeout'
        start_error = code in (125, 126, 127) or execution.get('exit_code') in (125, 126, 127)
        infrastructure = bool(state.get('OOMKilled') or start_error or timed_out or not state and not cancelled)
        reason = ('container exceeded its memory limit' if state.get('OOMKilled') else
                  'Docker could not start the verification command' if start_error else
                  'verification cancelled by controller' if cancelled else
                  'verification exceeded its time limit' if timed_out else
                  'verification container state unavailable: ' + info.strip() if not state else '')
        if reason: text = text[-3400:] + '\n' + reason
        result = {'unit': unit.identity, 'command': unit.command,
            'ok': code == 0 and valid_pytest and not infrastructure and unchanged and not cancelled and not timed_out,
            'source_unchanged': unchanged,
            'cancelled': cancelled, 'timed_out': timed_out, 'execution': execution, 'container_state': state,
            'diagnostic': reason,
            'returncode': code, 'collected': sorted(collected), 'passed': sorted(passed),
            'failed': evidence.get('failed', []), 'skipped': evidence.get('skipped', []), 'missing': missing,
            'call_failed': evidence.get('call_failed', []),
            'failure_details': evidence.get('failure_details', []),
            'node_seconds': evidence.get('node_seconds', {}),
            'cache_hit': False, 'seconds': time.monotonic() - started, 'infrastructure': infrastructure,
            'output': str(base / 'output.log'), 'excerpt': text[-4000:], 'inputs': inputs}
        if result['ok'] and inputs['complete'] and not unit.fresh:
            try: atomic_json(cache, {'result': result, 'digest': digest(result)})
            except OSError: pass  # Cache writes are optional; execution evidence is not.
        return result

    def remember_timings(self, checks):
        timings = Timings(self.root)
        timings.observe(checks); timings.save()

    def suite_units(self, snapshot, request):
        """Collect once after implementation; preserve every actual test node."""
        from .types import ValidationUnit
        identity = source_identity(snapshot)
        result = self.execute(identity, snapshot, ValidationUnit('suite-collection',
            'python -m pytest --collect-only -q tests', fresh=True, profile='sandbox'), threading.Event())
        if not result['ok']:
            if result.get('infrastructure'):
                raise RepairBlocked('verification_infrastructure', result['excerpt'])
            # Collection errors caused by the candidate are ordinary repair
            # feedback, not an environment stop before the writer can fix them.
            return [ValidationUnit('suite-collection-failure', 'python -m pytest -q tests',
                                   fresh=True, profile='sandbox')]
        grouped = {}
        for node in result['collected']:
            file = node.split('::', 1)[0]
            if not file.startswith('tests/') or '..' in Path(file).parts:
                raise RepairBlocked('verification_collection', 'collection produced an unsafe source path')
            grouped.setdefault(file, []).append(node)
        units = Timings(self.root).batches(grouped)
        units.extend(ValidationUnit('required:' + digest(command)[:20], command, profile='sandbox')
                     for command in dict.fromkeys(c for item in request.acceptance for c in item.commands))
        return units

    def validate_suite(self, identity, snapshot, units, cancel):
        # Discover ordinary failures together instead of consuming one repair
        # turn per newly encountered batch. Infrastructure still stops dispatch.
        result = self.validate(identity, snapshot, units, cancel, collect_all=True)
        self.remember_timings(result.checks)
        return result

    def compare_baseline(self, identity, snapshot, base_repository, base_commit, validation, cancel):
        from .comparison import compare
        return compare(self, identity, snapshot, base_repository, base_commit, validation, cancel)

    def regression(self, snapshot_id, snapshot, base_repository, base_commit, coverage, cancel):
        """Run current behavioral tests against original production code."""
        from .workspace import Workspace, git
        from .types import ValidationUnit
        import shlex
        import tempfile
        nodes = sorted({node for row in coverage for node in row['nodes']})
        changed = git(snapshot, 'diff', '--name-only', base_commit, '--').splitlines()
        product_changed = any(not p.startswith(('tests/', 'docs/')) and p != 'conftest.py' for p in changed)
        with tempfile.TemporaryDirectory(prefix='regression-', dir=self.root) as temporary:
            workspace = Workspace(Path(temporary) / 'baseline', base_repository, base_commit)
            baseline = workspace.prepare()
            # Keep the same assertions and fixtures on both sides. New tests
            # must fail on the old behavior, not merely be absent there.
            shutil.copytree(Path(snapshot) / 'tests', baseline / 'tests', dirs_exist_ok=True, symlinks=True)
            if (Path(snapshot) / 'conftest.py').is_file():
                shutil.copy2(Path(snapshot) / 'conftest.py', baseline / 'conftest.py')
            workspace.checkpoint()
            identity = source_identity(baseline)
            # One new module may depend on an API absent from the old source.
            # Keep it from preventing other, portable behavioral probes from
            # executing. Collection/setup errors are never counterexamples.
            grouped = {}
            for node in nodes: grouped.setdefault(node.split('::', 1)[0], []).append(node)
            collections = [ValidationUnit('behavior-collection:' + file,
                'python -m pytest --collect-only -q ' + ' '.join(shlex.quote(n) for n in batch), fresh=True, profile='sandbox')
                for file, batch in grouped.items()]
            collected = self.validate(identity, baseline, collections, cancel, collect_all=True)
            if cancel.is_set(): raise KeyboardInterrupt()
            executable = {}
            for check in collected.checks:
                if check['ok']:
                    for node in check.get('collected', []):
                        file = node.split('::', 1)[0]
                        if file not in grouped or not any(node == selected or node.startswith(selected + '[')
                                or node.startswith(selected + '::') for selected in grouped[file]):
                            raise RepairBlocked('verification_collection', 'baseline collection escaped reviewed coverage')
                        executable.setdefault(file, []).append(node)
            units = Timings(self.root).batches(executable, prefix='behavior-baseline', fresh=True, pack=False)
            result = self.validate(identity, baseline, units, cancel, collect_all=True) if units and not collected.infrastructure else None
            if cancel.is_set(): raise KeyboardInterrupt()
            checks = [*collected.checks, *(result.checks if result else [])]
            infrastructure = collected.infrastructure or bool(result and result.infrastructure) or any(
                check.get('returncode') in (125, 126, 127, 130, 137) for check in checks)
            changed_source = any(check.get('source_unchanged') is False for check in checks)
            examples = sorted({node for check in (result.checks if result else []) if check.get('returncode') == 1
                               and check.get('source_unchanged') is True and not check.get('cancelled') and not check.get('timed_out')
                               for node in check.get('call_failed', [])})
            demonstrated = bool(examples) and not infrastructure and not changed_source
            detail = next((check for check in checks if check.get('call_failed')), None)
            return {'ok': not infrastructure and not changed_source and (demonstrated or not product_changed),
                    'snapshot': snapshot_id, 'base': base_commit, 'runtime': self.runtime,
                    'product_changed': product_changed, 'demonstrated_regression': demonstrated,
                    'counterexamples': examples, 'checks': checks,
                    'infrastructure': bool(infrastructure),
                    'output': detail.get('output', '') if detail else '',
                    'reason': 'Baseline tests modified their source.' if changed_source else detail.get('excerpt', '') if demonstrated else
                        'No executed behavioral counterexample on the original source; collection and setup errors do not establish a regression.'}

    def boundary(self, snapshot_id, snapshot, frozen_target, payload, cancel):
        """One deterministic, credential-free replay at the original boundary."""
        from ..root_cause import RootCauseCoordinator
        from .evidence import identity as evidence_identity
        from .workspace import git
        from .replay_environment import prepare as prepare_environment, EnvironmentUnavailable
        identity = uuid.uuid4().hex
        base = self.root / 'executions' / identity
        name = 'aav2-' + identity
        target, source, output = base / 'target', base / 'source', base / 'result'
        project_path = replay_project_path(payload)
        before = evidence_identity(frozen_target)
        custody = {'clear': True}
        with execution_lease(base), disposable_source(snapshot, source, cleanup=lambda: custody['clear']):
            output.mkdir(exist_ok=True)
            try:
                RootCauseCoordinator._copy_diagnostic_tree(Path(frozen_target), target)
                # Dissociate diagnostic Git objects before entering Docker;
                # the real/frozen project is never mounted into the container.
                if (target / '.git').exists():
                    git(target, 'repack', '-a', '-d')
                    (target / '.git/objects/info/alternates').unlink(missing_ok=True)
                environments = prepare_environment(self.root / 'replay-environments', frozen_target, payload)
                atomic_json(output / 'request.json', {**payload, 'commit': git(source, 'rev-parse', 'HEAD'),
                                                     'replay_environments': [item.describe() for item in environments],
                                                     '_replay_project': str(project_path)})
                profile = 'session' if payload.get('invocation', {}).get('session_id') else 'standard'
                command = ['docker', 'run', '--init', '--name', name, *labels(self.root, identity, kind='verification'), '--network', 'none', '--read-only',
                    '--user', f'{os.getuid()}:{os.getgid()}', *REPLAY_ISOLATION[profile],
                    '--memory', '1g', '--pids-limit', '512',
                    '--tmpfs', '/tmp:rw,nosuid,exec,mode=1777,size=4g', '--workdir', str(project_path),
                    '-e', 'HOME=/tmp/home', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                    '-e', 'AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1', '-e', 'AUTO_AGENTS_STORAGE_DISABLED=1',
                    '--mount', f'type=bind,src={source},dst=/work,readonly',
                    '--mount', f'type=bind,src={target},dst={project_path}',
                    '--mount', f'type=bind,src={output},dst=/result']
                for environment in environments:
                    command += ['--mount', f'type=bind,src={environment.root},dst={environment.prefix},readonly']
                for script in (Path(__file__).with_name('boundary_driver.py'),
                               Path(__file__).parent.parent / 'session_replay.py',
                               Path(__file__).parent.parent / 'repair_runtime_identity.py'):
                    command += ['--mount', f'type=bind,src={script},dst=/opt/repair/{script.name},readonly']
                custody['clear'] = False
                code, text = run([*command, self.image, 'python', '/opt/repair/boundary_driver.py'],
                                 cancel=cancel, timeout=self.timeout, output=base / 'output.log')
                try: observed = json.loads((output / 'boundary.json').read_text())
                except (OSError, ValueError): observed = {'ok': False, 'error': text[-4000:]}
                for environment in environments:
                    environment.verify()
                after = evidence_identity(frozen_target)
                result = {'ok': code == 0 and observed.get('ok') is True and before == after,
                          'snapshot': snapshot_id, 'target': before, 'runtime': self.runtime,
                          'observed': observed, 'output': str(base / 'output.log'), 'returncode': code}
                result['isolation_profile'] = profile
                result['environment_inputs'] = [item.describe() for item in environments]
                infrastructure_reason = replay_infrastructure_reason(observed)
                if infrastructure_reason:
                    result.update(ok=False, infrastructure=True, reason=infrastructure_reason)
                atomic_json(base / 'boundary.json', result)
                return result
            except EnvironmentUnavailable as error:
                result = {'ok': False, 'infrastructure': True, 'reason': str(error),
                          'snapshot': snapshot_id, 'target': before, 'runtime': self.runtime,
                          'output': str(base / 'output.log')}
                atomic_json(base / 'boundary.json', result)
                return result
            finally:
                removed, _ = run(['docker', 'rm', '-f', name], timeout=15)
                custody['clear'] = custody['clear'] or removed == 0
                if custody['clear'] and target.exists(): shutil.rmtree(target)

    def validate(self, identity, snapshot, units, cancel, *, collect_all=False):
        # Dedupe only identical executable semantics; do not merge shell plans.
        unique = {digest([u.command, u.expected_nodes, u.profile, u.fresh]): u for u in units}
        if not unique: return ValidationResult(False, identity, failures=[{'reason': 'no mandatory acceptance checks'}])
        results = []
        pending = iter(unique.values())
        workers = self.concurrency()
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1
        capacity = max(workers, self.workers or max(1, cpus - 1))
        total_nodes = len({node for unit in unique.values() for node in unit.expected_nodes})
        stopped = threading.Event()
        execution_cancel = Cancellation(cancel, stopped)
        with ThreadPoolExecutor(max_workers=capacity) as pool:
            running = {}
            def dispatch():
                unit = next(pending, None)
                if unit is not None:
                    running[pool.submit(self.execute, identity, snapshot, unit, execution_cancel)] = unit
            try:
                for _ in range(workers):
                    if not execution_cancel.is_set(): dispatch()
                failed = False
                while running:
                    done, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in done:
                        running.pop(future)
                        result = future.result(); results.append(result)
                        failed = failed or not result['ok']
                        if result['infrastructure']: stopped.set()
                        if self.callback: self.callback('check_finished', {**result, 'completed': len(results),
                            'total': len(unique), 'workers': workers, 'total_nodes': total_nodes,
                            'tested_nodes': len({node for check in results for node in check.get('collected', [])})})
                    if (collect_all or not failed) and not execution_cancel.is_set():
                        # Review/container memory can be released mid-run. Use
                        # newly available slots without exceeding live limits.
                        workers = min(capacity, self.concurrency())
                        for _ in range(max(0, workers - len(running))): dispatch()
            except BaseException:
                # Cancel siblings before the executor waits for them. Otherwise
                # one raised error could wait for unrelated 30-minute checks.
                stopped.set()
                raise
        # Checks already in flight complete and retain their evidence. New
        # expensive copies/containers are not queued after a known failure.
        actionable = [r for r in results if not r['ok'] and (r['infrastructure'] or r.get('failed') or not r.get('cancelled'))]
        failures = [{'unit': r['unit'], 'command': r['command'],
                     'reason': (r.get('diagnostic') if r['infrastructure'] else '') or r.get('excerpt')
                               or 'verification failed without output (exit ' + str(r.get('returncode')) + ')',
                     'failed': r['failed'], 'missing': r['missing'], 'infrastructure': r['infrastructure'],
                     'failure_details': r.get('failure_details', [])}
                    for r in actionable]
        return ValidationResult(not failures and not cancel.is_set(), identity, results, failures,
            cancel.is_set(), any(r['infrastructure'] for r in actionable))
