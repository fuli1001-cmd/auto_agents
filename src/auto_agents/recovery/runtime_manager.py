"""Automatic source adoption before business custody, with one installed oracle."""
from contextlib import contextmanager, ExitStack, nullcontext
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4

from .model import digest, require, KernelError
from .store import KernelStore
from . import runtime_lifecycle as lifecycle
from .runtime_source import capture, source_identity


def notice(message):
    print('[运行版本] ' + message, file=sys.stderr, flush=True)


@contextmanager
def adoption_lock(store):
    with (store.root / 'runtime-adoption.lock').open('a+') as lock:
        announced = False
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced: notice('等待另一条命令完成版本检查'); announced = True
                time.sleep(.1)
        yield


def bound_source(store):
    operator = store.root / 'operator.json'
    config = json.loads(operator.read_text()) if operator.is_file() else {}
    source = config.get('source_root') or store.meta('runtime_source_root')
    require(bool(source) and Path(source).is_dir(), 'runtime_source', 'Installation source_root is unavailable')
    return Path(source).resolve()


def _transaction(store, key, **changes):
    with store.connect() as db:
        row = db.execute('SELECT payload FROM kernel_runtime_adoptions WHERE id=?', (key,)).fetchone()
        value = {**(json.loads(row['payload']) if row else {}), **changes}
        db.execute('INSERT INTO kernel_runtime_adoptions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                   (key, json.dumps(value)))


def environment_key(store, runtime):
    trusted = store.meta('trusted_verifier_runtime')
    require(trusted is not None, 'upgrade_verifier', 'An installed independent verifier is required')
    packages = sorted((p.metadata.get('Name', ''), p.version) for p in importlib.metadata.distributions())
    return digest({'source': runtime['source'], 'verifier': trusted['artifact_id'],
                   'policy': store.meta('trusted_verifier'), 'python': sys.version,
                   'executable': str(Path(sys.executable).resolve()), 'packages': packages,
                   'platform': platform.platform(), 'protocol': 2,
                   'manager_source': source_identity(Path(__file__).resolve().parents[3])})


def oracle(store, runtime, action, receipt=None):
    """Use the installed verifier's driver and corpus, never the candidate's."""
    trusted = store.meta('trusted_verifier_runtime')
    require(trusted is not None, 'upgrade_verifier', 'Independent verifier is not installed')
    from ..repair_v2.runtime_artifact import verify
    verify(trusted)
    script = '''import json,sys
sys.path.insert(0,sys.argv[4]+'/src')
from auto_agents.recovery.store import KernelStore
from auto_agents.recovery import release
from auto_agents.recovery.model import KernelError
s=KernelStore(sys.argv[1]); r=json.loads(sys.argv[2]); action=sys.argv[3]
failure={}
original=release.independent_verify
def observe(store,runtime,checks,verifier):
 def wrap(name,probe):
  def execute(candidate):
   value=probe(candidate)
   if value.get('ok') and name == 'all_entrypoints':
    import importlib.util
    from pathlib import Path
    path=Path(sys.argv[6])/'src/auto_agents/recovery/runtime_probes.py'
    spec=importlib.util.spec_from_file_location('installed_runtime_probes',path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    extra=module.run(store,sys.argv[6],candidate,value['image'])
    value={**value,'runtime_lifecycle':extra,'ok':extra['ok']}
    if not extra['ok']: value.update(report=extra['report'],log=extra['log'])
   if not value.get('ok'):
    report=value.get('report',{})
    import re
    log=store.read_bytes(value['log']).decode(errors='replace') if value.get('log') else ''
    transient=re.search(r'DiskSpaceError|PermissionError|TimeoutError|MemoryError|ConnectionError|'
                        r'No space left|Cannot connect to.*daemon|Resource temporarily unavailable',log,re.I)
    failure.update(check=name,log=value.get('log'),
        deterministic=bool(report.get('failed')) and report.get('exitstatus') == 1
                      and 'AssertionError' in log and not transient)
   return value
  return execute
 return original(store,runtime,{k:wrap(k,v) for k,v in checks.items()},verifier)
release.independent_verify=observe
try:
 if action == 'static':
  result=release.verify_release(s,r,sys.argv[4],callback=lambda name:print('[运行版本] 验证 '+name,file=sys.stderr,flush=True))
 else:
  result=release.verify_current_journal(s,r,json.loads(sys.argv[5]))
except KernelError as error:
 print('RUNTIME_FAILURE:'+json.dumps({'diagnostic':str(error),**failure})); raise SystemExit(3)
print('RUNTIME_RECEIPT:'+json.dumps(result))
'''
    lifecycle.register(store, trusted)
    with lifecycle.using(store, trusted, 'verifier'):
        env = {**os.environ, 'PYTHONPATH': str(Path(trusted['path']) / 'src'),
               'AUTO_AGENTS_RECOVERY_CONTROL': str(store.root), 'AUTO_AGENTS_STORAGE_MAINTENANCE': 'off'}
        result = subprocess.run([sys.executable, '-c', script, str(store.root), json.dumps(runtime), action,
                                 trusted['path'], json.dumps(receipt), str(Path(__file__).resolve().parents[3])], env=env, cwd=trusted['path'],
                                stdout=subprocess.PIPE, text=True)
    failures = [json.loads(line[len('RUNTIME_FAILURE:'):]) for line in result.stdout.splitlines() if line.startswith('RUNTIME_FAILURE:')]
    require(result.returncode == 0, 'runtime_verification',
            '新源码验证未通过；本次命令尚未启动，原运行版本仍被保留', **(failures[-1] if failures else {}))
    lines = [line[len('RUNTIME_RECEIPT:'):] for line in result.stdout.splitlines() if line.startswith('RUNTIME_RECEIPT:')]
    require(len(lines) == 1, 'upgrade_receipt', 'Independent verifier did not return one receipt')
    return json.loads(lines[0])


def _cached_receipt(store, runtime, key):
    ref = store.meta('runtime_static:' + key)
    if not ref: return None
    receipt = store.read(ref)
    from .upgrade import check_receipt
    try:
        check_receipt(store, receipt, runtime)
        image = store.read(receipt['checks']['journal_replay']).get('image')
        if image:
            result = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=15)
            if result.returncode: return None
        return receipt
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError): return None


