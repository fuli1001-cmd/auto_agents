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

from .dependencies import cache_key, pytest_parts, witness
from .store import atomic_json, digest
from .types import RepairBlocked, ValidationResult
from .workspace import source_identity
from .storage import disposable_source, execution_lease, recover_executions, require_space


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
                 vitest_version='5.0.0', workers=None, callback=None, timeout=1800, python=None):
        self.root = Path(root)
        self.requirements, self.node_version, self.vitest_version = tuple(requirements), node_version, vitest_version
        self.image, self.workers, self.callback, self.timeout = image, workers, callback, timeout
        self.runtime = ''
        self.python = python or __import__('sys').executable

    def prepare(self):
        if not shutil.which('docker'):
            raise RepairBlocked('docker_missing', 'self-repair V2 requires Docker on Linux/WSL')
        code, text = run(['docker', 'info', '--format', '{{.OSType}}'], timeout=15)
        if code or text.strip() != 'linux': raise RepairBlocked('docker_unavailable', text.strip())
        require_space(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
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
            self.image = tool_image(self.root, self.python, driver)
        code, identity_text = run(['docker', 'image', 'inspect', self.image, '--format', '{{.Id}}'], timeout=15)
        if code: raise RepairBlocked('image_unavailable', identity_text)
        self.runtime = digest({'image': identity_text.strip(), 'driver': driver,
                               'kernel': os.uname().release, 'policy': 2})

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
        if cache.exists() and not unit.fresh:
            envelope = json.loads(cache.read_text())
            result = envelope['result']
            if digest(result) == envelope.get('digest') and result.get('ok'):
                return {**result, 'cache_hit': True, 'seconds': 0.0}
        identity = uuid.uuid4().hex
        base = self.root / 'executions' / identity
        source, output = base / 'source', base / 'result'
        custody = {'clear': True}
        with execution_lease(base), disposable_source(snapshot, source, cleanup=lambda: custody['clear']):
            output.mkdir(exist_ok=True)
            return self._execute(snapshot_id, snapshot, unit, cancel, key, cache, inputs, identity, base, source, output, custody)

    def _execute(self, snapshot_id, snapshot, unit, cancel, key, cache, inputs, identity, base, source, output, custody):
        name = 'aav2-' + identity
        command = ['docker', 'run', '--name', name, '--network', 'none', '--read-only',
            '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '1g', '--pids-limit', '512', '--tmpfs', '/tmp:rw,nosuid,mode=1777',
            '-e', 'HOME=/tmp/home', '--workdir', '/work',
            '--mount', 'type=bind,src=' + str(source) + ',dst=/work',
            '--mount', 'type=bind,src=' + str(output) + ',dst=/result',
            '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/work/src',
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
        valid_pytest = (parts is None or bool(collected) and collected <= passed
                        and not evidence.get('failed') and not evidence.get('skipped') and not missing)
        infrastructure = bool(state.get('OOMKilled') or not state or code in (125, 126, 127))
        result = {'unit': unit.identity, 'command': unit.command, 'ok': code == 0 and valid_pytest and not infrastructure,
            'returncode': code, 'collected': sorted(collected), 'passed': sorted(passed),
            'failed': evidence.get('failed', []), 'skipped': evidence.get('skipped', []), 'missing': missing,
            'cache_hit': False, 'seconds': time.monotonic() - started, 'infrastructure': infrastructure,
            'output': str(base / 'output.log'), 'excerpt': text[-4000:], 'inputs': inputs}
        if result['ok'] and not unit.fresh: atomic_json(cache, {'result': result, 'digest': digest(result)})
        return result

    def validate(self, identity, snapshot, units, cancel):
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
                    if self.callback: self.callback('check_finished', result)
                if not failed and not cancel.is_set():
                    for _ in done: dispatch()
        # Checks already in flight complete and retain their evidence. New
        # expensive copies/containers are not queued after a known failure.
        failures = [{'unit': r['unit'], 'command': r['command'], 'reason': r['excerpt'],
                     'failed': r['failed'], 'missing': r['missing']} for r in results if not r['ok']]
        return ValidationResult(not failures and not cancel.is_set(), identity, results, failures,
            cancel.is_set(), any(r['infrastructure'] for r in results))
