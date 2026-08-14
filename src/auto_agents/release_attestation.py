from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from .release_jobs import ReleaseJobStore, candidate_identity, release_jobs_path


def release_attestation_path(project_root: Path) -> Path:
    """Return the durable release job database path."""
    return release_jobs_path(project_root)


def candidate_id(project_root: Path) -> str:
    return candidate_identity(project_root)[0]


def enqueue_release_verification(
    project_root: Path,
    *,
    source: str,
    affected_proof_ids: Iterable[str],
) -> dict[str, object]:
    return ReleaseJobStore(project_root).enqueue(
        source=source,
        affected_proof_ids=affected_proof_ids,
    )


def begin_release_verification(project_root: Path) -> dict[str, object]:
    store = ReleaseJobStore(project_root)
    job = store.latest()
    if not job or str(job.get("candidate_id")) != candidate_id(project_root):
        job = store.enqueue(source="manual-verify", affected_proof_ids=[])
    return store.mark_running(str(job["job_id"]))


def complete_release_verification(
    project_root: Path,
    result: Mapping[str, object],
) -> dict[str, object]:
    store = ReleaseJobStore(project_root)
    job = store.latest()
    if not job or str(job.get("candidate_id")) != candidate_id(project_root):
        job = store.enqueue(source="manual-verify", affected_proof_ids=[])
    return store.complete(str(job["job_id"]), result)


def current_release_attestation(project_root: Path) -> dict[str, object]:
    store = ReleaseJobStore(project_root)
    return {
        "latest": store.latest(),
        "worker": store.worker_status(),
    }