def wait_business(store):
    from ..artifact_store import alive
    announced = False
    while True:
        with store.connect() as db:
            owners = [json.loads(row[0]) for row in db.execute(
                "SELECT owner FROM kernel_runtime_uses WHERE purpose='business'")]
        if not any(alive(owner) for owner in owners): return
        if not announced: notice('等待正在运行的任务结束后切换；可按 Ctrl-C 取消本次等待'); announced = True
        time.sleep(.2)


def cutover(store, runtime, receipt):
    from ..run_lock import ProjectRunLock, RunAlreadyActiveError
    from .migration import check, apply_project, import_repairs
    from .authority import activate_project
    from .upgrade import activate
    wait_business(store)
    # Waiting does not fence already-admitted work. Only the short locked
    # cutover enters draining mode, so existing work can release its locks.
    while True:
        checked = check(store.root)
        if checked.get('active_operations'):
            require(all(row.get('state') != 'unreadable_lease' for row in checked['active_operations']),
                    'migration_blocked', '旧操作的运行凭据无法读取，已保留现场', report=checked)
            notice('等待旧版本的后台操作结束')
            time.sleep(.5)
            continue
        require(checked['ok'], 'migration_blocked', '现有操作尚未完成对账，无法切换运行版本', report=checked)
        locks = ExitStack()
        try:
            for manifest in checked['projects']:
                locks.enter_context(ProjectRunLock(Path(manifest['project'])))
        except RunAlreadyActiveError:
            locks.close()
            notice('等待旧版本持有的项目锁')
            time.sleep(.5)
            continue
        break
    prior = store.meta('mode', 'active')
    with locks:
        require(check(store.root) == checked, 'migration_changed', 'Business inputs changed before cutover')
        from ..artifact_store import process_identity
        store.set_meta('runtime_cutover', {'owner': process_identity(), 'prior': prior,
                       'before': store.meta('active_runtime'), 'after': runtime})
        store.set_meta('mode', 'draining')
        try:
            for manifest in checked['projects']:
                apply_project(store, manifest)
                import_repairs(store, store.root, manifest['project'])
            receipt = oracle(store, runtime, 'journal', receipt)
            result = activate(store, runtime, receipt, migration_manifests=checked['projects'])
            for manifest in checked['projects']: activate_project(store, manifest['project'])
        finally:
            if store.meta('mode') == 'draining': store.set_meta('mode', prior)
            store.set_meta('runtime_cutover', None)
    # The supervisor consumes the same sealed source, including dirty edits.
    sync_supervisor(store)
    return result


def sync_supervisor(store):
    operator = store.root / 'operator.json'
    if operator.is_file():
        from ..repair_control import ensure_supervisor
        ensure_supervisor(json.loads(operator.read_text()))


