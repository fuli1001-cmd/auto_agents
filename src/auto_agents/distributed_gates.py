from __future__ import annotations

import hashlib
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

from .gate_execution import (
    SHORT_RUNTIME_PROFILE,
    LocalGatePlanExecutor,
    exclusive_resource_lease,
)
from .gates import (
    GateCommandInfrastructureError,
    classify_reported_infrastructure_failure,
)
from .execution_recovery import ExecutionIncident
from .infrastructure_repair import repair_execution_infrastructure
from .models import CommandResult, GateConfig
from .worker_cluster import discover_workers, load_cluster_state
from .worker_service import WorkerClient, result_from_job_record
from .workers import (
    WORKER_PROTOCOL_VERSION,
    WorkerEndpoint,
    WorkerSlotLease,
    MANAGED_RUNTIME_LAYOUT_FEATURE,
    build_environment_manifest,
    enrich_worker_probe,
    forwarded_environment,
    load_local_worker_config,
    project_key,
    worker_probe,
)


class _EndpointAcquireCancelled(RuntimeError):
    """Raised when a queued gate command no longer needs a worker slot."""


class _WorkerAllocationError(RuntimeError):
    """Carry a user-facing worker-pool diagnosis into execution incidents."""

    def __init__(self, message: str, details: Mapping[str, object]) -> None:
        super().__init__(message)
        self.details = dict(details)


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
        result_context_fingerprint: str = "",
        environment_overrides: Optional[Mapping[str, str]] = None,
        proof_audit_sample_rate: float = 0.0,
    ) -> None:
        self.project_root = project_root.resolve()
        self.run_id = str(run_id)
        self.gate_config = gate_config
        self.metadata = dict(metadata)
        self.environment_overrides = dict(environment_overrides or {})
        self.local = LocalGatePlanExecutor(
            self.project_root,
            gate_config,
            metadata,
            run_id=run_id,
            worker_id="local",
            environment_fingerprint=environment_fingerprint,
            result_context_fingerprint=result_context_fingerprint,
            environment_overrides=self.environment_overrides,
            proof_audit_sample_rate=proof_audit_sample_rate,
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
        self._endpoint_rejections: list[str] = []

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
                features=tuple(
                    str(item) for item in local_probe.get("features", [])
                ),
                capability_details=dict(
                    local_probe.get("capability_details", {})
                ),
                failure_domain=dict(local_probe.get("failure_domain", {})),
            )
        ]
        self.local.worker_id = local_id
        rejection_reasons: list[str] = []
        if distributed.mode != "off" and cluster is None:
            rejection_reasons.append(
                "controller has no paired cluster state"
            )
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
            if not discovered:
                rejection_reasons.append(
                    "discovery returned no LAN worker before the configured timeout"
                )
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
                    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                        rejection_reasons.append(
                            f"{worker.worker_id}: probe failed ({error})"
                        )
                        continue
            for worker_id in sorted(probes):
                worker, client, probe = probes[worker_id]
                if not bool(probe.get("ok")):
                    rejection_reasons.append(
                        f"{worker_id}: probe returned ok=false"
                    )
                    continue
                expected_platform = str(
                    self.environment_manifest.get("platform", "")
                )
                if (
                    expected_platform
                    and str(probe.get("platform", "")) != expected_platform
                ):
                    rejection_reasons.append(
                        f"{worker_id}: platform mismatch "
                        f"(controller={expected_platform}, "
                        f"worker={str(probe.get('platform', '')) or 'unknown'})"
                    )
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
                    rejection_reasons.append(
                        f"{worker_id}: Python {required_python} is unavailable"
                    )
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
                    features=tuple(
                        sorted(
                            str(item).strip()
                            for item in probe.get("features", [])
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
        self._endpoint_rejections = list(rejection_reasons)
        if distributed.mode == "required" and len(self.endpoints) == 1:
            self.local.close()
            detail = "; ".join(rejection_reasons[:8])
            reason = (
                "distributed gates are required but no paired LAN worker is available"
                + (f": {detail}" if detail else "")
            )
            raise GateCommandInfrastructureError(
                reason,
                result=CommandResult(
                    command="distributed gate worker discovery",
                    ok=False,
                    returncode=1,
                    stderr=reason,
                    infrastructure_error=True,
                    infrastructure_failure_id="paired_lan_worker_unavailable",
                    infrastructure_capability="distributed_gate_worker",
                    infrastructure_contract="paired_worker_required",
                ),
                context="distributed gate worker discovery",
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

    def exclusive(self, command: str) -> bool:
        return self._resource_class(command) == "exclusive"

    def _resource_class(self, command: str) -> str:
        metadata = self.metadata.get(command)
        value = str(getattr(metadata, "resource_class", "normal")).strip().lower()
        return value if value in {"heavy", "exclusive"} else "normal"

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

    def _worker_allocation_error(
        self,
        command: str,
        *,
        status: str,
        exclude: set[str],
        allow_local_reuse: bool = True,
    ) -> _WorkerAllocationError:
        required_slots = self._required_slots(command)
        required_capabilities = sorted(self._requires(command))
        assessments: list[dict[str, object]] = []
        missing_capabilities: set[str] = set()
        capacity_shortfall = False
        busy = False
        for endpoint in self.endpoints:
            capabilities = sorted(set(endpoint.capabilities))
            missing = sorted(set(required_capabilities) - set(capabilities))
            missing_details: dict[str, object] = {}
            active_slots = self._active_slots.get(endpoint.worker_id, 0)
            available_slots = max(0, endpoint.max_slots - active_slots)
            already_tried = bool(
                endpoint.worker_id in exclude
                and not (allow_local_reuse and endpoint.transport == "local")
            )
            reasons: list[str] = []
            if already_tried:
                reasons.append("already tried")
            if endpoint.max_slots < required_slots:
                reasons.append(
                    f"total capacity {endpoint.max_slots} is below {required_slots}"
                )
                capacity_shortfall = True
            if missing:
                reasons.append("missing capabilities: " + ", ".join(missing))
                missing_capabilities.update(missing)
                for capability in missing:
                    detail = endpoint.capability_details.get(capability, {})
                    if not isinstance(detail, Mapping):
                        continue
                    missing_details[capability] = dict(detail)
                    error = " ".join(str(detail.get("error", "")).split())
                    if error:
                        reasons.append(
                            f"{capability} probe: {error[:500]}"
                        )
            if not reasons and available_slots < required_slots:
                reasons.append(
                    f"only {available_slots} of {endpoint.max_slots} slots are currently available"
                )
                busy = True
            assessments.append(
                {
                    "worker_id": endpoint.worker_id,
                    "transport": endpoint.transport,
                    "status": "eligible" if not reasons else "ineligible",
                    "reasons": reasons,
                    "required_slots": required_slots,
                    "available_slots": available_slots,
                    "max_slots": endpoint.max_slots,
                    "required_capabilities": required_capabilities,
                    "capabilities": capabilities,
                    "missing_capabilities": missing,
                    "missing_capability_details": missing_details,
                }
            )

        rejection_reasons = list(getattr(self, "_endpoint_rejections", []))
        capability_text = ", ".join(required_capabilities) or "none"
        headline = {
            "slot_wait_timeout": (
                "Verification could not start: worker slot scheduling timed out."
            ),
            "pinned_worker_ineligible": (
                "Verification could not start: the worker pinned to the sequential "
                "gate lane no longer satisfies the command."
            ),
        }.get(
            status,
            "Verification could not start: no eligible worker can run this command.",
        )
        lines = [
            headline,
            (
                f"Required worker: {required_slots} slot(s); "
                f"capabilities: {capability_text}."
            ),
            "Worker checks:",
        ]
        if assessments:
            for assessment in assessments:
                worker_id = str(assessment["worker_id"])
                transport = str(assessment["transport"])
                reasons = [str(item) for item in assessment["reasons"]]
                outcome = "; ".join(reasons) if reasons else "eligible but unavailable"
                lines.append(
                    f"- {worker_id} ({transport}): {outcome}; slots "
                    f"{assessment['available_slots']}/{assessment['max_slots']} available."
                )
        else:
            lines.append("- No local or remote worker endpoint was available.")
        for rejection in rejection_reasons[:8]:
            lines.append(f"- Unavailable worker/pool: {rejection}.")

        suggestions: list[str] = []
        for capability in sorted(missing_capabilities):
            if capability == "docker":
                suggestions.append(
                    "Start or reconnect the Docker daemon on a worker and verify "
                    "`docker version --format '{{.Server.Version}}'` succeeds."
                )
            elif capability == "ffmpeg":
                suggestions.append(
                    "Install or expose FFmpeg on a worker and verify "
                    "`ffmpeg -version` succeeds."
                )
            elif capability == "chrome":
                suggestions.append(
                    "Install or expose a supported Chrome/Chromium runtime on a worker."
                )
            else:
                suggestions.append(
                    f"Provision capability `{capability}` on at least one worker."
                )
        if capacity_shortfall:
            suggestions.append(
                f"Start or reconfigure a worker with at least {required_slots} total slots."
            )
        if busy:
            suggestions.append(
                "Wait for the listed worker slots to be released or add worker capacity."
            )
        if rejection_reasons:
            suggestions.append(
                "Start or reconnect the unavailable paired workers and restart workers after upgrades."
            )
        suggestions.extend(
            [
                "Run `auto-agents workers doctor --project <project>` to verify "
                "connectivity, runtime compatibility, capabilities, and capacity.",
                "Rerun the gate after at least one worker satisfies every "
                "listed requirement.",
            ]
        )
        lines.append("Suggested actions:")
        lines.extend(f"- {item}" for item in dict.fromkeys(suggestions))
        message = "\n".join(lines)
        return _WorkerAllocationError(
            message,
            {
                "schema_version": 1,
                "status": status,
                "required_slots": required_slots,
                "required_capabilities": required_capabilities,
                "workers": assessments,
                "endpoint_rejections": rejection_reasons,
                "suggested_actions": list(dict.fromkeys(suggestions)),
                "user_message": message,
            },
        )

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
                raise self._worker_allocation_error(
                    command,
                    status="pinned_worker_ineligible",
                    exclude=exclude,
                    allow_local_reuse=allow_local_reuse,
                )
            while not self._try_acquire(endpoint, required):
                if cancel_event is not None and cancel_event.is_set():
                    raise _EndpointAcquireCancelled
                if time.monotonic() >= deadline:
                    raise self._worker_allocation_error(
                        command,
                        status="slot_wait_timeout",
                        exclude=exclude,
                        allow_local_reuse=allow_local_reuse,
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
                raise self._worker_allocation_error(
                    command,
                    status="slot_wait_timeout",
                    exclude=exclude,
                    allow_local_reuse=allow_local_reuse,
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
                raise self._worker_allocation_error(
                    command,
                    status="no_eligible_worker",
                    exclude=exclude,
                    allow_local_reuse=allow_local_reuse,
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
        environment_overrides: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        job_id = uuid.uuid4().hex
        self._stage_remote(endpoint)
        if self.local.snapshot is None:
            raise RuntimeError("gate snapshot is unavailable")
        distributed = self.gate_config.distributed
        artifact_globs = self._metadata_list(command, "artifact_globs")
        memory_mb, memory_reserve_mb, memory_guard = self._memory_policy(command)
        environment = forwarded_environment(
            os.environ,
            distributed.extra_environment_denylist,
        )
        overrides = dict(environment_overrides or {})
        requested_runtime_profile = str(
            overrides.pop("AUTO_AGENTS_GATE_RUNTIME_PROFILE", "")
        ).strip()
        path_prepend = overrides.pop("AUTO_AGENTS_PATH_PREPEND", "")
        if path_prepend:
            environment["PATH"] = (
                f"{path_prepend}{os.pathsep}{environment.get('PATH', '')}"
            )
        environment.update(
            {
                key: value
                for key, value in overrides.items()
                if key.startswith("AUTO_AGENTS_CAPABILITY_")
                or key in {"AUTO_AGENTS_CRASH_DIR", "AUTO_AGENTS_INFRA_REPORT_PATH"}
            }
        )
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
            "worker_slot_wait_timeout_seconds": (
                self.gate_config.worker_slot_wait_timeout_seconds
            ),
            "environment_manifest": self.environment_manifest,
            "environment": environment,
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
        if MANAGED_RUNTIME_LAYOUT_FEATURE in endpoint.features:
            manifest["runtime_profile"] = (
                requested_runtime_profile or SHORT_RUNTIME_PROFILE
            )
        client = self.clients[endpoint.worker_id]
        client.submit(manifest)
        accepted = True
        deadline = (
            time.monotonic()
            + float(self.gate_config.worker_slot_wait_timeout_seconds)
            + float(timeout_seconds)
            + 90
        )
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
        environment_overrides: Optional[Mapping[str, str]] = None,
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
                timeout_seconds=(
                    self.gate_config.worker_slot_wait_timeout_seconds
                ),
                cancel_event=cancel_event,
                owner_metadata={
                    "project_root": str(self.project_root),
                    "run_id": self.run_id,
                    "plan_id": self.local.plan_id,
                    "lane": lane,
                    "backend": "local-isolated",
                    "command_sha256": hashlib.sha256(
                        command.encode("utf-8")
                    ).hexdigest(),
                },
            ):
                local_overrides = dict(environment_overrides or {})
                path_prepend = local_overrides.pop("AUTO_AGENTS_PATH_PREPEND", "")
                if path_prepend:
                    local_overrides["PATH"] = (
                        f"{path_prepend}{os.pathsep}{os.environ.get('PATH', '')}"
                    )
                result = self.local.run(
                    command,
                    lane=lane,
                    timeout_seconds=timeout_seconds,
                    adaptive_timeout_enabled=adaptive_timeout_enabled,
                    idle_timeout_seconds=idle_timeout_seconds,
                    cancel_event=cancel_event,
                    progress=progress,
                    environment_overrides=local_overrides,
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
                environment_overrides=environment_overrides,
            )
        self.local.record_timing(command, result)
        if progress is not None:
            progress("finish", command, result.duration_seconds)
        return result

    def _run_uncached(
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
            except _WorkerAllocationError as error:
                result.stderr = str(error)
                result.process_snapshot = {
                    **dict(result.process_snapshot),
                    "worker_allocation": dict(error.details),
                }
                return result
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
        # Operator-bound inputs and secrets are controller-local by default.
        # Do not forward them to LAN workers unless a future worker protocol
        # explicitly advertises matching named bindings and runtime digests.
        if self.environment_overrides:
            return self.local.run(
                command,
                lane=lane,
                timeout_seconds=timeout_seconds,
                adaptive_timeout_enabled=adaptive_timeout_enabled,
                idle_timeout_seconds=idle_timeout_seconds,
                cancel_event=cancel_event,
                progress=progress,
                environment_overrides=self.environment_overrides,
            )
        cached = self.local.cached_result(command)
        if cached is not None:
            if progress is not None:
                progress("cache_hit", command, 0.0)
            return cached
        result = self._run_uncached(
            command,
            lane=lane,
            timeout_seconds=timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=idle_timeout_seconds,
            cancel_event=cancel_event,
            progress=progress,
        )
        self.local.record_cached_result(command, result)
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
            "repair_scope": result.infrastructure_repair_scope,
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
        repaired = self._repair_reported_infrastructure(
            command=command,
            first_result=last,
            attempts=attempts,
            timeout_seconds=timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=idle_timeout_seconds,
            cancel_event=cancel_event,
            progress=progress,
        )
        return repaired or last

    def _managed_recovery_config(self) -> tuple[bool, int, bool, int]:
        try:
            payload = json.loads(
                (self.project_root / ".auto-agents" / "config.json").read_text(
                    encoding="utf-8"
                )
            )
            recovery = payload.get("execution", {}).get("recovery", {})
            return (
                bool(recovery.get("managed_runtime_downloads_enabled", True)),
                max(1, int(recovery.get("max_managed_runtime_candidates", 3))),
                bool(
                    recovery.get(
                        "managed_runtime_layout_repairs_enabled", True
                    )
                ),
                max(
                    1,
                    int(
                        recovery.get(
                            "max_managed_repair_attempts_per_incident", 6
                        )
                    ),
                ),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return True, 3, True, 6

    def _repair_reported_infrastructure(
        self,
        *,
        command: str,
        first_result: CommandResult,
        attempts: list[dict[str, object]],
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event: Optional[threading.Event],
        progress,
    ) -> Optional[CommandResult]:
        failure_id = first_result.infrastructure_failure_id
        lowered = (
            f"{failure_id} {first_result.stdout} {first_result.stderr}"
        ).lower()
        if not any(token in lowered for token in ("browser", "chrome", "chromium", "devtools")):
            return None
        (
            allow_downloads,
            max_candidates,
            allow_runtime_layout_repair,
            max_repair_attempts,
        ) = self._managed_recovery_config()
        incident_id = f"managed-{uuid.uuid4().hex[:12]}"
        last = first_result
        socket_path_failure = any(
            token in lowered
            for token in (
                "socket path too long",
                "singletonsocket",
                "enametoolong",
            )
        )
        if socket_path_failure and allow_runtime_layout_repair:
            runtime_attempts = 0
            for endpoint in self.endpoints:
                if MANAGED_RUNTIME_LAYOUT_FEATURE not in endpoint.features:
                    attempts.append(
                        {
                            "worker_id": endpoint.worker_id,
                            "event": "managed_infrastructure_repair",
                            "repaired": False,
                            "repair_driver_id": "short_runtime_path_repair",
                            "runtime_profile": SHORT_RUNTIME_PROFILE,
                            "reason": (
                                "worker does not advertise "
                                f"{MANAGED_RUNTIME_LAYOUT_FEATURE}"
                            ),
                        }
                    )
                    continue
                if runtime_attempts >= max_repair_attempts:
                    break
                runtime_attempts += 1
                candidate_key = (
                    f"{endpoint.worker_id}:"
                    f"{endpoint.failure_domain.get('id', 'unknown')}:"
                    f"{SHORT_RUNTIME_PROFILE}"
                )
                attempts.append(
                    {
                        "worker_id": endpoint.worker_id,
                        "event": "managed_infrastructure_repair",
                        "repaired": True,
                        "repair_driver_id": "short_runtime_path_repair",
                        "runtime_profile": SHORT_RUNTIME_PROFILE,
                        "candidate_key": candidate_key,
                        "reason": "retrying with a short per-job socket runtime",
                    }
                )
                current = self._run_on_endpoint(
                    endpoint,
                    command,
                    lane="",
                    timeout_seconds=timeout_seconds,
                    adaptive_timeout_enabled=adaptive_timeout_enabled,
                    idle_timeout_seconds=idle_timeout_seconds,
                    cancel_event=cancel_event,
                    progress=progress,
                    environment_overrides={
                        "AUTO_AGENTS_GATE_RUNTIME_PROFILE": SHORT_RUNTIME_PROFILE,
                    },
                )
                classify_reported_infrastructure_failure(
                    current,
                    self.gate_config.reported_infrastructure_markers,
                )
                attempts.append(self._infrastructure_attempt_evidence(current))
                current.infrastructure_attempts = list(attempts)
                last = current
                if not current.infrastructure_error:
                    return current
        elif socket_path_failure:
            attempts.append(
                {
                    "worker_id": "controller",
                    "event": "managed_infrastructure_repair",
                    "repaired": False,
                    "repair_driver_id": "short_runtime_path_repair",
                    "runtime_profile": SHORT_RUNTIME_PROFILE,
                    "reason": "managed runtime layout repair is disabled",
                }
            )
        for endpoint in self.endpoints:
            if "managed_capability_repair_v2" not in endpoint.features:
                attempts.append(
                    {
                        "worker_id": endpoint.worker_id,
                        "event": "managed_infrastructure_repair",
                        "repaired": False,
                        "reason": (
                            "worker does not advertise "
                            "managed_capability_repair_v2"
                        ),
                    }
                )
                continue
            failed_artifacts = {
                str(
                    item.get("capability_details", {})
                    .get("chrome", {})
                    .get("artifact_sha256", "")
                )
                for item in attempts
                if str(item.get("worker_id", "")) == endpoint.worker_id
                and isinstance(item.get("capability_details"), dict)
            }
            try:
                if endpoint.transport == "local":
                    repair = repair_execution_infrastructure(
                        ExecutionIncident(
                            incident_id=incident_id,
                            run_id=self.run_id,
                            source="gate",
                            kind="gate_reported_infrastructure_error",
                            stage="worker",
                            context="managed capability repair",
                            command=command,
                            stderr_tail=failure_id,
                        ),
                        failed_artifacts=sorted(failed_artifacts),
                        allow_downloads=allow_downloads,
                        max_candidates=max_candidates,
                    ).to_dict()
                else:
                    repair = self.clients[endpoint.worker_id].repair_capability(
                        capability="chrome",
                        run_id=self.run_id,
                        incident_id=incident_id,
                        failure_id=failure_id,
                        failed_artifacts=sorted(failed_artifacts),
                        allow_downloads=allow_downloads,
                        max_candidates=max_candidates,
                    )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                attempts.append(
                    {
                        "worker_id": endpoint.worker_id,
                        "event": "managed_infrastructure_repair",
                        "repaired": False,
                        "reason": str(error),
                    }
                )
                continue
            attempts.append(
                {
                    "worker_id": endpoint.worker_id,
                    "event": "managed_infrastructure_repair",
                    **repair,
                }
            )
            if not bool(repair.get("repaired", repair.get("ok", False))):
                continue
            environment = repair.get("environment", {})
            if not isinstance(environment, Mapping):
                continue
            current = self._run_on_endpoint(
                endpoint,
                command,
                lane="",
                timeout_seconds=timeout_seconds,
                adaptive_timeout_enabled=adaptive_timeout_enabled,
                idle_timeout_seconds=idle_timeout_seconds,
                cancel_event=cancel_event,
                progress=progress,
                environment_overrides={
                    str(key): str(value) for key, value in environment.items()
                },
            )
            classify_reported_infrastructure_failure(
                current,
                self.gate_config.reported_infrastructure_markers,
            )
            attempts.append(self._infrastructure_attempt_evidence(current))
            current.infrastructure_attempts = list(attempts)
            last = current
            if not current.infrastructure_error:
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
