from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import ssl
import tempfile
import threading
import time
from typing import Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .models import CommandResult
from .worker_cluster import (
    DiscoveryResponder,
    DiscoveredWorker,
    WORKER_API_PORT,
    canonical_json,
    certificate_paths,
    consume_pairing_token,
    discover_workers,
    load_cluster_state,
    sign_payload,
    verify_payload,
)
from .workers import (
    command_result_from_dict,
    load_local_worker_config,
    worker_artifacts,
    worker_cancel,
    worker_cleanup_plan,
    worker_execute,
    enrich_worker_probe,
    worker_gc,
    worker_probe as _worker_probe,
    worker_query,
    worker_stage,
)


def worker_probe(environment_id: str = "") -> dict[str, object]:
    return enrich_worker_probe(_worker_probe(environment_id))


MAX_REQUEST_BYTES = 512 * 1024 * 1024
AUTH_WINDOW_SECONDS = 60
_JOB_THREADS: dict[str, threading.Thread] = {}
_JOB_CANCEL_EVENTS: dict[str, threading.Event] = {}
_JOB_THREADS_LOCK = threading.Lock()


def _auth_payload(
    *,
    method: str,
    path: str,
    body: bytes = b"",
    body_sha256: str = "",
    timestamp: str,
    nonce: str,
) -> dict[str, object]:
    return {
        "method": method.upper(),
        "path": path,
        "body_sha256": body_sha256 or hashlib.sha256(body).hexdigest(),
        "timestamp": timestamp,
        "nonce": nonce,
    }


