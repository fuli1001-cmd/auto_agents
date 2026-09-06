"""Review effort follows actual Git changes, not the size of edited files."""

import subprocess

import pytest

from auto_agents.git_ops import changed_line_count
from auto_agents.models import ProjectConfig, TaskSpec
from auto_agents.orchestrator import Orchestrator


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
    ).stdout


def lines(count):
    return "".join(f"value_{i} = {i}\n" for i in range(count))


@pytest.fixture
def review(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Review Test")
    git(tmp_path, "config", "user.email", "review@example.com")
    (tmp_path / "app.py").write_text(lines(1000))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("assert True\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    (tmp_path / "tests/test_app.py").write_text("assert 1 == 1\n")
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.project_root = tmp_path
    orchestrator.config = ProjectConfig(project_name="review-test")
    orchestrator.config.execution.evidence_preflight.mode = "off"
    return orchestrator, TaskSpec("task-001", "Update app", "Adjust behavior", [])


@pytest.mark.parametrize("staging", ["unstaged", "staged", "mixed"])
def test_small_edit_in_large_file_stays_balanced(review, staging):
    orchestrator, task = review
    root = orchestrator.project_root
    (root / "app.py").write_text(lines(1000).replace("value_0 = 0", "value_0 = 42"))
    if staging != "unstaged":
        git(root, "add", "app.py")
    if staging == "mixed":
        (root / "app.py").write_text(lines(1000).replace("value_0 = 0", "value_0 = 43"))
    before = git(root, "diff", "--cached")
    assert changed_line_count(root, ["app.py"]) == 2
    assert orchestrator._review_effort_for_task(task) == "balanced"
    assert git(root, "diff", "--cached") == before


@pytest.mark.parametrize("count, expected", [(120, "balanced"), (121, "deep")])
def test_threshold_counts_additions_plus_deletions(review, count, expected):
    orchestrator, task = review
    root = orchestrator.project_root
    unchanged = "".join(lines(1000).splitlines(keepends=True)[count:])
    (root / "app.py").write_text(lines(count).replace("value_", "updated_") + unchanged)
    assert changed_line_count(root, ["app.py"]) == count * 2
    assert orchestrator._review_effort_for_task(task) == expected


@pytest.mark.parametrize("change", ["delete_file", "delete_lines", "untracked", "staged_addition"])
def test_large_deletions_and_additions_escalate(review, change):
    orchestrator, task = review
    root = orchestrator.project_root
    if change == "delete_file":
        (root / "app.py").unlink()
    elif change == "delete_lines":
        (root / "app.py").write_text(lines(10))
    else:
        (root / "new.py").write_text(lines(241))
        if change == "staged_addition":
            git(root, "add", "new.py")
    assert orchestrator._review_effort_for_task(task) == "deep"


def test_pure_rename_with_special_filename_stays_balanced(review):
    orchestrator, task = review
    root = orchestrator.project_root
    destination = "renamed 数据\tfile\nname.py"
    git(root, "mv", "app.py", destination)
    assert changed_line_count(root, [destination]) == 0
    assert orchestrator._review_effort_for_task(task) == "balanced"
    (root / destination).write_text(lines(1000).replace("value_0 = 0", "value_0 = 42"))
    assert changed_line_count(root, [destination]) == 2
    assert orchestrator._review_effort_for_task(task) == "balanced"


def test_staged_edit_reverted_in_worktree_is_not_double_counted(review):
    orchestrator, task = review
    root = orchestrator.project_root
    (root / "app.py").write_text("replaced\n")
    git(root, "add", "app.py")
    (root / "app.py").write_text(lines(1000))
    assert changed_line_count(root, ["app.py"]) == 0
    assert orchestrator._review_effort_for_task(task) == "balanced"


@pytest.mark.parametrize("tracked", [False, True])
def test_binary_changes_escalate(review, tracked):
    orchestrator, task = review
    root = orchestrator.project_root
    name = "app.py" if tracked else "asset.bin"
    (root / name).write_bytes(b"binary\0data")
    assert orchestrator._review_effort_for_task(task) == "deep"


def test_large_test_diff_does_not_count_toward_code_threshold(review):
    orchestrator, task = review
    root = orchestrator.project_root
    (root / "app.py").write_text(lines(1000) + "new_value = 1\n")
    (root / "tests/test_app.py").write_text(lines(1000))
    assert orchestrator._review_effort_for_task(task) == "balanced"


@pytest.mark.parametrize("staged", [False, True])
def test_unborn_repository_counts_additions(tmp_path, staged):
    git(tmp_path, "init", "-q")
    (tmp_path / "new.py").write_text("first\nlast")
    if staged:
        git(tmp_path, "add", "new.py")
    assert changed_line_count(tmp_path, ["new.py"]) == 2


def test_unavailable_diff_escalates(review, monkeypatch):
    orchestrator, task = review
    root = orchestrator.project_root
    (root / "app.py").write_text(lines(1000) + "new_value = 1\n")
    monkeypatch.setattr(
        "auto_agents.git_ops._git_bytes",
        lambda *args: subprocess.CompletedProcess(args, 1, b"", b"diff failed"),
    )
    assert orchestrator._review_effort_for_task(task) == "deep"


@pytest.mark.parametrize("condition", ["no_tests", "high_risk", "many_files", "evidence", "retry", "explicit_max"])
def test_other_escalation_conditions_remain_effective(review, condition):
    orchestrator, task = review
    root = orchestrator.project_root
    (root / "app.py").write_text(lines(1000) + "new_value = 1\n")
    if condition == "no_tests":
        (root / "tests/test_app.py").write_text("assert True\n")
    elif condition == "high_risk":
        (root / "pyproject.toml").write_text("[project]\n")
    elif condition == "many_files":
        for i in range(3):
            (root / f"extra_{i}.py").write_text("pass\n")
    elif condition == "evidence":
        orchestrator.config.execution.evidence_preflight.mode = "all"
    elif condition == "retry":
        task.review_history = [{"summary": "Missing validation"}, {"summary": "Broken persistence"}]
    else:
        orchestrator.config.efforts["review"] = "max"
    assert orchestrator._review_effort_for_task(task) == ("max" if condition == "explicit_max" else "deep")


@pytest.mark.parametrize("staging", ["unstaged", "staged", "mixed"])
def test_review_context_includes_final_diff_against_head(review, staging):
    orchestrator, _ = review
    root = orchestrator.project_root
    (root / "app.py").write_text(lines(1000).replace("value_0 = 0", "value_0 = 42"))
    if staging != "unstaged":
        git(root, "add", "app.py")
    if staging == "mixed":
        (root / "app.py").write_text(lines(1000).replace("value_0 = 0", "value_0 = 43"))
    context = orchestrator._build_review_context()
    assert "-value_0 = 0" in context
    assert ("+value_0 = 43" if staging == "mixed" else "+value_0 = 42") in context
    if staging == "mixed":
        assert "-value_0 = 42" not in context


def test_review_context_marks_missing_excerpts_and_keeps_proof(review):
    orchestrator, _ = review
    root = orchestrator.project_root
    (root / "new.py").write_text(lines(1000))
    context = orchestrator._build_review_context("Authoritative verification passed.", max_diff_chars=50)
    assert "Authoritative verification passed." in context
    assert "[diff truncated]" in context
    assert "excerpt truncated: read the complete file at new.py" in context


def test_review_context_in_unborn_repository_includes_staged_additions(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "new.py").write_text("important_value = 42\n")
    git(tmp_path, "add", "new.py")
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.project_root = tmp_path
    context = orchestrator._build_review_context()
    assert "No HEAD commit" in context
    assert "important_value = 42" in context


@pytest.mark.parametrize("kind", ["deleted", "renamed", "binary"])
def test_review_context_describes_nonstandard_changes(review, kind):
    orchestrator, _ = review
    root = orchestrator.project_root
    if kind == "deleted":
        (root / "app.py").unlink()
        expected = "deleted file mode"
    elif kind == "renamed":
        git(root, "mv", "app.py", "renamed.py")
        expected = "rename to renamed.py"
    else:
        (root / "app.py").write_bytes(b"binary\0data")
        expected = "Binary files"
    assert expected in orchestrator._build_review_context()
