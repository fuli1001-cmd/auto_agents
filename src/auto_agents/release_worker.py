from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping, Optional, TextIO

from .gate_execution import discover_dependency_links, install_dependency_links
from .config import gate_baseline_cache_path
from .foreground_activity import foreground_active
from .git_ops import (
    add_worktree,
    commit_all_except,
    head_ref,
    reconcile_managed_worktree,
    remove_worktree,
    working_tree_clean,
)
from .orchestrator import Orchestrator
from .release_jobs import ReleaseJobStore
from .run_lock import runtime_status


WORKER_ENV = "AUTO_AGENTS_RELEASE_WORKER"


def ensure_release_worker(project_root: Path) -> bool:
    """Start one low-priority detached worker when project policy enables it."""
    root = Path(project_root).expanduser().resolve()
    orchestrator = Orchestrator(root)
    policy = orchestrator.config.gates.release_worker
    acceleration = getattr(
        getattr(orchestrator.config, "execution", None),
        "acceleration",
        None,
    )
    if not (policy.enabled and policy.auto_start):
        return False
    if acceleration is not None and not (
        acceleration.enabled and acceleration.release_prewarm_enabled
    ):
        return False
    if os.environ.get(WORKER_ENV) == "1":
        return False
    entrypoint = Path(__file__).resolve().parents[2] / "auto_agents.py"
    log_path = root / ".auto-agents" / "state" / "release-worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(entrypoint), "release-worker", "--project", str(root)]
    if shutil.which("nice"):
        command = ["nice", "-n", "10", *command]
    environment = dict(os.environ)
    environment[WORKER_ENV] = "1"
    with log_path.open("ab") as log_stream:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            cwd=root,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    return True


def run_release_worker(
    project_root: Path,
    *,
    once: bool = False,
    output: Optional[TextIO] = None,
) -> int:
    root = Path(project_root).expanduser().resolve()
    stream = output or sys.stderr
    store = ReleaseJobStore(root)
    orchestrator = Orchestrator(root, agent_output_stream=stream)
    policy = orchestrator.config.gates.release_worker
    if not policy.enabled:
        store.set_worker(status="disabled", reason="release worker is disabled")
        return 0

    lock_path = root / ".auto-agents" / "state" / "release-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        store.requeue_abandoned()
        store.set_worker(status="idle", pid=os.getpid())
        while True:
            job = store.claim_latest(idle_delay_seconds=policy.idle_delay_seconds)
            if job is None:
                latest = store.latest()
                if str(latest.get("status", "")) == "pending":
                    _wait_for_idle_delay(latest, policy.idle_delay_seconds)
                    continue
                store.set_worker(status="idle", pid=os.getpid())
                return 0
            job_id = str(job["job_id"])
            store.set_worker(status="running", pid=os.getpid(), job_id=job_id)
            _process_job(root, store, job, stream=stream)
            if once:
                store.set_worker(status="stopped", pid=os.getpid())
                return 0
    except Exception as error:
        store.set_worker(status="failed", pid=os.getpid(), reason=str(error))
        print(f"[release-worker] fatal: {error}", file=stream, flush=True)
        return 1
    finally:
        os.close(lock_fd)


def _wait_for_idle_delay(job: Mapping[str, object], idle_delay_seconds: int) -> None:
    if idle_delay_seconds <= 0:
        return
    queued = str(job.get("queued_at", ""))
    try:
        from datetime import datetime

        elapsed = time.time() - datetime.fromisoformat(queued).timestamp()
    except (TypeError, ValueError):
        elapsed = 0
    time.sleep(max(0.05, min(1.0, idle_delay_seconds - elapsed)))