class WorkerClient:
    def __init__(
        self,
        worker: DiscoveredWorker,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.worker = worker
        self.timeout_seconds = timeout_seconds
        state = load_cluster_state(required=True)
        assert state is not None
        self.state = state

    def _connection(
        self,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> http.client.HTTPSConnection:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            self.worker.host,
            self.worker.port,
            timeout=timeout_seconds or self.timeout_seconds,
            context=context,
        )
        connection.connect()
        assert connection.sock is not None
        actual = hashlib.sha256(
            connection.sock.getpeercert(binary_form=True)
        ).hexdigest()
        if not hmac.compare_digest(actual, self.worker.tls_fingerprint):
            connection.close()
            raise RuntimeError(
                f"worker TLS fingerprint changed: {self.worker.worker_id}"
            )
        return connection

    def _headers(
        self,
        *,
        method: str,
        path: str,
        body_sha256: str,
        content_length: int,
    ) -> dict[str, str]:
        timestamp = f"{time.time():.6f}"
        nonce = secrets.token_hex(16)
        signature = sign_payload(
            self.state,
            _auth_payload(
                method=method,
                path=path,
                body_sha256=body_sha256,
                timestamp=timestamp,
                nonce=nonce,
            ),
        )
        return {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(content_length),
            "X-Auto-Agents-Cluster": self.state.cluster_id,
            "X-Auto-Agents-Timestamp": timestamp,
            "X-Auto-Agents-Nonce": nonce,
            "X-Auto-Agents-Signature": signature,
        }

    @staticmethod
    def _response(
        connection: http.client.HTTPSConnection,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        response = connection.getresponse()
        payload = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        if status >= 400:
            try:
                detail = json.loads(payload.decode("utf-8")).get("error", "")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = payload.decode("utf-8", errors="replace")
            raise RuntimeError(detail or f"worker request failed with HTTP {status}")
        return status, payload, headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        timeout_seconds: Optional[float] = None,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        body_sha256 = hashlib.sha256(body).hexdigest()
        connection = self._connection(timeout_seconds=timeout_seconds)
        connection.request(
            method,
            path,
            body=body,
            headers=self._headers(
                method=method,
                path=path,
                body_sha256=body_sha256,
                content_length=len(body),
            ),
        )
        return self._response(connection)

    def _request_file(
        self,
        method: str,
        path: str,
        source: Path,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        length = source.stat().st_size
        if length > MAX_REQUEST_BYTES:
            raise RuntimeError("worker request body exceeds the size limit")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        connection = self._connection(timeout_seconds=timeout_seconds)
        connection.putrequest(method, path)
        for key, value in self._headers(
            method=method,
            path=path,
            body_sha256=digest.hexdigest(),
            content_length=length,
        ).items():
            connection.putheader(key, value)
        connection.endheaders()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                connection.send(chunk)
        return self._response(connection)

    def probe(self, *, timeout_seconds: Optional[float] = None) -> dict[str, object]:
        _status, payload, _headers = self._request(
            "GET",
            "/v1/probe",
            timeout_seconds=timeout_seconds,
        )
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def stage(
        self,
        *,
        project_key: str,
        snapshot: str,
        source_ref: str,
        bundle: Path,
    ) -> dict[str, object]:
        query = urlencode(
            {
                "project_key": project_key,
                "snapshot": snapshot,
                "source_ref": source_ref,
            }
        )
        _status, payload, _headers = self._request_file(
            "POST",
            f"/v1/snapshots?{query}",
            bundle,
            timeout_seconds=max(self.timeout_seconds, 120),
        )
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def submit(self, manifest: Mapping[str, object]) -> dict[str, object]:
        body = canonical_json(manifest)
        _status, payload, _headers = self._request(
            "POST",
            "/v1/jobs",
            body=body,
        )
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def query(self, job_id: str) -> dict[str, object]:
        _status, payload, _headers = self._request(
            "GET",
            f"/v1/jobs/{job_id}",
        )
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def cancel(self, job_id: str) -> dict[str, object]:
        _status, payload, _headers = self._request(
            "POST",
            f"/v1/jobs/{job_id}/cancel",
        )
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def artifacts(self, job_id: str) -> bytes:
        _status, payload, _headers = self._request(
            "GET",
            f"/v1/jobs/{job_id}/artifacts",
            timeout_seconds=max(self.timeout_seconds, 120),
        )
        return payload

    def cleanup_plan(self, project_key: str, plan_id: str) -> dict[str, object]:
        body = canonical_json({"project_key": project_key, "plan_id": plan_id})
        _status, payload, _headers = self._request(
            "POST",
            "/v1/cleanup-plan",
            body=body,
        )
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def gc(self, max_age_seconds: float) -> dict[str, object]:
        body = canonical_json({"max_age_seconds": max_age_seconds})
        _status, payload, _headers = self._request("POST", "/v1/gc", body=body)
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def repair_capability(
        self,
        *,
        capability: str,
        run_id: str,
        incident_id: str,
        failure_id: str = "",
        failed_artifacts=(),
        allow_downloads: bool = True,
        max_candidates: int = 3,
    ) -> dict[str, object]:
        body = canonical_json(
            {
                "capability": capability,
                "run_id": run_id,
                "incident_id": incident_id,
                "failure_id": failure_id,
                "failed_artifacts": list(failed_artifacts),
                "allow_downloads": bool(allow_downloads),
                "max_candidates": int(max_candidates),
            }
        )
        _status, payload, _headers = self._request(
            "POST", "/v1/repair-capability", body=body
        )
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {}


class WorkerRequestHandler(BaseHTTPRequestHandler):
    server_version = "auto-agents-worker/2"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        if os.environ.get("AUTO_AGENTS_WORKER_HTTP_LOG", ""):
            super().log_message(format, *args)

    def _body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        limit = (
            16 * 1024
            if urlparse(self.path).path == "/v1/pair"
            else MAX_REQUEST_BYTES
        )
        if length < 0 or length > limit:
            raise ValueError("worker request body exceeds the size limit")
        return self.rfile.read(length)

    def _json(self, status: int, payload: Mapping[str, object]) -> None:
        body = canonical_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authenticated(
        self,
        body: bytes = b"",
        *,
        body_sha256: str = "",
    ) -> bool:
        state = load_cluster_state()
        if state is None:
            return False
        if self.headers.get("X-Auto-Agents-Cluster", "") != state.cluster_id:
            return False
        timestamp = self.headers.get("X-Auto-Agents-Timestamp", "")
        nonce = self.headers.get("X-Auto-Agents-Nonce", "")
        signature = self.headers.get("X-Auto-Agents-Signature", "")
        try:
            if abs(time.time() - float(timestamp)) > AUTH_WINDOW_SECONDS:
                return False
        except ValueError:
            return False
        valid = verify_payload(
            state,
            _auth_payload(
                method=self.command,
                path=self.path,
                body=body,
                body_sha256=body_sha256,
                timestamp=timestamp,
                nonce=nonce,
            ),
            signature,
        )
        if not valid:
            return False
        server = self.server
        assert isinstance(server, WorkerHTTPServer)
        return server.claim_nonce(nonce)

    def _dispatch(self) -> None:
        try:
            parsed = urlparse(self.path)
            if self.command == "POST" and parsed.path == "/v1/snapshots":
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError as error:
                    raise ValueError("invalid Content-Length") from error
                if length < 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("worker request body exceeds the size limit")
                digest = hashlib.sha256()
                remaining = length
                with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as stream:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("worker request body ended early")
                        stream.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if not self._authenticated(body_sha256=digest.hexdigest()):
                        self._json(
                            401,
                            {
                                "ok": False,
                                "error": "worker authentication failed",
                            },
                        )
                        return
                    stream.seek(0)
                    query = parse_qs(parsed.query)
                    payload = worker_stage(
                        key=str(query.get("project_key", [""])[0]),
                        snapshot_sha=str(query.get("snapshot", [""])[0]),
                        source_ref=str(query.get("source_ref", [""])[0]),
                        stream=stream,
                    )
                self._json(200, payload)
                return
            body = self._body()
            if self.command == "POST" and parsed.path == "/v1/pair":
                request = json.loads(body.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("pair request must be an object")
                state = consume_pairing_token(str(request.get("token", "")))
                self._json(
                    200,
                    {
                        "cluster_id": state.cluster_id,
                        "node_id": state.node_id,
                        "secret": state.secret,
                        "hostname": state.hostname,
                        "api_port": state.api_port,
                        "peers": {},
                    },
                )
                return
            if not self._authenticated(body):
                self._json(401, {"ok": False, "error": "worker authentication failed"})
                return
            if self.command == "GET" and parsed.path == "/v1/probe":
                self._json(200, worker_probe(""))
                return
            if self.command == "POST" and parsed.path == "/v1/jobs":
                manifest = json.loads(body.decode("utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("job manifest must be an object")
                job_id = str(manifest.get("job_id", ""))
                with _JOB_THREADS_LOCK:
                    existing = worker_query(job_id)
                    running = _JOB_THREADS.get(job_id)
                    if existing or (running is not None and running.is_alive()):
                        self._json(
                            200,
                            existing
                            or {
                                "ok": True,
                                "job_id": job_id,
                                "state": "accepted",
                            },
                        )
                        return

                    def execute() -> None:
                        try:
                            worker_execute(
                                manifest,
                                event_stream=io.StringIO(),
                                cancel_event=cancel_event,
                            )
                        finally:
                            with _JOB_THREADS_LOCK:
                                _JOB_THREADS.pop(job_id, None)
                                _JOB_CANCEL_EVENTS.pop(job_id, None)

                    cancel_event = threading.Event()
                    thread = threading.Thread(
                        target=execute,
                        name=f"auto-agents-worker-job-{job_id[:12]}",
                        daemon=True,
                    )
                    _JOB_THREADS[job_id] = thread
                    _JOB_CANCEL_EVENTS[job_id] = cancel_event
                    thread.start()
                self._json(
                    202,
                    {"ok": True, "job_id": job_id, "state": "accepted"},
                )
                return
            job_prefix = "/v1/jobs/"
            if parsed.path.startswith(job_prefix):
                suffix = parsed.path[len(job_prefix) :]
                job_id, _, action = suffix.partition("/")
                if self.command == "GET" and not action:
                    payload = worker_query(job_id)
                    if not payload:
                        with _JOB_THREADS_LOCK:
                            running = _JOB_THREADS.get(job_id)
                            if running is not None and running.is_alive():
                                payload = {
                                    "ok": True,
                                    "job_id": job_id,
                                    "state": "accepted",
                                }
                    self._json(200 if payload else 404, payload or {"error": "job not found"})
                    return
                if self.command == "POST" and action == "cancel":
                    with _JOB_THREADS_LOCK:
                        event = _JOB_CANCEL_EVENTS.get(job_id)
                        if event is not None:
                            event.set()
                    self._json(200, worker_cancel(job_id))
                    return
                if self.command == "GET" and action == "artifacts":
                    output = io.BytesIO()
                    worker_artifacts(job_id, output)
                    self._bytes(200, output.getvalue(), "application/gzip")
                    return
            if self.command == "POST" and parsed.path == "/v1/cleanup-plan":
                request = json.loads(body.decode("utf-8"))
                self._json(
                    200,
                    worker_cleanup_plan(
                        str(request.get("project_key", "")),
                        str(request.get("plan_id", "")),
                    ),
                )
                return
            if self.command == "POST" and parsed.path == "/v1/gc":
                request = json.loads(body.decode("utf-8"))
                self._json(
                    200,
                    worker_gc(float(request.get("max_age_seconds", 86400))),
                )
                return
            self._json(404, {"ok": False, "error": "worker endpoint not found"})
        except PermissionError as error:
            self._json(403, {"ok": False, "error": str(error)})
        except (
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            self._json(400, {"ok": False, "error": str(error)})

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()


class WorkerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, WorkerRequestHandler)
        self._nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()

    def claim_nonce(self, nonce: str) -> bool:
        if not nonce:
            return False
        now = time.time()
        with self._nonce_lock:
            self._nonces = {
                key: seen
                for key, seen in self._nonces.items()
                if now - seen <= AUTH_WINDOW_SECONDS
            }
            if nonce in self._nonces:
                return False
            self._nonces[nonce] = now
            return True

    def _repair_capability(self, body: bytes) -> None:
        if not self._authenticated(body):
            self._json(401, {"ok": False, "error": "authentication failed"})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        capability = str(payload.get("capability", "")).strip().lower()
        if capability != "chrome":
            self._json(
                400,
                {"ok": False, "error": "unsupported managed capability"},
            )
            return
        from .execution_recovery import ExecutionIncident
        from .infrastructure_repair import repair_execution_infrastructure

        failure_id = str(payload.get("failure_id", "")).strip()
        incident = ExecutionIncident(
            incident_id=str(payload.get("incident_id", "")).strip() or "worker-repair",
            run_id=str(payload.get("run_id", "")).strip() or "worker",
            source="gate",
            kind="gate_reported_infrastructure_error",
            stage="worker",
            context="managed capability repair",
            command="chrome",
            stderr_tail=failure_id or "browser runtime repair",
            process_snapshot={"infrastructure_failure_id": failure_id},
        )
        result = repair_execution_infrastructure(
            incident,
            failed_artifacts=[
                str(item) for item in payload.get("failed_artifacts", [])
            ],
            allow_downloads=bool(payload.get("allow_downloads", True)),
            max_candidates=max(1, int(payload.get("max_candidates", 3))),
        )
        self._json(200 if result.repaired else 409, {"ok": result.repaired, **result.to_dict()})


class WorkerService:
    def __init__(
        self,
        *,
        bind: str = "0.0.0.0",
        port: int = WORKER_API_PORT,
    ) -> None:
        self.bind = bind
        self.port = int(port)
        self.server: Optional[WorkerHTTPServer] = None
        self.discovery: Optional[DiscoveryResponder] = None

    def serve_forever(self) -> None:
        state = load_cluster_state(required=True)
        assert state is not None
        certificate, private_key = certificate_paths()
        config = load_local_worker_config()
        probe = worker_probe("")
        capabilities = [
            str(item) for item in probe.get("capabilities", [])
        ]
        server = WorkerHTTPServer((self.bind, self.port))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate), str(private_key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self.server = server
        self.discovery = DiscoveryResponder(
            port=self.port,
            max_slots=config.max_slots,
            capabilities=capabilities,
        )
        self.discovery.start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            discovery = self.discovery
            if discovery is not None:
                discovery.close()
                if self.discovery is discovery:
                    self.discovery = None
            server.server_close()
            if self.server is server:
                self.server = None

    def close(self) -> None:
        discovery = self.discovery
        if discovery is not None:
            discovery.close()
            if self.discovery is discovery:
                self.discovery = None
        server = self.server
        if server is not None:
            server.shutdown()
            server.server_close()
            if self.server is server:
                self.server = None


def result_from_job_record(record: Mapping[str, object]) -> Optional[CommandResult]:
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    return command_result_from_dict(result)


def lan_workers_status() -> dict[str, object]:
    local = worker_probe("")
    local["transport"] = "local"
    workers: list[dict[str, object]] = [local]
    state = load_cluster_state()
    if state is not None:
        for discovered in discover_workers():
            client = WorkerClient(discovered)
            try:
                payload = client.probe()
                payload["transport"] = "lan"
                payload["host"] = discovered.host
                payload["port"] = discovered.port
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                payload = {
                    "ok": False,
                    "worker_id": discovered.worker_id,
                    "transport": "lan",
                    "host": discovered.host,
                    "port": discovered.port,
                    "error": str(error),
                }
            workers.append(payload)
    return {
        "ok": all(bool(item.get("ok")) for item in workers),
        "cluster_id": state.cluster_id if state is not None else "",
        "workers": workers,
    }


def lan_workers_doctor(project_root: Optional[Path] = None) -> dict[str, object]:
    payload = lan_workers_status()
    workers = payload["workers"]
    expected_protocol = worker_probe("").get("protocol_version")
    for worker in workers:
        if worker.get("protocol_version") != expected_protocol:
            worker["ok"] = False
            worker["protocol_error"] = {
                "expected": expected_protocol,
                "reported": worker.get("protocol_version"),
            }
    for field, error_name in (
        ("auto_agents_version", "auto_agents_version_mismatch"),
        (
            "worker_implementation_fingerprint",
            "worker_implementation_mismatch",
        ),
    ):
        reported = {
            str(worker.get("worker_id", "")): str(worker.get(field, ""))
            for worker in workers
            if bool(worker.get("ok"))
        }
        if len(set(reported.values())) > 1:
            for worker in workers:
                worker["ok"] = False
                worker[error_name] = reported
    if project_root is not None:
        from .workers import build_environment_manifest

        environment = build_environment_manifest(project_root)
        payload["environment_manifest"] = {
            "environment_id": environment.get("environment_id", ""),
            "platform": environment.get("platform", ""),
            "python_version": (
                environment.get("python", {}).get("version", "")
                if isinstance(environment.get("python"), dict)
                else ""
            ),
            "node_packages": len(environment.get("node", []))
            if isinstance(environment.get("node"), list)
            else 0,
        }
        required_python = str(
            payload["environment_manifest"].get("python_version", "")
        )
        required_platform = str(
            payload["environment_manifest"].get("platform", "")
        )
        if required_platform:
            for worker in workers:
                if (
                    worker.get("transport") != "local"
                    and worker.get("platform") != required_platform
                ):
                    worker["ok"] = False
                    worker["platform_error"] = {
                        "required": required_platform,
                        "reported": worker.get("platform", ""),
                    }
        if required_python:
            for worker in workers:
                if worker.get("transport") == "local":
                    continue
                versions = worker.get("python_versions", [])
                if (
                    not isinstance(versions, list)
                    or required_python not in versions
                ):
                    worker["ok"] = False
                    worker["python_version_error"] = {
                        "required": required_python,
                        "reported": versions,
                    }
        payload["ok"] = all(
            bool(item.get("ok")) for item in workers
        )
    else:
        payload["ok"] = all(bool(item.get("ok")) for item in workers)
    return payload


def lan_workers_cleanup(max_age_seconds: float = 86400.0) -> dict[str, object]:
    results: list[dict[str, object]] = [
        {"worker_id": worker_probe("").get("worker_id", ""), **worker_gc(max_age_seconds)}
    ]
    if load_cluster_state() is not None:
        for discovered in discover_workers():
            try:
                value = WorkerClient(discovered).gc(max_age_seconds)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                value = {"ok": False, "error": str(error)}
            value["worker_id"] = discovered.worker_id
            results.append(value)
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "workers": results,
    }
