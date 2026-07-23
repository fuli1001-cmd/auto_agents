from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import glob
import hashlib
import hmac
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import BinaryIO, Iterator, Mapping, Optional, Sequence

from .gate_execution import (
    _run_git,
    auto_agents_state_root,
    exclusive_resource_lease,
    gate_environment,
    install_dependency_links,
    isolated_command,
)
from .models import CommandResult
from .process_supervision import process_group_exists, run_supervised_shell_command


WORKER_PROTOCOL_VERSION = 1

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


def controller_workers_config_path() -> Path:
    override = os.environ.get("AUTO_AGENTS_WORKERS_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "auto-agents" / "workers.json"


def local_worker_config_path() -> Path:
    override = os.environ.get("AUTO_AGENTS_WORKER_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "auto-agents" / "worker.json"


@dataclass(frozen=True)
class WorkerEndpoint:
    worker_id: str
    transport: str = "local"
    max_slots: int = 1
    ssh_host: str = ""
    command: str = "auto-agents"
    enabled: bool = True
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "WorkerEndpoint":
        worker_id = _safe_identifier(data.get("id", ""), "worker id")
        raw_capabilities = data.get("capabilities", [])
        if not isinstance(raw_capabilities, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in raw_capabilities
        ):
            raise ValueError(
                f"worker capabilities must be a list of non-empty strings: {worker_id}"
            )
        return cls(
            worker_id=worker_id,
            transport=str(data.get("transport", "local")).strip().lower(),
            max_slots=max(1, int(data.get("max_slots", 1))),
            ssh_host=str(data.get("ssh_host", "")).strip(),
            command=str(data.get("command", "auto-agents")).strip() or "auto-agents",
            enabled=bool(data.get("enabled", True)),
            capabilities=tuple(
                sorted(
                    {
                        str(item).strip().lower()
                        for item in raw_capabilities
                        if str(item).strip()
                    }
                )
            ),
        )


@dataclass(frozen=True)
class WorkerPool:
    name: str
    workers: tuple[WorkerEndpoint, ...]


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


def load_worker_pool(name: str, path: Optional[Path] = None) -> WorkerPool:
    config_path = path or controller_workers_config_path()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    pools = payload.get("pools", {})
    if not isinstance(pools, dict) or name not in pools:
        raise ValueError(f"worker pool not found: {name}")
    pool_payload = pools[name]
    if not isinstance(pool_payload, dict):
        raise ValueError(f"worker pool must be an object: {name}")
    raw_workers = pool_payload.get("workers", [])
    if not isinstance(raw_workers, list):
        raise ValueError(f"worker pool workers must be a list: {name}")
    workers = tuple(
        worker
        for item in raw_workers
        if isinstance(item, dict)
        for worker in [WorkerEndpoint.from_dict(item)]
        if worker.enabled and worker.worker_id
    )
    if not workers:
        raise ValueError(f"worker pool has no enabled workers: {name}")
    if len({worker.worker_id for worker in workers}) != len(workers):
        raise ValueError(f"worker pool contains duplicate worker ids: {name}")
    for worker in workers:
        if worker.transport not in {"local", "ssh"}:
            raise ValueError(
                f"unsupported worker transport {worker.transport}: {worker.worker_id}"
            )
        if worker.transport == "ssh" and not worker.ssh_host:
            raise ValueError(f"ssh worker is missing ssh_host: {worker.worker_id}")
    return WorkerPool(name=name, workers=workers)


def load_local_worker_config(path: Optional[Path] = None) -> LocalWorkerConfig:
    config_path = path or local_worker_config_path()
    if config_path.exists():
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        payload = {}
    root_text = str(payload.get("managed_root", "")).strip()
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
    return LocalWorkerConfig(
        worker_id=_safe_identifier(
            payload.get("worker_id", "local") or "local",
            "worker id",
        ),
        managed_root=managed_root,
        max_slots=max(1, int(payload.get("max_slots", 1))),
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
    def __init__(self, root: Path, worker_id: str, slots: int, required: int) -> None:
        self.root = root
        self.worker_id = worker_id
        self.slots = max(1, slots)
        self.required = max(1, min(required, self.slots))
        self.handles: list[object] = []

    def __enter__(self) -> "WorkerSlotLease":
        slot_root = self.root / "slots" / self.worker_id
        slot_root.mkdir(parents=True, exist_ok=True)
        while len(self.handles) < self.required:
            memory_available = _memory_available_bytes()
            required_memory = (
                4 * 1024**3 if self.required >= 2 else 2 * 1024**3
            )
            if (
                memory_available > 0
                and memory_available < required_memory + 2 * 1024**3
            ):
                time.sleep(0.5)
                continue
            for index in range(self.slots):
                if len(self.handles) >= self.required:
                    break
                path = slot_root / f"{index}.lock"
                handle = path.open("a+")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                self.handles.append(handle)
            if len(self.handles) < self.required:
                self._release()
                time.sleep(0.2)
        return self

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
        "chrome": ("google-chrome", "chromium", "chromium-browser"),
    }.items():
        if any(shutil.which(program) for program in programs):
            capabilities.add(capability)
    return {
        "ok": ok,
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "auto_agents_version": _auto_agents_version(),
        "worker_implementation_fingerprint": _worker_implementation_fingerprint(),
        "worker_id": config.worker_id,
        "managed_root": str(config.managed_root),
        "max_slots": config.max_slots,
        "cpu_count": os.cpu_count() or 1,
        "memory_available_bytes": memory_kib * 1024,
        "disk_free_bytes": disk.free,
        "capabilities": sorted(capabilities),
        "checks": checks,
    }


def _auto_agents_version() -> str:
    try:
        return importlib_metadata.version("auto-agents")
    except importlib_metadata.PackageNotFoundError:
        return "source"


def _worker_implementation_fingerprint() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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
    bundle_path = incoming / f"{_safe_id(snapshot_sha, 'snapshot sha')}.bundle"
    temporary = bundle_path.with_suffix(".bundle.part")
    with temporary.open("wb") as handle:
        shutil.copyfileobj(stream, handle)
    os.chmod(temporary, 0o600)
    os.replace(temporary, bundle_path)
    target_ref = _snapshot_ref(snapshot_sha)
    fetch = subprocess.run(
        [
            "git",
            f"--git-dir={mirror}",
            "fetch",
            str(bundle_path),
            f"{source_ref}:{target_ref}",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    bundle_path.unlink(missing_ok=True)
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
            backend="ssh-isolated",
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
    required_slots = 2 if str(manifest.get("resource_class", "")) == "heavy" else 1
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
        ):
            mirror, sandbox, _created = _worker_sandbox(config, manifest)
            links = _environment_links(config, environment_id)
            install_dependency_links(sandbox, links)
            runtime_root = config.managed_root / "runtime" / job_id
            env = gate_environment(
                sandbox,
                job_id=job_id,
                base={**os.environ, **forwarded},
                runtime_root=runtime_root,
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
                process = run_supervised_shell_command(
                    isolated_command(command),
                    cwd=sandbox,
                    env=env,
                    timeout_seconds=float(manifest.get("timeout_seconds", 7200)),
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
                backend="ssh-isolated",
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
            backend="ssh-isolated",
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
    if pgid <= 0 or not process_group_exists(pgid):
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


def ssh_command(
    endpoint: WorkerEndpoint,
    args: Sequence[str],
    *,
    connect_timeout_seconds: int,
) -> list[str]:
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, connect_timeout_seconds)}",
        endpoint.ssh_host,
        endpoint.command,
        "worker",
        *args,
    ]


def controller_worker_call(
    endpoint: WorkerEndpoint,
    args: Sequence[str],
    *,
    connect_timeout_seconds: int = 10,
    input_bytes: Optional[bytes] = None,
) -> subprocess.CompletedProcess[bytes]:
    if endpoint.transport == "local":
        raise ValueError("controller_worker_call requires an ssh endpoint")
    return subprocess.run(
        ssh_command(
            endpoint,
            args,
            connect_timeout_seconds=connect_timeout_seconds,
        ),
        input=input_bytes,
        capture_output=True,
    )


def controller_workers_doctor(
    pool_name: str,
    *,
    environment_id: str = "",
    project_root: Optional[Path] = None,
) -> dict[str, object]:
    pool = load_worker_pool(pool_name)
    expected_lock_hashes: dict[str, str] = {}
    if project_root is not None:
        for name in (
            "pyproject.toml",
            "poetry.lock",
            "uv.lock",
            "requirements.txt",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "workbench/package-lock.json",
        ):
            path = project_root / name
            if path.is_file():
                expected_lock_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    results: list[dict[str, object]] = []
    for endpoint in pool.workers:
        if endpoint.transport == "local":
            payload = worker_probe(environment_id)
        else:
            probe_args = ["probe"]
            if environment_id:
                probe_args.extend(["--environment-id", environment_id])
            try:
                process = controller_worker_call(
                    endpoint,
                    probe_args,
                )
                payload = json.loads(process.stdout.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                payload = {
                    "ok": False,
                    "error": str(error),
                }
        if not isinstance(payload, dict):
            payload = {
                "ok": False,
                "error": "worker probe returned a non-object payload",
            }
        payload["configured_id"] = endpoint.worker_id
        payload["transport"] = endpoint.transport
        if payload.get("worker_id") != endpoint.worker_id:
            payload["ok"] = False
            payload["worker_id_error"] = {
                "configured": endpoint.worker_id,
                "reported": payload.get("worker_id"),
            }
        if payload.get("protocol_version") != WORKER_PROTOCOL_VERSION:
            payload["ok"] = False
            payload["protocol_error"] = {
                "expected": WORKER_PROTOCOL_VERSION,
                "reported": payload.get("protocol_version"),
            }
        payload["reported_max_slots"] = payload.get("max_slots")
        payload["configured_max_slots"] = endpoint.max_slots
        reported_capabilities = {
            str(item).strip().lower()
            for item in payload.get("capabilities", [])
            if str(item).strip()
        }
        missing_capabilities = sorted(
            set(endpoint.capabilities) - reported_capabilities
        )
        if missing_capabilities:
            payload["ok"] = False
            payload["capability_error"] = {
                "configured": list(endpoint.capabilities),
                "reported": sorted(reported_capabilities),
                "missing": missing_capabilities,
            }
        if (
            payload.get("reported_max_slots") is not None
            and int(payload["reported_max_slots"]) < endpoint.max_slots
        ):
            payload["ok"] = False
            payload["slot_error"] = (
                f"worker exposes {payload['reported_max_slots']} slots but "
                f"pool config requests {endpoint.max_slots}"
            )
        checks = payload.get("checks", {})
        actual_lock_hashes = (
            checks.get("lock_hashes", {})
            if isinstance(checks, dict)
            else {}
        )
        if expected_lock_hashes and actual_lock_hashes != expected_lock_hashes:
            payload["ok"] = False
            payload["lock_hash_error"] = {
                "expected": expected_lock_hashes,
                "actual": actual_lock_hashes,
            }
        results.append(payload)
    environment_fingerprints: dict[str, str] = {}
    for item in results:
        checks = item.get("checks", {})
        executables = checks.get("executables", {}) if isinstance(checks, dict) else {}
        normalized: dict[str, object] = {}
        if isinstance(executables, dict):
            for name, entry in executables.items():
                if not isinstance(entry, dict):
                    continue
                normalized[str(name)] = {
                    "version": entry.get("version", ""),
                    "package_fingerprint": entry.get("package_fingerprint", ""),
                }
        environment_fingerprints[str(item.get("configured_id", ""))] = hashlib.sha256(
            json.dumps(normalized, sort_keys=True).encode("utf-8")
        ).hexdigest()
    if len(set(environment_fingerprints.values())) > 1:
        for item in results:
            item["ok"] = False
            item["environment_mismatch"] = environment_fingerprints
    versions = {
        str(item.get("configured_id", "")): str(
            item.get("auto_agents_version", "")
        )
        for item in results
    }
    if len(set(versions.values())) > 1:
        for item in results:
            item["ok"] = False
            item["auto_agents_version_mismatch"] = versions
    implementation_fingerprints = {
        str(item.get("configured_id", "")): str(
            item.get("worker_implementation_fingerprint", "")
        )
        for item in results
    }
    if len(set(implementation_fingerprints.values())) > 1:
        for item in results:
            item["ok"] = False
            item["worker_implementation_mismatch"] = implementation_fingerprints
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "pool": pool_name,
        "workers": results,
    }


def controller_workers_status(pool_name: str) -> dict[str, object]:
    return controller_workers_doctor(pool_name)


def controller_workers_cleanup(
    pool_name: str,
    *,
    max_age_seconds: float = 86400.0,
) -> dict[str, object]:
    pool = load_worker_pool(pool_name)
    results: list[dict[str, object]] = []
    for endpoint in pool.workers:
        if endpoint.transport == "local":
            payload = worker_gc(max_age_seconds)
        else:
            try:
                process = controller_worker_call(
                    endpoint,
                    ["gc", "--max-age-seconds", str(max_age_seconds)],
                )
                payload = json.loads(process.stdout.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                payload = {"ok": False, "error": str(error)}
        payload["worker_id"] = endpoint.worker_id
        results.append(payload)
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "pool": pool_name,
        "workers": results,
    }
