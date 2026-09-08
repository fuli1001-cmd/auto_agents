"""Carry cancelled repair work into a newly authorized invocation as unverified input."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from .io_utils import read_json
from .repair_control import alive, atomic_json, digest, git
from .self_repair_search import SelfRepairExperimentStore


def _identity(payload):
    # A new engine revision requires new proof, but need not erase repair work.
    # Every other input, including project, workflow, scope and environment,
    # must match. This is deliberately stricter than cross-project job dedup.
    return digest({key: value for key, value in payload.items() if key != "base"})


def _quiescent(directory):
    try:
        records = [read_json(path, default={}) for path in directory.glob("*-lease.json")]
        records.extend(read_json(directory / "processes.json", default={}).get("processes", []))
        return all(not alive(record.get("pid", 0), record.get("ticks", record.get("start_ticks", 0)))
                   for record in records)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _worktree_snapshot(root):
    head = git(root, "rev-parse", "HEAD")
    diff = git(root, "diff", "--binary", "HEAD", "--", check=False)
    if diff.returncode:
        raise RuntimeError("could not snapshot retained repair changes")
    listed = git(root, "ls-files", "--others", "--exclude-standard", "-z", check=False)
    if listed.returncode:
        raise RuntimeError("could not list retained repair files")
    names = listed.stdout.split("\0")
    untracked = {}
    for name in names:
        if not name:
            continue
        path = root / name
        content = str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
        untracked[name] = hashlib.sha256(content).hexdigest()
    return head, diff.stdout, untracked


def import_cancelled_repair(store, job, working, repository):
    """Seed fresh work only; never reactivate the cancelled job or its receipts."""
    payload = job["payload"]
    invocation = payload.get("invocation", {})
    subject = ("session-" + invocation["session_id"] if invocation.get("session_id")
               and not invocation.get("run_id") else invocation.get("run_id", ""))
    if not subject:
        return False
    directory = store.root / "jobs" / job["id"]
    receipt = directory / "prior-repair-import.json"
    retained = directory / "continuous/repair"
    destination = SelfRepairExperimentStore(working, subject, payload["fingerprint"])
    current = destination.load()
    if receipt.exists() or retained.exists() or (current and current.attempt_count):
        return False
    with store.connect() as db:
        rows = db.execute("SELECT id,payload FROM jobs WHERE state='cancelled' AND id!=? ORDER BY updated DESC",
                          (job["id"],)).fetchall()
    for row in rows:
        if _identity(json.loads(row["payload"])) != _identity(payload):
            continue
        source = store.root / "jobs" / row["id"]
        source_worktree = source / "continuous/repair"
        source_store = SelfRepairExperimentStore(source / "working-evidence", subject, payload["fingerprint"])
        if not source_worktree.exists() or not _quiescent(source):
            continue
        experiment = source_store.load()
        if experiment is None or experiment.status == "completed":
            continue
        snapshot = _worktree_snapshot(source_worktree)
        if snapshot[0] == experiment.base_commit and not snapshot[1] and not snapshot[2]:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        retained.parent.mkdir(parents=True, exist_ok=True)
        with repository.locked():
            git(repository.cache, "worktree", "add", "--detach", str(retained), snapshot[0])
        try:
            if snapshot[1]:
                patch = directory / "prior-repair.diff"
                patch.write_text(snapshot[1], encoding="utf-8")
                git(retained, "apply", "--index", "--binary", str(patch))
            for name in snapshot[2]:
                target = retained / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_worktree / name, target, follow_symlinks=False)
            if not _quiescent(source) or snapshot != _worktree_snapshot(source_worktree):
                raise RuntimeError("cancelled repair changed during recovery; retained source is untouched")
            # Only history and code cross the invocation boundary. Old replay,
            # approval, provider-session and full-suite receipts do not.
            candidate_ids = []
            for candidate in source_store.root.iterdir():
                if not candidate.is_dir() or not candidate.name.startswith("c"):
                    continue
                candidate_ids.append(candidate.name)
                for name in ("result.json", "candidate.diff", "partial-candidate.json", "partial-candidate.diff"):
                    old = candidate / name
                    if old.is_file() and not old.is_symlink():
                        new = destination.candidate_root(candidate.name) / name
                        new.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(old, new)
            experiment.status = "active"
            experiment.current_candidate_id = ""
            experiment.best_search_candidate_id = experiment.best_safe_candidate_id = "base"
            experiment.best_search_ref = experiment.best_safe_ref = experiment.base_commit
            experiment.frontier = []
            experiment.repair_design = {}
            experiment.repair_design_fingerprint = ""
            experiment.finding_groups = []
            experiment.active_finding_group_id = ""
            experiment.completed_contract_obligation_ids = []
            experiment.completed_finding_ids = []
            experiment.consecutive_non_improvements = 0
            for record in experiment.candidates.values():
                record.component_receipts = {}
                record.provider_session_id = record.provider_prompt_hash = ""
                if record.candidate_id != "base":
                    record.fatal = True
                    record.validation_rank = 0
                    record.validation_stage = "unverified"
                    record.passed_obligations = []
                    record.patch_fingerprint = ""
            # Same-base restarts also invalidate pending full-suite promotion.
            # Keep the historical statuses for diagnosis, but not resumable proof.
            for record in experiment.candidates.values():
                if record.candidate_id != "base" and record.status not in {
                    "candidate_review_rejected", "candidate_verification_failed", "candidate_replay_failed",
                }:
                    record.status = "candidate_interrupted"
            destination.save(experiment)
            previous_base = read_json(source / "continuous/base.json", default={}).get("revision", experiment.base_commit)
            atomic_json(retained.parent / "base.json", {"revision": previous_base})
            evidence = {"source_job": row["id"], "source_commit": snapshot[0],
                        "experiment_id": experiment.experiment_id, "attempt_count": experiment.attempt_count,
                        "candidate_ids": candidate_ids,
                        "verification_required": True}
            atomic_json(receipt, evidence)
            store.event(job["id"], "prior_repair_imported", evidence)
            return True
        except BaseException:
            with repository.locked():
                git(repository.cache, "worktree", "remove", "--force", str(retained), check=False)
            raise
    return False