def _process_job(
    project_root: Path,
    store: ReleaseJobStore,
    job: Mapping[str, object],
    *,
    stream: TextIO,
) -> None:
    job_id = str(job["job_id"])
    candidate_sha = str(job.get("candidate_sha", ""))
    if not candidate_sha:
        store.needs_user(job_id, reason="release candidate has no commit SHA")
        return
    if not store.is_current(job_id):
        store.supersede(job_id)
        return
    if _foreground_active(project_root):
        store.defer(job_id, reason="foreground workflow is active; release worker yielded")
        return

    managed_worktree_root = Path(tempfile.gettempdir()) / "auto-agents-release-worktrees"
    worktree_root = managed_worktree_root / str(job_id)
    dependency_links = discover_dependency_links(project_root)
    reconcile_managed_worktree(
        project_root,
        worktree_root,
        managed_root=managed_worktree_root,
        remove_existing=True,
    )
    add_worktree(project_root, worktree_root, ref=candidate_sha)
    recovery_commit = ""
    try:
        install_dependency_links(worktree_root, dependency_links)
        worker = Orchestrator(
            worktree_root,
            agent_output_stream=stream,
            gate_cache_path=gate_baseline_cache_path(project_root),
            gate_preempt_requested=lambda: _foreground_active(project_root),
        )
        parallel_workers = worker.config.gates.release_worker.background_parallel_workers
        worker.config.gates.parallel_workers = parallel_workers
        worker.config.gates.max_auto_workers = parallel_workers
        max_recovery = worker.config.gates.release_worker.max_recovery_attempts
        max_infrastructure = worker.config.gates.release_worker.max_infrastructure_retries

        while True:
            result = worker.run_verification(level="release")
            if not store.is_current(job_id):
                store.supersede(job_id)
                return
            if bool(result.get("ok")):
                if recovery_commit:
                    integrated = _integrate_recovery(
                        project_root,
                        store,
                        job,
                        recovery_commit,
                    )
                    if not integrated:
                        return
                    latest = store.enqueue(
                        source=f"release-recovery:{job_id}",
                        affected_proof_ids=result.get("proof_ids", []),
                    )
                    store.complete(
                        str(latest["job_id"]), result, recovery_commit=recovery_commit
                    )
                    store.supersede(job_id, superseded_by=str(latest["job_id"]))
                else:
                    store.complete(job_id, result)
                return

            if _was_foreground_preempted(result) or _foreground_active(project_root):
                store.defer(job_id, reason="foreground workflow preempted background release")
                return

            if _is_infrastructure_failure(result):
                current = store.get(job_id)
                attempt = int(current.get("infrastructure_attempts", 0))
                if attempt >= max_infrastructure:
                    store.needs_user(
                        job_id,
                        reason="release infrastructure retries exhausted",
                        failure_payload=result,
                    )
                    return
                store.record_infrastructure_retry(
                    job_id,
                    failure_payload=result,
                    reason=str(result.get("reason", "release infrastructure failure")),
                )
                return

            if not store.is_current(job_id):
                store.supersede(job_id)
                return
            current = store.get(job_id)
            attempt = int(current.get("recovery_attempts", 0))
            if attempt >= max_recovery:
                store.needs_user(
                    job_id,
                    reason="automatic release recovery exhausted",
                    failure_payload=result,
                )
                return
            if _foreground_active(project_root):
                store.defer(job_id, reason="foreground workflow preempted release recovery")
                return
            store.mark_recovering(
                job_id,
                failure_payload=result,
                reason=str(result.get("reason", "release verification failed")),
            )
            store.set_worker(status="recovering", pid=os.getpid(), job_id=job_id)
            if not _recover_failure(worker, result, job_id=job_id, attempt=attempt + 1):
                store.needs_user(
                    job_id,
                    reason="release recovery made no verified progress",
                    failure_payload=result,
                )
                return
            recovery_commit = commit_all_except(
                worktree_root,
                f"fix(release): recover failed proofs for {job_id}",
                exclude_prefixes=tuple(dependency_links),
            )
            store.mark_running(job_id)
    finally:
        remove_worktree(project_root, worktree_root, force=True)


