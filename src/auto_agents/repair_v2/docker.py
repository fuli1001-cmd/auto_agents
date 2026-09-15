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
from .store import atomic_json, digest
from .types import RepairBlocked, ValidationResult
from .workspace import source_identity
from .storage import disposable_source, execution_lease, recover_executions, require_space
from .cleanup import labels, reap_containers


MAX_OUTPUT_BYTES = 2 * 1024 * 1024


def run(command, *, cancel=None, timeout=1800, output=None, env=None):
    """Drain continuously, retain a bounded diagnostic tail and reap this group."""
    buffer, truncated, stopped = bytearray(), False, False
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env, start_new_session=True) as process:
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
        code, containers = run(['docker', 'ps', '-aq'], timeout=15)
        if code: raise RepairBlocked('docker_unavailable', containers)
        mounts = []
        if containers.strip():
            code, data = run(['docker', 'inspect', *containers.split()], timeout=15)
            if code: raise RepairBlocked('docker_unavailable', data)
            mounts = [Path(m['Source']).resolve() for c in json.loads(data) for m in c.get('Mounts', []) if m.get('Source')]
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
                               'kernel': os.uname().release, 'policy': 5, 'init': True})
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
        inputs = witness(snapshot, unit, self.runtime)
        key = cache_key(snapshot_id, unit, inputs)
        cache = self.root / 'cache' / (key + '.json')
        reusable = inputs['complete'] and not unit.fresh
        if cache.exists() and reusable:
            try:
                envelope = json.loads(cache.read_text())
                result = envelope['result']
                if (digest(result) == envelope.get('digest') and result.get('ok')
                        and result.get('inputs') == inputs and result.get('command') == unit.command):
                    return {**result, 'unit': unit.identity, 'cache_hit': True, 'seconds': 0.0}
            except (OSError, ValueError, KeyError, TypeError):
                pass  # A corrupt optimization record never blocks fresh proof.

        identity = uuid.uuid4().hex
        base = self.root / 'executions' / identity
        source, output = base / 'source', base / 'result'
        custody = {'clear': True}
        with execution_lease(base), disposable_source(snapshot, source, cleanup=lambda: custody['clear']):
            output.mkdir(exist_ok=True)
            return self._execute(snapshot_id, snapshot, unit, cancel, key, cache, inputs, identity, base, source, output, custody)

    def _execute(self, snapshot_id, snapshot, unit, cancel, key, cache, inputs, identity, base, source, output, custody):
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
        try:
            custody['clear'] = False
            code, text = run(command, cancel=cancel, timeout=self.timeout, output=base / 'output.log')
            info_code, info = run(['docker', 'inspect', name, '--format', '{{json .State}}'], timeout=10)
            state = json.loads(info) if not info_code else {}
        finally:
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
        infrastructure = bool(state.get('OOMKilled') or not state or code in (125, 126, 127))
        result = {'unit': unit.identity, 'command': unit.command, 'ok': code == 0 and valid_pytest and not infrastructure and unchanged,
            'source_unchanged': unchanged,
            'returncode': code, 'collected': sorted(collected), 'passed': sorted(passed),
            'failed': evidence.get('failed', []), 'skipped': evidence.get('skipped', []), 'missing': missing,
            'cache_hit': False, 'seconds': time.monotonic() - started, 'infrastructure': infrastructure,
            'output': str(base / 'output.log'), 'excerpt': text[-4000:], 'inputs': inputs}
        if result['ok'] and inputs['complete'] and not unit.fresh: atomic_json(cache, {'result': result, 'digest': digest(result)})
        return result

    def suite_units(self, snapshot, request):
        """Collect once after implementation; preserve every actual test node."""
        import shlex
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
        units = []
        for file, nodes in grouped.items():
            batches = [nodes[i:i + 32] for i in range(0, len(nodes), 32)]
            for index, batch in enumerate(batches):
                targets = [file] if len(batches) == 1 else batch
                units.append(ValidationUnit('suite:' + file + ':' + str(index),
                    'python -m pytest -q ' + ' '.join(shlex.quote(n) for n in targets),
                    expected_nodes=tuple(batch), profile='sandbox'))
        units.extend(ValidationUnit('required:' + digest(command)[:20], command, profile='sandbox')
                     for command in dict.fromkeys(c for item in request.acceptance for c in item.commands))
        return units

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
            result = self.execute(identity, baseline, ValidationUnit('behavior-baseline',
                'python -m pytest -q ' + ' '.join(shlex.quote(n) for n in nodes), fresh=True, profile='sandbox'), cancel)
            if cancel.is_set(): raise KeyboardInterrupt()
            infrastructure = result.get('infrastructure') or result['returncode'] in (125, 126, 127, 130, 137)
            demonstrated = result['returncode'] != 0 and not infrastructure
            return {'ok': not infrastructure and (demonstrated or not product_changed),
                    'snapshot': snapshot_id, 'base': base_commit, 'runtime': self.runtime,
                    'product_changed': product_changed, 'demonstrated_regression': demonstrated,
                    'counterexamples': result['failed'], 'returncode': result['returncode'],
                    'infrastructure': bool(infrastructure), 'output': result['output'],
                    'reason': result['excerpt'] if demonstrated or infrastructure else
                        'Original code passes every mapped check; add a behavioral regression test for the proposed fix.'}

    def boundary(self, snapshot_id, snapshot, frozen_target, payload, cancel):
        """One deterministic, credential-free replay at the original boundary."""
        from ..root_cause import RootCauseCoordinator
        from .evidence import identity as evidence_identity
        from .workspace import git
        identity = uuid.uuid4().hex
        base = self.root / 'executions' / identity
        name = 'aav2-' + identity
        target, source, output = base / 'target', base / 'source', base / 'result'
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
                atomic_json(output / 'request.json', {**payload, 'commit': git(source, 'rev-parse', 'HEAD')})
                command = ['docker', 'run', '--init', '--name', name, *labels(self.root, identity, kind='verification'), '--network', 'none', '--read-only',
                    '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--memory', '1g', '--pids-limit', '512',
                    '--tmpfs', '/tmp:rw,nosuid,exec,mode=1777,size=4g', '--workdir', '/target',
                    '-e', 'HOME=/tmp/home', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                    '-e', 'AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1', '-e', 'AUTO_AGENTS_STORAGE_DISABLED=1',
                    '--mount', f'type=bind,src={source},dst=/work,readonly',
                    '--mount', f'type=bind,src={target},dst=/target',
                    '--mount', f'type=bind,src={output},dst=/result']
                for script in (Path(__file__).with_name('boundary_driver.py'),
                               Path(__file__).parent.parent / 'session_replay.py'):
                    command += ['--mount', f'type=bind,src={script},dst=/opt/repair/{script.name},readonly']
                custody['clear'] = False
                code, text = run([*command, self.image, 'python', '/opt/repair/boundary_driver.py'],
                                 cancel=cancel, timeout=self.timeout, output=base / 'output.log')
                try: observed = json.loads((output / 'boundary.json').read_text())
                except (OSError, ValueError): observed = {'ok': False, 'error': text[-4000:]}
                after = evidence_identity(frozen_target)
                result = {'ok': code == 0 and observed.get('ok') is True and before == after,
                          'snapshot': snapshot_id, 'target': before, 'runtime': self.runtime,
                          'observed': observed, 'output': str(base / 'output.log'), 'returncode': code}
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
        with ThreadPoolExecutor(max_workers=workers) as pool:
            running = {}
            def dispatch():
                unit = next(pending, None)
                if unit is not None:
                    running[pool.submit(self.execute, identity, snapshot, unit, cancel)] = unit
            for _ in range(workers):
                if not cancel.is_set(): dispatch()
            failed = False
            while running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    running.pop(future)
                    result = future.result(); results.append(result)
                    failed = failed or not result['ok']
                    if self.callback: self.callback('check_finished', {**result, 'completed': len(results), 'total': len(unique)})
                if (collect_all or not failed) and not cancel.is_set():
                    for _ in done: dispatch()
        # Checks already in flight complete and retain their evidence. New
        # expensive copies/containers are not queued after a known failure.
        failures = [{'unit': r['unit'], 'command': r['command'], 'reason': r['excerpt'],
                     'failed': r['failed'], 'missing': r['missing']} for r in results if not r['ok'] and not (cancel.is_set() and (r.get('returncode') == 130 or r.get('cancelled')))]
        return ValidationResult(not failures and not cancel.is_set(), identity, results, failures,
            cancel.is_set(), any(r['infrastructure'] for r in results))
