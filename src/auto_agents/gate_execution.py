from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import threading
import uuid
from typing import Callable, Mapping, Optional, Sequence

from .models import CommandResult, GateConfig
from .process_supervision import run_supervised_shell_command


GateProgressCallback = Callable[[str, str, float], None]


def _run_git(
    project_root: Path,
    *args: str,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        env=dict(env) if env is not None else None,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or process.stdout.strip()
            or f"git {' '.join(args)} failed"
        )
    return process


@dataclass(frozen=True)
class GateSourceSnapshot:
    commit_sha: str
    ref_name: str


class GateSnapshotManager:
    """Capture the exact current filesystem state without touching the user index."""

    def __init__(self, project_root: Path, plan_id: str) -> None:
        self.project_root = project_root.resolve()
        self.plan_id = plan_id
        self.snapshot: Optional[GateSourceSnapshot] = None

    def create(self) -> GateSourceSnapshot:
        git_dir = _run_git(
            self.project_root, "rev-parse", "--git-common-dir"
        ).stdout.strip()
        common_dir = Path(git_dir)
        if not common_dir.is_absolute():
            common_dir = (self.project_root / common_dir).resolve()
        index_dir = common_dir / "auto-agents-gate-indexes"
        index_dir.mkdir(parents=True, exist_ok=True)
        index_path = index_dir / f"{self.plan_id}.index"
        index_path.unlink(missing_ok=True)
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index_path)
        env["GIT_AUTHOR_NAME"] = "auto_agents gate snapshot"
        env["GIT_AUTHOR_EMAIL"] = "auto-agents@localhost"
        env["GIT_COMMITTER_NAME"] = "auto_agents gate snapshot"
        env["GIT_COMMITTER_EMAIL"] = "auto-agents@localhost"
        try:
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=str(self.project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            if head.returncode == 0:
                _run_git(self.project_root, "read-tree", "HEAD", env=env)
            else:
                _run_git(self.project_root, "read-tree", "--empty", env=env)
            _run_git(self.project_root, "add", "-A", "--", ".", env=env)
            tree = _run_git(self.project_root, "write-tree", env=env).stdout.strip()
            commit_args = ["commit-tree", tree, "-m", f"auto_agents gate snapshot {self.plan_id}"]
            if head.returncode == 0 and head.stdout.strip():
                commit_args.extend(["-p", head.stdout.strip()])
            commit = _run_git(
                self.project_root, *commit_args, env=env
            ).stdout.strip()
            ref_name = f"refs/auto-agents/gate-snapshots/{self.plan_id}"
            _run_git(self.project_root, "update-ref", ref_name, commit)
            self.snapshot = GateSourceSnapshot(commit_sha=commit, ref_name=ref_name)
            return self.snapshot
        finally:
            index_path.unlink(missing_ok=True)

    def close(self) -> None:
        if self.snapshot is None:
            return
        try:
            _run_git(self.project_root, "update-ref", "-d", self.snapshot.ref_name)
        except RuntimeError:
            pass
        self.snapshot = None


def _metadata_list(metadata: object, name: str) -> list[str]:
    value = getattr(metadata, name, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _metadata_resource_class(metadata: object) -> str:
    value = str(getattr(metadata, "resource_class", "normal")).strip().lower()
    return "heavy" if value == "heavy" else "normal"


def auto_agents_state_root() -> Path:
    """Return a process-shared writable state directory.

    Sandboxed callers may expose a read-only home directory.  Resource locks
    still need a host-wide location, so prefer explicit/XDG state roots and
    fall back to a namespaced system temporary directory when the conventional
    home location cannot be created.
    """

    candidates: list[Path] = []
    override = os.environ.get("AUTO_AGENTS_STATE_HOME", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state:
        candidates.append(Path(xdg_state).expanduser() / "auto-agents")
    candidates.append(Path.home() / ".local" / "state" / "auto-agents")
    candidates.append(
        Path(tempfile.gettempdir())
        / f"auto-agents-state-{getattr(os, 'getuid', lambda: 0)()}"
    )
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate.resolve()
        except OSError:
            continue
    raise RuntimeError("no writable state directory is available for gate execution")


@contextmanager
def exclusive_resource_lease(
    resources: Sequence[str],
    *,
    worker_id: str,
) -> object:
    handles: list[object] = []
    root = auto_agents_state_root() / "resource-locks"
    root.mkdir(parents=True, exist_ok=True)
    try:
        for resource in sorted(set(resources)):
            scope, _, name = resource.partition(":")
            identity = (
                f"host:{worker_id}:{name}"
                if scope == "host"
                else f"pool:{name}"
            )
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            handle = (root / f"{digest}.lock").open("a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _safe_artifact_pattern(pattern: str) -> str:
    normalized = pattern.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or normalized.startswith(".git/")
        or normalized == ".git"
    ):
        raise ValueError(f"unsafe gate artifact glob: {pattern}")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def isolated_command(command: str) -> str:
    """Disable runner caches that write through shared dependency links."""
    if "vitest" in command and "--no-cache" not in command and "--cache" not in command:
        return f"{command} --no-cache"
    return command


def gate_environment(
    sandbox: Path,
    *,
    job_id: str,
    base: Optional[Mapping[str, str]] = None,
    runtime_root: Optional[Path] = None,
) -> dict[str, str]:
    env = dict(base or os.environ)
    runtime_root = runtime_root or (sandbox / ".auto-agents-gate-runtime")
    temp_root = runtime_root / "tmp"
    cache_root = runtime_root / "cache"
    temp_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "AUTO_AGENTS_GATE_JOB_ID": job_id,
            "AUTO_AGENTS_GATE_SANDBOX_ROOT": str(sandbox),
            "AUTO_AGENTS_TEST": "True",
            "PYTEST_CURRENT_TEST": "auto_agents_gate_run",
            "TESTING": "True",
            "TMPDIR": str(temp_root),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
            "XDG_CACHE_HOME": str(cache_root / "xdg"),
            "XDG_STATE_HOME": str(runtime_root / "state"),
            "PYTHONPYCACHEPREFIX": str(cache_root / "pycache"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "npm_config_cache": str(cache_root / "npm"),
        }
    )
    return env


def discover_dependency_links(project_root: Path) -> dict[str, Path]:
    candidates: set[Path] = {
        Path(".conda"),
        Path(".venv"),
        Path("node_modules"),
    }
    for lock_name in (
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "npm-shrinkwrap.json",
    ):
        for lock_path in project_root.glob(f"*/{lock_name}"):
            candidates.add(lock_path.parent.relative_to(project_root) / "node_modules")
        if (project_root / lock_name).exists():
            candidates.add(Path("node_modules"))
    links: dict[str, Path] = {}
    for relative in sorted(candidates, key=lambda item: item.as_posix()):
        source = (project_root / relative).resolve()
        if source.exists() and source.is_dir():
            links[relative.as_posix()] = source
    return links


def install_dependency_links(sandbox: Path, links: Mapping[str, Path]) -> None:
    for relative, source in links.items():
        target = sandbox / relative
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=True)


class LocalGatePlanExecutor:
    """Run one gate plan in snapshot-backed, per-command worktrees."""

    def __init__(
        self,
        project_root: Path,
        gate_config: GateConfig,
        metadata: Mapping[str, object],
        *,
        run_id: str = "",
        dependency_links: Optional[Mapping[str, Path]] = None,
        worker_id: str = "local",
    ) -> None:
        self.project_root = project_root.resolve()
        self.gate_config = gate_config
        self.metadata = dict(metadata)
        self.plan_id = f"{run_id or 'gate'}-{uuid.uuid4().hex[:12]}"
        configured_root = gate_config.isolation.worktree_root.strip()
        self.worktree_root = (
            Path(configured_root).expanduser().resolve()
            if configured_root
            else (
                self.project_root.parent
                / f".{self.project_root.name}-auto-agents-gate-worktrees"
            ).resolve()
        )
        self.snapshot_manager = GateSnapshotManager(self.project_root, self.plan_id)
        self.snapshot: Optional[GateSourceSnapshot] = None
        self.dependency_links = dict(
            dependency_links
            if dependency_links is not None
            else discover_dependency_links(self.project_root)
        )
        self.worker_id = worker_id
        self._shared_sandboxes: dict[str, Path] = {}
        self._published_hashes: dict[str, str] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> "LocalGatePlanExecutor":
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.snapshot = self.snapshot_manager.create()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def priority(self, command: str) -> tuple[int, str]:
        metadata = self.metadata.get(command)
        return (
            0 if _metadata_resource_class(metadata) == "heavy" else 1,
            command,
        )

    def _sandbox(self, lane: str, job_id: str) -> tuple[Path, bool]:
        if lane:
            with self._lock:
                existing = self._shared_sandboxes.get(lane)
                if existing is not None:
                    return existing, False
        if self.snapshot is None:
            raise RuntimeError("gate executor snapshot has not been created")
        sandbox = self.worktree_root / self.plan_id / (lane or job_id)
        sandbox.parent.mkdir(parents=True, exist_ok=True)
        if sandbox.exists():
            raise RuntimeError(f"gate sandbox already exists: {sandbox}")
        _run_git(
            self.project_root,
            "worktree",
            "add",
            "--detach",
            str(sandbox),
            self.snapshot.commit_sha,
        )
        install_dependency_links(sandbox, self.dependency_links)
        if lane:
            with self._lock:
                self._shared_sandboxes[lane] = sandbox
        return sandbox, True

    def _mutation_paths(self, sandbox: Path) -> list[str]:
        process = _run_git(
            sandbox,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        paths: list[str] = []
        ignored = tuple(
            list(self.dependency_links)
            + [".auto-agents-gate-runtime", ".auto-agents-gate-tmp", ".auto-agents-gate-cache"]
        )
        for line in process.stdout.splitlines():
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path and not any(
                path == prefix or path.startswith(prefix.rstrip("/") + "/")
                for prefix in ignored
            ):
                paths.append(path)
        return paths

    def _publish_artifacts(
        self,
        sandbox: Path,
        command: str,
        job_id: str,
    ) -> dict[str, str]:
        metadata = self.metadata.get(command)
        patterns = [
            _safe_artifact_pattern(item)
            for item in _metadata_list(metadata, "artifact_globs")
        ]
        if not patterns:
            return {}
        matches: dict[str, Path] = {}
        for pattern in patterns:
            for source in sandbox.glob(pattern):
                if not source.is_file() or source.is_symlink():
                    continue
                relative = source.relative_to(sandbox).as_posix()
                matches[relative] = source
        max_files = self.gate_config.isolation.artifact_max_files
        if len(matches) > max_files:
            raise RuntimeError(
                f"gate artifacts exceed file limit {max_files}: {len(matches)}"
            )
        total_bytes = sum(path.stat().st_size for path in matches.values())
        max_bytes = self.gate_config.isolation.artifact_max_bytes
        if total_bytes > max_bytes:
            raise RuntimeError(
                f"gate artifacts exceed byte limit {max_bytes}: {total_bytes}"
            )

        archive_root = (
            self.project_root
            / ".auto-agents"
            / "runs"
            / self.plan_id
            / "gate-artifacts"
            / job_id
        )
        artifacts: dict[str, str] = {}
        for relative, source in sorted(matches.items()):
            digest = _sha256(source)
            with self._lock:
                previous = self._published_hashes.get(relative)
                if previous is not None and previous != digest:
                    raise RuntimeError(
                        f"gate artifact collision for {relative}: {previous} != {digest}"
                    )
                self._published_hashes[relative] = digest
            for destination in (archive_root / relative, self.project_root / relative):
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(
                    f".{destination.name}.{job_id}.tmp"
                )
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
            artifacts[relative] = digest
        return artifacts

    def _publish_diagnostics(self, sandbox: Path, job_id: str) -> None:
        """Preserve orchestrator-owned verification logs produced in isolation."""

        diagnostic_root = sandbox / ".auto-agents" / "failed-verification-logs"
        if not diagnostic_root.is_dir():
            return
        for source in diagnostic_root.rglob("*"):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(sandbox)
            destination = self.project_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{job_id}.tmp")
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)

    def run(
        self,
        command: str,
        *,
        lane: str = "",
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event: Optional[threading.Event] = None,
        progress: Optional[GateProgressCallback] = None,
    ) -> CommandResult:
        job_id = uuid.uuid4().hex
        sandbox: Optional[Path] = None
        cleanup = not bool(lane)
        try:
            sandbox, _created = self._sandbox(lane, job_id)
            runtime_root = self.worktree_root / self.plan_id / ".runtime" / job_id
            env = gate_environment(
                sandbox,
                job_id=job_id,
                runtime_root=runtime_root,
            )
            if progress is not None:
                progress("start", command, 0.0)
            metadata = self.metadata.get(command)
            with exclusive_resource_lease(
                _metadata_list(metadata, "exclusive_resources"),
                worker_id=self.worker_id,
            ):
                process = run_supervised_shell_command(
                    isolated_command(command),
                    cwd=sandbox,
                    env=env,
                    timeout_seconds=timeout_seconds,
                    adaptive_timeout_enabled=adaptive_timeout_enabled,
                    idle_timeout_seconds=idle_timeout_seconds,
                    kind="gate",
                    cancel_event=cancel_event,
                    progress=(
                        (lambda event, elapsed: progress(event, command, elapsed))
                        if progress is not None
                        else None
                    ),
                )
            mutations = self._mutation_paths(sandbox)
            self._publish_diagnostics(sandbox, job_id)
            stderr = process.stderr
            ok = process.returncode == 0 and not process.termination_reason
            returncode = process.returncode
            artifacts: dict[str, str] = {}
            if ok:
                try:
                    artifacts = self._publish_artifacts(
                        sandbox, command, job_id
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    ok = False
                    returncode = returncode or 1
                    stderr = f"{stderr}\nartifact publication failed: {error}".strip()
            result = CommandResult(
                command=command,
                ok=ok,
                returncode=returncode,
                stdout=process.stdout,
                stderr=stderr,
                duration_seconds=process.duration_seconds,
                termination_reason=process.termination_reason,
                timeout_seconds=process.timeout_seconds,
                cleanup_incomplete=process.cleanup_incomplete,
                last_activity_seconds=process.last_activity_seconds,
                activity_kind=process.activity_kind,
                process_snapshot=process.process_snapshot,
                job_id=job_id,
                worker_id=self.worker_id,
                backend="local-isolated",
                mutation_paths=mutations,
                artifacts=artifacts,
            )
            if progress is not None:
                progress("finish", command, result.duration_seconds)
            return result
        except (OSError, RuntimeError, ValueError) as error:
            return CommandResult(
                command=command,
                ok=False,
                returncode=125,
                stderr=str(error),
                termination_reason="infrastructure_error",
                timeout_seconds=float(timeout_seconds),
                job_id=job_id,
                worker_id=self.worker_id,
                backend="local-isolated",
                infrastructure_error=True,
            )
        finally:
            if cleanup and sandbox is not None:
                try:
                    _run_git(
                        self.project_root,
                        "worktree",
                        "remove",
                        "--force",
                        str(sandbox),
                    )
                except RuntimeError:
                    pass

    def close(self) -> None:
        for sandbox in list(self._shared_sandboxes.values()):
            try:
                _run_git(
                    self.project_root,
                    "worktree",
                    "remove",
                    "--force",
                    str(sandbox),
                )
            except RuntimeError:
                pass
        self._shared_sandboxes.clear()
        self.snapshot_manager.close()
        plan_root = self.worktree_root / self.plan_id
        shutil.rmtree(plan_root / ".runtime", ignore_errors=True)
        try:
            plan_root.rmdir()
        except OSError:
            pass
