from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from .config import (
    ensure_auto_gitignore,
    operator_dir,
    project_runtime_dir,
    runtime_requirements_lock_path,
)
from .io_utils import read_json, write_json
from .operator_inputs import UserInputRequest


INSTALL_KINDS = {"file", "zip", "tar"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> Path:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe runtime artifact path: {value}")
    return Path(*path.parts)


@dataclass(frozen=True)
class RuntimeRequirement:
    tool_id: str
    version: str
    source_url: str
    sha256: str = ""
    install_kind: str = "file"
    executable: str = ""
    license: str = ""
    platform: str = ""
    size_bytes: int = 0

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RuntimeRequirement":
        item = cls(
            tool_id=str(payload.get("tool_id", "")).strip(),
            version=str(payload.get("version", "")).strip(),
            source_url=str(payload.get("source_url", "")).strip(),
            sha256=str(payload.get("sha256", "")).strip().lower(),
            install_kind=str(payload.get("install_kind", "file")).strip(),
            executable=str(payload.get("executable", "")).strip(),
            license=str(payload.get("license", "")).strip(),
            platform=str(payload.get("platform", "")).strip(),
            size_bytes=max(0, int(payload.get("size_bytes", 0) or 0)),
        )
        if not item.tool_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
            for character in item.tool_id
        ):
            raise ValueError("runtime tool_id must be lowercase and filesystem-safe")
        if not item.version or item.version.lower() == "latest":
            raise ValueError("runtime version must be explicit")
        parsed = urlparse(item.source_url)
        if parsed.scheme not in {"https", "file"}:
            raise ValueError("runtime source_url must use https or file")
        if item.sha256 and (
            len(item.sha256) != 64
            or any(character not in "0123456789abcdef" for character in item.sha256)
        ):
            raise ValueError("runtime sha256 must be 64 lowercase hex characters")
        if item.install_kind not in INSTALL_KINDS:
            raise ValueError(f"unsupported install_kind: {item.install_kind}")
        if item.executable:
            _safe_relative(item.executable)
        return item

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class RuntimeInstallPlan:
    requirements: List[RuntimeRequirement] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [item.to_dict() for item in self.requirements],
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    @property
    def total_size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.requirements)

    def approval_request(self) -> UserInputRequest:
        summary = "\n".join(
            f"- {item.tool_id} {item.version}; source={item.source_url}; "
            + (f"sha256={item.sha256}" if item.sha256 else "摘要=首次使用时锁定")
            for item in self.requirements
        )
        return UserInputRequest.from_dict(
            {
                "key": f"runtime.install.{self.fingerprint[:24]}",
                "kind": "install_approval",
                "question": (
                    "是否允许把以下缺失工具安装到当前目标项目？\n"
                    f"{summary}\n预计下载大小：{self.total_size_bytes} 字节"
                ),
                "purpose": "为目标项目提供可复现的测试和运行工具，不修改系统环境。",
                "why_required": "缺少这些工具时，依赖真实系统边界的任务无法执行。",
                "how_to_obtain": [
                    "程序只会写入目标项目的 .auto-agents/runtime 目录。",
                    "安装前验证锁定摘要；没有官方摘要时会明确标记首次信任。",
                ],
                "recommended_answer": "确认来源和版本符合项目合同后选择 y；不确定时选择 n。",
                "default": False,
                "persistence": "project",
                "sensitivity": "private",
                "subject_fingerprint": self.fingerprint,
                "question_version": 1,
                "validation": {
                    "runtime_manifest": [item.to_dict() for item in self.requirements]
                },
            }
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": 1,
            "fingerprint": self.fingerprint,
            "total_size_bytes": self.total_size_bytes,
            "requirements": [item.to_dict() for item in self.requirements],
        }


class ProjectRuntimeManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.root = project_runtime_dir(self.project_root)
        self.inventory_path = operator_dir(self.project_root) / "runtime.json"
        self.lock_path = runtime_requirements_lock_path(self.project_root)

    def plan(self, requirements: Iterable[Mapping[str, object]]) -> RuntimeInstallPlan:
        items = [RuntimeRequirement.from_dict(item) for item in requirements]
        unique = {(item.tool_id, item.version): item for item in items}
        return RuntimeInstallPlan(
            requirements=[unique[key] for key in sorted(unique)]
        )

    def inventory(self) -> Dict[str, Dict[str, object]]:
        payload = read_json(self.inventory_path, default={})
        raw = payload.get("tools", {}) if isinstance(payload, dict) else {}
        return {
            str(key): dict(value)
            for key, value in dict(raw).items()
            if isinstance(value, dict)
        }

    def installed(self, requirement: RuntimeRequirement) -> bool:
        record = self.inventory().get(requirement.tool_id)
        if not record or str(record.get("version", "")) != requirement.version:
            return False
        executable = Path(str(record.get("runtime_path", "")))
        if not executable.is_file():
            return False
        digest = _sha256(executable)
        recorded_executable = str(record.get("sha256", ""))
        recorded_source = str(record.get("source_sha256", ""))
        if requirement.sha256 and recorded_source != requirement.sha256:
            return False
        return bool(recorded_executable) and digest == recorded_executable

    def missing(self, plan: RuntimeInstallPlan) -> RuntimeInstallPlan:
        return RuntimeInstallPlan(
            requirements=[item for item in plan.requirements if not self.installed(item)]
        )

    def install(self, plan: RuntimeInstallPlan) -> Dict[str, Dict[str, object]]:
        ensure_auto_gitignore(self.project_root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._install_lock():
            inventory = self.inventory()
            installed: Dict[str, Dict[str, object]] = {}
            for requirement in plan.requirements:
                if self.installed(requirement):
                    installed[requirement.tool_id] = inventory[requirement.tool_id]
                    continue
                record = self._install_one(requirement)
                inventory[requirement.tool_id] = record
                installed[requirement.tool_id] = record
            self.inventory_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json(self.inventory_path, {"version": 1, "tools": inventory})
            if os.name == "posix":
                self.inventory_path.chmod(0o600)
            prior_lock = read_json(self.lock_path, default={})
            prior_requirements = (
                prior_lock.get("requirements", [])
                if isinstance(prior_lock, dict)
                else []
            )
            combined = self.plan(
                [
                    *(
                        item
                        for item in prior_requirements
                        if isinstance(item, dict)
                    ),
                    *(item.to_dict() for item in plan.requirements),
                ]
            )
            write_json(self.lock_path, combined.to_dict())
            return installed

    @contextmanager
    def _install_lock(self):
        lock_path = operator_dir(self.project_root) / "runtime.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock_path.open("a+", encoding="utf-8") as handle:
            if os.name == "posix":
                lock_path.chmod(0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "posix":
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _install_one(self, requirement: RuntimeRequirement) -> Dict[str, object]:
        tool_root = self.root / requirement.tool_id / requirement.version
        if tool_root.exists():
            raise RuntimeError(
                f"refusing to replace an unverified runtime directory: {tool_root}"
            )
        tool_root.parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(
            tempfile.mkdtemp(prefix=f".{requirement.tool_id}-", dir=str(tool_root.parent))
        )
        download = temporary_root / "download"
        try:
            with urllib.request.urlopen(requirement.source_url, timeout=120) as response:
                with download.open("wb") as output:
                    shutil.copyfileobj(response, output)
            observed_sha = _sha256(download)
            if requirement.sha256 and observed_sha != requirement.sha256:
                raise RuntimeError(
                    f"runtime digest mismatch for {requirement.tool_id}: "
                    f"expected {requirement.sha256}, got {observed_sha}"
                )
            content_root = temporary_root / "content"
            content_root.mkdir()
            if requirement.install_kind == "file":
                relative = _safe_relative(
                    requirement.executable or requirement.tool_id
                )
                destination = content_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(download, destination)
            elif requirement.install_kind == "zip":
                self._extract_zip(download, content_root)
            else:
                self._extract_tar(download, content_root)
            relative_executable = _safe_relative(
                requirement.executable or requirement.tool_id
            )
            executable = content_root / relative_executable
            if not executable.is_file():
                raise RuntimeError(
                    f"runtime executable is missing after extraction: {relative_executable}"
                )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            os.replace(content_root, tool_root)
            final_executable = tool_root / relative_executable
            final_sha = _sha256(final_executable)
            return {
                "tool_id": requirement.tool_id,
                "version": requirement.version,
                "source_url": requirement.source_url,
                "source_sha256": observed_sha,
                "sha256": final_sha,
                "runtime_path": str(final_executable.resolve()),
                "license": requirement.license,
                "platform": requirement.platform,
                "trust": "verified" if requirement.sha256 else "tofu",
            }
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    @staticmethod
    def _extract_zip(archive: Path, destination: Path) -> None:
        with zipfile.ZipFile(archive) as handle:
            for info in handle.infolist():
                relative = _safe_relative(info.filename)
                target = destination / relative
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

    @staticmethod
    def _extract_tar(archive: Path, destination: Path) -> None:
        with tarfile.open(archive, mode="r:*") as handle:
            for member in handle.getmembers():
                if member.issym() or member.islnk():
                    raise RuntimeError("runtime archives must not contain links")
                relative = _safe_relative(member.name)
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = handle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"could not extract runtime member: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
