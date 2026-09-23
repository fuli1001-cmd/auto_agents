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
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore


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


def _ready_metadata_repair_scene(tmp_path: Path):
    """Model the retained run: ready attempt 1, unavailable checkpoint 0."""
    root = tmp_path / "project"
    orch = _project(root)
    _write(root, "app.py", "VALUE = 1\n")
    _write(root, "spec.md", "Synthetic independent iteration\n")
    _write(root, ".auto-agents/state/sessions/stopped/session_state.json", '{"status":"stopped"}\n')
    base = _commit(root)
    owner = TaskSpec(
        "task-pp-07", "Retained candidate", "Verify the existing work", ["Keep the contract"],
        status="in_progress", verify_retry_epoch=1,
        persistence_change={"storage_transition": "none"},
        verification_refs=["tests/test_candidate.py::test_contract"],
        verify_history=[{"attempt": 1, "decision": "pass", "candidate_fingerprint": "earlier"}],
        review_history=[{"attempt": 1, "summary": "Earlier review"}],
    )
    other = TaskSpec("task-pp-08", "Separate obligation", "Keep pending", ["Publish its own evidence"])
    state = RunState(run_id="82288622684f", current_stage="implement", tasks=[owner, other])
    workflow = WorkflowStore(root).create_root(WorkflowRef("run", state.run_id))
    state.resume_context.update(
        workflow_id=workflow.workflow_id, spec_file=str(root / "spec.md"),
        parallel_sequential_retry_tasks=[owner.task_id, other.task_id],
    )
    state.stage_summaries = {stage: "Retained accepted evidence" for stage in ("clarify", "design", "plan", "provider_research")}
    state.agent_attempts = {"plan": 3, "implement-task-pp-07": 2}
    state.task_review_cache[owner.task_id] = {"decision": "pass", "fingerprint": "earlier"}
    state.task_failure_checkpoints = {
        task.task_id: {
            "task_id": task.task_id, "status": "unavailable", "ref": "",
            "has_candidate_changes": False, "changed_paths": [],
            "verify_retry_epoch": 0, "commit_sha": base, "base_ref": base,
            "reason": "ignored dependency rejected checkpoint staging",
        } for task in state.tasks
    }
    state.last_recovery_route = {"outcome": "publication_pending", "other_task": other.task_id}
    orch._persist_tasks(state.tasks)
    _write(root, "app.py", "VALUE = 2\n")
    _write(root, ".auto-agents/state/synthetic-diagnostic.json", '{"quote":"DROP TABLE example"}\n')
    orch._set_task_attempt_base_ref(state, owner, base)
    orch._set_implementation_ready_marker(state, owner, True)
    orch._block_run(
        state, owner="auto_agents", category="metadata_schema_false_positive_and_checkpoint_failure",
        reason="metadata guard and checkpoint staging failed", fingerprint="retained-failure",
    )
    save_run_state(root, state)
    return root, orch, state, owner


