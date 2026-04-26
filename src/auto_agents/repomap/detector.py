"""Project eligibility detection for the repo map feature."""
from __future__ import annotations

from pathlib import Path
from typing import Tuple


def is_python_project(
    project_root: Path,
    *,
    min_py_files: int = 3,
    min_py_ratio: float = 0.10,
    max_scan: int = 5000,
) -> Tuple[bool, str]:
    """Heuristic: treat the project as Python-eligible when either:

    - obvious Python signals exist (pyproject.toml / setup.py / .conda), or
    - at least `min_py_files` .py files AND >= `min_py_ratio` of scanned files.

    Returns (eligible, reason). The reason string is suitable for metrics.
    """
    root = Path(project_root)
    if not root.is_dir():
        return False, "no_project_root"

    # Hard signals first - cheap and decisive.
    for marker in ("pyproject.toml", "setup.py", "setup.cfg"):
        if (root / marker).is_file():
            return True, f"marker:{marker}"
    if (root / ".conda").is_dir():
        return True, "marker:.conda"

    py_count = 0
    other_count = 0
    scanned = 0
    skip_dirs = {
        ".git", ".auto-agents", ".conda", ".conda-pkgs", ".venv", "venv",
        "node_modules", "__pycache__", "build", "dist",
    }
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        scanned += 1
        if scanned > max_scan:
            break
        if path.suffix == ".py":
            py_count += 1
        else:
            other_count += 1

    if py_count == 0:
        return False, "no_python_files"
    total = py_count + other_count
    if py_count < min_py_files:
        return False, f"too_few_python_files:{py_count}"
    ratio = py_count / total if total else 0.0
    if ratio < min_py_ratio:
        return False, f"low_python_ratio:{ratio:.2f}"
    return True, f"python_files:{py_count}"
