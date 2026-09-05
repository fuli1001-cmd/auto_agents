from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.models import GateConfig, VerificationStep
from auto_agents.verification_selection import StaticDependencyIndex, select_verification_steps


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src" / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app" / "service.py").write_text("VALUE = 1\n")
    (root / "tests" / "test_service.py").write_text(
        "from app.service import VALUE\n\ndef test_value(): assert VALUE == 1\n"
    )
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


def _step(proof_id: str, *, levels: list[str], impact_paths: list[str]) -> VerificationStep:
    return VerificationStep(
        proof_id=proof_id,
        runner="pytest",
        targets=["tests/test_service.py"],
        levels=levels,
        impact_paths=impact_paths,
        parallel_safe=True,
        cache_scope="source",
    )


def test_affected_selection_uses_static_reverse_imports(tmp_path: Path) -> None:
    root = _project(tmp_path)
    selected = select_verification_steps(
        [_step("service.contract", levels=["affected"], impact_paths=["tests/**"])],
        root,
        GateConfig(verification_policy_version=4),
        level="affected",
        changed_paths=["src/app/service.py"],
    )
    assert selected.proof_ids == ["service.contract"]
    assert selected.unmapped_paths == []


def test_unmapped_changes_add_only_fallback_proofs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    fallback = _step("core.smoke", levels=["affected"], impact_paths=["tests/**"])
    selected = select_verification_steps(
        [fallback],
        root,
        GateConfig(
            verification_policy_version=4,
            fallback_proof_ids=["core.smoke"],
            unmapped_change_policy="fallback",
        ),
        level="affected",
        changed_paths=["unknown/config.bin"],
    )
    assert selected.proof_ids == ["core.smoke"]
    assert selected.unmapped_paths == ["unknown/config.bin"]


def test_release_blocking_path_escalates_to_release(tmp_path: Path) -> None:
    root = _project(tmp_path)
    affected = _step("service.contract", levels=["affected"], impact_paths=["src/**"])
    release = _step("release.full", levels=["release"], impact_paths=[])
    release.targets = ["tests/test_release.py"]
    selected = select_verification_steps(
        [affected, release],
        root,
        GateConfig(
            verification_policy_version=4,
            release_blocking_paths=["src/app/service.py"],
        ),
        level="affected",
        changed_paths=["src/app/service.py"],
    )
    assert selected.level == "release"
    assert selected.proof_ids == ["release.full"]


def test_proof_dependencies_are_included_once(tmp_path: Path) -> None:
    root = _project(tmp_path)
    base = _step("schema.contract", levels=["affected"], impact_paths=["schema/**"])
    api = _step("api.contract", levels=["affected"], impact_paths=["src/api/**"])
    api.depends_on_proofs = ["schema.contract"]
    selected = select_verification_steps(
        [base, api],
        root,
        GateConfig(verification_policy_version=4),
        level="affected",
        changed_paths=["src/api/routes.py"],
    )
    assert selected.proof_ids == ["schema.contract", "api.contract"]


def test_python_package_reexports_are_included_in_dependency_closure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "src/app/__init__.py").write_text("from .service import VALUE\n")
    (root / "tests/test_service.py").write_text("from app import VALUE\n")
    _git(root, "add", "-A")

    dependencies = StaticDependencyIndex(root).closure_for_targets(["tests/test_service.py"])

    assert dependencies == {
        "tests/test_service.py", "src/app/__init__.py", "src/app/service.py"
    }
    selected = select_verification_steps(
        [_step("service.contract", levels=["affected"], impact_paths=["tests/**"])],
        root,
        GateConfig(verification_policy_version=4),
        level="affected",
        changed_paths=["src/app/service.py"],
    )
    assert selected.proof_ids == ["service.contract"]
    assert selected.unmapped_paths == []


def test_python_submodule_import_includes_package_initializers(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "src/app/__init__.py").write_text("from .settings import configure\nconfigure()\n")
    (root / "src/app/settings.py").write_text("def configure(): pass\n")
    _git(root, "add", "-A")

    dependencies = StaticDependencyIndex(root).closure_for_targets(["tests/test_service.py"])

    assert dependencies == {
        "tests/test_service.py", "src/app/service.py", "src/app/__init__.py",
        "src/app/settings.py",
    }


def test_javascript_parent_imports_expand_transitive_impact(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "src/app/service.ts").write_text("export const value = 1;\n")
    (root / "src/app/index.ts").write_text("export { value } from './service';\n")
    (root / "tests/service.test.ts").write_text("import { value } from '../src/app';\n")
    _git(root, "add", "-A")

    dependencies = StaticDependencyIndex(root).closure_for_targets(["tests/service.test.ts"])

    assert dependencies == {
        "tests/service.test.ts", "src/app/index.ts", "src/app/service.ts"
    }
