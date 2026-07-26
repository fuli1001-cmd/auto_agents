from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Mapping, Optional

from .gate_execution import LocalGatePlanExecutor, exclusive_resource_lease
from .gates import classify_reported_infrastructure_failure
from .models import CommandResult, GateConfig
from .worker_cluster import discover_workers, load_cluster_state
from .worker_service import WorkerClient, result_from_job_record
from .workers import (
    WORKER_PROTOCOL_VERSION,
    WorkerEndpoint,
    WorkerSlotLease,
    build_environment_manifest,
    enrich_worker_probe,
    forwarded_environment,
    load_local_worker_config,
    project_key,
    worker_probe,
)


class _EndpointAcquireCancelled(RuntimeError):
    """Raised when a queued gate command no longer needs a worker slot."""


class DistributedGatePlanExecutor:
    """Dispatch isolated gates to the local executor and paired LAN workers."""

    def __init__(
        self,
        project_root: Path,
        gate_config: GateConfig,
        metadata: Mapping[str, object],
        *,
        run_id: str = "",
        environment_fingerprint: str = "",
    ) -> None:
        self.project_root = project_root.resolve()
        self.gate_config = gate_config
        self.metadata = dict(metadata)
        self.local = LocalGatePlanExecutor(
            self.project_root,
            gate_config,
            metadata,
            run_id=run_id,
            worker_id="local",
            environment_fingerprint=environment_fingerprint,
        )
        self.key = project_key(self.project_root)
        self.environment_manifest = build_environment_manifest(self.project_root)
        self._bundle_path: Optional[Path] = None
        self._bundle_lock = threading.Lock()
        self._staged: set[str] = set()
        self._stage_locks: dict[str, threading.Lock] = {}
        self._lane_endpoint: dict[str, WorkerEndpoint] = {}
        self._lane_successes: dict[str, int] = {}
        self._lock = threading.RLock()
        self._slot_condition = threading.Condition(self._lock)
        self._active_slots: dict[str, int] = {}
        self.endpoints: list[WorkerEndpoint] = []
        self.clients: dict[str, WorkerClient] = {}

    def __enter__(self) -> "DistributedGatePlanExecutor":
        self.local.__enter__()
        distributed = self.gate_config.distributed
        local_config = load_local_worker_config()
        local_probe = enrich_worker_probe(worker_probe(""))
        local_slots = local_config.max_slots
        maximum = self.gate_config.max_auto_workers
        if isinstance(maximum, int):
            local_slots = min(local_slots, max(1, maximum))
        cluster = load_cluster_state()
        local_id = (
            cluster.node_id if cluster is not None else local_config.worker_id
        )
        self.endpoints = [
            WorkerEndpoint(
                worker_id=local_id,
                transport="local",
                max_slots=max(1, local_slots),
                capabilities=tuple(
                    str(item)
                    for item in local_probe.get("capabilities", [])
                ),
                capability_details=dict(
                    local_probe.get("capability_details", {})
                ),
                failure_domain=dict(local_probe.get("failure_domain", {})),
            )
        ]
        self.local.worker_id = local_id
        if distributed.mode != "off" and cluster is not None:
            try:
                discovered = discover_workers(
                    distributed.discovery_timeout_seconds
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                if distributed.mode == "required":
                    self.local.close()
                    raise RuntimeError(
                        "distributed gates are required but LAN discovery failed"
                    )
                discovered = []
            candidates = []
            for worker in discovered:
                client = WorkerClient(
                    worker,
                    timeout_seconds=distributed.request_timeout_seconds,
                )
                candidates.append((worker, client))
            probes = {}
            with ThreadPoolExecutor(
                max_workers=max(1, len(candidates))
            ) as pool:
                futures = {
                    pool.submit(
                        client.probe,
                        timeout_seconds=min(
                            3.0,
                            float(distributed.request_timeout_seconds),
                        ),
                    ): (worker, client)
                    for worker, client in candidates
                }
                for future in as_completed(futures):
                    worker, client = futures[future]
                    try:
                        probes[worker.worker_id] = (worker, client, future.result())
                    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                        continue
            for worker_id in sorted(probes):
                worker, client, probe = probes[worker_id]
                if not bool(probe.get("ok")):
                    continue
                expected_platform = str(
                    self.environment_manifest.get("platform", "")
                )
                if (
                    expected_platform
                    and str(probe.get("platform", "")) != expected_platform
                ):
                    continue
                python_manifest = self.environment_manifest.get("python", {})
                required_python = (
                    str(python_manifest.get("version", ""))
                    if isinstance(python_manifest, dict)
                    else ""
                )
                python_versions = probe.get("python_versions", [])
                if (
                    required_python
                    and (
                        not isinstance(python_versions, list)
                        or required_python not in python_versions
                    )
                ):
                    continue
                endpoint = WorkerEndpoint(
                    worker_id=worker.worker_id,
                    transport="https",
                    host=worker.host,
                    port=worker.port,
                    tls_fingerprint=worker.tls_fingerprint,
                    max_slots=max(
                        1,
                        min(worker.max_slots, int(probe.get("max_slots", 1))),
                    ),
                    capabilities=tuple(
                        sorted(
                            str(item).strip().lower()
                            for item in probe.get("capabilities", [])
                            if str(item).strip()
                        )
                    ),
                    capability_details=dict(
                        probe.get("capability_details", {})
                    ),
                    failure_domain=dict(probe.get("failure_domain", {})),
                )
                self.endpoints.append(endpoint)
                self.clients[endpoint.worker_id] = client
        if distributed.mode == "required" and len(self.endpoints) == 1:
            self.local.close()
            raise RuntimeError(
                "distributed gates are required but no paired LAN worker is available"
            )
        for endpoint in self.endpoints:
            self._active_slots[endpoint.worker_id] = 0
            self._stage_locks[endpoint.worker_id] = threading.Lock()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def priority(self, command: str) -> tuple[object, ...]:
        return self.local.priority(command)

    def estimated_duration(self, command: str) -> Optional[float]:
        return self.local.estimated_duration(command)

    def capacity(self) -> int:
        return max(1, sum(endpoint.max_slots for endpoint in self.endpoints))

    def required_slots(self, command: str) -> int:
        return self._required_slots(command)

    def _resource_class(self, command: str) -> str:
        metadata = self.metadata.get(command)
        return (
            "heavy"
            if str(getattr(metadata, "resource_class", "normal")).lower() == "heavy"
            else "normal"
        )

    def _required_slots(self, command: str) -> int:
        declared = self._metadata_int(command, "cpu_slots")
        if declared > 0:
            return declared
        return 2 if self._resource_class(command) == "heavy" else 1

    def _metadata_int(self, command: str, field: str) -> int:
        raw = getattr(self.metadata.get(command), field, 0)
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    def _memory_policy(self, command: str) -> tuple[int, int, str]:
        guard = str(
            getattr(self.metadata.get(command), "memory_guard", "off")
        ).strip().lower()
        if guard not in {"off", "advisory", "required"}:
            guard = "off"
        return (
            self._metadata_int(command, "memory_mb"),
            self._metadata_int(command, "memory_reserve_mb"),
            guard,
        )

    def _metadata_list(self, command: str, field: str) -> list[str]:
        raw = getattr(self.metadata.get(command), field, [])
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def _requires(self, command: str) -> set[str]:
        return {
            item.strip().lower()
            for item in self._metadata_list(command, "requires")
            if item.strip()
        }

    def _endpoint_supports(self, endpoint: WorkerEndpoint, command: str) -> bool:
        required = self._requires(command)
        return not required or required.issubset(set(endpoint.capabilities))

    def _try_acquire(self, endpoint: WorkerEndpoint, required: int) -> bool:
        with self._slot_condition:
            active = self._active_slots.get(endpoint.worker_id, 0)
            if active + required > endpoint.max_slots:
                return False
            self._active_slots[endpoint.worker_id] = active + required
            return True

    def _release(self, endpoint: WorkerEndpoint, required: int) -> None:
        with self._slot_condition:
            self._active_slots[endpoint.worker_id] = max(
                0, self._active_slots[endpoint.worker_id] - required
            )
            self._slot_condition.notify_all()

    def _acquire_endpoint(
        self,
        command: str,
        *,
        exclude: set[str],
        lane: str,
        cancel_event: Optional[threading.Event] = None,
        wait_timeout_seconds: float = 7200.0,
        allow_local_reuse: bool = True,
    ) -> tuple[WorkerEndpoint, int]:
        required = self._required_slots(command)
        deadline = time.monotonic() + max(0.1, float(wait_timeout_seconds))
        if lane and lane in self._lane_endpoint:
            endpoint = self._lane_endpoint[lane]
            if (
                endpoint.max_slots < required
                or not self._endpoint_supports(endpoint, command)
            ):
                raise RuntimeError(
                    "the worker pinned to the sequential gate lane no longer "
                    "satisfies this command's capacity or capabilities"
                )
            while not self._try_acquire(endpoint, required):
                if cancel_event is not None and cancel_event.is_set():
                    raise _EndpointAcquireCancelled
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "worker slot scheduling timed out while waiting for "
                        f"{required} slot(s) on {endpoint.worker_id}"
                    )
                with self._slot_condition:
                    self._slot_condition.wait(
                        timeout=min(0.1, max(0.0, deadline - time.monotonic()))
                    )
            return endpoint, required
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise _EndpointAcquireCancelled
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "worker slot scheduling timed out while waiting for "
                    f"{required} slot(s)"
                )
            candidates = [
                endpoint
                for endpoint in self.endpoints
                if endpoint.worker_id not in exclude
                and endpoint.max_slots >= required
                and self._endpoint_supports(endpoint, command)
            ]
            if not candidates and allow_local_reuse:
                candidates = [
                    endpoint
                    for endpoint in self.endpoints
                    if endpoint.transport == "local"
                    and endpoint.max_slots >= required
                    and self._endpoint_supports(endpoint, command)
                ]
            if not candidates:
                requirements = ", ".join(sorted(self._requires(command))) or "none"
                raise RuntimeError(
                    "no untried worker has enough slots and required capabilities "
                    f"({requirements})"
                )
            with self._lock:
                candidates.sort(
                    key=lambda endpoint: (
                        self._active_slots.get(endpoint.worker_id, 0)
                        / max(1, endpoint.max_slots),
                        0 if endpoint.transport == "local" else 1,
                        endpoint.worker_id,
                    )
                )
            for endpoint in candidates:
                if self._try_acquire(endpoint, required):
                    if lane:
                        self._lane_endpoint[lane] = endpoint
                    return endpoint, required
            with self._slot_condition:
                self._slot_condition.wait(
                    timeout=min(0.1, max(0.0, deadline - time.monotonic()))
                )

    def _bundle(self) -> Path:
        with self._bundle_lock:
            if self._bundle_path is not None:
                return self._bundle_path
            if self.local.snapshot is None:
                raise RuntimeError("gate snapshot is unavailable")
            descriptor, name = tempfile.mkstemp(
                prefix="auto-agents-gate-", suffix=".bundle"
            )
            os.close(descriptor)
            path = Path(name)
            path.unlink(missing_ok=True)
            process = subprocess.run(
                [
                    "git",
                    "bundle",
                    "create",
                    str(path),
                    self.local.snapshot.ref_name,
                ],
                cwd=str(self.project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            if process.returncode != 0:
                raise RuntimeError(
                    process.stderr.strip() or "git bundle create failed"
                )
            os.chmod(path, 0o600)
            self._bundle_path = path
            return path

    def _stage_remote(self, endpoint: WorkerEndpoint) -> None:
        with self._stage_locks[endpoint.worker_id]:
            with self._lock:
                if endpoint.worker_id in self._staged:
                    return
            if self.local.snapshot is None:
                raise RuntimeError("gate snapshot is unavailable")
            client = self.clients[endpoint.worker_id]
            client.stage(
                project_key=self.key,
                snapshot=self.local.snapshot.commit_sha,
                source_ref=self.local.snapshot.ref_name,
                bundle=self._bundle(),
            )
            with self._lock:
                self._staged.add(endpoint.worker_id)

    def _publish_remote_artifacts(
        self,
        endpoint: WorkerEndpoint,
        command: str,
        job_id: str,
    ) -> dict[str, str]:
        archive_bytes = self.clients[endpoint.worker_id].artifacts(job_id)
        if not archive_bytes:
            return {}
        temporary_root = (
            self.local.worktree_root
            / self.local.plan_id
            / ".runtime"
            / f"remote-artifacts-{job_id}"
        )
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or member.issym()
                    or member.islnk()
                    or (not member.isfile() and not member.isdir())
                ):
                    raise RuntimeError(
                        f"unsafe remote artifact member: {member.name}"
                    )
                destination = temporary_root.joinpath(*path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(
                        f"remote artifact member is unreadable: {member.name}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
        try:
            self.local._publish_diagnostics(temporary_root, job_id)
            return self.local._publish_artifacts(
                temporary_root, command, job_id
            )
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    def _run_remote(
        self,
        endpoint: WorkerEndpoint,
        command: str,
        *,
        lane: str,
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event: Optional[threading.Event],
    ) -> CommandResult:
        job_id = uuid.uuid4().hex
        self._stage_remote(endpoint)
        if self.local.snapshot is None:
            raise RuntimeError("gate snapshot is unavailable")
        distributed = self.gate_config.distributed
        artifact_globs = self._metadata_list(command, "artifact_globs")
        memory_mb, memory_reserve_mb, memory_guard = self._memory_policy(command)
        manifest = {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "project_key": self.key,
            "snapshot": self.local.snapshot.commit_sha,
            "plan_id": self.local.plan_id,
            "job_id": job_id,
            "lane": lane,
            "command": command,
            "resource_class": self._resource_class(command),
            "cpu_slots": self._metadata_int(command, "cpu_slots"),
            "memory_mb": memory_mb,
            "memory_reserve_mb": memory_reserve_mb,
            "memory_guard": memory_guard,
            "environment_manifest": self.environment_manifest,
            "environment": forwarded_environment(
                os.environ,
                distributed.extra_environment_denylist,
            ),
            "timeout_seconds": timeout_seconds,
            "adaptive_timeout_enabled": adaptive_timeout_enabled,
            "idle_timeout_seconds": idle_timeout_seconds,
            "artifact_globs": artifact_globs
            + [".auto-agents/failed-verification-logs/**/*"],
            "exclusive_resources": [
                resource
                for resource in self._metadata_list(
                    command, "exclusive_resources"
                )
                if resource.startswith("host:")
            ],
            "dynamic_ports": self._metadata_list(command, "dynamic_ports"),
            "artifact_max_files": self.gate_config.isolation.artifact_max_files,
            "artifact_max_bytes": self.gate_config.isolation.artifact_max_bytes,
        }
        client = self.clients[endpoint.worker_id]
        client.submit(manifest)
        accepted = True
        deadline = time.monotonic() + float(timeout_seconds) + 90
        cancellation_sent = False
        last_record: dict[str, object] = {}
        while time.monotonic() < deadline:
            try:
                if (
                    cancel_event is not None
                    and cancel_event.is_set()
                    and not cancellation_sent
                ):
                    client.cancel(job_id)
                    cancellation_sent = True
                last_record = client.query(job_id)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                if accepted:
                    return CommandResult(
                        command=command,
                        ok=False,
                        returncode=125,
                        stderr=(
                            "worker accepted the job but its final state could not "
                            f"be confirmed: {error}"
                        ),
                        termination_reason="remote_state_uncertain",
                        job_id=job_id,
                        worker_id=endpoint.worker_id,
                        backend="lan-worker",
                        infrastructure_error=True,
                    )
                raise
            if last_record.get("state") in {
                "terminal",
                "cancelled",
                "cleanup_incomplete",
            }:
                result = result_from_job_record(last_record)
                if result is None:
                    raise RuntimeError("terminal worker job has no result")
                result.worker_id = endpoint.worker_id
                result.backend = "lan-worker"
                if result.ok and result.artifacts:
                    result.artifacts = self._publish_remote_artifacts(
                        endpoint, command, job_id
                    )
                return result
            time.sleep(0.5)
        return CommandResult(
            command=command,
            ok=False,
            returncode=125,
            stderr="worker job state did not become terminal before the controller deadline",
            termination_reason="remote_state_uncertain",
            job_id=job_id,
            worker_id=endpoint.worker_id,
            backend="lan-worker",
            infrastructure_error=True,
        )

    def _run_on_endpoint(
        self,
        endpoint: WorkerEndpoint,
        command: str,
        *,
        lane: str,
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event: Optional[threading.Event],
        progress,
    ) -> CommandResult:
        required = self._required_slots(command)
        memory_mb, memory_reserve_mb, memory_guard = self._memory_policy(command)
        if endpoint.transport == "local":
            local_config = load_local_worker_config()
            with WorkerSlotLease(
                local_config.managed_root,
                endpoint.worker_id,
                endpoint.max_slots,
                required,
                memory_mb=memory_mb,
                memory_reserve_mb=memory_reserve_mb,
                memory_guard=memory_guard,
                cancel_event=cancel_event,
            ):
                result = self.local.run(
                    command,
                    lane=lane,
                    timeout_seconds=timeout_seconds,
                    adaptive_timeout_enabled=adaptive_timeout_enabled,
                    idle_timeout_seconds=idle_timeout_seconds,
                    cancel_event=cancel_event,
                    progress=progress,
                )
                result.worker_id = endpoint.worker_id
                return result
        if progress is not None:
            progress("start", command, 0.0)
        with exclusive_resource_lease(
            [
                resource
                for resource in self._metadata_list(
                    command, "exclusive_resources"
                )
                if resource.startswith("pool:")
            ],
            worker_id="controller",
        ):
            result = self._run_remote(
                endpoint,
                command,
                lane=lane,
                timeout_seconds=timeout_seconds,
                adaptive_timeout_enabled=adaptive_timeout_enabled,
                idle_timeout_seconds=idle_timeout_seconds,
                cancel_event=cancel_event,
            )
        self.local.record_timing(command, result)
        if progress is not None:
            progress("finish", command, result.duration_seconds)
        return result

    def run(
        self,
        command: str,
        *,
        lane: str = "",
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event: Optional[threading.Event] = None,
        progress=None,
    ) -> CommandResult:
        if cancel_event is not None and cancel_event.is_set():
            return CommandResult(
                command=command,
                ok=False,
                returncode=130,
                stderr="command cancelled because a peer gate command failed",
                termination_reason="cancelled",
            )
        attempted: set[str] = set()
        retry_limit = max(
            0, self.gate_config.distributed.infrastructure_retry_limit
        )
        result = CommandResult(
            command=command,
            ok=False,
            returncode=125,
            termination_reason="infrastructure_error",
            infrastructure_error=True,
        )
        for _attempt in range(retry_limit + 1):
            try:
                endpoint, required = self._acquire_endpoint(
                    command,
                    exclude=attempted,
                    lane=lane,
                    cancel_event=cancel_event,
                    wait_timeout_seconds=timeout_seconds,
                )
            except _EndpointAcquireCancelled:
                return CommandResult(
                    command=command,
                    ok=False,
                    returncode=130,
                    stderr="command cancelled while waiting for a worker slot",
                    termination_reason="cancelled",
                )
            except RuntimeError as error:
                result.stderr = str(error)
                return result
            attempted.add(endpoint.worker_id)
            try:
                try:
                    result = self._run_on_endpoint(
                        endpoint,
                        command,
                        lane=lane,
                        timeout_seconds=timeout_seconds,
                        adaptive_timeout_enabled=adaptive_timeout_enabled,
                        idle_timeout_seconds=idle_timeout_seconds,
                        cancel_event=cancel_event,
                        progress=progress,
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    result = CommandResult(
                        command=command,
                        ok=False,
                        returncode=125,
                        stderr=str(error),
                        termination_reason="infrastructure_error",
                        worker_id=endpoint.worker_id,
                        backend=(
                            "local-isolated"
                            if endpoint.transport == "local"
                            else "lan-worker"
                        ),
                        infrastructure_error=True,
                    )
            finally:
                self._release(endpoint, required)
            classify_reported_infrastructure_failure(
                result,
                self.gate_config.reported_infrastructure_markers,
            )
            if result.infrastructure_failure_id:
                return self._retry_reported_infrastructure(
                    command=command,
                    lane=lane,
                    first_result=result,
                    attempted=attempted,
                    timeout_seconds=timeout_seconds,
                    adaptive_timeout_enabled=adaptive_timeout_enabled,
                    idle_timeout_seconds=idle_timeout_seconds,
                    cancel_event=cancel_event,
                    progress=progress,
                )
            if not result.infrastructure_error:
                if lane and result.ok:
                    self._lane_successes[lane] = (
                        self._lane_successes.get(lane, 0) + 1
                    )
                return result
            if result.termination_reason == "remote_state_uncertain":
                return result
            if lane and self._lane_successes.get(lane, 0) > 0:
                result.termination_reason = "remote_lane_state_lost"
                return result
            if lane:
                self._lane_endpoint.pop(lane, None)
        return result

    def _infrastructure_attempt_evidence(
        self,
        result: CommandResult,
    ) -> dict[str, object]:
        endpoint = next(
            (
                item
                for item in getattr(self, "endpoints", [])
                if item.worker_id == result.worker_id
            ),
            None,
        )
        return {
            "worker_id": result.worker_id,
            "backend": result.backend,
            "job_id": result.job_id,
            "ok": result.ok,
            "returncode": result.returncode,
            "failure_id": result.infrastructure_failure_id,
            "termination_reason": result.termination_reason,
            "stdout_tail": result.stdout[-1000:],
            "stderr_tail": result.stderr[-1000:],
            "capability_details": (
                dict(endpoint.capability_details) if endpoint else {}
            ),
            "failure_domain": (
                dict(endpoint.failure_domain) if endpoint else {}
            ),
        }

    def _retry_reported_infrastructure(
        self,
        *,
        command: str,
        lane: str,
        first_result: CommandResult,
        attempted: set[str],
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event: Optional[threading.Event],
        progress,
    ) -> CommandResult:
        attempts = [self._infrastructure_attempt_evidence(first_result)]
        attempted_domains = {
            str(item.get("failure_domain", {}).get("id", ""))
            for item in attempts
            if isinstance(item.get("failure_domain"), dict)
            and str(item.get("failure_domain", {}).get("id", ""))
        }
        first_result.infrastructure_attempts = list(attempts)
        # A stateful lane cannot safely move after earlier commands succeeded.
        if lane and self._lane_successes.get(lane, 0) > 0:
            return first_result
        if lane:
            self._lane_endpoint.pop(lane, None)
        maximum = max(
            1,
            int(
                self.gate_config.distributed.reported_infrastructure_max_workers
            ),
        )
        last = first_result
        while len(attempted) < maximum:
            try:
                endpoint, required = self._acquire_endpoint(
                    command,
                    exclude=attempted,
                    lane="",
                    cancel_event=cancel_event,
                    wait_timeout_seconds=timeout_seconds,
                    allow_local_reuse=False,
                )
            except _EndpointAcquireCancelled:
                return CommandResult(
                    command=command,
                    ok=False,
                    returncode=130,
                    stderr="command cancelled while waiting for a worker slot",
                    termination_reason="cancelled",
                    infrastructure_attempts=list(attempts),
                )
            except RuntimeError:
                break
            domain_id = str(endpoint.failure_domain.get("id", ""))
            if domain_id and domain_id in attempted_domains:
                attempted.add(endpoint.worker_id)
                self._release(endpoint, required)
                continue
            if domain_id:
                attempted_domains.add(domain_id)
            attempted.add(endpoint.worker_id)
            try:
                try:
                    current = self._run_on_endpoint(
                        endpoint,
                        command,
                        lane="",
                        timeout_seconds=timeout_seconds,
                        adaptive_timeout_enabled=adaptive_timeout_enabled,
                        idle_timeout_seconds=idle_timeout_seconds,
                        cancel_event=cancel_event,
                        progress=progress,
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    current = CommandResult(
                        command=command,
                        ok=False,
                        returncode=125,
                        stderr=str(error),
                        termination_reason="infrastructure_error",
                        worker_id=endpoint.worker_id,
                        backend=(
                            "local-isolated"
                            if endpoint.transport == "local"
                            else "lan-worker"
                        ),
                        infrastructure_error=True,
                    )
            finally:
                self._release(endpoint, required)
            classify_reported_infrastructure_failure(
                current,
                self.gate_config.reported_infrastructure_markers,
            )
            attempts.append(self._infrastructure_attempt_evidence(current))
            current.infrastructure_attempts = list(attempts)
            last = current
            if not current.infrastructure_error:
                return current
            if current.termination_reason == "remote_state_uncertain":
                return current
        last.infrastructure_attempts = list(attempts)
        return last

    def close(self) -> None:
        for endpoint in self.endpoints:
            if endpoint.transport != "https" or endpoint.worker_id not in self._staged:
                continue
            try:
                self.clients[endpoint.worker_id].cleanup_plan(
                    self.key, self.local.plan_id
                )
            except (OSError, RuntimeError, ValueError):
                pass
        self.local.close()
        if self._bundle_path is not None:
            self._bundle_path.unlink(missing_ok=True)
            self._bundle_path = None