def ensure_current_runtime(store, source=None, *, automatic=True, retry=False):
    """Return the adopted artifact; caller holds adoption_lock through acquire."""
    from ..artifact_store import process_identity
    from ..repair_v2.runtime_artifact import verify
    source = Path(source).resolve() if source else bound_source(store)
    recover_cutover(store)
    if automatic:
        from .runtime_delivery import resume
        resume(store)
    lifecycle.migrate(store)
    for attempt in range(3):
        wanted = source_identity(source)
        active = store.meta('active_runtime')
        if active and active['source'] == wanted:
            verify(active)
            lifecycle.register(store, active)
            sync_supervisor(store)
            return active
        notice('检测到源码变化，准备并验证 ' + wanted[:12])
        transaction = uuid4().hex
        _transaction(store, transaction, owner=process_identity(), status='capturing', source_root=str(source))
        runtime = None
        key = None
        try:
            runtime = capture(store, source, expected=wanted)
            _transaction(store, transaction, status='verifying', runtime=runtime)
            key = environment_key(store, runtime)
            failure = store.meta('runtime_failure:' + key) if not retry else None
            if failure:
                raise KernelError('runtime_verification',
                    '相同源码和环境的验证此前失败，请修复后重试；可用 repair upgrade 显式重新验证', **failure)
            receipt = None if retry else _cached_receipt(store, runtime, key)
            if receipt is None:
                receipt = oracle(store, runtime, 'static')
                store.set_meta('runtime_static:' + key, store.put(receipt))
            if source_identity(source) != wanted:
                _transaction(store, transaction, status='superseded')
                continue
            _transaction(store, transaction, status='waiting')
            wait_business(store)
            if source_identity(source) != wanted:
                _transaction(store, transaction, status='superseded')
                continue
            result = cutover(store, runtime, receipt)
            _transaction(store, transaction, status='complete', result=result)
            notice('已采用源码 ' + wanted[:12])
            return runtime
        except BaseException as error:
            _transaction(store, transaction, status='failed', error=str(error))
            if key and isinstance(error, KernelError) and error.details.get('deterministic'):
                store.set_meta('runtime_failure:' + key, error.details)
            if isinstance(error, KernelError) and error.code == 'source_changed' and attempt < 2: continue
            raise
        finally:
            if runtime: lifecycle.release_produced(store, runtime)
            lifecycle.maintain(store)
    raise KernelError('source_changed', '源码在启动期间持续变化，请完成修改后重试原命令')


def recover_cutover(store):
    """The adoption lock proves no other adopter can still own this fence."""
    pending = store.meta('runtime_cutover')
    if not pending: return
    from ..artifact_store import alive
    require(not alive(pending['owner']), 'upgrade_busy', 'The prior cutover owner is still alive')
    active = store.meta('active_runtime')
    require(active in (pending['before'], pending['after']), 'upgrade_recovery', 'Cutover runtime identity changed')
    if active == pending['after']:
        from .authority import activate_project
        for project in store.meta('activation', {}).get('projects', []): activate_project(store, project)
    elif store.meta('mode') == 'draining': store.set_meta('mode', pending['prior'])
    store.set_meta('runtime_cutover', None)


def adopt_source(control, source, *, retry=True):
    store = KernelStore(control)
    with adoption_lock(store):
        runtime = ensure_current_runtime(store, source, automatic=False, retry=retry)
    return {'ok': True, 'runtime': runtime['artifact_id'], 'epoch': store.meta('epoch')}


