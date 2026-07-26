from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import glob
import hashlib
import hmac
from importlib import metadata as importlib_metadata
import json
import logging
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import BinaryIO, Mapping, Optional, Sequence

from .gate_execution import (
    LEGACY_RUNTIME_PROFILE,
    SHORT_RUNTIME_PROFILE,
    _run_git,
    auto_agents_state_root,
    dynamic_port_lease,
    exclusive_resource_lease,
    gate_environment,
    install_dependency_links,
    isolated_command,
    short_job_runtime_root,
)
from .models import CommandResult
from .process_supervision import process_group_exists, run_supervised_shell_command


WORKER_PROTOCOL_VERSION = 4
MANAGED_RUNTIME_LAYOUT_FEATURE = "managed_runtime_layout_repair_v1"
LOGGER = logging.getLogger(__name__)

SYSTEM_ENVIRONMENT_DENYLIST = frozenset(
    {
        "HOME",
        "PATH",
        "PWD",
        "OLDPWD",
        "SHELL",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "NODE_PATH",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
        "TERM",
        "SHLVL",
        "_",
        "WSLENV",
        "WSL_DISTRO_NAME",
        "WSL_INTEROP",
    }
)
SYSTEM_ENVIRONMENT_DENY_PREFIXES = (
    "AUTO_AGENTS_",
    "BASH_FUNC_",
    "GIT_",
)


def _safe_identifier(value: object, name: str) -> str:
    normalized = str(value).strip()
    if not normalized or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in normalized
    ):
        raise ValueError(f"invalid {name}: {value}")
    return normalized


