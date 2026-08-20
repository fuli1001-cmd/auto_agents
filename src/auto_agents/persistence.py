from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Dict, List, Mapping, Sequence

from .models import PersistenceConfig, PersistenceTargetConfig


SCHEMA_PATH_PATTERN = re.compile(
    r"(?:^|/)(?:migrations?|alembic/versions|prisma|db/migrate)(?:/|$)"
    r"|(?:^|/)(?:schema\.prisma|schema\.rb)$"
    r"|\.sql$",
    re.IGNORECASE,
)
SCHEMA_DIFF_PATTERN = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|SCHEMA)\b"
    r"|\b(?:ADD|DROP|RENAME)\s+COLUMN\b"
    r"|\bop\.(?:create_table|drop_table|add_column|drop_column|alter_column|rename_table)\b"
    r"|\b(?:createTable|dropTable|addColumn|removeColumn|renameColumn)\s*\("
    r"|\bmigrations\.(?:CreateModel|DeleteModel|AddField|RemoveField|AlterField)\b",
    re.IGNORECASE,
)
SCHEMA_CODE_PATH_PATTERN = re.compile(
    r"(?:^|/)(?:db|database|sqlite|repository|models?|storage)[^/]*\.(?:py|rb|js|ts|go|rs|java)$",
    re.IGNORECASE,
)
SCHEMA_COLUMN_PATTERN = re.compile(
    r"^(?!(?:if|elif|else|for|while|return|yield|raise|assert|with|from|import|"
    r"class|def|async|await|try|except|finally|match|case)\b)"
    r"[`\"']?[A-Za-z_][A-Za-z0-9_]*[`\"']?\s+"
    r"(?:INTEGER|INT|TEXT|REAL|BLOB|BOOLEAN|BOOL|VARCHAR|CHAR|TIMESTAMP|DATETIME|JSON|UUID)\b"
    r"(?=\s*(?:\([^)]*\)\s*)?(?:,|$|NOT\b|NULL\b|PRIMARY\b|UNIQUE\b|DEFAULT\b|"
    r"REFERENCES\b|CHECK\b|COLLATE\b|GENERATED\b|AUTOINCREMENT\b))",
    re.IGNORECASE,
)
_IGNORED_DETECTION_PREFIXES = (
    ".auto-agents/",
    "docs/",
    "specs/",
    "tests/",
    "test/",
)
_SHELL_CONTROL_PATTERN = re.compile(r"[|;&<>`\n\r]")


class PersistenceContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistenceFinding:
    path: str
    kind: str
    evidence: str

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "kind": self.kind, "evidence": self.evidence}


