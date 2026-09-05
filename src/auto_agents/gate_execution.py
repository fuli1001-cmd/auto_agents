from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .models import CommandResult, GateConfig
from .gate_result_cache import GateResultCache
from .gate_timing import GateTimingStore
from .git_ops import (
    normalize_repository_exclusions,
    repository_path_is_excluded,
)
from .process_supervision import run_supervised_shell_command


GateProgressCallback = Callable[[str, str, float], None]
SHORT_RUNTIME_PROFILE = "short_socket_path_v1"
LEGACY_RUNTIME_PROFILE = "legacy_v1"
_SHORT_RUNTIME_SOCKET_BUDGET = 100
_SHORT_RUNTIME_STALE_SECONDS = 24 * 60 * 60
GATE_SNAPSHOT_RUNTIME_PATHS = (
    ".auto-agents-gate-runtime",
    ".auto-agents-gate-tmp",
    ".auto-agents-gate-cache",
)


def short_job_runtime_root(job_id: str, *, create: bool = True) -> Path:
    """Return a short, user-owned per-job root suitable for Unix sockets."""
    normalized = str(job_id).strip()
    if not normalized:
        raise ValueError("gate job id is required for a short runtime")
    if os.name != "posix":
        root = Path(tempfile.gettempdir()) / (
            "auto-agents-gate-" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
        )
    else:
        base = Path("/tmp")
        if not base.is_dir() or not os.access(base, os.W_OK | os.X_OK):
            base = Path(tempfile.gettempdir())
        uid = os.getuid() if hasattr(os, "getuid") else 0
        prefix = f"aag-{uid}-"
        root = base / (prefix + hashlib.sha256(normalized.encode()).hexdigest()[:12])
        worst_socket = root / "t" / ("s" * 64)
        if len(os.fsencode(str(worst_socket))) > _SHORT_RUNTIME_SOCKET_BUDGET:
            raise RuntimeError(
                "short_runtime_root_unavailable: no writable temporary root "
                "satisfies the Unix socket path budget"
            )
        if create:
            now = time.time()
            for candidate in base.glob(f"{prefix}*"):
                marker = candidate / ".auto-agents-runtime.json"
                try:
                    if (
                        marker.is_file()
                        and now - candidate.stat().st_mtime
                        > _SHORT_RUNTIME_STALE_SECONDS
                    ):
                        shutil.rmtree(candidate, ignore_errors=True)
                except OSError:
                    continue
    if create:
        if root.is_symlink():
            raise RuntimeError("short runtime root must not be a symbolic link")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            root.chmod(0o700)
        (root / ".auto-agents-runtime.json").write_text(
            json.dumps({"job_id": normalized}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return root


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
    tree_sha: str
    ref_name: str


class GateSnapshotManager:
    """Capture the exact current filesystem state without touching the user index."""

    def __init__(
        self,
        project_root: Path,
        plan_id: str,
        *,
        excluded_paths: Sequence[str] = (),
    ) -> None:
        self.project_root = project_root.resolve()
        self.plan_id = plan_id
        self.excluded_paths = normalize_repository_exclusions(excluded_paths)
        self.snapshot: Optional[GateSourceSnapshot] = None

    def _force_remove_excluded_index_entries(
        self,
        env: Mapping[str, str],
    ) -> None:
        if not self.excluded_paths:
            return
        listed = _run_git(
            self.project_root,
            "ls-files",
            "-z",
            "--",
            *(
                f":(top,literal){path}"
                for path in self.excluded_paths
            ),
            env=env,
        )
        entries = [path for path in listed.stdout.split("\0") if path]
        for offset in range(0, len(entries), 256):
            _run_git(
                self.project_root,
                "update-index",
                "--force-remove",
                "--",
                *entries[offset : offset + 256],
                env=env,
            )

    def _negative_exclusion_pathspecs(
        self,
        env: Mapping[str, str],
    ) -> tuple[str, ...]:
        """Exclude unignored paths without explicitly naming ignored ones.

        Git rejects an ignored directory when it is also supplied as a
        negative pathspec to git add. Ignored paths are already omitted by the
        positive "." pathspec, while the index cleanup below removes any
        tracked entries. Keep literal negative pathspecs for the remaining
        exclusions so dependency links and runtime surfaces are never staged.
        """

        pathspecs: list[str] = []
        for path in self.excluded_paths:
            ignored = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "--no-index",
                    "--quiet",
                    "--",
                    path,
                ],
                cwd=str(self.project_root),
                env=dict(env),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            if ignored.returncode == 0:
                continue
            if ignored.returncode != 1:
                raise RuntimeError(
                    ignored.stderr.strip()
                    or ignored.stdout.strip()
                    or f"git check-ignore failed for excluded path: {path}"
                )
            pathspecs.append(f":(top,exclude,literal){path}")
        return tuple(pathspecs)

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
            _run_git(
                self.project_root,
                "add",
                "-A",
                "--",
                ".",
                *self._negative_exclusion_pathspecs(env),
                env=env,
            )
            self._force_remove_excluded_index_entries(env)
            tree = _run_git(self.project_root, "write-tree", env=env).stdout.strip()
            commit_args = ["commit-tree", tree, "-m", f"auto_agents gate snapshot {self.plan_id}"]
            if head.returncode == 0 and head.stdout.strip():
                commit_args.extend(["-p", head.stdout.strip()])
            commit = _run_git(
                self.project_root, *commit_args, env=env
            ).stdout.strip()
            ref_name = f"refs/auto-agents/gate-snapshots/{self.plan_id}"
            _run_git(self.project_root, "update-ref", ref_name, commit)
            self.snapshot = GateSourceSnapshot(
                commit_sha=commit,
                tree_sha=tree,
                ref_name=ref_name,
            )
            return self.snapshot
        finally:
            index_path.unlink(missing_ok=True)

    def create_from_commit_paths(
        self,
        *,
        base_ref: str,
        source_ref: str,
        paths: Sequence[str],
    ) -> GateSourceSnapshot:
        """Create a snapshot by overlaying selected source paths on a base tree."""

        try:
            requested_paths = sorted(normalize_repository_exclusions(paths))
        except ValueError as error:
            raise ValueError(
                "snapshot paths must be safe repository-relative paths"
            ) from error
        if not requested_paths:
            raise ValueError("snapshot paths must be safe repository-relative paths")
        normalized_paths = [
            path
            for path in requested_paths
            if not repository_path_is_excluded(path, self.excluded_paths)
        ]

        base_commit = _run_git(
            self.project_root,
            "rev-parse",
            "--verify",
            f"{base_ref}^{{commit}}",
        ).stdout.strip()
        source_commit = _run_git(
            self.project_root,
            "rev-parse",
            "--verify",
            f"{source_ref}^{{commit}}",
        ).stdout.strip()
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
            _run_git(self.project_root, "read-tree", base_commit, env=env)
            for path in normalized_paths:
                entry = subprocess.run(
                    [
                        "git",
                        "ls-tree",
                        "-z",
                        source_commit,
                        "--",
                        f":(top,literal){path}",
                    ],
                    cwd=str(self.project_root),
                    capture_output=True,
                )
                if entry.returncode != 0:
                    raise RuntimeError(
                        entry.stderr.decode("utf-8", errors="replace").strip()
                        or f"could not inspect snapshot source path {path}"
                    )
                entries = [item for item in entry.stdout.split(b"\0") if item]
                if not entries:
                    _run_git(
                        self.project_root,
                        "update-index",
                        "--force-remove",
                        "--",
                        path,
                        env=env,
                    )
                    continue
                exact_entries = []
                for item in entries:
                    metadata, separator, raw_path = item.partition(b"\t")
                    if separator and raw_path.decode(
                        "utf-8", errors="surrogateescape"
                    ) == path:
                        exact_entries.append(metadata.split())
                if (
                    len(exact_entries) != 1
                    or len(exact_entries[0]) != 3
                    or exact_entries[0][1] != b"blob"
                ):
                    raise RuntimeError(
                        f"snapshot source path is not one exact blob: {path}"
                    )
                mode, _kind, object_id = exact_entries[0]
                _run_git(
                    self.project_root,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    mode.decode("ascii"),
                    object_id.decode("ascii"),
                    path,
                    env=env,
                )

            self._force_remove_excluded_index_entries(env)
            tree = _run_git(
                self.project_root, "write-tree", env=env
            ).stdout.strip()
            commit = _run_git(
                self.project_root,
                "commit-tree",
                tree,
                "-m",
                f"auto_agents gate snapshot {self.plan_id}",
                "-p",
                base_commit,
                env=env,
            ).stdout.strip()
            ref_name = f"refs/auto-agents/gate-snapshots/{self.plan_id}"
            _run_git(self.project_root, "update-ref", ref_name, commit)
            self.snapshot = GateSourceSnapshot(
                commit_sha=commit,
                tree_sha=tree,
                ref_name=ref_name,
            )
            return self.snapshot
        finally:
            index_path.unlink(missing_ok=True)

    def use_ref(self, source_ref: str) -> GateSourceSnapshot:
        """Use an immutable existing commit/ref as the plan source."""
        commit = _run_git(
            self.project_root, "rev-parse", "--verify", f"{source_ref}^{{commit}}"
        ).stdout.strip()
        tree = _run_git(
            self.project_root, "rev-parse", "--verify", f"{commit}^{{tree}}"
        ).stdout.strip()
        self.snapshot = GateSourceSnapshot(
            commit_sha=commit,
            tree_sha=tree,
            ref_name="",
        )
        return self.snapshot

    def close(self) -> None:
        if self.snapshot is None:
            return
        if self.snapshot.ref_name:
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
    return value if value in {"heavy", "exclusive"} else "normal"


def _metadata_signature(metadata: object) -> str:
    payload = {
        "proof_ids": sorted(_metadata_list(metadata, "proof_ids")),
        "risk": str(getattr(metadata, "risk", "medium")),
        "resource_class": _metadata_resource_class(metadata),
        "cpu_slots": int(getattr(metadata, "cpu_slots", 0) or 0),
        "memory_mb": int(getattr(metadata, "memory_mb", 0) or 0),
        "memory_reserve_mb": int(
            getattr(metadata, "memory_reserve_mb", 0) or 0
        ),
        "memory_guard": str(getattr(metadata, "memory_guard", "off")),
        "requires": sorted(_metadata_list(metadata, "requires")),
        "exclusive_resources": sorted(
            _metadata_list(metadata, "exclusive_resources")
        ),
        "dynamic_ports": sorted(_metadata_list(metadata, "dynamic_ports")),
        "artifact_globs": sorted(_metadata_list(metadata, "artifact_globs")),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _effective_result_cache_scope(metadata: object) -> str:
    scope = str(getattr(metadata, "result_cache_scope", "off")).strip().lower()
    if scope != "auto":
        return scope
    if (
        str(getattr(metadata, "cache_scope", "run_context")).strip().lower()
        != "source"
        or _metadata_list(metadata, "artifact_globs")
        or _metadata_list(metadata, "exclusive_resources")
        or _metadata_list(metadata, "dynamic_ports")
    ):
        return "candidate"
    return "auto"


def _path_observation_digest(path: Path) -> str:
    if path.is_symlink():
        return f"link:{path.readlink()}"
    if path.is_dir():
        encoded = json.dumps(
            sorted(item.name for item in path.iterdir()),
            ensure_ascii=False,
        ).encode("utf-8")
        return "dir:" + hashlib.sha256(encoded).hexdigest()
    return "file:" + _sha256(path)


def _observed_input_manifest(
    trace_path: Path,
    sandbox: Path,
    dependency_links: Mapping[str, Path],
) -> tuple[dict[str, str], bool]:
    sandbox = sandbox.resolve()
    try:
        text = trace_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, False
    network_observed = "connect(" in text or "sendto(" in text
    if re.search(
        r"\b(?:chdir|fchdir)\("
        r"|\b(?:openat2?|newfstatat|fstatat64|faccessat2?|readlinkat|statx)\(\s*(?!\s|AT_FDCWD\b)",
        text,
    ):
        # This tracer does not track per-process cwd or directory descriptors.
        # Such traces cannot certify inputs for reuse on another source tree.
        return {}, network_observed
    ignored = {
        ".git",
        ".auto-agents-gate-runtime",
        ".auto-agents-gate-tmp",
        ".auto-agents-gate-cache",
        *dependency_links.keys(),
    }
    manifest: dict[str, str] = {}
    for match in re.finditer(r'"(?:[^"\\]|\\.)*"', text):
        try:
            raw = json.loads(match.group(0))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw, str) or not raw or raw.startswith(
            ("/dev/", "/proc/", "/sys/")
        ):
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = sandbox / candidate
        try:
            lexical_relative = candidate.relative_to(sandbox)
        except ValueError:
            lexical_relative = None
        if lexical_relative is not None:
            relative = lexical_relative.as_posix()
            if any(
                relative == prefix or relative.startswith(prefix.rstrip("/") + "/")
                for prefix in ignored
            ):
                continue
            component = sandbox
            for part in lexical_relative.parts:
                component = component / part
                if component.is_symlink():
                    # Resolving only the final target loses symlink dependencies,
                    # including intermediate links that can later be retargeted.
                    return {}, network_observed
        try:
            resolved = candidate.resolve()
            relative = resolved.relative_to(sandbox).as_posix()
        except (OSError, ValueError):
            continue
        if any(
            relative == prefix or relative.startswith(prefix.rstrip("/") + "/")
            for prefix in ignored
        ):
            continue
        if not resolved.exists():
            # Negative lookups are inputs too: a later source change that
            # creates the probed path must invalidate this certificate.
            manifest[f"!{relative}"] = "missing"
            parent = resolved.parent
            try:
                parent_relative = parent.relative_to(sandbox.resolve()).as_posix()
            except ValueError:
                continue
            if parent.exists() and parent_relative not in {"", "."}:
                try:
                    manifest[parent_relative] = _path_observation_digest(parent)
                except OSError:
                    return {}, network_observed
            continue
        try:
            manifest[relative] = _path_observation_digest(resolved)
        except OSError:
            return {}, network_observed
    return manifest, network_observed


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
        probe: Optional[Path] = None
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".write-probe-{uuid.uuid4().hex}"
            with probe.open("xb") as handle:
                handle.write(b"ok")
            probe.unlink()
            return candidate.resolve()
        except OSError:
            if probe is not None:
                try:
                    probe.unlink()
                except OSError:
                    pass
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


@contextmanager
def dynamic_port_lease(
    names: Sequence[str],
    *,
    max_attempts: int = 128,
) -> object:
    """Reserve unique loopback ports for one gate job."""

    normalized = list(dict.fromkeys(str(item).strip() for item in names))
    for name in normalized:
        if (
            not name
            or not name[0].islower()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in name
            )
        ):
            raise ValueError(
                "dynamic port names must use lowercase snake_case"
            )
    if not normalized:
        yield {}
        return

    root = auto_agents_state_root() / "port-locks"
    root.mkdir(parents=True, exist_ok=True)
    handles: list[object] = []
    reservations: list[socket.socket] = []
    allocated: dict[str, int] = {}
    used_ports: set[int] = set()
    try:
        for name in normalized:
            for _attempt in range(max(1, max_attempts)):
                port = 49152 + secrets.randbelow(65535 - 49152 + 1)
                if port in used_ports:
                    continue
                handle = (root / f"{port}.lock").open("a+")
                try:
                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError:
                    handle.close()
                    continue

                tcp_socket: Optional[socket.socket] = None
                udp_socket: Optional[socket.socket] = None
                try:
                    tcp_socket = socket.socket(
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                    )
                    udp_socket = socket.socket(
                        socket.AF_INET,
                        socket.SOCK_DGRAM,
                    )
                    tcp_socket.bind(("127.0.0.1", port))
                    udp_socket.bind(("127.0.0.1", port))
                except OSError:
                    if tcp_socket is not None:
                        tcp_socket.close()
                    if udp_socket is not None:
                        udp_socket.close()
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    continue

                assert tcp_socket is not None
                assert udp_socket is not None
                handles.append(handle)
                reservations.extend((tcp_socket, udp_socket))
                allocated[name] = port
                used_ports.add(port)
                break
            else:
                raise RuntimeError(
                    f"unable to allocate dynamic gate port for {name!r} "
                    f"after {max(1, max_attempts)} attempts"
                )

        for reservation in reservations:
            reservation.close()
        reservations.clear()
        yield dict(allocated)
    finally:
        for reservation in reservations:
            reservation.close()
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
    runtime_profile: str = SHORT_RUNTIME_PROFILE,
    dynamic_ports: Optional[Mapping[str, int]] = None,
) -> dict[str, str]:
    env = dict(base or os.environ)
    for key in list(env):
        if (
            key in {
                "AUTO_AGENTS_GATE_HOST",
                "AUTO_AGENTS_GATE_PORTS_JSON",
            }
            or key.startswith("AUTO_AGENTS_GATE_PORT_")
        ):
            env.pop(key, None)
    runtime_profile = str(runtime_profile or SHORT_RUNTIME_PROFILE).strip()
    if runtime_profile not in {SHORT_RUNTIME_PROFILE, LEGACY_RUNTIME_PROFILE}:
        raise ValueError(f"unsupported gate runtime profile: {runtime_profile}")
    if runtime_profile == SHORT_RUNTIME_PROFILE:
        runtime_root = short_job_runtime_root(job_id)
    else:
        runtime_root = runtime_root or (sandbox / ".auto-agents-gate-runtime")
    temp_root = runtime_root / "t"
    cache_root = runtime_root / "c"
    xdg_runtime_root = runtime_root / "r"
    temp_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    xdg_runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        xdg_runtime_root.chmod(0o700)
    env.update(
        {
            "AUTO_AGENTS_GATE_JOB_ID": job_id,
            "AUTO_AGENTS_GATE_SANDBOX_ROOT": str(sandbox),
            "AUTO_AGENTS_GATE_RUNTIME_PROFILE": runtime_profile,
            "AUTO_AGENTS_GATE_RUNTIME_ROOT": str(runtime_root),
            "AUTO_AGENTS_TEST": "True",
            "PYTEST_CURRENT_TEST": "auto_agents_gate_run",
            "TESTING": "True",
            "TMPDIR": str(temp_root),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
            "XDG_RUNTIME_DIR": str(xdg_runtime_root),
            "XDG_CACHE_HOME": str(cache_root / "x"),
            "XDG_STATE_HOME": str(runtime_root / "s"),
            "PYTHONPYCACHEPREFIX": str(cache_root / "p"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "npm_config_cache": str(cache_root / "n"),
        }
    )
    ports = {
        str(name): int(port)
        for name, port in dict(dynamic_ports or {}).items()
    }
    if ports:
        env["AUTO_AGENTS_GATE_HOST"] = "127.0.0.1"
        env["AUTO_AGENTS_GATE_PORTS_JSON"] = json.dumps(
            ports,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        for name, port in ports.items():
            env[f"AUTO_AGENTS_GATE_PORT_{name.upper()}"] = str(port)
    return env


def dependency_link_paths(project_root: Path) -> tuple[str, ...]:
    candidates: set[Path] = {
        Path(".conda"),
        Path(".venv"),
        Path("node_modules"),
        Path(".auto-agents/runtime"),
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
    return tuple(
        relative.as_posix()
        for relative in sorted(candidates, key=lambda item: item.as_posix())
    )


def repository_exclusion_paths(
    project_root: Path,
    *,
    dependency_links: Mapping[str, Path] | Iterable[str] = (),
    surface_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """Compose dependency and surface exclusions under one path contract."""

    discovered_paths = (
        dependency_links.keys()
        if isinstance(dependency_links, Mapping)
        else dependency_links
    )
    return normalize_repository_exclusions(
        (
            *dependency_link_paths(project_root),
            *discovered_paths,
            *surface_paths,
        )
    )


def self_referential_dependency_links(project_root: Path) -> list[str]:
    """Return dependency links that lexically point back to themselves.

    Dependency links installed into a worktree use absolute source paths. If
    one of those links is accidentally committed and checked out at its source
    path, its payload becomes a self-reference. Detect that signature without
    resolving the link, because resolving it is the operation that raises.
    """

    leaked: list[str] = []
    for relative in dependency_link_paths(project_root):
        candidate = project_root / relative
        if not candidate.is_symlink():
            continue
        try:
            raw_target = candidate.readlink()
        except OSError:
            continue
        target = raw_target if raw_target.is_absolute() else candidate.parent / raw_target
        candidate_text = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        target_text = os.path.normcase(os.path.abspath(os.fspath(target)))
        if candidate_text == target_text:
            leaked.append(relative)
    return leaked


def discover_dependency_links(project_root: Path) -> dict[str, Path]:
    links: dict[str, Path] = {}
    for relative in dependency_link_paths(project_root):
        try:
            source = (project_root / relative).resolve(strict=True)
            is_directory = source.is_dir()
        except (OSError, RuntimeError):
            # Broken and cyclic dependency links are unusable, but discovery
            # must remain total so the orchestrator can enter its recovery
            # path instead of crashing while inspecting them.
            continue
        if is_directory:
            links[relative] = source
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
        environment_fingerprint: str = "",
        result_context_fingerprint: str = "",
        source_ref: str = "",
        use_result_cache: bool = True,
        cache_path: Optional[Path] = None,
        preempt_requested: Optional[Callable[[], bool]] = None,
        environment_overrides: Optional[Mapping[str, str]] = None,
        proof_audit_sample_rate: float = 0.0,
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
        self.dependency_links = dict(
            dependency_links
            if dependency_links is not None
            else discover_dependency_links(self.project_root)
        )
        self.snapshot_manager = GateSnapshotManager(
            self.project_root,
            self.plan_id,
            excluded_paths=repository_exclusion_paths(
                self.project_root,
                dependency_links=self.dependency_links,
                surface_paths=GATE_SNAPSHOT_RUNTIME_PATHS,
            ),
        )
        self.snapshot: Optional[GateSourceSnapshot] = None
        self.worker_id = worker_id
        self.source_ref = str(source_ref).strip()
        self.use_result_cache = bool(use_result_cache)
        self.preempt_requested = preempt_requested
        self.environment_overrides = dict(environment_overrides or {})
        self.proof_audit_sample_rate = min(
            1.0,
            max(0.0, float(proof_audit_sample_rate)),
        )
        self.timing_store = GateTimingStore(
            self.project_root,
            cache_path=cache_path,
            environment_fingerprint=environment_fingerprint,
        )
        self.result_cache = GateResultCache(
            self.project_root,
            cache_path=cache_path,
            environment_fingerprint=environment_fingerprint,
            context_fingerprint=result_context_fingerprint,
            max_age_seconds=gate_config.cache_max_age_seconds,
        )
        self._shared_sandboxes: dict[str, Path] = {}
        self._published_hashes: dict[str, str] = {}
        self._cache_miss_reasons: dict[str, str] = {}
        self._timing_estimates: Optional[dict[str, Optional[float]]] = None
        self._lock = threading.Lock()

    def __enter__(self) -> "LocalGatePlanExecutor":
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.snapshot = (
            self.snapshot_manager.use_ref(self.source_ref)
            if self.source_ref
            else self.snapshot_manager.create()
        )
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def priority(self, command: str) -> tuple[object, ...]:
        metadata = self.metadata.get(command)
        estimate = self.estimated_duration(command)
        return (
            0 if estimate is not None else 1,
            -(estimate or 0.0),
            0 if _metadata_resource_class(metadata) == "heavy" else 1,
        )

    def estimated_duration(self, command: str) -> Optional[float]:
        with self._lock:
            if self._timing_estimates is None:
                self._timing_estimates = self.timing_store.estimate_many(self.metadata)
            if command not in self._timing_estimates:
                self._timing_estimates[command] = self.timing_store.estimate(
                    command, self.metadata.get(command)
                )
            return self._timing_estimates[command]

    def required_slots(self, command: str) -> int:
        metadata = self.metadata.get(command)
        try:
            declared = max(0, int(getattr(metadata, "cpu_slots", 0) or 0))
        except (TypeError, ValueError):
            declared = 0
        if declared > 0:
            return declared
        return 2 if _metadata_resource_class(metadata) == "heavy" else 1

    def exclusive(self, command: str) -> bool:
        return _metadata_resource_class(self.metadata.get(command)) == "exclusive"

    def record_timing(self, command: str, result: CommandResult) -> None:
        self.timing_store.record(command, result, self.metadata.get(command))
        with self._lock:
            if self._timing_estimates is not None:
                self._timing_estimates.pop(command, None)

    def cached_result(self, command: str) -> Optional[CommandResult]:
        if (
            not self.use_result_cache
            or self.gate_config.verification_policy_version < 2
            or self.snapshot is None
        ):
            self._cache_miss_reasons[command] = "cache_not_eligible"
            return None
        metadata = self.metadata.get(command)
        result, reason = self.result_cache.lookup_with_reason(
            command,
            source_fingerprint=self.snapshot.tree_sha,
            cache_scope=str(
                getattr(metadata, "cache_scope", "run_context")
            ).strip().lower(),
            result_cache_scope=_effective_result_cache_scope(metadata),
            metadata_signature=_metadata_signature(metadata),
        )
        self._cache_miss_reasons[command] = reason
        if result is not None and self.proof_audit_sample_rate > 0:
            audit_bucket = int(
                hashlib.sha256(
                    f"{self.snapshot.tree_sha}\0{command}".encode("utf-8")
                ).hexdigest()[:16],
                16,
            ) / float(0xFFFFFFFFFFFFFFFF)
            if audit_bucket < self.proof_audit_sample_rate:
                self._cache_miss_reasons[command] = "proof_audit_sample"
                return None
        return result

    def record_cached_result(
        self,
        command: str,
        result: CommandResult,
    ) -> None:
        if (
            not self.use_result_cache
            or self.gate_config.verification_policy_version < 2
            or self.snapshot is None
        ):
            return
        metadata = self.metadata.get(command)
        self.result_cache.record(
            command,
            result,
            source_fingerprint=self.snapshot.tree_sha,
            cache_scope=str(
                getattr(metadata, "cache_scope", "run_context")
            ).strip().lower(),
            result_cache_scope=_effective_result_cache_scope(metadata),
            metadata_signature=_metadata_signature(metadata),
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
        ignored = repository_exclusion_paths(
            sandbox,
            dependency_links=self.dependency_links,
            surface_paths=GATE_SNAPSHOT_RUNTIME_PATHS,
        )
        for line in process.stdout.splitlines():
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path and not repository_path_is_excluded(path, ignored):
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
        environment_overrides: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        job_id = uuid.uuid4().hex
        sandbox: Optional[Path] = None
        runtime_root: Optional[Path] = None
        cleanup = not bool(lane)
        try:
            metadata = self.metadata.get(command)
            result_cache_scope = _effective_result_cache_scope(metadata)
            cached = self.cached_result(command)
            if cached is not None:
                if progress is not None:
                    progress("cache_hit", command, 0.0)
                return cached
            if self.preempt_requested is not None and self.preempt_requested():
                return CommandResult(
                    command=command,
                    ok=False,
                    returncode=125,
                    stderr="background release yielded to a foreground workflow",
                    termination_reason="foreground_preempted",
                    infrastructure_error=True,
                    infrastructure_failure_id="foreground_preempted",
                    job_id=job_id,
                    worker_id=self.worker_id,
                    backend="local-isolated",
                )
            sandbox, _created = self._sandbox(lane, job_id)
            requested_profile = str(
                dict(environment_overrides or {}).get(
                    "AUTO_AGENTS_GATE_RUNTIME_PROFILE", SHORT_RUNTIME_PROFILE
                )
            )
            runtime_root = (
                short_job_runtime_root(job_id)
                if requested_profile == SHORT_RUNTIME_PROFILE
                else self.worktree_root / self.plan_id / ".runtime" / job_id
            )
            if progress is not None:
                progress("start", command, 0.0)
            trace_path: Optional[Path] = None
            traced_command = isolated_command(command)
            if (
                result_cache_scope in {"observed_inputs", "auto"}
                and shutil.which("strace")
            ):
                trace_path = runtime_root / "input-trace.log"
                traced_command = (
                    "strace -f -qq -e trace=%file,%network,fchdir -o "
                    f"{shlex.quote(str(trace_path))} "
                    f"sh -lc {shlex.quote(traced_command)}"
                )
            with exclusive_resource_lease(
                _metadata_list(metadata, "exclusive_resources"),
                worker_id=self.worker_id,
            ):
                with dynamic_port_lease(
                    _metadata_list(metadata, "dynamic_ports")
                ) as dynamic_ports:
                    merged_overrides = {
                        **self.environment_overrides,
                        **dict(environment_overrides or {}),
                    }
                    env = gate_environment(
                        sandbox,
                        job_id=job_id,
                        base={**os.environ, **merged_overrides},
                        runtime_root=runtime_root,
                        runtime_profile=requested_profile,
                        dynamic_ports=dynamic_ports,
                    )
                    monitor_stop = threading.Event()
                    foreground_preempted = threading.Event()
                    monitor = None
                    if self.preempt_requested is not None and cancel_event is not None:
                        def monitor_foreground() -> None:
                            while not monitor_stop.wait(0.5):
                                if self.preempt_requested is not None and self.preempt_requested():
                                    foreground_preempted.set()
                                    cancel_event.set()
                                    return

                        monitor = threading.Thread(
                            target=monitor_foreground,
                            name="release-foreground-monitor",
                            daemon=True,
                        )
                        monitor.start()
                    try:
                        process = run_supervised_shell_command(
                            traced_command,
                            cwd=sandbox,
                            env=env,
                            timeout_seconds=timeout_seconds,
                            adaptive_timeout_enabled=adaptive_timeout_enabled,
                            idle_timeout_seconds=idle_timeout_seconds,
                            kind="gate",
                            cancel_event=cancel_event,
                            progress=(
                                (
                                    lambda event, elapsed: progress(
                                        event, command, elapsed
                                    )
                                )
                                if progress is not None
                                else None
                            ),
                        )
                    finally:
                        monitor_stop.set()
                        if monitor is not None:
                            monitor.join(timeout=1.0)
            mutations = self._mutation_paths(sandbox)
            self._publish_diagnostics(sandbox, job_id)
            stderr = process.stderr
            stdout = process.stdout
            for sensitive_value in sorted(
                {value for value in merged_overrides.values() if value},
                key=len,
                reverse=True,
            ):
                stdout = stdout.replace(sensitive_value, "[REDACTED]")
                stderr = stderr.replace(sensitive_value, "[REDACTED]")
            termination_reason = (
                "foreground_preempted"
                if foreground_preempted.is_set()
                else process.termination_reason
            )
            ok = process.returncode == 0 and not termination_reason
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
                stdout=stdout,
                stderr=stderr,
                duration_seconds=process.duration_seconds,
                termination_reason=termination_reason,
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
                cache_miss_reason=self._cache_miss_reasons.get(
                    command,
                    "not_checked",
                ),
            )
            if trace_path is not None and result.ok:
                observed_inputs, network_observed = _observed_input_manifest(
                    trace_path,
                    sandbox,
                    self.dependency_links,
                )
                result.observed_inputs = observed_inputs
                result.input_trace_complete = bool(observed_inputs)
                result.network_observed = network_observed
            self.record_cached_result(command, result)
            if progress is not None:
                progress("finish", command, result.duration_seconds)
            self.record_timing(command, result)
            return result
        except (OSError, RuntimeError, ValueError) as error:
            result = CommandResult(
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
            self.record_timing(command, result)
            return result
        finally:
            if runtime_root is not None:
                shutil.rmtree(runtime_root, ignore_errors=True)
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