@pytest.mark.parametrize("partially_staged", [False, True])
def test_applied_metadata_repair_reaches_fresh_verification_without_resetting_history(
    tmp_path: Path, partially_staged: bool,
) -> None:
    root, orch, original, owner = _ready_metadata_repair_scene(tmp_path)
    if partially_staged:
        # The failed add can change index status without changing candidate bytes.
        _git(root, "add", "--", "app.py")
    plan_path = root / ".auto-agents/state/task_plan.json"
    protected = [plan_path, root / "spec.md", root / "app.py",
                 root / ".auto-agents/state/sessions/stopped/session_state.json"]
    before_bytes = {p: p.read_bytes() for p in protected}
    index_before = _git(root, "ls-files", "--stage", "-z")
    before = copy.deepcopy(original.to_dict())
    with patch.object(orch, "_call_with_failover", side_effect=AssertionError("no provider in recovery")):
        # Exactly the pinned boundary's preparation and resume calls.
        state = orch.mark_self_repair_applied("verified-engine-candidate")
        assert orch._resume_blocked_run(state)
    assert state.run_id == original.run_id and state.status == "pending"
    assert state.current_stage == "implement" and not state.active_blocker
    receipt = state.last_recovery_route["metadata_checkpoint_repair"]
    assert receipt["task_id"] == owner.task_id and receipt["outcome"] == "verification_ready"
    assert receipt["repaired_blocker"]["fingerprint"] == original.active_blocker["fingerprint"]
    assert receipt["superseded_checkpoint"] == before["task_failure_checkpoints"][owner.task_id]
    assert receipt["previous_review_cache"] == before["task_review_cache"][owner.task_id]
    assert owner.task_id not in state.task_failure_checkpoints
    assert state.task_failure_checkpoints["task-pp-08"] == before["task_failure_checkpoints"]["task-pp-08"]
    assert state.agent_attempts == original.agent_attempts
    assert state.stage_summaries == original.stage_summaries
    assert [task.to_dict() for task in state.tasks] == before["tasks"]
    assert state.resume_context == original.resume_context
    assert {p: p.read_bytes() for p in protected} == before_bytes
    assert _git(root, "ls-files", "--stage", "-z") == index_before
    assert WorkflowStore(root).load(receipt["workflow_id"]).root.native_id == state.run_id

    restarted = Orchestrator(root)
    state = load_run_state(root)
    snapshot = copy.deepcopy(state.to_dict())
    assert not restarted._resume_blocked_run(state)
    assert state.to_dict() == snapshot  # Repeated resume does not repeat preparation.
    reached = []

    class VerificationEntered(Exception):
        pass

    def managed_verification(task, *, state):
        reached.append((state.run_id, task.task_id, task.verification_refs))
        assert state.resume_context["implementation_ready_tasks"][task.task_id]
        assert not task.commit_sha and task.status == "in_progress"
        assert task.task_id not in state.task_review_cache
        raise VerificationEntered()

    with (
        patch.object(restarted, "_route_frontend_design_contract_prerequisite", return_value=None),
        patch.object(restarted, "_ensure_evidence_preflight", return_value=None),
        patch.object(restarted, "_ensure_task_verify_baseline", return_value=False),
        patch.object(restarted, "_quick_verify_failure_details", return_value=None),
        patch.object(restarted, "_run_task_verify", side_effect=managed_verification),
        patch.object(restarted, "_run_agent_with_retries", side_effect=AssertionError("do not regenerate implementation")),
    ):
        with pytest.raises(VerificationEntered):
            restarted._execute_task_in_main_worktree(state, state.tasks, state.tasks[0])
    assert reached == [(original.run_id, owner.task_id, owner.verification_refs)]
    assert state.agent_attempts == original.agent_attempts
    assert not state.tasks[0].commit_sha and state.tasks[0].status != "done"


@pytest.mark.parametrize("obstacle", [
    "not_applied", "foreign_category", "foreign_owner", "approval", "input",
    "workflow", "not_ready", "ambiguous_owner", "changed_content", "schema_change",
    "changed_head", "foreign_paths", "current_checkpoint", "checkpoint_ref",
])
def test_metadata_repair_does_not_override_unproven_or_current_blockers(
    tmp_path: Path, obstacle: str,
) -> None:
    root, orch, original, owner = _ready_metadata_repair_scene(tmp_path)
    state = orch.mark_self_repair_applied("verified-engine-candidate")
    if obstacle == "not_applied":
        state = copy.deepcopy(original)
    elif obstacle == "foreign_category":
        state.active_blocker["category"] = "other_failure"
    elif obstacle == "foreign_owner":
        state.active_blocker["owner"] = "target_project"
    elif obstacle == "approval":
        state.pending_approval = "persistence-reset"
    elif obstacle == "input":
        state.active_input_request_id = "unanswered"
    elif obstacle == "workflow":
        state.resume_context.pop("workflow_id")
    elif obstacle == "not_ready":
        state.resume_context["implementation_ready_tasks"][owner.task_id] = False
    elif obstacle == "ambiguous_owner":
        state.tasks[1].status = "in_progress"
        state.resume_context["implementation_ready_tasks"][state.tasks[1].task_id] = True
    elif obstacle == "changed_content":
        _write(root, "app.py", "VALUE = 3\n")
    elif obstacle == "schema_change":
        _write(root, "app.py", "db.execute('CREATE TABLE example (id INTEGER)')\n")
        # Genuine DDL remains guarded even with matching retained ownership.
        orch._set_implementation_ready_marker(state, state.tasks[0], True)
    elif obstacle == "changed_head":
        state.resume_context["task_attempt_base_refs"][owner.task_id] = "different-base"
    elif obstacle == "foreign_paths":
        _write(root, "unrelated.py", "keep unrelated work\n")
    elif obstacle == "current_checkpoint":
        state.task_failure_checkpoints[owner.task_id]["verify_retry_epoch"] = owner.verify_retry_epoch
    elif obstacle == "checkpoint_ref":
        state.task_failure_checkpoints[owner.task_id]["ref"] = "refs/retained/current"
    before = copy.deepcopy(state.to_dict())
    assert not orch._resume_metadata_checkpoint_repair(state)
    assert state.to_dict() == before
    if obstacle not in {"not_applied", "foreign_category", "foreign_owner"}:
        assert not orch._resume_blocked_run(state)
        assert state.status == "blocked"
        assert state.active_blocker["fingerprint"] == before["active_blocker"]["fingerprint"]
        assert state.task_failure_checkpoints == before["task_failure_checkpoints"]
        assert state.agent_attempts == before["agent_attempts"]
