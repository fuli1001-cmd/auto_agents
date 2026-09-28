"""Stable, network-free release gates; candidates cannot select their tests."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading

from .model import digest, require
from .store import KernelStore
from .upgrade import MANDATORY_CHECKS, activate, independent_verify

GATES = {
    'journal_replay': ['tests/test_recovery_kernel.py'],
    'all_entrypoints': ['tests/test_recovery_native.py', 'tests/test_recovery_engine.py', 'tests/test_recovery_bootstrap.py'],
    'migration': ['tests/test_recovery_migration.py'],
    'receipt_integrity': ['tests/test_session_candidate_receipts.py'],
    'closed_incident_upgrade': ['tests/test_recovery_kernel.py::test_closed_incident_survives_version_adoption'],
    'unknown_effect': ['tests/test_recovery_kernel.py::test_unknown_model_outcome_is_reconciled_without_second_dispatch'],
    'host_confinement': ['tests/test_verification_runtime_mounts.py', 'tests/test_verification_metadata.py'],
    'review_protocol': ['tests/test_recovery_kernel.py::test_recorded_zero_thirteen_review_has_precise_diagnostics'],
    'candidate_feedback': ['tests/test_candidate_selection_feedback.py'],
    'restart_matrix': ['tests/test_recovery_restart.py'],
    'rollback_compatibility': ['tests/test_recovery_upgrade.py'],
}

PLUGIN = '''import json
from pathlib import Path
FAILED=[]
PASSED=[]
SKIPPED=[]
def pytest_runtest_logreport(report):
    if report.failed: FAILED.append(report.nodeid)
    if report.skipped: SKIPPED.append(report.nodeid)
    if report.when == 'call' and report.passed: PASSED.append(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    Path('/result/proof.json').write_text(json.dumps({'exitstatus':int(exitstatus),
        'failed':FAILED,'passed':PASSED,'skipped':SKIPPED,'collected':session.testscollected}))
'''


def verifier_identity(root):
    root = Path(root)
    files = [root / 'conftest.py', *sorted((root / 'tests').glob('*.py'))]
    import hashlib
    return digest({'gates': GATES, 'files': {str(p.relative_to(root)):
        hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'driver':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'protocol': 2})


def verify_release(store, runtime, trusted_root, *, callback=None):
    from ..repair_v2.docker import DockerVerifier, REPLAY_ISOLATION, run
    from ..repair_v2.runtime_artifact import verify
    adopted = store.meta('active_runtime')
    trusted_runtime = store.meta('trusted_verifier_runtime') if adopted else None
    if trusted_runtime is None:
        trusted_runtime = prepare_runtime(store,trusted_root)
    verify(trusted_runtime)
    trusted_root = trusted_runtime['path']
    trusted_root = Path(trusted_root).resolve()
    verifier = verifier_identity(trusted_root)
    installed = store.meta('trusted_verifier') if adopted else None
    require(installed in (None, verifier), 'verifier_upgrade', 'Stable verifier changes require a separate installation update')
    if installed is None: store.set_meta('trusted_verifier', verifier)
    assert set(GATES) == MANDATORY_CHECKS
    from .environment import prepare
    runner = DockerVerifier(store.root / 'kernel-verification',python=prepare(store,trusted_root))
    runner.prepare()
    with tempfile.TemporaryDirectory(prefix='kernel-oracle-', dir=store.root) as temporary:
        oracle = Path(temporary)
        shutil.copytree(trusted_root / 'tests', oracle / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(trusted_root / 'conftest.py', oracle / 'conftest.py')
        (oracle / 'kernel_gate_plugin.py').write_text(PLUGIN)
        (oracle / 'pytest.ini').write_text('[pytest]\n')
        def probe(name):
            def execute(artifact):
                if callback: callback(name)
                result = oracle / ('result-' + name); result.mkdir()
                command = ['docker','run','--rm','--init','--network','none','--read-only',
                    '--user',f'{os.getuid()}:{os.getgid()}',*REPLAY_ISOLATION['session'],
                    '--memory','2g','--pids-limit','512','--tmpfs','/tmp:rw,nosuid,exec,mode=1777,size=4g',
                    '--workdir','/work','-e','HOME=/tmp/home','-e','PYTHONDONTWRITEBYTECODE=1',
                    '-e','PYTHONPATH=/work/src:/checks','-e','AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1',
                    '--mount',f'type=bind,src={artifact["path"]},dst=/work,readonly',
                    '--mount',f'type=bind,src={oracle / "tests"},dst=/work/tests,readonly',
                    '--mount',f'type=bind,src={oracle / "conftest.py"},dst=/work/conftest.py,readonly',
                    '--mount',f'type=bind,src={oracle},dst=/checks,readonly',
                    '--mount',f'type=bind,src={result},dst=/result', runner.image, 'python','-c',
                    "from pathlib import Path; import pytest; p=Path('/tmp/home'); p.mkdir(); "
                    "(p/'.gitconfig').write_text('[user]\\n name = verification\\n email = verification@localhost\\n'); "
                    'raise SystemExit(pytest.main(' + repr(['-q','-p','kernel_gate_plugin','-p','no:cacheprovider',
                        '-c','/checks/pytest.ini','--confcutdir=/work',
                        *['/work/' + path for path in GATES[name]]]) + '))']
                code, text = run(command, timeout=1800, output=result / 'output.log')
                proof = json.loads((result/'proof.json').read_text()) if (result/'proof.json').exists() else {}
                return {'ok': code == 0 and proof.get('exitstatus') == 0 and bool(proof.get('passed'))
                        and not proof.get('failed') and not proof.get('skipped'),
                        'report': proof, 'log': store.put_file(result/'output.log'), 'image': runner.image,
                        'verifier_runtime': runner.runtime}
            return execute
        receipt = independent_verify(store, runtime, {name: probe(name) for name in GATES}, verifier)
        receipt = {**receipt,'trusted_runtime':trusted_runtime}
        reference = store.put(receipt)
        store.set_meta('verified_upgrades',[*store.meta('verified_upgrades',[]),reference])
        return receipt


def prepare_runtime(store, source):
    from ..repair_v2.runtime_artifact import build
    from ..repair_v2.workspace import git, source_identity
    source = Path(source).resolve()
    require(not git(source,'status','--porcelain'), 'runtime_dirty', 'Commit the complete candidate before core adoption')
    return build(store.root / 'kernel-releases', source, source_identity(source), {'kernel_schema': 1, 'rpc': 2})


def verify_current_journal(store, runtime, receipt):
    """The proposed core must replay the actual history at the cutover fence."""
    from .upgrade import check_receipt
    from ..repair_v2.docker import run, REPLAY_ISOLATION
    check_receipt(store,receipt,runtime)
    fixture = store.read(receipt['checks']['journal_replay'])
    image = fixture['image']
    require(image.startswith('sha256:'),'upgrade_image','Replay requires the immutable gate image')
    with tempfile.TemporaryDirectory(prefix='kernel-replay-',dir=store.root) as temporary:
        directory = Path(temporary)
        with store.connect() as source, sqlite3.connect(directory/'control.sqlite3') as target:
            source.execute('BEGIN')
            frontier = digest({r['id']:r['revision'] for r in source.execute('SELECT id,revision FROM kernel_streams')})
            source.backup(target)
        script = "import sys\nsys.path.insert(0,'/work/src')\nfrom auto_agents.recovery.store import KernelStore\ns=KernelStore('/state',readonly=True)\nwith s.connect() as db:\n ids=[r['id'] for r in db.execute('SELECT id FROM kernel_streams')]\nfor identity in ids: s.replay(identity)\nprint(s.frontier())\n"
        command = ['docker','run','--rm','--init','--network','none','--read-only','--user',f'{os.getuid()}:{os.getgid()}',
            *REPLAY_ISOLATION['standard'],'--memory','1g','--pids-limit','128','--tmpfs','/tmp:rw,nosuid,mode=1777,size=256m',
            '-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPATH=/work/src',
            '--mount',f'type=bind,src={runtime["path"]},dst=/work,readonly',
            '--mount',f'type=bind,src={directory},dst=/state,readonly',image,'python','-c',script]
        code, output = run(command,timeout=120,output=directory/'replay.log')
        log_ref = store.put_file(directory/'replay.log')
        require(code == 0 and output.strip().splitlines()[-1:] == [frontier],
                'upgrade_journal','Proposed runtime cannot replay current business history',
                log_ref=log_ref,returncode=code,detail=output[-2000:])
        require(store.frontier() == frontier,'upgrade_journal','Business history changed while replaying')
        proof = {**fixture,'ok':True,'state_frontier':frontier,'fixture_proof':receipt['checks']['journal_replay'],
                 'log':log_ref}
        updated = {**receipt,'state_frontier':frontier,'checks':{**receipt['checks'],'journal_replay':store.put(proof)}}
        reference = store.put(updated)
        store.set_meta('verified_upgrades',[*store.meta('verified_upgrades',[]),reference])
        return updated


def upgrade(control, source, trusted_root, *, callback=None):
    installed = KernelStore(control, readonly=True).meta('active_runtime')
    if installed and (Path(installed['path']) / 'src/auto_agents/recovery/runtime_manager.py').is_file():
        from .runtime_manager import adopt_source
        return adopt_source(control, source)
    from contextlib import ExitStack
    from ..run_lock import ProjectRunLock
    from .migration import check, apply_project, import_repairs
    from .authority import activate_project
    store = KernelStore(control)
    runtime = prepare_runtime(store, source)
    receipt = verify_release(store, runtime, trusted_root, callback=callback)
    operator = store.root / 'operator.json'
    if operator.is_file():
        from ..repair_control import ensure_supervisor, rpc
        config = json.loads(operator.read_text())
        ensure_supervisor(config)
        require('recovery-kernel-v1' in rpc(config, {'op':'ping'}).get('capabilities', []),
                'supervisor_protocol', 'Supervisor cannot fence legacy dispatch during adoption')
    checked = check(control)
    require(checked['ok'], 'migration_blocked', 'Upgrade tests passed but live migration is not quiescent', report=checked)
    # Acquire every project lock in a stable order before checking or changing
    # any ownership marker. No project is switched ahead of the others.
    prior_mode = store.meta('mode', 'staged')
    # The compatible supervisor observes this fence before launching another
    # legacy worker; active native invocations are drained by the project locks.
    store.set_meta('mode', 'draining')
    try:
        with ExitStack() as locks:
            for manifest in checked['projects']:
                locks.enter_context(ProjectRunLock(Path(manifest['project'])))
            current = check(control)
            require(current == checked, 'migration_changed', 'Control inputs changed during release verification')
            for manifest in current['projects']:
                apply_project(store, manifest)
                import_repairs(store, control, manifest['project'])
            receipt = verify_current_journal(store,runtime,receipt)
            result = activate(store, runtime, receipt, migration_manifests=current['projects'])
            for manifest in current['projects']: activate_project(store, manifest['project'])
    except BaseException:
        if store.meta('mode') == 'draining': store.set_meta('mode', prior_mode)
        raise
    return result
