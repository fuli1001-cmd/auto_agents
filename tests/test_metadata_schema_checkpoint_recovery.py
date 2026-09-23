"""Synthetic reproductions of the metadata/ignored-dependency recovery incident.

Git, persistence detection and checkpoint publication/restoration are real. No
provider requests, live workflow recovery or operator repositories are used.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import load_run_state, save_run_state
from auto_agents.git_ops import commit_all_except
from auto_agents.models import RunState, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.persistence import detect_persistence_schema_changes


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True,
    ).stdout


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _project(root: Path) -> Orchestrator:
    Orchestrator.init_project(root, "checkpoint-regression", "mock")
    _git(root, "config", "user.name", "Checkpoint Test")
    _git(root, "config", "user.email", "checkpoint@example.invalid")
    return Orchestrator(root)


def _commit(root: Path) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "synthetic fixture baseline")
    return _git(root, "rev-parse", "HEAD").decode().strip()


@pytest.mark.parametrize("metadata_state", ["tracked", "staged", "untracked"])
def test_retained_metadata_sql_does_not_require_persistence_strategy(
    tmp_path: Path, metadata_state: str,
) -> None:
    root = tmp_path / "project"
    orchestrator = _project(root)
    metadata = ".auto-agents/state/repair-scope-evidence/synthetic-diagnostic.json"
    if metadata_state != "untracked":
        _write(root, metadata, '{"diagnostic": "before"}\n')
    _write(root, "app/db.py", "VALUE = 1\n")
    _commit(root)
    _write(root, metadata, '{"diagnostic": "quoted DROP TABLE projects"}\n')
    if metadata_state == "staged":
        _git(root, "add", "--", metadata)
    task = TaskSpec(
        "task-metadata", "Metadata regression", "Synthetic fixture", [],
        persistence_change={"storage_transition": "none"},
    )

    assert detect_persistence_schema_changes(root) == []
    assert orchestrator._persistence_contract_issue(task) == ""

    for relative in (
        metadata, "./" + metadata, "././" + metadata,
        metadata.replace("/", "\\"),
        ("././" + metadata).replace("/", "\\"),
        "docs/schema.sql", "specs/schema.sql", "tests/test_db.py",
        "README.md", "DESIGN.md",
    ):
        assert detect_persistence_schema_changes(
            root, diff_text=f"+++ b/{relative}\n+quoted DROP TABLE projects\n",
        ) == [], relative

    # Each positive control is independently visible to both the detector and
    # the actual guard, even while the SQL-quoting metadata remains present.
    for relative, content in (
        ("app/db.py", "connection.execute('ALTER TABLE projects ADD COLUMN n INTEGER')\n"),
        ("migrations/002.sql", "SELECT 1;\n"),
        ("auto-agents/evidence.json", '{"quote": "DROP TABLE projects"}\n'),
        (".auto-agents-other/schema.sql", "SELECT 1;\n"),
    ):
        _write(root, relative, content)
        findings = detect_persistence_schema_changes(root)
        assert findings and {finding.path for finding in findings} == {relative}
        assert "user-approved persistence strategy" in orchestrator._persistence_contract_issue(task)
        if relative == "app/db.py":
            _write(root, relative, "VALUE = 1\n")
        else:
            (root / relative).unlink()
    assert orchestrator._persistence_contract_issue(task) == ""


def _dependencies(root: Path, kind: str) -> None:
    for relative in (".conda", ".venv", "workbench/node_modules"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "symlink":
            # Also prove that exclusion does not resolve self-referential links.
            path.symlink_to(path)
        else:
            _write(root, relative + "/marker.txt", "dependency must survive\n")


@pytest.mark.parametrize("dependency_kind", ["directory", "symlink"])
def test_ignored_dependency_roots_do_not_prevent_checkpoint_publication(
    tmp_path: Path, dependency_kind: str,
) -> None:
    root = tmp_path / "project"
    orchestrator = _project(root)
    _write(root, ".gitignore", ".conda\n.venv\nworkbench/node_modules\n")
    _write(root, "workbench/package-lock.json", "{}\n")
    metadata = ".auto-agents/state/synthetic-retained.json"
    _write(root, metadata, '{"diagnostic": "baseline"}\n')
    for relative in ("app/db.py", "updated.txt", "deleted.txt", "staged-deleted.txt", "unrelated.txt"):
        _write(root, relative, "baseline\n")
    base = _commit(root)
    _dependencies(root, dependency_kind)
    _write(root, metadata, '{"diagnostic": "quoted DROP TABLE projects"}\n')
    _git(root, "add", "--", metadata)
    _write(root, ".antigravitycli/new-evidence.txt", "keep excluded working content\n")
    _git(root, "add", "--", ".antigravitycli/new-evidence.txt")
    _write(root, "updated.txt", "staged intermediate\n")
    _git(root, "add", "--", "updated.txt")
    expected = {
        "app/db.py": "connection.execute('CREATE TABLE example (id INTEGER)')\n",
        "updated.txt": "final unstaged content\n",
        "literal[1]*.txt": "literal candidate\n",
        "two words.txt": "another candidate\n",
    }
    for relative, content in expected.items():
        _write(root, relative, content)
    (root / "deleted.txt").unlink()
    _git(root, "rm", "--", "staged-deleted.txt")
    task = TaskSpec(
        "task-checkpoint", "Checkpoint regression", "Synthetic fixture", [],
        status="in_progress", verify_retry_epoch=1,
        persistence_change={"storage_transition": "none"},
    )
    state = RunState(run_id="run-checkpoint", current_stage="implement", tasks=[task])
    state.agent_attempts = {"implement-task-checkpoint": 2}
    state.resume_context["workflow_id"] = "wf-checkpoint"
    observations = []

    def legitimate_guard_failure(*args, **kwargs):
        issue = orchestrator._persistence_contract_issue(task)
        assert "app/db.py" in issue
        assert metadata not in issue
        return {
            "ok": False, "review": issue, "reason": issue,
            "failure_ids": ["persistence_schema_strategy_missing"],
            "comparable_failures": False, "rewind_to_stage": "clarify",
        }

    def observe_rewind(saved, owner, tasks, result, stage):
        assert saved is state and owner is task and stage == "clarify"
        checkpoint = saved.task_failure_checkpoints[task.task_id]
        assert checkpoint["status"] == "recoverable"
        assert checkpoint["has_candidate_changes"]
        assert _git(root, "rev-parse", checkpoint["ref"]).decode().strip() == checkpoint["commit_sha"]
        assert set(checkpoint["changed_paths"]) == set(expected) | {"deleted.txt", "staged-deleted.txt"}
        assert not task.commit_sha and task.status != "done"
        save_run_state(root, saved)
        assert load_run_state(root).task_failure_checkpoints[task.task_id]["ref"] == checkpoint["ref"]
        observations.append(checkpoint["ref"])
        return saved

    # Only unrelated preflight/baseline scheduling and agent work are stubbed.
    # The main-worktree branch, Git commit/ref publication and restore are real.
    with (
        patch.object(orchestrator, "_route_frontend_design_contract_prerequisite", return_value=None),
        patch.object(orchestrator, "_ensure_evidence_preflight", return_value=None),
        patch.object(orchestrator, "_ensure_task_verify_baseline", return_value=False),
        patch.object(orchestrator, "_execute_task_with_retries", side_effect=legitimate_guard_failure),
        patch.object(orchestrator, "_handle_review_stage_rewind", side_effect=observe_rewind),
    ):
        assert orchestrator._execute_task_in_main_worktree(state, [task], task) is state

    assert len(observations) == 1
    checkpoint = state.task_failure_checkpoints[task.task_id]
    sha = checkpoint["commit_sha"]
    assert set(_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha).split(b"\0")) - {b""} == {
        name.encode() for name in (*expected, "deleted.txt", "staged-deleted.txt")
    }
    assert _git(root, "show", f"{sha}:{metadata}") == b'{"diagnostic": "baseline"}\n'
    assert (root / metadata).read_text() == '{"diagnostic": "quoted DROP TABLE projects"}\n'
    assert (root / ".antigravitycli/new-evidence.txt").read_text() == "keep excluded working content\n"
    assert state.agent_attempts == {"implement-task-checkpoint": 2}
    assert state.resume_context["workflow_id"] == "wf-checkpoint"

    restore_root = tmp_path / "restored"
    _git(root, "worktree", "add", "--detach", str(restore_root), base)
    try:
        _dependencies(restore_root, dependency_kind)
        _write(restore_root, "unrelated.txt", "unrelated staged\n")
        _git(restore_root, "add", "--", "unrelated.txt")
        _write(restore_root, "unrelated.txt", "unrelated unstaged\n")
        _write(restore_root, "unrelated-new.txt", "untracked work\n")
        staged_before = _git(restore_root, "diff", "--cached", "--", "unrelated.txt")
        restored_state = copy.deepcopy(state)
        restorer = Orchestrator(restore_root)
        assert restorer._restore_task_failure_checkpoint(restored_state, task, restore_root) == checkpoint["ref"]
        for relative, content in expected.items():
            assert (restore_root / relative).read_text() == content
        assert not (restore_root / "deleted.txt").exists()
        assert not (restore_root / "staged-deleted.txt").exists()
        assert (restore_root / "unrelated.txt").read_text() == "unrelated unstaged\n"
        assert (restore_root / "unrelated-new.txt").read_text() == "untracked work\n"
        assert _git(restore_root, "diff", "--cached", "--", "unrelated.txt") == staged_before
        for relative in (".conda", ".venv", "workbench/node_modules"):
            if dependency_kind == "symlink":
                assert (restore_root / relative).is_symlink()
            else:
                assert (restore_root / relative / "marker.txt").read_text() == "dependency must survive\n"
        assert not restored_state.tasks[0].commit_sha
    finally:
        _git(root, "worktree", "remove", "--force", str(restore_root))


@pytest.mark.parametrize("unborn", [False, True])
def test_commit_exclusions_preserve_literal_names_and_index_changes(
    tmp_path: Path, unborn: bool,
) -> None:
    root = tmp_path / "project"
    _project(root)
    _write(root, ".gitignore", ".venv\n")
    _write(root, "updated.txt", "before\n")
    _write(root, "deleted.txt", "delete me\n")
    _write(root, "old.txt", "rename me\n")
    _write(root, "vendor/[cache]/tracked.txt", "excluded baseline\n")
    if not unborn:
        _commit(root)
    else:
        _git(root, "add", "-A")
    _write(root, "updated.txt", "after\n")
    (root / "deleted.txt").unlink()
    _git(root, "mv", "old.txt", "renamed.txt")
    names = [" leading.txt", "trailing.txt ", "line\nbreak.txt", "literal[1]*.txt", ":(exclude)evil", "-dash.txt"]
    for name in names:
        _write(root, name, "literal content\n")
    _write(root, "vendor/[cache]/tracked.txt", "excluded working content\n")
    _git(root, "--literal-pathspecs", "add", "--", "vendor/[cache]/tracked.txt")
    # This lookalike must not be swallowed by a glob or a loose prefix match.
    _write(root, "vendor/c/eligible.txt", "keep sibling\n")
    (root / ".venv").symlink_to(root / ".venv")
    sha = commit_all_except(root, "synthetic candidate", (".auto-agents", "vendor/[cache]", ".venv"))
    tree = set(_git(root, "ls-tree", "-r", "--name-only", "-z", sha).split(b"\0"))
    for name in names:
        assert name.encode() in tree
        assert _git(root, "show", f"{sha}:{name}") == b"literal content\n"
    assert b"deleted.txt" not in tree and b"old.txt" not in tree
    assert _git(root, "show", f"{sha}:updated.txt") == b"after\n"
    assert _git(root, "show", f"{sha}:renamed.txt") == b"rename me\n"
    assert b"vendor/c/eligible.txt" in tree and b".venv" not in tree
    if unborn:
        assert b"vendor/[cache]/tracked.txt" not in tree
    else:
        assert _git(root, "show", f"{sha}:vendor/[cache]/tracked.txt") == b"excluded baseline\n"
    assert (root / "vendor/[cache]/tracked.txt").read_text() == "excluded working content\n"


@pytest.mark.parametrize("failure", ["commit", "publication"])
def test_checkpoint_failure_never_dispatches_rewind(tmp_path: Path, failure: str) -> None:
    root = tmp_path / "project"
    orchestrator = _project(root)
    _write(root, "app.py", "before\n")
    _commit(root)
    _write(root, "app.py", "candidate\n")
    task = TaskSpec("task-failure", "Failure", "Synthetic fixture", [], status="in_progress")
    state = RunState(run_id="run-failure", current_stage="implement", tasks=[task])
    target = "commit_all_except" if failure == "commit" else "update_ref"
    with (
        patch.object(orchestrator, "_route_frontend_design_contract_prerequisite", return_value=None),
        patch.object(orchestrator, "_ensure_evidence_preflight", return_value=None),
        patch.object(orchestrator, "_ensure_task_verify_baseline", return_value=False),
        patch.object(orchestrator, "_execute_task_with_retries", return_value={
            "ok": False, "review": "legitimate clarification", "rewind_to_stage": "clarify",
        }),
        patch(f"auto_agents.orchestrator.{target}", side_effect=RuntimeError("synthetic Git failure")),
        patch.object(orchestrator, "_handle_review_stage_rewind") as dispatch,
    ):
        with pytest.raises(RuntimeError, match="synthetic Git failure"):
            orchestrator._execute_task_in_main_worktree(state, [task], task)
        dispatch.assert_not_called()
    assert task.task_id not in state.task_failure_checkpoints
    assert not task.commit_sha and task.status != "done"
    assert (root / "app.py").read_text() == "candidate\n"