def launch(store, arguments, *, artifact=None):
    """Parent custody survives child exec and preserves terminal signal behavior."""
    with adoption_lock(store) if artifact is None else nullcontext():
        runtime = artifact or ensure_current_runtime(store)
        if artifact is None:
            for _ in range(3):
                if source_identity(bound_source(store)) == runtime['source']: break
                runtime = ensure_current_runtime(store)
            else: raise KernelError('source_changed', '源码在启动期间持续变化，请完成修改后重试原命令')
        lifecycle.register(store, runtime)
        token = lifecycle.acquire(store, runtime, 'business' if artifact is None else 'maintenance')
    env = {**os.environ, 'PYTHONPATH': str(Path(runtime['path']) / 'src'),
           'AUTO_AGENTS_RECOVERY_CONTROL': str(store.root), 'AUTO_AGENTS_RUNTIME_USE': token,
           'AUTO_AGENTS_RUNTIME_ID': runtime['artifact_id']}
    script = ('import sys; sys.path.insert(0,' + repr(str(Path(runtime['path']) / 'src'))
              + '); from auto_agents.cli import main; raise SystemExit(main(sys.argv[1:]))')
    try:
        child = subprocess.Popen([sys.executable, '-c', script, *arguments], env=env, cwd=os.getcwd())
        import signal
        previous = {}
        def forward(signum, frame):
            if child.poll() is None: child.send_signal(signum)
        try:
            for signum in (signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(signum, forward)
            while True:
                try:
                    code = child.wait()
                    return code if code >= 0 else 128 - code
                except KeyboardInterrupt:
                    # Foreground child receives terminal SIGINT too. Its lease
                    # remains until it actually exits, including cleanup.
                    continue
        finally:
            for signum, handler in previous.items(): signal.signal(signum, handler)
    finally:
        lifecycle.release(store, token)


def inherited(store):
    """Environment text alone cannot grant a pinned-runtime bypass."""
    token = os.environ.get('AUTO_AGENTS_RUNTIME_USE', '')
    if not token: return None
    from ..artifact_store import alive
    with store.connect() as db:
        row = db.execute('SELECT r.artifact,u.owner,u.purpose FROM kernel_runtime_uses u JOIN kernel_runtimes r ON r.id=u.runtime '
                         "WHERE u.token=? AND r.state='ready'", (token,)).fetchone()
    require(row is not None and alive(json.loads(row['owner'])), 'runtime_lease', 'Inherited runtime lease is no longer valid')
    artifact = json.loads(row['artifact'])
    require(artifact['artifact_id'] == os.environ.get('AUTO_AGENTS_RUNTIME_ID'), 'runtime_lease', 'Inherited runtime identity changed')
    if json.loads(row['owner'])['pid'] != os.getpid():
        # Every internal CLI process has its own owner; a surviving child keeps
        # business custody even after its launch wrapper is killed.
        child_token = lifecycle.acquire(KernelStore(store.root), artifact, row['purpose'], parent=token)
        os.environ['AUTO_AGENTS_RUNTIME_USE'] = child_token
    return artifact


def release_inherited():
    token = os.environ.get('AUTO_AGENTS_RUNTIME_USE')
    root = os.environ.get('AUTO_AGENTS_RECOVERY_CONTROL')
    if not token or not root: return
    store = KernelStore(root)
    with store.connect() as db:
        row = db.execute('SELECT owner FROM kernel_runtime_uses WHERE token=?', (token,)).fetchone()
    if row and json.loads(row['owner'])['pid'] == os.getpid(): lifecycle.release(store, token)


def suspend_business(store):
    """Self-repair hands off its own process and waiting launch wrappers."""
    token = os.environ.get('AUTO_AGENTS_RUNTIME_USE')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        while token:
            row = db.execute('SELECT owner FROM kernel_runtime_uses WHERE token=?', (token,)).fetchone()
            if not row: break
            db.execute("UPDATE kernel_runtime_uses SET purpose='continuation' WHERE token=?", (token,))
            token = json.loads(row['owner']).get('parent')
    for name in ('AUTO_AGENTS_RUNTIME_USE', 'AUTO_AGENTS_RUNTIME_ID'):
        os.environ.pop(name, None)


if __name__ == '__main__':
    store = KernelStore(sys.argv[1])
    try:
        arguments = sys.argv[2:]
        if arguments[:2] == ['repair', 'upgrade']:
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument('--runtime', required=True)
            parser.add_argument('--json', action='store_true')
            options = parser.parse_args(arguments[2:])
            print(json.dumps(adopt_source(store.root, options.runtime))); code = 0
        elif (not arguments or any(a in {'-h', '--help'} for a in arguments)
              or arguments[0] in {'status', 'stop', 'cancel', 'storage'}
              or arguments[:2] in (['repair','status'], ['repair','migrate'], ['repair','cancel'])):
            code = launch(store, arguments, artifact=store.meta('active_runtime'))
        else: code = launch(store, arguments)
    except (OSError, RuntimeError, ValueError) as error:
        notice(str(error)); code = 3
        details = getattr(error, 'details', {})
        if details.get('check'): notice('失败检查：' + details['check'])
        if details.get('log'): notice('诊断日志：' + str(store.blob_path(details['log'])))
    finally:
        manager_token = os.environ.get('AUTO_AGENTS_MANAGER_USE')
        if manager_token:
            with store.connect() as db:
                row = db.execute('SELECT owner FROM kernel_runtime_uses WHERE token=?', (manager_token,)).fetchone()
            if row and json.loads(row['owner'])['pid'] == os.getpid(): lifecycle.release(store, manager_token)
    raise SystemExit(code)