def local_worker_config_path() -> Path:
    override = os.environ.get("AUTO_AGENTS_WORKER_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "auto-agents" / "worker.json"


def automatic_worker_slots() -> int:
    cpu_slots = max(1, (os.cpu_count() or 1) // 4)
    memory = _memory_total_bytes()
    reserve = 2 * 1024**3
    memory_slots = (
        max(1, int((memory - reserve) // (2 * 1024**3)))
        if memory > reserve
        else 1
    )
    return max(1, min(4, cpu_slots, memory_slots))


@dataclass(frozen=True)
class WorkerEndpoint:
    worker_id: str
    transport: str = "local"
    max_slots: int = 1
    host: str = ""
    port: int = 0
    tls_fingerprint: str = ""
    enabled: bool = True
    capabilities: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    capability_details: Mapping[str, object] = field(default_factory=dict)
    failure_domain: Mapping[str, object] = field(default_factory=dict)


def enrich_worker_probe(probe: Mapping[str, object]) -> dict[str, object]:
    """Add bounded runtime health and failure-domain evidence to a probe."""
    enriched = dict(probe)
    capabilities = {
        str(item).strip().lower()
        for item in probe.get("capabilities", [])
        if str(item).strip()
    }
    details: dict[str, object] = {
        capability: {"state": "available"}
        for capability in sorted(capabilities)
    }
    if "chrome" in capabilities:
        browser = (
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or ""
        )
        chrome: dict[str, object] = {
            "state": "unknown",
            "path": browser,
            "version": "",
            "artifact_sha256": "",
            "probe_kind": "headless_dump_dom",
        }
        if browser:
            try:
                version = subprocess.run(
                    [browser, "--version"],
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                chrome["version"] = (version.stdout or version.stderr).strip()
                digest = hashlib.sha256()
                with open(browser, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                chrome["artifact_sha256"] = digest.hexdigest()
                with tempfile.TemporaryDirectory(
                    prefix="auto-agents-chrome-probe-"
                ) as data_dir:
                    launch = subprocess.run(
                        [
                            browser,
                            "--headless=new",
                            "--disable-gpu",
                            "--disable-dev-shm-usage",
                            "--no-sandbox",
                            f"--user-data-dir={data_dir}",
                            "--dump-dom",
                            "about:blank",
                        ],
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
                chrome["state"] = (
                    "healthy" if launch.returncode == 0 else "unhealthy"
                )
                if launch.returncode != 0:
                    chrome["error"] = (launch.stderr or launch.stdout)[-2000:]
            except (OSError, subprocess.SubprocessError) as error:
                chrome["state"] = "unhealthy"
                chrome["error"] = str(error)
        else:
            chrome["state"] = "unhealthy"
            chrome["error"] = "no Chrome or Chromium executable resolved on PATH"
        details["chrome"] = chrome

    release = platform.release()
    lowered = release.lower()
    virtualization = (
        "wsl2"
        if "microsoft" in lowered
        else "container"
        if Path("/.dockerenv").exists()
        else "native"
    )
    domain_payload = {
        "platform": str(probe.get("platform", platform.system().lower())),
        "architecture": platform.machine(),
        "kernel_family": release.split("-", 1)[0],
        "virtualization": virtualization,
        "capability_artifacts": {
            name: str(value.get("artifact_sha256", ""))
            for name, value in details.items()
            if isinstance(value, dict) and value.get("artifact_sha256")
        },
    }
    domain_id = hashlib.sha256(
        json.dumps(domain_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    enriched["capability_details"] = details
    enriched["failure_domain"] = {"id": domain_id, **domain_payload}
    enriched["features"] = sorted(
        {
            *(
                str(item)
                for item in probe.get("features", [])
                if str(item).strip()
            ),
            "capability_details_v1",
            "failure_domain_v1",
            "managed_capability_repair_v2",
            MANAGED_RUNTIME_LAYOUT_FEATURE,
        }
    )
    return enriched

@dataclass(frozen=True)
class WorkerEnvironment:
    environment_id: str
    links: Mapping[str, str] = field(default_factory=dict)
    executables: Mapping[str, str] = field(default_factory=dict)
    lock_hashes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalWorkerConfig:
    worker_id: str
    managed_root: Path
    max_slots: int
    environments: Mapping[str, WorkerEnvironment]


def load_local_worker_config(path: Optional[Path] = None) -> LocalWorkerConfig:
    config_path = path or local_worker_config_path()
    explicit_config = path is not None or bool(
        os.environ.get("AUTO_AGENTS_WORKER_CONFIG", "").strip()
    )
    if explicit_config and config_path.exists():
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        payload = {}
    root_text = (
        os.environ.get("AUTO_AGENTS_WORKER_ROOT", "").strip()
        or str(payload.get("managed_root", "")).strip()
    )
    if root_text:
        managed_root = Path(root_text).expanduser().resolve()
    else:
        preferred_root = Path.home() / ".local" / "share" / "auto-agents-worker"
        try:
            preferred_root.mkdir(parents=True, exist_ok=True)
            managed_root = preferred_root.resolve()
        except OSError:
            managed_root = (auto_agents_state_root() / "worker").resolve()
    raw_environments = payload.get("environments", {})
    environments: dict[str, WorkerEnvironment] = {}
    if isinstance(raw_environments, dict):
        for environment_id, item in raw_environments.items():
            if not isinstance(item, dict):
                continue
            raw_links = item.get("links", {})
            raw_executables = item.get("executables", {})
            raw_lock_hashes = item.get("lock_hashes", {})
            environments[str(environment_id)] = WorkerEnvironment(
                environment_id=str(environment_id),
                links={
                    str(key): str(value)
                    for key, value in raw_links.items()
                }
                if isinstance(raw_links, dict)
                else {},
                executables={
                    str(key): str(value)
                    for key, value in raw_executables.items()
                }
                if isinstance(raw_executables, dict)
                else {},
                lock_hashes={
                    str(key): str(value)
                    for key, value in raw_lock_hashes.items()
                }
                if isinstance(raw_lock_hashes, dict)
                else {},
            )
    worker_id = str(payload.get("worker_id", "")).strip()
    if not worker_id:
        try:
            from .worker_cluster import load_cluster_state

            cluster = load_cluster_state()
            worker_id = cluster.node_id if cluster is not None else ""
        except RuntimeError:
            worker_id = ""
    if not worker_id:
        worker_id = hashlib.sha256(
            socket.gethostname().encode("utf-8")
        ).hexdigest()[:24]
    slots_text = os.environ.get("AUTO_AGENTS_WORKER_SLOTS", "").strip()
    max_slots = (
        automatic_worker_slots()
        if not slots_text or slots_text.lower() == "auto"
        else max(1, int(slots_text))
    )
    if explicit_config and "max_slots" in payload and not slots_text:
        max_slots = max(1, int(payload.get("max_slots", 1)))
    return LocalWorkerConfig(
        worker_id=_safe_identifier(worker_id, "worker id"),
        managed_root=managed_root,
        max_slots=max_slots,
        environments=environments,
    )


def forwarded_environment(
    source: Mapping[str, str],
    extra_denylist: Sequence[str] = (),
) -> dict[str, str]:
    denied = SYSTEM_ENVIRONMENT_DENYLIST | {
        str(item).strip() for item in extra_denylist if str(item).strip()
    }
    result: dict[str, str] = {}
    for key, value in source.items():
        if key in denied or any(key.startswith(prefix) for prefix in SYSTEM_ENVIRONMENT_DENY_PREFIXES):
            continue
        result[str(key)] = str(value)
    return result


def redact_values(text: str, values: Sequence[str]) -> str:
    result = str(text or "")
    for value in sorted({item for item in values if item}, key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")
    return result


def gate_environment_fingerprint(
    *,
    isolation_mode: str,
    environment_id: str,
    distributed: bool,
    extra_denylist: Sequence[str] = (),
) -> str:
    payload = {
        "isolation_mode": isolation_mode,
        "environment_id": environment_id,
        "distributed": bool(distributed),
        "python": sys.version,
        "environment": sorted(
            forwarded_environment(os.environ, extra_denylist).items()
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    key_path = auto_agents_state_root() / "cache-hmac.key"
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            temporary = key_path.with_name(f".{key_path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(os.urandom(32))
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, key_path)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
        key = key_path.read_bytes()
    except OSError:
        key = os.urandom(32)
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def project_key(project_root: Path) -> str:
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    identity = (
        str((project_root / common.stdout.strip()).resolve())
        if common.returncode == 0
        else str(project_root.resolve())
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _safe_id(value: str, name: str) -> str:
    return _safe_identifier(value, name)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class WorkerSlotLease:
    def __init__(
        self,
        root: Path,
        worker_id: str,
        slots: int,
        required: int,
        *,
        memory_mb: int = 0,
        memory_reserve_mb: int = 0,
        memory_guard: str = "off",
        timeout_seconds: float = 30.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.root = root
        self.worker_id = worker_id
        self.slots = max(1, slots)
        self.required = max(1, min(required, self.slots))
        self.memory_mb = max(0, int(memory_mb))
        self.memory_reserve_mb = max(0, int(memory_reserve_mb))
        self.memory_guard = str(memory_guard).strip().lower() or "off"
        if self.memory_guard not in {"off", "advisory", "required"}:
            raise ValueError(f"unsupported worker memory guard: {memory_guard}")
        if self.memory_guard != "off" and self.memory_mb <= 0:
            raise ValueError(
                "worker memory_mb must be positive when memory_guard is enabled"
            )
        self.timeout_seconds = max(0.0, timeout_seconds)
        self.cancel_event = cancel_event
        self.handles: list[object] = []

    def __enter__(self) -> "WorkerSlotLease":
        slot_root = self.root / "slots" / self.worker_id
        slot_root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        memory_threshold = (
            (self.memory_mb + self.memory_reserve_mb) * 1024**2
            if self.memory_guard != "off"
            else 0
        )
        memory_available = 0
        memory_warning_emitted = False
        while len(self.handles) < self.required:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise RuntimeError("worker slot acquisition was cancelled")
            memory_available = _memory_available_bytes()
            if (
                memory_available > 0
                and memory_threshold > 0
                and memory_available < memory_threshold
            ):
                if self.memory_guard == "advisory":
                    if not memory_warning_emitted:
                        LOGGER.warning(
                            "worker memory is below the command declaration: "
                            "%d MiB available, %d MiB requested plus %d MiB reserve; "
                            "continuing because memory_guard=advisory",
                            memory_available // 1024**2,
                            self.memory_mb,
                            self.memory_reserve_mb,
                        )
                        memory_warning_emitted = True
                elif time.monotonic() >= deadline:
                    raise RuntimeError(
                        "worker memory capacity remained unavailable for "
                        f"{self.timeout_seconds:.1f}s: "
                        f"{memory_available // 1024**2} MiB available, "
                        f"{self.memory_mb} MiB requested plus "
                        f"{self.memory_reserve_mb} MiB safety reserve"
                    )
                else:
                    self._wait(0.5)
                    continue
            allocation_handle = (slot_root / ".allocation.lock").open("a+")
            try:
                try:
                    fcntl.flock(
                        allocation_handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError:
                    pass
                else:
                    for index in range(self.slots):
                        if len(self.handles) >= self.required:
                            break
                        path = slot_root / f"{index}.lock"
                        handle = path.open("a+")
                        try:
                            fcntl.flock(
                                handle.fileno(),
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                        except BlockingIOError:
                            handle.close()
                            continue
                        self.handles.append(handle)
                    if len(self.handles) >= self.required:
                        return self
                    self._release()
            finally:
                try:
                    fcntl.flock(allocation_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    allocation_handle.close()
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "worker slots remained unavailable for "
                    f"{self.timeout_seconds:.1f}s: "
                    f"{self.required} of {self.slots} slots required"
                )
            self._wait(0.2)
        return self

    def _wait(self, seconds: float) -> None:
        remaining = max(
            0.0,
            min(seconds, self.timeout_seconds),
        )
        if self.cancel_event is not None:
            self.cancel_event.wait(timeout=remaining)
        else:
            time.sleep(remaining)

    def _release(self) -> None:
        for handle in self.handles:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self.handles.clear()

    def __exit__(self, _type, _value, _traceback) -> None:
        self._release()


def _memory_available_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _memory_total_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _mirror_path(config: LocalWorkerConfig, key: str) -> Path:
    return config.managed_root / "mirrors" / f"{_safe_id(key, 'project key')}.git"


def _snapshot_ref(snapshot_sha: str) -> str:
    return f"refs/auto-agents/snapshots/{_safe_id(snapshot_sha, 'snapshot sha')}"


def worker_probe(environment_id: str = "") -> dict[str, object]:
    config = load_local_worker_config()
    try:
        config.managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        return {
            "ok": False,
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "auto_agents_version": _auto_agents_version(),
            "worker_implementation_fingerprint": _worker_implementation_fingerprint(),
            "worker_id": config.worker_id,
            "managed_root": str(config.managed_root),
            "max_slots": config.max_slots,
            "error": str(error),
            "capabilities": [],
            "checks": {},
        }
    environment = config.environments.get(environment_id) if environment_id else None
    checks: dict[str, object] = {}
    if environment_id:
        checks["environment_registered"] = environment is not None
    if environment is not None:
        links: dict[str, object] = {}
        for relative, value in environment.links.items():
            path = Path(value).expanduser()
            links[relative] = {
                "path": str(path),
                "exists": path.exists(),
                "directory": path.is_dir(),
            }
        checks["links"] = links
        executables: dict[str, object] = {}
        for name, value in environment.executables.items():
            path = Path(value).expanduser()
            entry: dict[str, object] = {
                "path": str(path),
                "exists": path.exists(),
                "executable": os.access(path, os.X_OK),
            }
            if path.exists() and os.access(path, os.X_OK):
                try:
                    version = subprocess.run(
                        [str(path), "--version"],
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        timeout=15,
                    )
                    entry["version"] = (
                        version.stdout.strip() or version.stderr.strip()
                    )[:500]
                    if name.lower() == "python":
                        freeze = subprocess.run(
                            [str(path), "-m", "pip", "freeze", "--all"],
                            text=True,
                            encoding="utf-8",
                            capture_output=True,
                            timeout=60,
                        )
                        if freeze.returncode == 0:
                            entry["package_fingerprint"] = hashlib.sha256(
                                freeze.stdout.encode("utf-8")
                            ).hexdigest()
                except (OSError, subprocess.TimeoutExpired) as error:
                    entry["version_error"] = str(error)
            executables[name] = entry
        checks["executables"] = executables
        checks["lock_hashes"] = dict(environment.lock_hashes)
    disk = shutil.disk_usage(config.managed_root.parent)
    memory_kib = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                memory_kib = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass
    ok = all(
        bool(item.get("exists")) and bool(item.get("directory"))
        for item in checks.get("links", {}).values()
    ) if isinstance(checks.get("links"), dict) else True
    ok = ok and all(
        bool(item.get("exists")) and bool(item.get("executable"))
        for item in checks.get("executables", {}).values()
    ) if isinstance(checks.get("executables"), dict) else ok
    if environment_id and environment is None:
        ok = False
    capabilities: set[str] = set()
    if environment is not None:
        capabilities.update(name.strip().lower() for name in environment.executables)
        if any(
            relative == ".conda" or relative == ".venv"
            for relative in environment.links
        ):
            capabilities.add("python")
        if any(
            relative == "node_modules" or relative.endswith("/node_modules")
            for relative in environment.links
        ):
            capabilities.add("node")
    for capability, programs in {
        "python": ("python3", "python"),
        "node": ("node",),
        "ffmpeg": ("ffmpeg",),
        "ffprobe": ("ffprobe",),
        "chrome": ("google-chrome", "chromium", "chromium-browser"),
    }.items():
        if any(shutil.which(program) for program in programs):
            capabilities.add(capability)
    runtimes: dict[str, str] = {}
    python_versions: list[str] = []
    for minor in range(9, 15):
        executable = shutil.which(f"python3.{minor}")
        if executable:
            python_versions.append(f"3.{minor}")
    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if current_version not in python_versions:
        python_versions.append(current_version)
    runtime_commands = {
        "python": [sys.executable, "--version"],
        "node": [shutil.which("node") or "", "--version"],
        "ffmpeg": [shutil.which("ffmpeg") or "", "-version"],
        "ffprobe": [shutil.which("ffprobe") or "", "-version"],
        "chrome": [
            shutil.which("google-chrome")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or "",
            "--version",
        ],
    }
    for name, command in runtime_commands.items():
        if not command[0]:
            continue
        try:
            result = subprocess.run(
                command,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            runtimes[name] = (
                result.stdout.strip() or result.stderr.strip()
            ).splitlines()[0][:300]
    return {
        "ok": ok,
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "auto_agents_version": _auto_agents_version(),
        "worker_implementation_fingerprint": _worker_implementation_fingerprint(),
        "worker_id": config.worker_id,
        "managed_root": str(config.managed_root),
        "max_slots": config.max_slots,
        "cpu_count": os.cpu_count() or 1,
        "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        "memory_available_bytes": memory_kib * 1024,
        "disk_free_bytes": disk.free,
        "capabilities": sorted(capabilities),
        "runtimes": runtimes,
        "python_versions": sorted(python_versions),
        "checks": checks,
    }


def _auto_agents_version() -> str:
    try:
        return importlib_metadata.version("auto-agents")
    except importlib_metadata.PackageNotFoundError:
        return "source"


def _worker_implementation_fingerprint() -> str:
    try:
        digest = hashlib.sha256()
        source_root = Path(__file__).resolve().parent
        for name in ("workers.py", "worker_cluster.py", "worker_service.py"):
            digest.update(name.encode("utf-8"))
            digest.update((source_root / name).read_bytes())
        return digest.hexdigest()
    except OSError:
        return ""


def worker_stage(
    *,
    key: str,
    snapshot_sha: str,
    source_ref: str,
    stream: BinaryIO,
) -> dict[str, object]:
    config = load_local_worker_config()
    config.managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    mirror = _mirror_path(config, key)
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr.strip() or "git init --bare failed")
    incoming = config.managed_root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    safe_snapshot = _safe_id(snapshot_sha, "snapshot sha")
    temporary = incoming / (
        f".{safe_snapshot}.{os.getpid()}.{threading.get_ident()}.bundle"
    )
    with temporary.open("wb") as handle:
        shutil.copyfileobj(stream, handle)
    os.chmod(temporary, 0o600)
    target_ref = _snapshot_ref(snapshot_sha)
    try:
        fetch = subprocess.run(
            [
                "git",
                f"--git-dir={mirror}",
                "fetch",
                str(temporary),
                f"{source_ref}:{target_ref}",
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
    finally:
        temporary.unlink(missing_ok=True)
    if fetch.returncode != 0:
        raise RuntimeError(fetch.stderr.strip() or "git fetch bundle failed")
    resolved = subprocess.run(
        ["git", f"--git-dir={mirror}", "rev-parse", target_ref],
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if resolved.returncode != 0 or resolved.stdout.strip() != snapshot_sha:
        subprocess.run(
            ["git", f"--git-dir={mirror}", "update-ref", "-d", target_ref],
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        raise RuntimeError("staged snapshot identity does not match the manifest")
    return {
        "ok": True,
        "worker_id": config.worker_id,
        "snapshot": snapshot_sha,
        "ref": target_ref,
    }


def _worker_sandbox(
    config: LocalWorkerConfig,
    manifest: Mapping[str, object],
) -> tuple[Path, Path, bool]:
    key = _safe_id(str(manifest.get("project_key", "")), "project key")
    snapshot_sha = _safe_id(str(manifest.get("snapshot", "")), "snapshot sha")
    plan_id = _safe_id(str(manifest.get("plan_id", "")), "plan id")
    job_id = _safe_id(str(manifest.get("job_id", "")), "job id")
    lane = str(manifest.get("lane", "")).strip()
    leaf = _safe_id(lane, "lane") if lane else job_id
    mirror = _mirror_path(config, key)
    sandbox = config.managed_root / "sandboxes" / key / plan_id / leaf
    if sandbox.exists():
        return mirror, sandbox, False
    sandbox.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.run(
        [
            "git",
            f"--git-dir={mirror}",
            "worktree",
            "add",
            "--detach",
            str(sandbox),
            _snapshot_ref(snapshot_sha),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "remote git worktree add failed")
    return mirror, sandbox, True


def _environment_links(
    config: LocalWorkerConfig,
    environment_id: str,
) -> dict[str, Path]:
    if not environment_id:
        return {}
    environment = config.environments.get(environment_id)
    if environment is None:
        raise RuntimeError(f"worker environment is not registered: {environment_id}")
    links: dict[str, Path] = {}
    for relative, value in environment.links.items():
        normalized = PurePosixPath(relative.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise RuntimeError(f"unsafe worker environment link: {relative}")
        source = Path(value).expanduser().resolve()
        if not source.is_dir():
            raise RuntimeError(f"worker environment link is missing: {source}")
        links[normalized.as_posix()] = source
    return links


def _frozen_requirement_name(line: str) -> str:
    raw_name = re.split(r"[<>=!~ @]", line.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", raw_name).lower()


def build_environment_manifest(project_root: Path) -> dict[str, object]:
    """Describe a reproducible gate environment without machine-specific paths."""

    project_root = project_root.resolve()
    python_executable = project_root / ".conda" / "bin" / "python"
    python_payload: dict[str, object] = {}
    if python_executable.is_file():
        version = subprocess.run(
            [
                str(python_executable),
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )
        freeze = subprocess.run(
            [str(python_executable), "-m", "pip", "freeze", "--all"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=120,
        )
        if version.returncode == 0 and freeze.returncode == 0:
            requirements = [
                line.strip()
                for line in freeze.stdout.splitlines()
                if line.strip()
                and " @ file:" not in line
                and not line.startswith("-e ")
                # auto-agents is the controller/worker runtime, not a target
                # project dependency. Editable installs can appear as a pinned
                # package in newer pip freeze output, and that private version
                # may not exist on the package index used by a remote worker.
                and _frozen_requirement_name(line) != "auto-agents"
            ]
            python_payload = {
                "version": version.stdout.strip(),
                "requirements": requirements,
            }
    node_packages: list[dict[str, object]] = []
    ignored_directories = {
        ".auto-agents",
        ".conda",
        ".git",
        ".tmp",
        ".tmp-tests",
        "node_modules",
    }
    for directory, names, files in os.walk(project_root):
        names[:] = [name for name in names if name not in ignored_directories]
        if "package-lock.json" not in files or "package.json" not in files:
            continue
        package_root = Path(directory)
        lock_path = package_root / "package-lock.json"
        package_json = package_root / "package.json"
        node_packages.append(
            {
                "root": package_root.relative_to(project_root).as_posix() or ".",
                "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
                "package_sha256": hashlib.sha256(package_json.read_bytes()).hexdigest(),
            }
        )
    node_packages.sort(key=lambda item: str(item["root"]))
    payload: dict[str, object] = {
        "schema_version": 1,
        "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        "python": python_payload,
        "node": node_packages,
    }
    payload["environment_id"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def _python_for_version(version: str) -> str:
    candidates = [f"python{version}", "python3", "python"]
    for candidate in candidates:
        executable = shutil.which(candidate)
        if not executable:
            continue
        process = subprocess.run(
            [
                executable,
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=15,
        )
        if process.returncode == 0 and process.stdout.strip() == version:
            return executable
    raise RuntimeError(f"worker does not provide Python {version}")


def _auto_environment_links(
    config: LocalWorkerConfig,
    manifest: Mapping[str, object],
    sandbox: Path,
) -> dict[str, Path]:
    environment = manifest.get("environment_manifest", {})
    if not isinstance(environment, dict):
        return {}
    expected_platform = str(environment.get("platform", "")).strip()
    local_platform = f"{platform.system().lower()}-{platform.machine().lower()}"
    if expected_platform and expected_platform != local_platform:
        raise RuntimeError(
            "worker platform does not match the controller environment: "
            f"{local_platform} != {expected_platform}"
        )
    environment_id = _safe_id(
        str(environment.get("environment_id", "")),
        "environment id",
    )
    environment_root = config.managed_root / "environments" / environment_id
    lock_path = config.managed_root / "environment-locks" / f"{environment_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        complete = environment_root / "complete.json"
        if not complete.is_file():
            if environment_root.exists():
                shutil.rmtree(environment_root)
            temporary = environment_root.with_name(
                f".{environment_root.name}.{os.getpid()}.build"
            )
            shutil.rmtree(temporary, ignore_errors=True)
            temporary.mkdir(parents=True, exist_ok=True)
            try:
                python_payload = environment.get("python", {})
                if isinstance(python_payload, dict) and python_payload.get("version"):
                    version = str(python_payload["version"])
                    executable = _python_for_version(version)
                    process = subprocess.run(
                        [executable, "-m", "venv", str(temporary / ".conda")],
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        timeout=300,
                    )
                    if process.returncode != 0:
                        raise RuntimeError(
                            process.stderr.strip()
                            or f"failed to create Python {version} environment"
                        )
                    requirements = python_payload.get("requirements", [])
                    if isinstance(requirements, list) and requirements:
                        requirements_path = temporary / "requirements.freeze.txt"
                        requirements_path.write_text(
                            "\n".join(str(item) for item in requirements) + "\n",
                            encoding="utf-8",
                        )
                        install = subprocess.run(
                            [
                                str(temporary / ".conda" / "bin" / "python"),
                                "-m",
                                "pip",
                                "install",
                                "-r",
                                str(requirements_path),
                            ],
                            text=True,
                            encoding="utf-8",
                            capture_output=True,
                            timeout=1800,
                        )
                        if install.returncode != 0:
                            raise RuntimeError(
                                install.stderr.strip()
                                or "failed to install frozen Python dependencies"
                            )
                raw_node = environment.get("node", [])
                if isinstance(raw_node, list):
                    for item in raw_node:
                        if not isinstance(item, dict):
                            continue
                        root_text = str(item.get("root", "."))
                        root = PurePosixPath(root_text)
                        if root.is_absolute() or ".." in root.parts:
                            raise RuntimeError(
                                f"unsafe Node package root: {root_text}"
                            )
                        source_root = sandbox if root_text == "." else sandbox / root_text
                        node_root = temporary / "node" / hashlib.sha256(
                            root_text.encode("utf-8")
                        ).hexdigest()[:16]
                        node_root.mkdir(parents=True, exist_ok=True)
                        for name in ("package.json", "package-lock.json"):
                            source = source_root / name
                            if not source.is_file():
                                raise RuntimeError(
                                    f"worker snapshot is missing {root_text}/{name}"
                                )
                            shutil.copy2(source, node_root / name)
                        npm = shutil.which("npm")
                        if not npm:
                            raise RuntimeError("worker does not provide npm")
                        install = subprocess.run(
                            [npm, "ci"],
                            cwd=str(node_root),
                            text=True,
                            encoding="utf-8",
                            capture_output=True,
                            timeout=1800,
                            env={
                                **os.environ,
                                "npm_config_cache": str(
                                    config.managed_root / "package-cache" / "npm"
                                ),
                            },
                        )
                        if install.returncode != 0:
                            raise RuntimeError(
                                install.stderr.strip()
                                or f"npm ci failed for {root_text}"
                            )
                (temporary / "complete.json").write_text(
                    json.dumps(environment, sort_keys=True, indent=2),
                    encoding="utf-8",
                )
                environment_root.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(temporary, environment_root)
                except OSError:
                    if not complete.is_file():
                        raise
                    shutil.rmtree(temporary, ignore_errors=True)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    links: dict[str, Path] = {}
    python_environment = environment_root / ".conda"
    if python_environment.is_dir():
        links[".conda"] = python_environment
    raw_node = environment.get("node", [])
    if isinstance(raw_node, list):
        for item in raw_node:
            if not isinstance(item, dict):
                continue
            root_text = str(item.get("root", "."))
            node_root = environment_root / "node" / hashlib.sha256(
                root_text.encode("utf-8")
            ).hexdigest()[:16] / "node_modules"
            relative = (
                "node_modules"
                if root_text == "."
                else f"{root_text.rstrip('/')}/node_modules"
            )
            if node_root.is_dir():
                links[relative] = node_root
    return links


def _worker_mutations(sandbox: Path, dependency_links: Sequence[str]) -> list[str]:
    process = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=str(sandbox),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        return ["<git-status-failed>"]
    paths: list[str] = []
    for line in process.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path and not any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for prefix in dependency_links
        ):
            paths.append(path)
    return paths


def _artifact_files(
    sandbox: Path,
    patterns: Sequence[str],
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, Path]:
    matches: dict[str, Path] = {}
    for raw_pattern in patterns:
        pattern = str(raw_pattern).replace("\\", "/").strip()
        parsed = PurePosixPath(pattern)
        if (
            not pattern
            or parsed.is_absolute()
            or ".." in parsed.parts
            or pattern == ".git"
            or pattern.startswith(".git/")
        ):
            raise RuntimeError(f"unsafe artifact glob: {raw_pattern}")
        for value in glob.glob(str(sandbox / pattern), recursive=True):
            path = Path(value)
            if path.is_file() and not path.is_symlink():
                matches[path.relative_to(sandbox).as_posix()] = path
    if len(matches) > max_files:
        raise RuntimeError(f"artifact file limit exceeded: {len(matches)} > {max_files}")
    total = sum(path.stat().st_size for path in matches.values())
    if total > max_bytes:
        raise RuntimeError(f"artifact byte limit exceeded: {total} > {max_bytes}")
    return matches


def _create_artifact_archive(
    config: LocalWorkerConfig,
    sandbox: Path,
    job_id: str,
    patterns: Sequence[str],
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[Path, dict[str, str]]:
    files = _artifact_files(
        sandbox,
        patterns,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    archive = config.managed_root / "artifacts" / f"{job_id}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    with tarfile.open(archive, "w:gz") as handle:
        for relative, path in sorted(files.items()):
            handle.add(path, arcname=relative, recursive=False)
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    os.chmod(archive, 0o600)
    return archive, hashes


def worker_execute(
    manifest: Mapping[str, object],
    *,
    event_stream=sys.stdout,
    cancel_event: Optional[threading.Event] = None,
) -> CommandResult:
    config = load_local_worker_config()
    config.managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_id = _safe_id(str(manifest.get("job_id", "")), "job id")
    command = str(manifest.get("command", ""))
    try:
        protocol_version = int(manifest.get("protocol_version", 0) or 0)
    except (TypeError, ValueError):
        protocol_version = 0
    if protocol_version != WORKER_PROTOCOL_VERSION:
        return CommandResult(
            command=command,
            ok=False,
            returncode=125,
            stderr=(
                "unsupported worker protocol version: "
                f"{manifest.get('protocol_version', 0)}"
            ),
            termination_reason="infrastructure_error",
            job_id=job_id,
            worker_id=config.worker_id,
            backend="lan-worker",
            infrastructure_error=True,
        )
    environment_id = str(manifest.get("environment_id", "")).strip()
    forwarded = {
        str(key): str(value)
        for key, value in dict(manifest.get("environment", {})).items()
    } if isinstance(manifest.get("environment"), dict) else {}
    registry_path = config.managed_root / "jobs" / f"{job_id}.json"
    mirror: Optional[Path] = None
    sandbox: Optional[Path] = None
    lane = str(manifest.get("lane", "")).strip()
    keep_sandbox = bool(lane)
    try:
        declared_slots = max(0, int(manifest.get("cpu_slots", 0) or 0))
        memory_mb = max(0, int(manifest.get("memory_mb", 0) or 0))
        memory_reserve_mb = max(
            0, int(manifest.get("memory_reserve_mb", 0) or 0)
        )
    except (TypeError, ValueError):
        declared_slots = 0
        memory_mb = 0
        memory_reserve_mb = 0
    required_slots = declared_slots or (
        2 if str(manifest.get("resource_class", "")) == "heavy" else 1
    )
    memory_guard = str(manifest.get("memory_guard", "off")).strip().lower()
    if memory_guard not in {"off", "advisory", "required"}:
        memory_guard = "off"
    base_record: dict[str, object] = {
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "job_id": job_id,
        "worker_id": config.worker_id,
        "plan_id": str(manifest.get("plan_id", "")),
        "state": "staging",
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "updated_at": time.time(),
    }
    _write_json_atomic(registry_path, base_record)
    try:
        with WorkerSlotLease(
            config.managed_root,
            config.worker_id,
            config.max_slots,
            required_slots,
            memory_mb=memory_mb,
            memory_reserve_mb=memory_reserve_mb,
            memory_guard=memory_guard,
            cancel_event=cancel_event,
        ):
            mirror, sandbox, _created = _worker_sandbox(config, manifest)
            if isinstance(manifest.get("environment_manifest"), dict):
                links = _auto_environment_links(config, manifest, sandbox)
            else:
                links = _environment_links(config, environment_id)
            install_dependency_links(sandbox, links)
            runtime_profile = str(
                manifest.get("runtime_profile", SHORT_RUNTIME_PROFILE)
            ).strip()
            if runtime_profile not in {
                SHORT_RUNTIME_PROFILE,
                LEGACY_RUNTIME_PROFILE,
            }:
                raise ValueError(
                    f"unsupported gate runtime profile: {runtime_profile}"
                )
            runtime_root = (
                short_job_runtime_root(job_id)
                if runtime_profile == SHORT_RUNTIME_PROFILE
                else config.managed_root / "runtime" / job_id
            )
            record = {
                **base_record,
                "state": "accepted",
                "sandbox": str(sandbox),
                "lane": lane,
                "updated_at": time.time(),
            }
            _write_json_atomic(registry_path, record)
            try:
                print(
                    json.dumps(
                        {
                            "type": "accepted",
                            "job_id": job_id,
                            "worker_id": config.worker_id,
                        }
                    ),
                    file=event_stream,
                    flush=True,
                )
            except BrokenPipeError:
                pass

            def on_start(pid: int, pgid: int) -> None:
                record.update(
                    {
                        "state": "running",
                        "pid": pid,
                        "pgid": pgid,
                        "updated_at": time.time(),
                    }
                )
                _write_json_atomic(registry_path, record)

            def progress(event: str, elapsed: float) -> None:
                record.update(
                    {
                        "state": "running",
                        "last_event": event,
                        "elapsed_seconds": elapsed,
                        "updated_at": time.time(),
                    }
                )
                _write_json_atomic(registry_path, record)
                try:
                    print(
                        json.dumps(
                            {
                                "type": "heartbeat",
                                "job_id": job_id,
                                "elapsed_seconds": elapsed,
                            }
                        ),
                        file=event_stream,
                        flush=True,
                    )
                except BrokenPipeError:
                    pass

            with exclusive_resource_lease(
                [
                    str(item)
                    for item in manifest.get("exclusive_resources", [])
                    if str(item).startswith("host:")
                ],
                worker_id=config.worker_id,
            ):
                raw_dynamic_ports = manifest.get("dynamic_ports", [])
                if not isinstance(raw_dynamic_ports, list):
                    raise ValueError("dynamic_ports must be a list")
                with dynamic_port_lease(
                    [str(item) for item in raw_dynamic_ports]
                ) as dynamic_ports:
                    record["dynamic_ports"] = dict(dynamic_ports)
                    _write_json_atomic(registry_path, record)
                    env = gate_environment(
                        sandbox,
                        job_id=job_id,
                        base={**os.environ, **forwarded},
                        runtime_root=runtime_root,
                        runtime_profile=runtime_profile,
                        dynamic_ports=dynamic_ports,
                    )
                    process = run_supervised_shell_command(
                        isolated_command(command),
                        cwd=sandbox,
                        env=env,
                        timeout_seconds=float(
                            manifest.get("timeout_seconds", 7200)
                        ),
                        adaptive_timeout_enabled=bool(
                            manifest.get("adaptive_timeout_enabled", True)
                        ),
                        idle_timeout_seconds=float(
                            manifest.get("idle_timeout_seconds", 900)
                        ),
                        heartbeat_seconds=15.0,
                        progress=progress,
                        on_start=on_start,
                        kind="gate-worker",
                        cancel_event=cancel_event,
                    )
            mutations = _worker_mutations(sandbox, list(links))
            stderr = redact_values(process.stderr, list(forwarded.values()))
            stdout = redact_values(process.stdout, list(forwarded.values()))
            ok = process.returncode == 0 and not process.termination_reason
            returncode = process.returncode
            artifact_hashes: dict[str, str] = {}
            artifact_archive = ""
            if ok:
                archive, artifact_hashes = _create_artifact_archive(
                    config,
                    sandbox,
                    job_id,
                    [
                        str(item)
                        for item in manifest.get("artifact_globs", [])
                    ],
                    max_files=int(manifest.get("artifact_max_files", 2000)),
                    max_bytes=int(
                        manifest.get("artifact_max_bytes", 256 * 1024 * 1024)
                    ),
                )
                artifact_archive = str(archive)
            result = CommandResult(
                command=command,
                ok=ok,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=process.duration_seconds,
                termination_reason=process.termination_reason,
                timeout_seconds=process.timeout_seconds,
                cleanup_incomplete=process.cleanup_incomplete,
                last_activity_seconds=process.last_activity_seconds,
                activity_kind=process.activity_kind,
                process_snapshot=process.process_snapshot,
                job_id=job_id,
                worker_id=config.worker_id,
                backend="lan-worker",
                mutation_paths=mutations,
                artifacts=artifact_hashes,
            )
            record.update(
                {
                    "state": "terminal",
                    "result": asdict(result),
                    "artifact_archive": artifact_archive,
                    "updated_at": time.time(),
                }
            )
            _write_json_atomic(registry_path, record)
            try:
                print(
                    json.dumps({"type": "result", "result": asdict(result)}),
                    file=event_stream,
                    flush=True,
                )
            except BrokenPipeError:
                pass
            return result
    except (OSError, RuntimeError, ValueError) as error:
        result = CommandResult(
            command=command,
            ok=False,
            returncode=125,
            stderr=redact_values(str(error), list(forwarded.values())),
            termination_reason="infrastructure_error",
            job_id=job_id,
            worker_id=config.worker_id,
            backend="lan-worker",
            infrastructure_error=True,
        )
        base_record.update(
            {
                "state": "terminal",
                "result": asdict(result),
                "updated_at": time.time(),
            }
        )
        _write_json_atomic(registry_path, base_record)
        try:
            print(
                json.dumps({"type": "result", "result": asdict(result)}),
                file=event_stream,
                flush=True,
            )
        except BrokenPipeError:
            pass
        return result
    finally:
        if sandbox is not None and mirror is not None and not keep_sandbox:
            subprocess.run(
                [
                    "git",
                    f"--git-dir={mirror}",
                    "worktree",
                    "remove",
                    "--force",
                    str(sandbox),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
        runtime_path = locals().get("runtime_root")
        if isinstance(runtime_path, Path):
            shutil.rmtree(runtime_path, ignore_errors=True)
        shutil.rmtree(config.managed_root / "runtime" / job_id, ignore_errors=True)


def worker_query(job_id: str) -> dict[str, object]:
    config = load_local_worker_config()
    return _read_json(
        config.managed_root / "jobs" / f"{_safe_id(job_id, 'job id')}.json"
    )


def worker_cancel(job_id: str, grace_seconds: float = 5.0) -> dict[str, object]:
    config = load_local_worker_config()
    path = config.managed_root / "jobs" / f"{_safe_id(job_id, 'job id')}.json"
    record = _read_json(path)
    pgid = int(record.get("pgid", 0) or 0)
    if record.get("state") == "terminal":
        return {"ok": True, "job_id": job_id, "stopped": True}
    if pgid <= 0:
        record.update(
            {
                "job_id": job_id,
                "state": "cancellation_requested",
                "cancel_requested": True,
                "updated_at": time.time(),
            }
        )
        _write_json_atomic(path, record)
        return {"ok": True, "job_id": job_id, "stopped": False}
    if not process_group_exists(pgid):
        return {"ok": True, "job_id": job_id, "stopped": True}
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while process_group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    stopped = not process_group_exists(pgid)
    record.update(
        {
            "state": "cancelled" if stopped else "cleanup_incomplete",
            "updated_at": time.time(),
        }
    )
    _write_json_atomic(path, record)
    return {"ok": stopped, "job_id": job_id, "stopped": stopped}


def worker_artifacts(job_id: str, stream: BinaryIO) -> None:
    record = worker_query(job_id)
    archive_text = str(record.get("artifact_archive", "")).strip()
    if not archive_text:
        return
    archive = Path(archive_text)
    if archive.is_file():
        with archive.open("rb") as handle:
            shutil.copyfileobj(handle, stream)


def worker_cleanup_plan(key: str, plan_id: str) -> dict[str, object]:
    config = load_local_worker_config()
    key = _safe_id(key, "project key")
    plan_id = _safe_id(plan_id, "plan id")
    mirror = _mirror_path(config, key)
    plan_root = config.managed_root / "sandboxes" / key / plan_id
    if plan_root.exists() and mirror.exists():
        for sandbox in sorted(plan_root.iterdir()):
            subprocess.run(
                [
                    "git",
                    f"--git-dir={mirror}",
                    "worktree",
                    "remove",
                    "--force",
                    str(sandbox),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
        shutil.rmtree(plan_root, ignore_errors=True)
    return {"ok": True, "plan_id": plan_id}


def worker_gc(max_age_seconds: float = 86400.0) -> dict[str, object]:
    config = load_local_worker_config()
    now = time.time()
    removed_jobs = 0
    jobs_root = config.managed_root / "jobs"
    if jobs_root.exists():
        for path in jobs_root.glob("*.json"):
            record = _read_json(path)
            updated = float(record.get("updated_at", 0.0) or 0.0)
            pgid = int(record.get("pgid", 0) or 0)
            if now - updated < max_age_seconds or (
                pgid > 0 and process_group_exists(pgid)
            ):
                continue
            artifact = Path(str(record.get("artifact_archive", "")))
            artifact.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            removed_jobs += 1
    return {"ok": True, "removed_jobs": removed_jobs}


def command_result_from_dict(payload: Mapping[str, object]) -> CommandResult:
    fields = CommandResult.__dataclass_fields__
    return CommandResult(
        **{
            name: payload[name]
            for name in fields
            if name in payload
        }
    )
