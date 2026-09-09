"""Carry cancelled repair work into a newly authorized invocation as unverified input."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time

from .io_utils import read_json
from .repair_control import alive, atomic_json, digest, git
from .self_repair_search import SelfRepairCandidateRecord, SelfRepairExperimentStore

RESTART_QUIESCENCE_SECONDS = 15
_HISTORY_FILES = ("result.json", "candidate.diff", "partial-candidate.json", "partial-candidate.diff")


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


def _restart_snapshot(source, source_worktree, source_store, experiment, repository):
    if not (source / "continuous/fallback.json").exists():
        return _worktree_snapshot(source_worktree), ""
    # Older workers abandoned the continuous worktree when deep search began.
    # Prefer that search's current checkpoint or latest completed candidate;
    # the old continuous HEAD can be many hours behind both.
    current_id = experiment.current_candidate_id
    latest = max(experiment.candidates.values(), key=lambda item: item.created_at, default=None)
    if not current_id and latest is not None and latest.infrastructure_failure:
        current_id = latest.candidate_id
    if current_id:
        candidate = source_store.candidate_root(current_id)
        prefix = f"refs/auto-agents/self-repair/candidates/{source_store.safe_root}/{experiment.experiment_id}/"
        commit = git(repository.cache, "rev-parse", "--verify", prefix + current_id, check=False)
        if commit.returncode == 0:
            return (commit.stdout.strip(), "", {}), current_id
        metadata = read_json(candidate / "partial-candidate.json", default={})
        if metadata.get("status") in {"generated", "interrupted"}:
            patch = (candidate / "partial-candidate.diff").read_bytes()
            if (metadata.get("candidate_id") != current_id
                    or hashlib.sha256(patch).hexdigest() != metadata.get("patch_sha256")):
                raise RuntimeError("latest interrupted repair checkpoint is inconsistent; refusing to use older work")
            base = metadata.get("base_ref", "")
            known = {experiment.base_commit: experiment.base_commit}
            for record in experiment.candidates.values():
                if record.candidate_commit:
                    known[record.candidate_commit] = record.candidate_commit
                    if record.candidate_ref:
                        known[record.candidate_ref] = record.candidate_commit
            if not base or base not in known:
                raise RuntimeError("latest interrupted repair checkpoint has an unknown parent")
            commit = git(repository.cache, "rev-parse", "--verify", known[base] + "^{commit}")
            return (commit, patch.decode("utf-8"), {}), current_id
    for record in sorted(experiment.candidates.values(), key=lambda item: item.created_at, reverse=True):
        if record.candidate_id != "base" and not record.fatal and record.candidate_commit:
            return (record.candidate_commit, "", {}), record.candidate_id
    return _worktree_snapshot(source_worktree), ""


def _discard_incomplete_import(marker, destination, retained, repository):
    pending = read_json(marker)
    if retained.exists():
        with repository.locked():
            git(repository.cache, "worktree", "remove", "--force", str(retained))
    for candidate_id in pending["candidate_ids"]:
        for name in _HISTORY_FILES:
            (destination.candidate_root(candidate_id) / name).unlink(missing_ok=True)
    if pending["previous_experiment"] is None:
        destination.path.unlink(missing_ok=True)
    else:
        atomic_json(destination.path, pending["previous_experiment"])
    (retained.parent / "base.json").unlink(missing_ok=True)
    (retained.parent / "fallback.json").unlink(missing_ok=True)
    marker.unlink()


def _wait_for_quiescence(source):
    deadline = time.monotonic() + RESTART_QUIESCENCE_SECONDS
    while not _quiescent(source):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"previous repair {source.name} is still stopping; its candidate was not replaced")
        time.sleep(0.1)


def import_cancelled_repair(store, job, working, repository, *, revision=None):
    """Seed fresh work only; never reactivate the cancelled job or its receipts."""
    payload = job["payload"]
    invocation = payload.get("invocation", {})
    subject = ("session-" + invocation["session_id"] if invocation.get("session_id")
               and not invocation.get("run_id") else invocation.get("run_id", ""))
    if not subject:
        return False
    directory = store.root / "jobs" / job["id"]
    receipt = directory / "prior-repair-import.json"
    marker = directory / "prior-repair-importing.json"
    retained = directory / "continuous/repair"
    destination = SelfRepairExperimentStore(working, subject, payload["fingerprint"])
    if receipt.exists():
        if not retained.exists():
            raise RuntimeError("committed repair import is missing its retained worktree; refusing to start from base")
        store.event(job["id"], "prior_repair_imported", read_json(receipt))
        marker.unlink(missing_ok=True)
        return False
    if marker.exists():
        _discard_incomplete_import(marker, destination, retained, repository)
    current = destination.load()
    if retained.exists() or (current and current.attempt_count):
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
        if not source_worktree.exists():
            continue
        _wait_for_quiescence(source)
        experiment = source_store.load()
        if experiment is None or experiment.status == "completed":
            continue
        snapshot, source_candidate = _restart_snapshot(source, source_worktree, source_store, experiment, repository)
        if snapshot[0] == experiment.base_commit and not snapshot[1] and not snapshot[2]:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        retained.parent.mkdir(parents=True, exist_ok=True)
        candidate_ids = [path.name for path in source_store.root.iterdir()
                         if path.is_dir() and not path.is_symlink() and path.name.startswith("c")]
        atomic_json(marker, {"source_job": row["id"], "candidate_ids": candidate_ids,
                             "previous_experiment": current.to_dict() if current else None})
        try:
            with repository.locked():
                git(repository.cache, "worktree", "add", "--detach", str(retained), snapshot[0])
            if snapshot[1]:
                patch = directory / "prior-repair.diff"
                patch.write_text(snapshot[1], encoding="utf-8")
                git(retained, "apply", "--index", "--binary", str(patch))
            for name in snapshot[2]:
                target = retained / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_worktree / name, target, follow_symlinks=False)
            if not _quiescent(source) or (snapshot, source_candidate) != _restart_snapshot(source, source_worktree, source_store, experiment, repository):
                raise RuntimeError("cancelled repair changed during recovery; retained source is untouched")
            # Only history and code cross the invocation boundary. Old replay,
            # approval, provider-session and full-suite receipts do not.
            for candidate_id in candidate_ids:
                candidate = source_store.candidate_root(candidate_id)
                for name in _HISTORY_FILES:
                    old = candidate / name
                    if old.is_file() and not old.is_symlink():
                        new = destination.candidate_root(candidate.name) / name
                        new.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(old, new)
            experiment.status = "active"
            experiment.current_candidate_id = ""
            experiment.base_commit = revision or payload["base"]
            experiment.candidates["base"] = SelfRepairCandidateRecord(
                "base", parent_candidate_id="", parent_ref=experiment.base_commit,
                candidate_ref=experiment.base_commit, candidate_commit=experiment.base_commit,
                status="base", validation_stage="base",
            )
            if current:
                experiment.evidence_fingerprint = current.evidence_fingerprint
            experiment.best_search_candidate_id = experiment.best_safe_candidate_id = "base"
            experiment.best_search_ref = experiment.best_safe_ref = experiment.base_commit
            experiment.frontier = []
            retain_design = bool(current and current.contract_fingerprint == experiment.contract_fingerprint
                                 and experiment.repair_design.get("contract_fingerprint") == experiment.contract_fingerprint)
            if retain_design:
                for group in experiment.finding_groups:
                    group["status"] = "pending"
            else:
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
            if (source / "continuous/fallback.json").exists():
                atomic_json(retained.parent / "fallback.json", {"reason": "retain deep design and continue the recovered code"})
            evidence = {"source_job": row["id"], "source_commit": snapshot[0],
                        "source_candidate": source_candidate,
                        "experiment_id": experiment.experiment_id, "attempt_count": experiment.attempt_count,
                        "candidate_ids": candidate_ids,
                        "verification_required": True}
            atomic_json(receipt, evidence)
            store.event(job["id"], "prior_repair_imported", evidence)
            marker.unlink(missing_ok=True)
            return True
        except BaseException:
            if not receipt.exists():
                _discard_incomplete_import(marker, destination, retained, repository)
            raise
    return False
