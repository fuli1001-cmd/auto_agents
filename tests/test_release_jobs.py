from __future__ import annotations
import subprocess
import tempfile
import io
import contextlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from auto_agents.cli import main
from auto_agents.foreground_activity import ForegroundActivity, foreground_active
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.git_ops import add_worktree, list_worktrees
from auto_agents.models import GateConfig
from auto_agents.release_jobs import ReleaseJobStore

def _repo(root: Path) -> None:
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 'tests@example.com'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Tests'], cwd=root, check=True)
    (root / 'value.txt').write_text('one\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'value.txt'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'one'], cwd=root, check=True)

def test_release_jobs_coalesce_to_latest_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        first = store.enqueue(source='fix:first', affected_proof_ids=['affected.one'])
        (root / 'value.txt').write_text('two\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'value.txt'], cwd=root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'two'], cwd=root, check=True)
        second = store.enqueue(source='fix:second', affected_proof_ids=['affected.two'])
        assert first['job_id'] != second['job_id']
        assert store.get(str(first['job_id']))['status'] == 'superseded'
        assert store.latest()['job_id'] == second['job_id']
        claimed = store.claim_latest()
        assert claimed is not None
        assert claimed['status'] == 'running'
        passed = store.complete(str(claimed['job_id']), {'ok': True, 'proof_ids': ['release.all'], 'logical_commands': 1})
        assert passed['status'] == 'passed'

def test_same_passed_candidate_is_not_requeued() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        job = store.enqueue(source='run:first', affected_proof_ids=[])
        store.complete(str(job['job_id']), {'ok': True})
        duplicate = store.enqueue(source='run:again', affected_proof_ids=[])
        assert duplicate['job_id'] == job['job_id']
        assert duplicate['status'] == 'passed'

def test_abandoned_active_job_is_requeued_after_worker_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _repo(root)
        store = ReleaseJobStore(root)
        store.enqueue(source='run:first', affected_proof_ids=[])
        claimed = store.claim_latest()
        assert claimed is not None and claimed['status'] == 'running'
        assert store.requeue_abandoned() == 1
        assert store.latest()['status'] == 'pending'

def test_attest_requires_exact_clean_head() -> None:
    from auto_agents.control import Store
    from auto_agents.control.workspace import git
    from auto_agents.config import save_project_config
    from auto_agents.models import ProjectConfig
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);_repo(root)
        save_project_config(root,ProjectConfig('fixture'))
        store=Store(root);revision=git(root,'rev-parse','HEAD')
        work=store.create_workflow('run','Full release verification','source',inputs={'release_only':True,'release_full':True})
        work=store.transition(work,'RUNNING')
        store.transition(work,'COMPLETED',result={'verified_revision':revision,'verification':{'ok':True}})
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            assert main(['attest','--project',str(root)])==0
        (root/'value.txt').write_text('dirty\n',encoding='utf-8')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            assert main(['attest','--project',str(root)])==3
        assert store.works()[0]['status']=='COMPLETED'

def test_foreground_activity_lease_is_observable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lease = ForegroundActivity(root)
        assert not foreground_active(root)
        lease.acquire()
        try:
            assert foreground_active(root)
        finally:
            lease.release()
        assert not foreground_active(root)

def test_local_gate_executor_preempts_before_background_dispatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'project'
        root.mkdir()
        _repo(root)
        executor = LocalGatePlanExecutor(root, GateConfig(), {}, preempt_requested=lambda: True)
        with executor:
            result = executor.run("python -c 'raise SystemExit(99)'", timeout_seconds=10, adaptive_timeout_enabled=False, idle_timeout_seconds=10)
        assert not result.ok
        assert result.termination_reason == 'foreground_preempted'
        assert result.infrastructure_failure_id == 'foreground_preempted'
