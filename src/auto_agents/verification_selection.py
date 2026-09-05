from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence

from .models import GateConfig, VerificationStep


_IGNORED_PREFIXES = (
    ".auto-agents/",
    ".conda/",
    ".git/",
    ".tmp/",
    ".tmp-tests/",
    "node_modules/",
)
_JS_IMPORT = re.compile(
    r"(?:from\s+|import\s*\(?|require\s*\()\s*['\"](?P<path>\.{1,2}/[^'\"]+)['\"]"
)


def _normalized(path: str) -> str:
    return str(path).replace("\\", "/").strip().lstrip("./")


def _matches(path: str, pattern: str) -> bool:
    path = _normalized(path)
    pattern = _normalized(pattern)
    if not path or not pattern:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/")
        and fnmatch.fnmatchcase(path, pattern[3:])
    )


def _target_file(target: str) -> str:
    return _normalized(str(target).split("::", 1)[0])


class StaticDependencyIndex:
    """Small tracked-file import graph used to expand changed-file impact."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.files = self._tracked_source_files()
        self.module_files = self._python_module_files()
        self._dependencies: dict[str, set[str]] = {}

    def closure_for_targets(self, targets: Iterable[str]) -> set[str]:
        pending = [_target_file(target) for target in targets]
        seen: set[str] = set()
        while pending:
            relative = pending.pop()
            if not relative or relative in seen or relative not in self.files:
                continue
            seen.add(relative)
            for dependency in self._file_dependencies(relative):
                if dependency not in seen:
                    pending.append(dependency)
        return seen

    def _tracked_source_files(self) -> set[str]:
        process = subprocess.run(
            ["git", "ls-files", "*.py", "*.pyi", "*.js", "*.jsx", "*.ts", "*.tsx"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            return set()
        return {
            _normalized(line)
            for line in process.stdout.splitlines()
            if line.strip()
            and not _normalized(line).startswith(_IGNORED_PREFIXES)
        }

    def _python_module_files(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in self.files:
            if not relative.endswith((".py", ".pyi")):
                continue
            module = relative.rsplit(".", 1)[0].replace("/", ".")
            aliases = {module}
            if relative.startswith("src/"):
                aliases.add(module[4:])
            elif "/src/" in f"/{relative}":
                aliases.add(module.split(".src.", 1)[-1])
            for alias in aliases:
                result[alias] = relative
                if alias.endswith(".__init__"):
                    result[alias[: -len(".__init__")]] = relative
        return result

    def _file_dependencies(self, relative: str) -> set[str]:
        cached = self._dependencies.get(relative)
        if cached is not None:
            return cached
        path = self.project_root / relative
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self._dependencies[relative] = set()
            return set()
        if relative.endswith((".py", ".pyi")):
            dependencies = self._python_dependencies(relative, text)
        else:
            dependencies = self._javascript_dependencies(relative, text)
        self._dependencies[relative] = dependencies
        return dependencies

    def _python_dependencies(self, relative: str, text: str) -> set[str]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        current = relative.rsplit(".", 1)[0].replace("/", ".")
        if current.endswith(".__init__"):
            package = current[: -len(".__init__")]
        else:
            package = current.rpartition(".")[0]
        dependencies: set[str] = set()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    keep = max(0, len(parts) - node.level + 1)
                    base = ".".join([*parts[:keep], base]).strip(".")
                names.append(base)
                names.extend(
                    f"{base}.{alias.name}".strip(".") for alias in node.names
                )
            for name in names:
                candidate = name
                while candidate:
                    matched = self.module_files.get(candidate)
                    if matched:
                        dependencies.add(matched)
                    # Importing a submodule also executes its package initializers.
                    candidate = candidate.rpartition(".")[0]
        return dependencies

    def _javascript_dependencies(self, relative: str, text: str) -> set[str]:
        parent = Path(relative).parent
        dependencies: set[str] = set()
        for match in _JS_IMPORT.finditer(text):
            base = parent / match.group("path")
            candidates = [
                base,
                *[Path(f"{base}{suffix}") for suffix in (".ts", ".tsx", ".js", ".jsx")],
                *[base / f"index{suffix}" for suffix in (".ts", ".tsx", ".js", ".jsx")],
            ]
            for candidate in candidates:
                normalized = _normalized(candidate.as_posix())
                if normalized in self.files:
                    dependencies.add(normalized)
                    break
        return dependencies


@dataclass
class VerificationSelection:
    requested_level: str
    level: str
    steps: list[VerificationStep]
    changed_paths: list[str] = field(default_factory=list)
    mapped_paths: list[str] = field(default_factory=list)
    unmapped_paths: list[str] = field(default_factory=list)
    proof_ids: list[str] = field(default_factory=list)
    forced_release_reason: str = ""


def select_verification_steps(
    steps: Sequence[VerificationStep],
    project_root: Path,
    gate_config: GateConfig,
    *,
    level: str,
    changed_paths: Iterable[str] = (),
) -> VerificationSelection:
    requested_level = str(level).strip().lower()
    if requested_level not in {"affected", "release"}:
        raise ValueError(f"unsupported verification level: {level}")
    changed = list(dict.fromkeys(_normalized(path) for path in changed_paths if _normalized(path)))
    indexed = {step.proof_id: step for step in steps if step.proof_id}

    effective_level = requested_level
    forced_reason = ""
    if requested_level == "affected" and any(
        _matches(path, pattern)
        for path in changed
        for pattern in gate_config.release_blocking_paths
    ):
        effective_level = "release"
        forced_reason = "changed path matches release_blocking_paths"

    eligible = [step for step in steps if effective_level in _step_levels(step)]
    if effective_level == "release":
        selected = remove_release_target_overlap(eligible, steps)
        mapped = changed
        unmapped: list[str] = []
    elif not changed:
        selected = []
        mapped = []
        unmapped = []
    else:
        dependency_index = StaticDependencyIndex(project_root)
        selected = []
        mapped_set: set[str] = set()
        for step in eligible:
            declared = [*step.impact_paths, *[_target_file(item) for item in step.targets]]
            dependencies = dependency_index.closure_for_targets(step.targets)
            matched = {
                path
                for path in changed
                if any(_matches(path, pattern) for pattern in declared)
                or path in dependencies
            }
            if matched:
                selected.append(step)
                mapped_set.update(matched)
        # A changed test file is itself executable impact evidence. Reuse the
        # release step's runner/environment metadata but narrow it to that one
        # file, instead of falling back to unrelated smoke proofs.
        for path in changed:
            if not _is_test_path(path) or any(
                any(_target_file(target) == path for target in step.targets)
                for step in selected
            ):
                continue
            owner = next(
                (
                    step
                    for step in steps
                    if "release" in _step_levels(step)
                    and any(_target_file(target) == path for target in step.targets)
                ),
                None,
            )
            if owner is None:
                continue
            digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:10]
            selected.append(
                replace(
                    owner,
                    proof_id=f"affected.changed-test.{digest}",
                    levels=["affected"],
                    cadence="implement_and_final",
                    impact_paths=[path],
                    targets=[path],
                    depends_on_proofs=[],
                )
            )
            mapped_set.add(path)
        mapped = [path for path in changed if path in mapped_set]
        unmapped = [path for path in changed if path not in mapped_set]
        if unmapped:
            if gate_config.unmapped_change_policy == "release":
                effective_level = "release"
                selected = remove_release_target_overlap(
                    [step for step in steps if "release" in _step_levels(step)],
                    steps,
                )
                forced_reason = "changed paths are outside the declared/static impact graph"
                mapped = changed
                unmapped = []
            elif gate_config.unmapped_change_policy == "fallback":
                selected.extend(
                    indexed[proof_id]
                    for proof_id in gate_config.fallback_proof_ids
                    if proof_id in indexed and indexed[proof_id] not in selected
                )

    selected = _include_dependencies(selected, indexed)
    if effective_level == "affected" and any(step.risk == "critical" for step in selected):
        effective_level = "release"
        selected = remove_release_target_overlap(
            [step for step in steps if "release" in _step_levels(step)],
            steps,
        )
        forced_reason = "affected proof is classified critical"
    proof_ids = list(dict.fromkeys(step.proof_id for step in selected if step.proof_id))
    return VerificationSelection(
        requested_level=requested_level,
        level=effective_level,
        steps=selected,
        changed_paths=changed,
        mapped_paths=mapped,
        unmapped_paths=unmapped,
        proof_ids=proof_ids,
        forced_release_reason=forced_reason,
    )


def _step_levels(step: VerificationStep) -> set[str]:
    if step.levels:
        return {item.strip().lower() for item in step.levels}
    return (
        {"release"}
        if step.cadence.strip().lower() == "final_only"
        else {"affected", "release"}
    )


def _is_test_path(path: str) -> bool:
    name = Path(path).name.lower()
    return (
        path.startswith(("tests/", "test/", "workbench/src/"))
        and (
            name.startswith("test_")
            or ".test." in name
            or ".spec." in name
        )
    )


def _include_dependencies(
    selected: Sequence[VerificationStep],
    indexed: dict[str, VerificationStep],
) -> list[VerificationStep]:
    result = list(selected)
    present = {step.proof_id for step in result}
    cursor = 0
    while cursor < len(result):
        step = result[cursor]
        cursor += 1
        for proof_id in step.depends_on_proofs:
            dependency = indexed.get(proof_id)
            if dependency is not None and proof_id not in present:
                result.append(dependency)
                present.add(proof_id)
    order = {proof_id: index for index, proof_id in enumerate(indexed)}
    return sorted(result, key=lambda item: order.get(item.proof_id, len(order)))


def remove_release_target_overlap(
    release_steps: Sequence[VerificationStep],
    all_steps: Sequence[VerificationStep],
) -> list[VerificationStep]:
    """Do not physically execute whole-file affected proofs again in release."""
    covered = {
        _target_file(target)
        for step in all_steps
        if "affected" in _step_levels(step)
        for target in step.targets
        if "::" not in target
    }
    covered_selectors: dict[str, set[str]] = {}
    for step in all_steps:
        if "affected" not in _step_levels(step):
            continue
        for target in step.targets:
            if "::" in target:
                covered_selectors.setdefault(_target_file(target), set()).add(target)
    result: list[VerificationStep] = []
    for step in release_steps:
        remaining = [
            target for target in step.targets if _target_file(target) not in covered
        ]
        args = list(step.args)
        if step.runner.strip().lower() == "pytest":
            for target in remaining:
                for selector in sorted(covered_selectors.get(_target_file(target), set())):
                    deselect = f"--deselect={selector}"
                    if deselect not in args:
                        args.append(deselect)
        if step.targets and not remaining:
            continue
        if remaining == step.targets and args == step.args:
            result.append(step)
            continue
        digest = hashlib.sha256(
            json.dumps(
                {"targets": sorted(remaining), "args": args},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:10]
        result.append(
            replace(
                step,
                proof_id=f"{step.proof_id}.remaining-{digest}",
                targets=remaining,
                args=args,
            )
        )
    return result
