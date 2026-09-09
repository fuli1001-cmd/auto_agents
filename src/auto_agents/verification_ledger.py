"""Private, process-shared certificates for managed verification.

Only trusted executors use this API. Provider output is never imported as proof.
The existing gate cache remains the certificate backend; this module supplies
repository scoping, exact-input identities, single-flight and audit revocation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import sqlite3
import subprocess
import time

from .gate_result_cache import GateResultCache
from .models import CommandResult
from .repair_control import digest, private_directory, atomic_json

LEDGER_VERSION = 1


def engine_command(command):
    """One engine pytest policy for self-tests, focused gates and full shards."""
    try:
        args = shlex.split(command)
    except ValueError:
        return command
    if args[:3] == ["python", "-m", "pytest"]:
        tail = args[3:]
        normalized = []
        index = 0
        while index < len(tail):
            if tail[index:index + 2] == ["-p", "no:cacheprovider"]:
                index += 2
            else:
                normalized.append(tail[index])
                index += 1
        return shlex.join([*args[:3], "-p", "no:cacheprovider", *normalized])
    return command


def ledger_root():
    return Path(os.environ.get("AUTO_AGENTS_VERIFICATION_ROOT") or str(
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "auto-agents/verification")).expanduser().resolve()


def repository_identity(root):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, timeout=15)
    return digest(str(Path(result.stdout.strip()).resolve()) if result.returncode == 0 else str(Path(root).resolve()))


def source_identity(root):
    """Include dirty and untracked inputs, never just the last committed tree."""
    root = Path(root).resolve()
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError("managed verification requires a versioned source snapshot")
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"], capture_output=True, check=True, timeout=15)
    tracked_names = set(os.fsdecode(tracked.stdout).split("\0"))
    items = []
    for name in sorted(set(os.fsdecode(result.stdout).split("\0")) - {""}):
        parts = Path(name).parts
        if name not in tracked_names and any(part in {
                ".pytest_cache", "__pycache__", ".conda", ".venv", "node_modules",
                ".auto-agents-gate-runtime", ".auto-agents-gate-tmp", ".auto-agents-gate-cache"} for part in parts):
            continue
        path = root / name
        if path.is_symlink():
            value = "link:" + os.readlink(path)
        elif path.is_file():
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            value = "missing"
        items.append((name, value))
    # Git-aware tests cannot silently reuse a result after a history change.
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
    return digest([items, head.stdout.strip()])


class VerificationLedger:
    def __init__(self, root, *, repository=None, environment="", context="", scope="project", mode="on"):
        self.source = Path(root).resolve()
        self.scope = scope
        self.context = context
        self.environment = environment
        self.mode = mode
        self.root = private_directory(private_directory(ledger_root()) / digest([scope, repository or repository_identity(root)]))
        self.cache = GateResultCache(root, cache_path=self.root / "proofs.sqlite3",
            environment_fingerprint=environment,
            context_fingerprint=context or digest([str(self.source), os.environ.get("AUTO_AGENTS_REPAIR_JOB", "")]))
        self.events = self.root / "events.jsonl"

    def event(self, **details):
        row = {"version": LEDGER_VERSION, "time": time.time(), **details}
        fd = os.open(self.events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)

    @contextmanager
    def single_flight(self, key, *, cancelled=None):
        # Fixed lock buckets bound inode growth without unlinking live lock
        # files. A rare collision only serializes two unrelated requests.
        path = self.root / ("flight-" + digest(key)[:3] + ".lock")
        with path.open("a+") as handle:
            os.chmod(path, 0o600)
            while True:
                if cancelled and cancelled():
                    raise InterruptedError("verification cancelled while waiting for an identical proof")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def execute(self, command, run, *, source=None, metadata="", fresh=False,
                cancelled=None, audit_rate=0.0, result_cache_scope="candidate", input_mode="observe"):
        if (self.root / "revoked.json").exists():
            result_cache_scope = "candidate"
        source = source or source_identity(self.source)
        metadata = digest([LEDGER_VERSION, metadata])
        key = digest([command, source, metadata, self.environment, self.context, self.scope])
        started = time.monotonic()
        with self.single_flight(key, cancelled=cancelled):
            queued = time.monotonic() - started
            kwargs = dict(source_fingerprint=source, cache_scope="run_context",
                          result_cache_scope=result_cache_scope, metadata_signature=metadata)
            cached, reason = (self.cache.lookup_with_reason(command, **{**kwargs, "result_cache_scope": "candidate"})
                              if not fresh and self.mode != "off" else (None, "fresh" if fresh else "off"))
            if cached is None and not fresh and self.mode != "off" and not self.context and input_mode != "off":
                cached, reason = self.cache.lookup_with_reason(command, **{**kwargs, "cache_scope": "source"})
            observed_hit = cached is not None and cached.backend == "result-cache-observed-inputs"
            if observed_hit and input_mode == "off":
                cached, reason = None, "input_reuse_off"
            audit = cached is not None and (self.mode == "observe" or (observed_hit and input_mode != "on")
                    or secrets.randbelow(1_000_000) < audit_rate * 1_000_000)
            if cached is not None and not audit:
                cached.proof_ref = key
                cached.queue_seconds = queued
                self.event(kind="verification", proof=key, cache="hit", queue_seconds=queued,
                           duration_seconds=time.monotonic() - started, executed=0)
                return cached
            result = run()
            result.proof_ref = key
            result.queue_seconds += queued
            result.cache_miss_reason = "audit" if audit else reason
            # Execution must bind an unchanged snapshot. Writes to ordinary
            # inputs cannot be hidden by a reported successful exit status.
            unchanged = source_identity(self.source) == source
            if not unchanged:
                result.process_snapshot["execution_returncode"] = result.returncode
                result.ok, result.returncode = False, 125
                result.infrastructure_error = True
                result.infrastructure_failure_id = "verification_source_changed"
                result.stderr += "\nverification source changed during execution; proof is invalid"
            valid = (result.ok and result.returncode == 0 and not result.cleanup_incomplete
                     and not result.termination_reason and not result.infrastructure_error and unchanged)
            if cached is not None and audit and (not valid or (cached.executed_tests and cached.executed_tests != result.executed_tests)):
                self.revoke("audit disagreed with a success certificate")
                result.ok = False
                result.stderr += "\nverification cache audit disagreed; namespace revoked"
            elif valid and self.mode != "off":
                self.cache.record(command, result, **kwargs)
                if result.input_trace_complete and not self.context:
                    self.cache.record(command, result, **{**kwargs, "cache_scope": "source"})
            self.event(kind="verification", proof=key, cache=result.cache_miss_reason,
                       queue_seconds=result.queue_seconds, duration_seconds=time.monotonic() - started,
                       execution_seconds=result.duration_seconds, executed=1, ok=result.ok, unchanged=unchanged,
                       phases=result.phase_seconds, test_count=len(result.executed_tests), slowest=result.test_timings,
                       input_trace_complete=result.input_trace_complete, input_trace_reason=result.input_trace_reason,
                       observed_shadow=observed_hit and input_mode == "observe")
            return result

    def revoke(self, reason):
        # Schema-owned tables only; unrelated timing data remains advisory.
        with self.cache._connect() as db:
            db.execute("DELETE FROM gate_proof_certificates")
            db.execute("DELETE FROM gate_result_successes")
        atomic_json(self.root / "revoked.json", {"reason": reason, "time": time.time()})
        self.event(kind="revocation", reason=reason)


def verification_summary(root=None):
    root = Path(root) if root else ledger_root()
    totals = {"requests": 0, "executed": 0, "hits": 0, "queue_seconds": 0.0, "execution_seconds": 0.0}
    misses = {}
    timings = {}
    for path in root.glob("*/events.jsonl") if root.exists() else ():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") != "verification":
                continue
            totals["requests"] += 1
            totals["executed"] += row.get("executed", 0)
            totals["hits"] += row.get("cache") == "hit"
            for key in ("queue_seconds", "execution_seconds"):
                totals[key] += row.get(key, 0)
            if row.get("cache") != "hit":
                reason = row.get("cache", "unknown")
                misses[reason] = misses.get(reason, 0) + 1
            for item in row.get("slowest", []):
                name = item["nodeid"]
                timing = timings.setdefault(name, {"nodeid": name, "seconds": 0.0, "samples": 0})
                timing["seconds"] += item.get("seconds", 0)
                timing["samples"] += 1
    return {"scope": "user-shared verification executors", **totals, "miss_reasons": misses,
            "slow_tests": sorted(timings.values(), key=lambda item: item["seconds"], reverse=True)[:10]}