def _recover_failure(
    worker: Orchestrator,
    result: Mapping[str, object],
    *,
    job_id: str,
    attempt: int,
) -> bool:
    failures = [
        dict(item)
        for item in result.get("commands", [])
        if isinstance(item, dict) and not bool(item.get("ok"))
    ]
    prompt = _recovery_prompt(result, failures, attempt=attempt)
    before = head_ref(worker.project_root)
    worker._run_agent_with_retries(
        None,
        "implement",
        f"release-recovery-{job_id}-{attempt}",
        prompt,
        run_id=f"release-{job_id}",
        effort=worker.config.efforts.get("implement", "deep"),
        task_origin="evidence_repair",
    )
    if working_tree_clean(worker.project_root):
        return False
    failed_commands = list(
        dict.fromkeys(str(item.get("command", "")) for item in failures if item.get("command"))
    )
    if failed_commands:
        targeted, mutation_error = worker._run_gate_commands_for_commands(
            failed_commands,
            collect_all=True,
            context="release recovery failed proofs",
        )
        if mutation_error or not targeted.ok:
            return False
    changed = subprocess.run(
        ["git", "diff", "--name-only", before],
        cwd=worker.project_root,
        capture_output=True,
        text=True,
    )
    changed_paths = [line.strip() for line in changed.stdout.splitlines() if line.strip()]
    affected = worker.run_verification(level="affected", changed_path_set=changed_paths)
    return bool(affected.get("ok"))


def _recovery_prompt(
    result: Mapping[str, object],
    failures: list[dict[str, object]],
    *,
    attempt: int,
) -> str:
    rendered = []
    for failure in failures:
        rendered.append(
            "\n".join(
                [
                    f"Command: {failure.get('command', '')}",
                    f"stdout:\n{str(failure.get('stdout', ''))[-6000:]}",
                    f"stderr:\n{str(failure.get('stderr', ''))[-6000:]}",
                ]
            )
        )
    return "\n\n".join(
        [
            f"Automatic release verification recovery attempt {attempt}.",
            "The exhaustive release gate failed on this committed candidate.",
            "Diagnose whether the implementation or repository tests violate the active requirements and nearby public contracts, then make the smallest correct code/test fix.",
            "Do not edit .auto-agents state, configuration, planning documents, input specs, DESIGN.md, or approved prototype artifacts.",
            "Do not run the broad release suite; the orchestrator will run the failed proofs, affected proofs, and release attestation after your edit.",
            f"Gate summary: {result.get('reason', '')}",
            "Failed proofs:",
            *rendered,
        ]
    )


def _is_infrastructure_failure(result: Mapping[str, object]) -> bool:
    commands = result.get("commands", [])
    failures = [
        item
        for item in commands
        if isinstance(item, dict) and not bool(item.get("ok"))
    ]
    return bool(failures) and all(
        bool(item.get("infrastructure_failure"))
        or bool(item.get("termination_reason"))
        for item in failures
    )


def _was_foreground_preempted(result: Mapping[str, object]) -> bool:
    return any(
        str(item.get("termination_reason", "")) == "foreground_preempted"
        for item in result.get("commands", [])
        if isinstance(item, dict) and not bool(item.get("ok"))
    )


def _foreground_active(project_root: Path) -> bool:
    return bool(runtime_status(project_root).get("active")) or foreground_active(
        project_root
    )


def _integrate_recovery(
    project_root: Path,
    store: ReleaseJobStore,
    job: Mapping[str, object],
    recovery_commit: str,
) -> bool:
    job_id = str(job["job_id"])
    candidate_sha = str(job.get("candidate_sha", ""))
    current_head = head_ref(project_root)
    current_clean = working_tree_clean(project_root)
    if current_head != candidate_sha or not current_clean:
        subprocess.run(
            ["git", "update-ref", f"refs/auto-agents/release-recovery/{job_id}", recovery_commit],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        store.supersede(job_id)
        if current_clean and current_head:
            store.enqueue(source=f"superseded-release:{job_id}", affected_proof_ids=[])
        return False
    cherry_pick = subprocess.run(
        ["git", "cherry-pick", f"{candidate_sha}..{recovery_commit}"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if cherry_pick.returncode == 0:
        return True
    subprocess.run(
        ["git", "cherry-pick", "--abort"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    store.needs_user(
        job_id,
        reason=cherry_pick.stderr.strip() or "release recovery integration failed",
    )
    return False
