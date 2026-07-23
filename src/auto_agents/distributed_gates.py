from __future__ import annotations

from dataclasses import asdict
import io
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from typing import Mapping, Optional

from .gate_execution import LocalGatePlanExecutor, exclusive_resource_lease
from .models import CommandResult, GateConfig
from .workers import (
    WorkerEndpoint,
    WorkerSlotLease,
    command_result_from_dict,
    controller_worker_call,
    forwarded_environment,
    load_local_worker_config,
    load_worker_pool,
    project_key,
    ssh_command,
    worker_probe,
)


class DistributedGatePlanExecutor:
    """Dispatch isolated gate commands across local and SSH workers."""

    def __init__(
        self,
        project_root: Path,
        gate_config: GateConfig,
        metadata: Mapping[str, object],
        *,
        run_id: str = "",
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
        )
        self.key = project_key(self.project_root)
        self._bundle_path: Optional[Path] = None
        self._staged: set[str] = set()
        self._lane_endpoint: dict[str, WorkerEndpoint] = {}
        self._lane_successes: dict[str, int] = {}
        self._lock = threading.Lock()
        self._active_slots: dict[str, int] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self.endpoints: list[WorkerEndpoint] = []

    def __enter__(self) -> "DistributedGatePlanExecutor":
        self.local.__enter__()
        distributed = self.gate_config.distributed
        try:
            pool = load_worker_pool(distributed.worker_pool)
            self.endpoints = list(pool.workers)
        except (OSError, ValueError, json.JSONDecodeError):
            if distributed.fallback != "local":
                self.local.close()
                raise
            capabilities = tuple(
                str(item)
                for item in worker_probe(distributed.environment_id).get(
                    "capabilities", []
                )
            )
            self.endpoints = [
                WorkerEndpoint(
                    worker_id="local",
                    transport="local",
                    max_slots=max(1, self.gate_config.max_auto_workers),
                    capabilities=capabilities,
                )
            ]
        if not any(endpoint.transport == "local" for endpoint in self.endpoints):
            capabilities = tuple(
                str(item)
                for item in worker_probe(
                    self.gate_config.distributed.environment_id
                ).get("capabilities", [])
            )
            self.endpoints.insert(
                0,
                WorkerEndpoint(
                    worker_id="local",
                    transport="local",
                    max_slots=max(1, self.gate_config.max_auto_workers),
                    capabilities=capabilities,
                ),
            )
        for endpoint in self.endpoints:
            self._active_slots[endpoint.worker_id] = 0
            self._semaphores[endpoint.worker_id] = threading.BoundedSemaphore(
                endpoint.max_slots
            )
        local_endpoint = next(
            (
                endpoint
                for endpoint in self.endpoints
                if endpoint.transport == "local"
            ),
            None,
        )
        if local_endpoint is not None:
            self.local.worker_id = local_endpoint.worker_id
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def priority(self, command: str) -> tuple[int, str]:
        return self.local.priority(command)

    def _resource_class(self, command: str) -> str:
        metadata = self.metadata.get(command)
        return (
            "heavy"
            if str(getattr(metadata, "resource_class", "normal")).lower() == "heavy"
            else "normal"
        )

    def _required_slots(self, command: str) -> int:
        return 2 if self._resource_class(command) == "heavy" else 1

    def _artifact_globs(self, command: str) -> list[str]:
        metadata = self.metadata.get(command)
        raw = getattr(metadata, "artifact_globs", [])
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def _exclusive_resources(self, command: str) -> list[str]:
        metadata = self.metadata.get(command)
        raw = getattr(metadata, "exclusive_resources", [])
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def _requires(self, command: str) -> set[str]:
        metadata = self.metadata.get(command)
        raw = getattr(metadata, "requires", [])
        if not isinstance(raw, list):
            return set()
        return {str(item).strip().lower() for item in raw if str(item).strip()}

    def _endpoint_supports(self, endpoint: WorkerEndpoint, command: str) -> bool:
        required = self._requires(command)
        return not required or required.issubset(set(endpoint.capabilities))

    def _try_acquire(self, endpoint: WorkerEndpoint, required: int) -> bool:
        semaphore = self._semaphores[endpoint.worker_id]
        acquired = 0
        for _ in range(required):
            if not semaphore.acquire(blocking=False):
                for _ in range(acquired):
                    semaphore.release()
                return False
            acquired += 1
        with self._lock:
            self._active_slots[endpoint.worker_id] += required
        return True

    def _release(self, endpoint: WorkerEndpoint, required: int) -> None:
        with self._lock:
            self._active_slots[endpoint.worker_id] = max(
                0, self._active_slots[endpoint.worker_id] - required
            )
        semaphore = self._semaphores[endpoint.worker_id]
        for _ in range(required):
            semaphore.release()

    def _acquire_endpoint(
        self,
        command: str,
        *,
        exclude: set[str],
        lane: str,
    ) -> tuple[WorkerEndpoint, int]:
        required = self._required_slots(command)
        if lane and lane in self._lane_endpoint:
            endpoint = self._lane_endpoint[lane]
            while not self._try_acquire(endpoint, required):
                time.sleep(0.1)
            return endpoint, required
        while True:
            candidates = [
                endpoint
                for endpoint in self.endpoints
                if endpoint.worker_id not in exclude
                and endpoint.max_slots >= required
                and self._endpoint_supports(endpoint, command)
            ]
            if not candidates:
                candidates = [
                    endpoint
                    for endpoint in self.endpoints
                    if endpoint.transport == "local"
                    and endpoint.max_slots >= required
                    and self._endpoint_supports(endpoint, command)
                ]
            if not candidates:
                required_capabilities = ", ".join(sorted(self._requires(command))) or "none"
                raise RuntimeError(
                    "no gate worker has enough slots and required capabilities "
                    f"({required_capabilities})"
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
            time.sleep(0.1)

    def _bundle(self) -> Path:
        if self._bundle_path is not None:
            return self._bundle_path
        if self.local.snapshot is None:
            raise RuntimeError("gate snapshot is unavailable")
        handle, name = tempfile.mkstemp(prefix="auto-agents-gate-", suffix=".bundle")
        os.close(handle)
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
            raise RuntimeError(process.stderr.strip() or "git bundle create failed")
        os.chmod(path, 0o600)
        self._bundle_path = path
        return path

    def _stage_remote(self, endpoint: WorkerEndpoint) -> None:
        with self._lock:
            if endpoint.worker_id in self._staged:
                return
        if self.local.snapshot is None:
            raise RuntimeError("gate snapshot is unavailable")
        bundle = self._bundle()
        command = ssh_command(
            endpoint,
            [
                "stage",
                "--project-key",
                self.key,
                "--snapshot",
                self.local.snapshot.commit_sha,
                "--source-ref",
                self.local.snapshot.ref_name,
            ],
            connect_timeout_seconds=self.gate_config.distributed.connect_timeout_seconds,
        )
        with bundle.open("rb") as stream:
            process = subprocess.run(command, stdin=stream, capture_output=True)
        if process.returncode != 0:
            raise RuntimeError(
                process.stderr.decode("utf-8", errors="replace").strip()
                or "remote snapshot staging failed"
            )
        with self._lock:
            self._staged.add(endpoint.worker_id)

    @staticmethod
    def _parse_events(output: bytes) -> tuple[bool, Optional[CommandResult]]:
        accepted = False
        result: Optional[CommandResult] = None
        for raw_line in output.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "accepted":
                accepted = True
            if event.get("type") == "result" and isinstance(event.get("result"), dict):
                result = command_result_from_dict(event["result"])
        return accepted, result

    def _query_remote(
        self,
        endpoint: WorkerEndpoint,
        job_id: str,
    ) -> dict[str, object]:
        process = controller_worker_call(
            endpoint,
            ["query", "--job-id", job_id],
            connect_timeout_seconds=self.gate_config.distributed.connect_timeout_seconds,
        )
        if process.returncode != 0:
            return {}
        try:
            payload = json.loads(process.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _fetch_artifacts(
        self,
        endpoint: WorkerEndpoint,
        command: str,
        job_id: str,
    ) -> dict[str, str]:
        process = controller_worker_call(
            endpoint,
            ["artifacts", "--job-id", job_id],
            connect_timeout_seconds=self.gate_config.distributed.connect_timeout_seconds,
        )
        if process.returncode != 0:
            raise RuntimeError("remote artifact download failed")
        temporary_root = (
            self.local.worktree_root
            / self.local.plan_id
            / ".runtime"
            / f"remote-artifacts-{job_id}"
        )
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(process.stdout), mode="r:gz") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or member.issym()
                    or member.islnk()
                ):
                    raise RuntimeError(
                        f"unsafe remote artifact member: {member.name}"
                    )
                destination = temporary_root.joinpath(*path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RuntimeError(
                        f"unsupported remote artifact member: {member.name}"
                    )
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
                temporary_root,
                command,
                job_id,
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
        try:
            self._stage_remote(endpoint)
        except (OSError, RuntimeError) as error:
            return CommandResult(
                command=command,
                ok=False,
                returncode=125,
                stderr=str(error),
                termination_reason="infrastructure_error",
                job_id=job_id,
                worker_id=endpoint.worker_id,
                backend="ssh-isolated",
                infrastructure_error=True,
            )
        if self.local.snapshot is None:
            raise RuntimeError("gate snapshot is unavailable")
        distributed = self.gate_config.distributed
        manifest = {
            "protocol_version": 1,
            "project_key": self.key,
            "snapshot": self.local.snapshot.commit_sha,
            "plan_id": self.local.plan_id,
            "job_id": job_id,
            "lane": lane,
            "command": command,
            "resource_class": self._resource_class(command),
            "environment_id": distributed.environment_id,
            "environment": forwarded_environment(
                os.environ,
                distributed.extra_environment_denylist,
            ),
            "timeout_seconds": timeout_seconds,
            "adaptive_timeout_enabled": adaptive_timeout_enabled,
            "idle_timeout_seconds": idle_timeout_seconds,
            "artifact_globs": self._artifact_globs(command)
            + [".auto-agents/failed-verification-logs/**/*"],
            "exclusive_resources": [
                resource
                for resource in self._exclusive_resources(command)
                if resource.startswith("host:")
            ],
            "artifact_max_files": self.gate_config.isolation.artifact_max_files,
            "artifact_max_bytes": self.gate_config.isolation.artifact_max_bytes,
        }
        returncode, stdout, stderr = self._execute_remote_stream(
            endpoint,
            json.dumps(manifest).encode("utf-8"),
            absolute_timeout_seconds=(
                float(timeout_seconds)
                + float(distributed.heartbeat_timeout_seconds)
            ),
            cancel_event=cancel_event,
            job_id=job_id,
        )
        accepted, result = self._parse_events(stdout)
        if result is None and accepted:
            record = self._query_remote(endpoint, job_id)
            if (
                record.get("state") == "terminal"
                and isinstance(record.get("result"), dict)
            ):
                result = command_result_from_dict(record["result"])
            else:
                return CommandResult(
                    command=command,
                    ok=False,
                    returncode=125,
                    stderr=(
                        "remote connection ended after job acceptance; "
                        "remote state could not be confirmed terminal"
                    ),
                    termination_reason="remote_state_uncertain",
                    job_id=job_id,
                    worker_id=endpoint.worker_id,
                    backend="ssh-isolated",
                    infrastructure_error=True,
                )
        if result is None:
            return CommandResult(
                command=command,
                ok=False,
                returncode=125,
                stderr=stderr.decode("utf-8", errors="replace").strip()
                or "remote worker did not return a result",
                termination_reason="infrastructure_error",
                job_id=job_id,
                worker_id=endpoint.worker_id,
                backend="ssh-isolated",
                infrastructure_error=True,
            )
        result.worker_id = endpoint.worker_id
        result.backend = "ssh-isolated"
        if result.ok and result.artifacts:
            try:
                result.artifacts = self._fetch_artifacts(
                    endpoint,
                    command,
                    job_id,
                )
            except (OSError, RuntimeError, tarfile.TarError) as error:
                result.ok = False
                result.returncode = result.returncode or 1
                result.stderr = (
                    f"{result.stderr}\nartifact publication failed: {error}"
                ).strip()
        return result

    def _execute_remote_stream(
        self,
        endpoint: WorkerEndpoint,
        manifest: bytes,
        *,
        absolute_timeout_seconds: float,
        cancel_event: Optional[threading.Event],
        job_id: str,
    ) -> tuple[int, bytes, bytes]:
        command = ssh_command(
            endpoint,
            ["execute"],
            connect_timeout_seconds=self.gate_config.distributed.connect_timeout_seconds,
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(manifest)
        process.stdin.close()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        started = time.monotonic()
        last_activity = started
        heartbeat_timeout = float(
            self.gate_config.distributed.heartbeat_timeout_seconds
        )
        cancellation_sent = False
        while selector.get_map():
            now = time.monotonic()
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and not cancellation_sent
            ):
                controller_worker_call(
                    endpoint,
                    ["cancel", "--job-id", job_id],
                    connect_timeout_seconds=self.gate_config.distributed.connect_timeout_seconds,
                )
                cancellation_sent = True
            if (
                now - started > absolute_timeout_seconds
                or now - last_activity > heartbeat_timeout
            ):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            events = selector.select(timeout=1.0)
            if not events and process.poll() is not None:
                events = [
                    (key, selectors.EVENT_READ)
                    for key in list(selector.get_map().values())
                ]
            for key, _mask in events:
                data = os.read(key.fileobj.fileno(), 65536)
                if data:
                    chunks[str(key.data)].append(data)
                    last_activity = time.monotonic()
                else:
                    selector.unregister(key.fileobj)
        selector.close()
        returncode = process.wait()
        return (
            int(returncode),
            b"".join(chunks["stdout"]),
            b"".join(chunks["stderr"]),
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
        if endpoint.transport == "local":
            local_config = load_local_worker_config()
            with WorkerSlotLease(
                local_config.managed_root,
                endpoint.worker_id,
                endpoint.max_slots,
                required,
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
                for resource in self._exclusive_resources(command)
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
        for _attempt in range(retry_limit + 1):
            try:
                endpoint, required = self._acquire_endpoint(
                    command,
                    exclude=attempted,
                    lane=lane,
                )
            except RuntimeError as error:
                return CommandResult(
                    command=command,
                    ok=False,
                    returncode=125,
                    stderr=str(error),
                    termination_reason="infrastructure_error",
                    backend="distributed",
                    infrastructure_error=True,
                )
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
                            else "ssh-isolated"
                        ),
                        infrastructure_error=True,
                    )
            finally:
                self._release(endpoint, required)
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
        local_endpoint = next(
            (
                endpoint
                for endpoint in self.endpoints
                if endpoint.transport == "local"
            ),
            None,
        )
        if (
            local_endpoint is not None
            and local_endpoint.worker_id not in attempted
            and self.gate_config.distributed.fallback == "local"
            and local_endpoint.max_slots >= self._required_slots(command)
            and self._endpoint_supports(local_endpoint, command)
        ):
            endpoint, required = self._acquire_endpoint(
                command,
                exclude={
                    endpoint.worker_id
                    for endpoint in self.endpoints
                    if endpoint.transport != "local"
                },
                lane=lane,
            )
            try:
                try:
                    return self._run_on_endpoint(
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
                    return CommandResult(
                        command=command,
                        ok=False,
                        returncode=125,
                        stderr=str(error),
                        termination_reason="infrastructure_error",
                        worker_id=endpoint.worker_id,
                        backend="local-isolated",
                        infrastructure_error=True,
                    )
            finally:
                self._release(endpoint, required)
        return result

    def close(self) -> None:
        for endpoint in self.endpoints:
            if endpoint.transport != "ssh" or endpoint.worker_id not in self._staged:
                continue
            controller_worker_call(
                endpoint,
                [
                    "cleanup-plan",
                    "--project-key",
                    self.key,
                    "--plan-id",
                    self.local.plan_id,
                ],
                connect_timeout_seconds=self.gate_config.distributed.connect_timeout_seconds,
            )
        self.local.close()
        if self._bundle_path is not None:
            self._bundle_path.unlink(missing_ok=True)
            self._bundle_path = None