def detect_persistence_schema_changes(
    project_root: Path,
    *,
    diff_text: str | None = None,
) -> List[PersistenceFinding]:
    root = project_root.resolve()
    scan_untracked = diff_text is None
    if diff_text is None:
        process = subprocess.run(
            ["git", "diff", "HEAD", "--no-ext-diff", "--unified=0", "--", "."],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if process.returncode != 0:
            process = subprocess.run(
                ["git", "diff", "--no-ext-diff", "--unified=0", "--", "."],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        diff_text = process.stdout if process.returncode == 0 else ""

    findings: List[PersistenceFinding] = []
    current_path = ""
    path_reported = set()
    for raw_line in str(diff_text).splitlines():
        if raw_line.startswith("+++ b/"):
            current_path = raw_line[6:].strip()
            if _path_is_detection_ignored(current_path):
                continue
            if SCHEMA_PATH_PATTERN.search(current_path):
                findings.append(
                    PersistenceFinding(
                        path=current_path,
                        kind="schema_path",
                        evidence=current_path,
                    )
                )
                path_reported.add(current_path)
            continue
        if (
            not current_path
            or _path_is_detection_ignored(current_path)
            or not raw_line.startswith(("+", "-"))
            or raw_line.startswith(("+++", "---"))
        ):
            continue
        content = raw_line[1:].strip()
        if SCHEMA_DIFF_PATTERN.search(content) or (
            SCHEMA_CODE_PATH_PATTERN.search(current_path)
            and SCHEMA_COLUMN_PATTERN.search(content.rstrip(","))
        ):
            finding = PersistenceFinding(
                path=current_path,
                kind="schema_ddl",
                evidence=content[:240],
            )
            if finding not in findings:
                findings.append(finding)

    if scan_untracked:
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if untracked.returncode == 0:
            for raw_path in untracked.stdout.splitlines():
                path = raw_path.strip().replace("\\", "/")
                if not path or _path_is_detection_ignored(path):
                    continue
                candidate = root / path
                if SCHEMA_PATH_PATTERN.search(path):
                    finding = PersistenceFinding(path, "schema_path", path)
                    if finding not in findings:
                        findings.append(finding)
                    continue
                if not candidate.is_file() or candidate.stat().st_size > 1024 * 1024:
                    continue
                try:
                    content = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                match = SCHEMA_DIFF_PATTERN.search(content)
                if match:
                    finding = PersistenceFinding(
                        path, "schema_ddl", match.group(0)[:240]
                    )
                    if finding not in findings:
                        findings.append(finding)
    return findings


def _path_is_detection_ignored(path: str) -> bool:
    normalized = str(path).replace("\\", "/").lstrip("./")
    if normalized in {"README.md", "DESIGN.md"}:
        return True
    if normalized.startswith(_IGNORED_DETECTION_PREFIXES):
        return True
    return bool(
        re.search(r"(?:^|/)[^/]+\.(?:test|spec)\.[cm]?[jt]sx?$", normalized)
        or re.search(r"(?:^|/)test_[^/]+\.py$", normalized)
    )


def persistence_change_strategy(change: Mapping[str, object] | None) -> str:
    if not isinstance(change, Mapping):
        return "none"
    return str(change.get("strategy", "none") or "none").strip()


def persistence_action_fingerprint(
    change: Mapping[str, object],
    targets: Sequence[PersistenceTargetConfig],
    *,
    candidate_fingerprint: str,
) -> str:
    payload = {
        "version": 1,
        "change": dict(change),
        "targets": [target.to_dict() for target in targets],
        "candidate_fingerprint": candidate_fingerprint,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def persistence_candidate_fingerprint(project_root: Path) -> str:
    root = project_root.resolve()
    process = subprocess.run(
        [
            "git",
            "diff",
            "HEAD",
            "--no-ext-diff",
            "--binary",
            "--",
            ".",
            ":(exclude).auto-agents/**",
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    digest = hashlib.sha256()
    if process.returncode == 0:
        digest.update(process.stdout)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "."],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if untracked.returncode == 0:
        for raw_path in sorted(untracked.stdout.splitlines()):
            path = raw_path.strip().replace("\\", "/")
            if not path or path.startswith(".auto-agents/"):
                continue
            candidate = root / path
            digest.update(path.encode("utf-8"))
            if candidate.is_file():
                try:
                    digest.update(candidate.read_bytes())
                except OSError:
                    pass
    return digest.hexdigest()


def persistence_targets_for_change(
    config: PersistenceConfig,
    change: Mapping[str, object],
) -> List[PersistenceTargetConfig]:
    target_ids = change.get("target_ids", [])
    if not isinstance(target_ids, list) or not target_ids:
        raise PersistenceContractError("persistence change has no target_ids")
    targets: List[PersistenceTargetConfig] = []
    for raw_target_id in target_ids:
        target_id = str(raw_target_id).strip()
        target = config.target(target_id)
        if target is None:
            raise PersistenceContractError(
                f"persistence target is not configured: {target_id}; run persistence-configure"
            )
        targets.append(target)
    return targets


def build_persistence_action_manifest(
    project_root: Path,
    change: Mapping[str, object],
    config: PersistenceConfig,
    *,
    candidate_fingerprint: str,
) -> Dict[str, object]:
    strategy = persistence_change_strategy(change)
    targets = persistence_targets_for_change(config, change)
    entries: List[Dict[str, object]] = []
    for target in targets:
        entry: Dict[str, object] = {
            "target_id": target.target_id,
            "environment": target.environment,
            "kind": target.kind,
            "execution": (
                "generate_only" if target.environment == "production" else "automatic"
            ),
            "commands": {
                "apply": list(target.apply_argv),
                "initialize": list(target.initialize_argv),
                "reset": list(target.reset_argv),
                "verify": list(target.verify_argv),
            },
        }
        if strategy == "clean_break" and target.environment != "production":
            if target.kind == "local_file":
                entry["destructive_paths"] = [
                    str(path.relative_to(project_root.resolve()))
                    for path in _local_target_paths(project_root, target)
                ]
            else:
                entry["destructive_paths"] = list(target.associated_paths)
        entries.append(entry)
    fingerprint = persistence_action_fingerprint(
        change, targets, candidate_fingerprint=candidate_fingerprint
    )
    return {
        "version": 1,
        "strategy": strategy,
        "decision_id": str(change.get("decision_id", "")),
        "to_version": str(change.get("to_version", "")),
        "candidate_fingerprint": candidate_fingerprint,
        "fingerprint": fingerprint,
        "targets": entries,
    }


def execute_persistence_action(
    project_root: Path,
    change: Mapping[str, object],
    config: PersistenceConfig,
) -> Dict[str, object]:
    root = project_root.resolve()
    strategy = persistence_change_strategy(change)
    if strategy in {"none", "initial_schema"}:
        return {"ok": True, "strategy": strategy, "targets": [], "executed": False}
    targets = persistence_targets_for_change(config, change)
    _preflight_persistence_targets(root, strategy, targets)
    results: List[Dict[str, object]] = []
    for target in targets:
        if target.environment == "production":
            results.append(
                {
                    "target_id": target.target_id,
                    "environment": target.environment,
                    "status": "generate_only",
                }
            )
            continue

        target_result: Dict[str, object] = {
            "target_id": target.target_id,
            "environment": target.environment,
            "status": "running",
            "steps": [],
        }
        steps = target_result["steps"]
        assert isinstance(steps, list)
        if strategy == "clean_break":
            if target.kind == "compose_service" and not target.reset_argv:
                raise PersistenceContractError(
                    f"compose clean_break target {target.target_id} requires reset_argv"
                )
            if target.reset_argv:
                steps.append(_run_target_command(root, target, "reset", target.reset_argv))
            for path in _destructive_target_paths(root, target):
                _remove_path(path)
                steps.append({"step": "delete", "path": str(path.relative_to(root)), "ok": True})
            if not target.initialize_argv:
                raise PersistenceContractError(
                    f"clean_break target {target.target_id} requires initialize_argv"
                )
            steps.append(
                _run_target_command(root, target, "initialize", target.initialize_argv)
            )
        else:
            if not target.apply_argv:
                raise PersistenceContractError(
                    f"{strategy} target {target.target_id} requires apply_argv"
                )
            steps.append(_run_target_command(root, target, "apply", target.apply_argv))

        if not target.verify_argv:
            raise PersistenceContractError(
                f"persistence target {target.target_id} requires verify_argv"
            )
        steps.append(_run_target_command(root, target, "verify", target.verify_argv))
        target_result["status"] = "verified"
        results.append(target_result)
    return {"ok": True, "strategy": strategy, "targets": results, "executed": True}


def _preflight_persistence_targets(
    root: Path,
    strategy: str,
    targets: Sequence[PersistenceTargetConfig],
) -> None:
    """Validate every automatic target before any target can mutate persistent data."""
    for target in targets:
        if target.environment == "production":
            continue
        commands: List[tuple[str, Sequence[str]]] = []
        if strategy == "clean_break":
            if target.kind == "compose_service" and not target.reset_argv:
                raise PersistenceContractError(
                    f"compose clean_break target {target.target_id} requires reset_argv"
                )
            if target.reset_argv:
                commands.append(("reset", target.reset_argv))
            _destructive_target_paths(root, target)
            if not target.initialize_argv:
                raise PersistenceContractError(
                    f"clean_break target {target.target_id} requires initialize_argv"
                )
            commands.append(("initialize", target.initialize_argv))
        else:
            if not target.apply_argv:
                raise PersistenceContractError(
                    f"{strategy} target {target.target_id} requires apply_argv"
                )
            commands.append(("apply", target.apply_argv))
        if not target.verify_argv:
            raise PersistenceContractError(
                f"persistence target {target.target_id} requires verify_argv"
            )
        commands.append(("verify", target.verify_argv))
        for step, argv in commands:
            _validate_runtime_argv(argv, target.target_id, step)
            _preflight_pytest_command(root, target, step, argv)


def _preflight_pytest_command(
    root: Path,
    target: PersistenceTargetConfig,
    step: str,
    argv: Sequence[str],
) -> None:
    if not _is_pytest_command(argv) or not any(".py::" in item for item in argv):
        return
    collect_argv = [*argv, "--collect-only"]
    try:
        process = subprocess.run(
            collect_argv,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=target.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PersistenceContractError(
            f"persistence {step} preflight failed for {target.target_id}: {error}"
        ) from error
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "pytest collection failed")[-2000:]
        raise PersistenceContractError(
            f"persistence {step} configuration is stale for {target.target_id}: "
            "pytest could not collect the configured selector(s) before any persistence "
            f"action was executed: {detail.strip()}; run persistence-configure"
        )


def _is_pytest_command(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    executable = Path(str(argv[0])).name.lower()
    if executable in {"pytest", "pytest.exe", "py.test", "py.test.exe"}:
        return True
    return (
        executable in {"python", "python3", "python.exe", "python3.exe"}
        or executable.startswith("python3.")
    ) and len(argv) >= 3 and list(argv[1:3]) == ["-m", "pytest"]


def _run_target_command(
    root: Path,
    target: PersistenceTargetConfig,
    step: str,
    argv: Sequence[str],
) -> Dict[str, object]:
    _validate_runtime_argv(argv, target.target_id, step)
    try:
        process = subprocess.run(
            list(argv),
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=target.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PersistenceContractError(
            f"persistence {step} failed for {target.target_id}: {error}"
        ) from error
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "command failed")[-2000:]
        raise PersistenceContractError(
            f"persistence {step} failed for {target.target_id}: {detail.strip()}"
        )
    return {
        "step": step,
        "argv": list(argv),
        "ok": True,
        "output_tail": (process.stdout or "")[-1000:],
    }


def _validate_runtime_argv(argv: Sequence[str], target_id: str, step: str) -> None:
    if not argv or any(
        not isinstance(item, str)
        or not item.strip()
        or _SHELL_CONTROL_PATTERN.search(item)
        for item in argv
    ):
        raise PersistenceContractError(
            f"persistence {step} argv is unsafe for target {target_id}"
        )


def _local_target_paths(
    project_root: Path,
    target: PersistenceTargetConfig,
) -> List[Path]:
    root = project_root.resolve()
    locator = target.locator
    raw_path = str(locator.get("path", "")).strip()
    path_env = str(locator.get("path_env", "")).strip()
    if path_env:
        raw_path = os.environ.get(path_env, "").strip()
        if not raw_path:
            raise PersistenceContractError(
                f"persistence path environment variable is unset: {path_env}"
            )
    if not raw_path:
        raise PersistenceContractError(
            f"local_file persistence target {target.target_id} has no path"
        )
    candidate = Path(raw_path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    paths = [_validated_destructive_path(root, candidate)]
    paths.extend(
        _validated_destructive_path(root, root / raw_path)
        for raw_path in target.associated_paths
    )
    return list(dict.fromkeys(paths))


def _destructive_target_paths(
    project_root: Path,
    target: PersistenceTargetConfig,
) -> List[Path]:
    if target.kind == "local_file":
        return _local_target_paths(project_root, target)
    root = project_root.resolve()
    return [
        _validated_destructive_path(root, root / raw_path)
        for raw_path in target.associated_paths
    ]


def _validated_destructive_path(root: Path, candidate: Path) -> Path:
    unresolved = candidate.absolute()
    if unresolved == root or unresolved.parent == root.parent and unresolved.name == root.name:
        raise PersistenceContractError("persistence cleanup cannot target the project root")
    if candidate.is_symlink():
        raise PersistenceContractError(f"persistence cleanup refuses symlink: {candidate}")
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise PersistenceContractError(
            f"persistence cleanup target is outside the project: {candidate}"
        ) from error
    if not relative.parts:
        raise PersistenceContractError("persistence cleanup cannot target the project root")
    if not _git_ignored(root, relative):
        raise PersistenceContractError(
            f"persistence cleanup target must be git-ignored: {relative}"
        )
    return resolved


def _git_ignored(root: Path, relative: Path) -> bool:
    process = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", str(relative)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return process.returncode == 0


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise PersistenceContractError(f"persistence cleanup refuses symlink: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
